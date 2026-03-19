#!/usr/bin/env python3
"""
AutoResearchClaw research agent for MorphoSource.

Decomposes a research topic into targeted MorphoSource queries, iteratively
searches for relevant specimen data, and synthesizes findings into a
research report with actionable recommendations.

All progress is posted as comments on the associated GitHub issue for
full visibility.
"""

import json
import logging
import os
import sys
import time
import traceback
import importlib.util
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env for local development (no-op when file is absent or in CI)
# ---------------------------------------------------------------------------

def _load_dotenv():
    """Walk up from this script's directory to find a .env file and load it."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    search = Path(__file__).resolve().parent
    for _ in range(5):
        env_file = search / ".env"
        if env_file.is_file():
            if load_dotenv:
                load_dotenv(env_file, override=False)
            else:
                with open(env_file) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip())
            return str(env_file)
        search = search.parent
    return None

_env_path = _load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.INFO,
    format=LOG_FORMAT,
    stream=sys.stdout,
)
log = logging.getLogger("AutoResearchClaw")

# ---------------------------------------------------------------------------
# OpenAI client setup
# ---------------------------------------------------------------------------

if "openai" in sys.modules:
    OpenAI = getattr(sys.modules["openai"], "OpenAI", None)  # type: ignore
else:
    _openai_spec = importlib.util.find_spec("openai")
    if _openai_spec:
        from openai import OpenAI  # type: ignore
    else:
        OpenAI = None  # type: ignore

import query_formatter
import morphosource_api

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
MAX_QUERIES = 5
_LLM_RETRIES = 3
MAX_REFINEMENT_ROUNDS = 2
MORPHOSOURCE_API_BASE = "https://www.morphosource.org/api"

log.info("Model: %s | Debug: %s", OPENAI_MODEL, _debug)


# ---------------------------------------------------------------------------
# GitHub Issue Reporter
# ---------------------------------------------------------------------------


class GitHubIssueReporter:
    """Posts progress updates as comments on a GitHub issue."""

    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.issue_number = os.environ.get("ISSUE_NUMBER")
        self.enabled = bool(self.token and self.repo and self.issue_number)
        if self.enabled:
            log.info(
                "GitHub Issue reporting enabled → #%s in %s",
                self.issue_number,
                self.repo,
            )
        else:
            missing = []
            if not self.token:
                missing.append("GITHUB_TOKEN")
            if not self.repo:
                missing.append("GITHUB_REPOSITORY")
            if not self.issue_number:
                missing.append("ISSUE_NUMBER")
            log.info(
                "GitHub Issue reporting disabled (missing: %s)", ", ".join(missing)
            )

    def post_comment(self, body):
        """Post a comment on the tracked GitHub issue."""
        if not self.enabled:
            return False
        url = (
            f"https://api.github.com/repos/{self.repo}"
            f"/issues/{self.issue_number}/comments"
        )
        payload = json.dumps({"body": body}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.getcode() == 201:
                    log.info("Posted comment to issue #%s", self.issue_number)
                    return True
                log.warning("Unexpected status %d posting comment", resp.getcode())
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:300]
            log.warning("Failed to post comment: HTTP %d — %s", exc.code, body_text)
        except Exception as exc:
            log.warning("Failed to post comment: %s", exc)
        return False

    def update_labels(self, labels):
        """Replace labels on the tracked issue."""
        if not self.enabled:
            return False
        url = (
            f"https://api.github.com/repos/{self.repo}"
            f"/issues/{self.issue_number}/labels"
        )
        payload = json.dumps({"labels": labels}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="PUT")
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.getcode() == 200
        except Exception as exc:
            log.warning("Failed to update labels: %s", exc)
            return False


_reporter: GitHubIssueReporter | None = None


def _post_to_issue(body):
    """Post a comment to the tracked GitHub issue if reporting is active."""
    if _reporter:
        _reporter.post_comment(body)


# ---------------------------------------------------------------------------
# LLM helper with robust error handling
# ---------------------------------------------------------------------------


def _call_llm(messages, max_tokens=2000, json_mode=False, label="LLM"):
    """Call OpenAI chat completions with detailed logging and automatic
    parameter adaptation for different model families.

    Returns the content string (may be empty) or None on hard failure.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("[%s] OPENAI_API_KEY not set", label)
        return None
    if OpenAI is None:
        log.warning("[%s] openai package not installed", label)
        return None

    client = OpenAI(api_key=api_key)

    kwargs = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.7,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    kwargs["max_tokens"] = max_tokens

    log.debug(
        "[%s] Calling model=%s, max_tokens=%d, json_mode=%s, messages=%d",
        label,
        OPENAI_MODEL,
        max_tokens,
        json_mode,
        len(messages),
    )

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as first_err:
        err_str = str(first_err).lower()
        # Some models (o-series, gpt-5 reasoning) need max_completion_tokens
        if "max_tokens" in err_str or "not supported" in err_str:
            log.info(
                "[%s] max_tokens not supported, retrying with max_completion_tokens",
                label,
            )
            del kwargs["max_tokens"]
            kwargs["max_completion_tokens"] = max_tokens
            kwargs.pop("temperature", None)
            if json_mode:
                kwargs.pop("response_format", None)
                log.debug("[%s] Removed json_mode for compatibility", label)
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as retry_err:
                log.error("[%s] Retry also failed: %s", label, retry_err)
                return None
        else:
            log.error("[%s] API call failed: %s", label, first_err)
            return None

    choice = response.choices[0] if response.choices else None
    finish_reason = choice.finish_reason if choice else "no_choices"
    content = choice.message.content if choice else None
    usage = response.usage

    log.info(
        "[%s] finish_reason=%s | content_length=%s | model=%s | usage=%s",
        label,
        finish_reason,
        len(content) if content else "None",
        getattr(response, "model", OPENAI_MODEL),
        (
            f"prompt={usage.prompt_tokens}/completion={usage.completion_tokens}"
            if usage
            else "N/A"
        ),
    )

    if not content:
        log.warning(
            "[%s] Empty content returned. finish_reason=%s | "
            "This may indicate the model refused, hit a filter, "
            "or the prompt needs adjustment.",
            label,
            finish_reason,
        )
        if hasattr(choice, "message") and hasattr(choice.message, "refusal"):
            refusal = choice.message.refusal
            if refusal:
                log.warning("[%s] Model refusal: %s", label, refusal)

    return (content or "").strip()


# ---------------------------------------------------------------------------
# Stage 0: Fetch seed media record from MorphoSource
# ---------------------------------------------------------------------------


def fetch_seed_media(media_id):
    """Fetch a single media record from MorphoSource to seed the research.

    Returns a dict with the API response, or None on failure.
    """
    media_id = media_id.strip().lstrip("0") or "0"
    padded_id = media_id.zfill(9)
    url = f"{MORPHOSOURCE_API_BASE}/media/{padded_id}"
    log.info("Fetching seed media: %s → %s", padded_id, url)

    headers = {"Accept": "application/json"}
    api_key = os.environ.get("MORPHOSOURCE_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        if _requests:
            resp = _requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                log.info("Seed media fetched successfully")
                return data
            log.warning("Seed media fetch returned HTTP %d", resp.status_code)
            return None
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.getcode() == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    log.info("Seed media fetched successfully")
                    return data
            log.warning("Seed media fetch returned HTTP %d", resp.getcode())
            return None
    except Exception as exc:
        log.error("Failed to fetch seed media %s: %s", padded_id, exc)
        return None


def _summarize_seed(seed_data):
    """Extract a human-readable summary from a MorphoSource media record."""
    if not seed_data:
        return ""

    record = seed_data
    if "response" in seed_data and isinstance(seed_data["response"], dict):
        record = seed_data["response"]

    def _val(d, key):
        v = d.get(key)
        if isinstance(v, list):
            return v[0] if v else None
        return v

    lines = []
    title = _val(record, "title") or _val(record, "name")
    if title:
        lines.append(f"**Title:** {title}")

    media_type = _val(record, "media_type")
    modality = _val(record, "modality")
    if media_type or modality:
        lines.append(f"**Type/Modality:** {media_type or ''} / {modality or ''}")

    taxonomy = _val(record, "physical_object_taxonomy_name")
    if taxonomy:
        lines.append(f"**Taxonomy:** {taxonomy}")

    org = _val(record, "physical_object_organization")
    if org:
        lines.append(f"**Organization:** {org}")

    obj_title = _val(record, "physical_object_title")
    if obj_title:
        lines.append(f"**Specimen:** {obj_title}")

    device = _val(record, "device")
    if device:
        lines.append(f"**Device:** {device}")

    description = _val(record, "short_description") or _val(record, "description")
    if description:
        lines.append(f"**Description:** {description}")

    return "\n".join(lines) if lines else json.dumps(record, indent=2)[:1000]


# ---------------------------------------------------------------------------
# Stage 1: Decompose research topic into MorphoSource search queries
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM_PROMPT = (
    "You are a research planning assistant specializing in biological specimen data. "
    "The user will provide a high-level research goal. Your job is to decompose it "
    "into 3-5 specific search queries that should be run against MorphoSource "
    "(a database of 3-D specimen scans, media and physical objects).\n\n"
    "Return a JSON object with a single key \"queries\" whose value is an array "
    "of objects, each with:\n"
    '  "query": a concise natural-language search phrase,\n'
    '  "rationale": one sentence explaining why this query helps the research.\n\n'
    "Example output:\n"
    '{"queries": [{"query": "CT scans of Anolis lizards", '
    '"rationale": "Retrieve available micro-CT data for the focal genus."}]}\n'
    "Return ONLY the JSON object, no other text."
)

_DECOMPOSE_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "for", "on", "by",
    "is", "are", "was", "were", "be", "been", "with", "at", "from", "as",
    "it", "its", "that", "this", "these", "those", "how", "what", "which",
    "identify", "analyze", "analyse", "find", "show", "most", "promising",
    "research", "pathways", "opportunities", "wedges", "commercialization",
    "ecosystem", "starting", "let", "lets", "rethink", "about",
}

_CONCEPT_QUERIES = {
    "specimen": {"query": "specimen", "rationale": "Browse physical specimen records."},
    "specimens": {"query": "specimen", "rationale": "Browse physical specimen records."},
    "ct": {"query": "CT scan", "rationale": "Browse CT scan media records."},
    "x-ray": {"query": "CT scan", "rationale": "Browse X-ray / CT media records."},
    "xray": {"query": "CT scan", "rationale": "Browse X-ray / CT media records."},
    "micro-ct": {"query": "CT scan", "rationale": "Browse microCT scan records."},
    "scan": {"query": "CT scan", "rationale": "Browse 3-D scan media."},
    "scans": {"query": "CT scan", "rationale": "Browse 3-D scan media."},
    "metadata": {"query": "metadata", "rationale": "Search records by metadata fields."},
    "3d": {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
    "3-d": {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
    "mesh": {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
}


def _heuristic_decompose(topic):
    """Extract concrete, searchable sub-queries from a research topic when
    the LLM is unavailable or returns an unparseable response."""
    import re as _re

    queries = []
    seen_query_texts = set()
    lower_topic = topic.lower()

    for keyword, entry in _CONCEPT_QUERIES.items():
        if keyword in lower_topic and entry["query"] not in seen_query_texts:
            queries.append(dict(entry))
            seen_query_texts.add(entry["query"])

    tokens = _re.findall(r"\b([A-Z][a-z]{3,})\b", topic)
    taxon_stopwords = {
        "Analyze", "Analysis", "Identify", "Data", "Database",
        "MorphoSource", "Morphosource", "Research", "Pathways",
        "Opportunities", "Wedges", "Commercialization", "Promising",
        "Ecosystem", "Starting", "Show", "Find", "List", "Browse",
    }
    for token in tokens:
        if token in taxon_stopwords:
            continue
        query_text = f"{token} specimens"
        if query_text not in seen_query_texts:
            queries.append({
                "query": query_text,
                "rationale": f"Search for {token} records on MorphoSource.",
            })
            seen_query_texts.add(query_text)

    if not queries:
        words = _re.findall(r"[A-Za-z][A-Za-z'-]+", topic)
        keywords = [
            w for w in words
            if w.lower() not in _DECOMPOSE_STOPWORDS and len(w) > 2
        ]
        q_text = " ".join(keywords[:3]) if keywords else topic
        queries.append({
            "query": q_text,
            "rationale": "Broad keyword search (heuristic fallback).",
        })

    return queries[:MAX_QUERIES]


def _parse_decompose_response(text):
    """Parse an LLM decomposition response into a list of query dicts.

    Handles JSON objects with a 'queries' key, bare JSON arrays,
    markdown-fenced JSON, and JSON embedded in surrounding prose.
    """
    if not text:
        return None

    def _try_parse(candidate):
        """Try parsing a candidate string as our expected JSON format."""
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, dict) and "queries" in parsed:
            queries = parsed["queries"]
            if isinstance(queries, list) and queries:
                return queries
        if isinstance(parsed, list) and parsed:
            return parsed
        return None

    # Try markdown-fenced blocks
    if "```" in text:
        parts = text.split("```")
        for part in parts[1::2]:
            candidate = part
            if candidate.startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()
            if candidate:
                result = _try_parse(candidate)
                if result:
                    return result

    stripped = text.strip()
    if not stripped:
        return None

    result = _try_parse(stripped)
    if result:
        return result

    # Try extracting JSON object or array from surrounding text
    for open_char, close_char in [("{", "}"), ("[", "]")]:
        start = stripped.find(open_char)
        end = stripped.rfind(close_char)
        if start != -1 and end > start:
            result = _try_parse(stripped[start:end + 1])
            if result:
                return result

    return None


def decompose_topic(topic, seed_context=None):
    """Break a research topic into specific MorphoSource search queries.

    Parameters
    ----------
    topic : str
        The research topic.
    seed_context : str or None
        Optional context from a seed media record to ground the queries.
    """
    log.info("Decomposing topic: %s", topic)
    if seed_context:
        log.info("Seed context provided (%d chars)", len(seed_context))

    user_content = topic
    if seed_context:
        user_content = (
            f"{topic}\n\n"
            f"Use the following MorphoSource media record as a starting point. "
            f"Design queries that explore related specimens, similar scan types, "
            f"the same taxonomic group, and complementary datasets:\n\n"
            f"{seed_context}"
        )

    last_exc = None
    for attempt in range(1, _LLM_RETRIES + 1):
        log.info("Decomposition attempt %d/%d", attempt, _LLM_RETRIES)
        try:
            content = _call_llm(
                messages=[
                    {"role": "system", "content": _DECOMPOSE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=2000,
                json_mode=True,
                label=f"Decompose-{attempt}",
            )

            if content is None:
                log.warning("Attempt %d: _call_llm returned None (hard failure)", attempt)
                break

            queries = _parse_decompose_response(content)
            if queries is not None:
                log.info("Decomposed into %d queries via LLM", len(queries))
                return queries[:MAX_QUERIES]

            log.warning(
                "Attempt %d: LLM response could not be parsed as queries. "
                "Raw content (first 500 chars): %.500r",
                attempt,
                content,
            )
        except Exception as exc:
            last_exc = exc
            log.error("Attempt %d failed with exception: %s", attempt, exc)
            log.debug(traceback.format_exc())

        if attempt < _LLM_RETRIES:
            sleep_time = min(2 ** (attempt - 1), 4)
            log.info("Sleeping %ds before retry", sleep_time)
            time.sleep(sleep_time)

    if last_exc:
        log.warning(
            "Decomposition failed after %d attempts (last error: %s), "
            "using heuristic fallback",
            _LLM_RETRIES,
            last_exc,
        )
    else:
        log.warning("LLM returned empty/unparseable content, using heuristic fallback")

    queries = _heuristic_decompose(topic)
    log.info("Heuristic fallback generated %d queries", len(queries))
    return queries


# ---------------------------------------------------------------------------
# Stage 2: Execute MorphoSource searches for each sub-query
# ---------------------------------------------------------------------------


def execute_searches(queries):
    """Run each sub-query through the existing query pipeline."""
    results = []
    for i, item in enumerate(queries, 1):
        query_text = item["query"]
        log.info("Search %d/%d: %s", i, len(queries), query_text)

        try:
            formatted = query_formatter.format_query(query_text)
            api_params = formatted.get(
                "api_params", {"q": query_text, "per_page": 10}
            )

            search_result = morphosource_api.search_morphosource(
                api_params,
                formatted.get("formatted_query", query_text),
                query_info=formatted,
            )

            result_count = search_result.get("summary", {}).get("count", 0)
            result_status = search_result.get("summary", {}).get("status", "unknown")
            log.info(
                "Search '%s' → %d results (%s)", query_text, result_count, result_status
            )

            results.append({
                "query": query_text,
                "rationale": item.get("rationale", ""),
                "formatted_query": formatted.get("formatted_query") or query_text,
                "api_endpoint": formatted.get("api_endpoint") or "media",
                "result_count": result_count,
                "result_status": result_status,
                "result_data": search_result.get("full_data", {}),
            })
        except Exception as exc:
            log.error("Search '%s' failed: %s", query_text, exc)
            log.debug(traceback.format_exc())
            results.append({
                "query": query_text,
                "rationale": item.get("rationale", ""),
                "formatted_query": query_text,
                "api_endpoint": "unknown",
                "result_count": 0,
                "result_status": f"error: {exc}",
                "result_data": {},
            })

    return results


# ---------------------------------------------------------------------------
# Stage 2b: Iterative refinement for zero-result queries
# ---------------------------------------------------------------------------


def _refine_zero_result_queries(search_results):
    """Generate simplified queries for searches that returned zero results."""
    import re as _re

    refined = []
    seen = set()
    for r in search_results:
        if r["result_count"] > 0:
            continue

        original = r["query"]
        words = _re.findall(r"[A-Za-z][A-Za-z'-]+", original)
        meaningful = [
            w for w in words
            if w.lower() not in _DECOMPOSE_STOPWORDS and len(w) > 2
        ]

        for word in meaningful:
            if word.lower() not in seen:
                refined.append({
                    "query": word,
                    "rationale": f"Simplified retry of '{original}' (zero results).",
                })
                seen.add(word.lower())

        if len(meaningful) >= 2:
            shorter = " ".join(meaningful[:2])
            if shorter.lower() not in seen:
                refined.append({
                    "query": shorter,
                    "rationale": f"Paired-term retry of '{original}'.",
                })
                seen.add(shorter.lower())

    return refined[:MAX_QUERIES]


def refine_searches(search_results):
    """Re-query zero-result searches with simplified terms."""
    refined_queries = _refine_zero_result_queries(search_results)
    if not refined_queries:
        return [], False

    log.info("Refining %d zero-result queries", len(refined_queries))
    new_results = execute_searches(refined_queries)
    return new_results, True


# ---------------------------------------------------------------------------
# Stage 3: Synthesize a research report
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM_PROMPT = (
    "You are a research synthesis assistant. The user will provide a research "
    "topic and a set of MorphoSource search results. Write a structured research "
    "report in Markdown with the following sections:\n\n"
    "## Research Topic\nRestate the research goal.\n\n"
    "## Available Data on MorphoSource\nSummarise the specimens, scans and "
    "media found for each sub-query. Include counts and highlight particularly "
    "relevant datasets.\n\n"
    "## Recommendations\n"
    "Provide concrete, actionable recommendations in these categories:\n"
    "- **Data to analyse**: which MorphoSource datasets to prioritise and why.\n"
    "- **Additional data collection**: specimens or scans that are missing and "
    "should be obtained (field work, museum loans, new CT scans, etc.).\n"
    "- **Analysis methods**: appropriate morphometric, phylogenetic or statistical "
    "methods for the available data.\n"
    "- **Next steps**: a prioritised checklist of actions a researcher should take.\n\n"
    "## Conclusion\nBriefly summarise how MorphoSource data can support this research.\n\n"
    "Be specific, cite result counts, and reference actual MorphoSource endpoints when possible."
)


def synthesize_report(topic, search_results, seed_context=None):
    """Generate a Markdown research report from collected search results."""
    log.info("Synthesizing report for %d search results", len(search_results))

    context_items = []
    for r in search_results:
        entry = {
            "query": r["query"],
            "rationale": r["rationale"],
            "formatted_query": r["formatted_query"],
            "endpoint": r["api_endpoint"],
            "result_count": r["result_count"],
            "status": r["result_status"],
        }
        data = r.get("result_data", {})
        for key in ("media", "physical_objects"):
            items = data.get(key, [])
            if not items and isinstance(data.get("response"), dict):
                items = data["response"].get(key, [])
            if items:
                entry["sample_records"] = items[:3]
                break
        context_items.append(entry)

    seed_section = ""
    if seed_context:
        seed_section = (
            f"\n\nSeed media record (starting point for this research):\n"
            f"{seed_context}"
        )

    user_message = (
        f"Research topic: {topic}{seed_section}\n\n"
        f"MorphoSource search results:\n{json.dumps(context_items, indent=2)}"
    )

    last_exc = None
    for attempt in range(1, _LLM_RETRIES + 1):
        log.info("Synthesis attempt %d/%d", attempt, _LLM_RETRIES)
        try:
            report = _call_llm(
                messages=[
                    {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=4000,
                json_mode=False,
                label=f"Synthesize-{attempt}",
            )

            if report is None:
                log.warning("Synthesis attempt %d: _call_llm returned None", attempt)
                break

            if report:
                log.info("Synthesis successful (%d chars)", len(report))
                return {"status": "success", "report": report}

            log.warning("Synthesis attempt %d returned empty report", attempt)
        except Exception as exc:
            last_exc = exc
            log.error("Synthesis attempt %d failed: %s", attempt, exc)
            log.debug(traceback.format_exc())

        if attempt < _LLM_RETRIES:
            sleep_time = min(2 ** (attempt - 1), 4)
            log.info("Sleeping %ds before retry", sleep_time)
            time.sleep(sleep_time)

    reason = str(last_exc) if last_exc else "LLM returned empty response"
    log.warning("Synthesis failed, using fallback report (reason: %s)", reason)
    return _fallback_report(topic, search_results, reason=reason)


def _fallback_report(topic, search_results, reason=None):
    """Build a plain-text report when the LLM is unavailable."""
    lines = [
        "## Research Topic",
        "",
        topic,
        "",
        "## Available Data on MorphoSource",
        "",
    ]
    for r in search_results:
        status = "+" if r["result_count"] > 0 else "!"
        lines.append(
            f"- {status} **{r['query']}** — {r['result_count']} result(s) "
            f"via `{r['api_endpoint']}` endpoint ({r['result_status']})"
        )
        data = r.get("result_data", {})
        for key in ("media", "physical_objects"):
            items = data.get(key, [])
            if not items and isinstance(data.get("response"), dict):
                items = data["response"].get(key, [])
            for item in items[:3]:
                if isinstance(item, dict):
                    name = (
                        item.get("name")
                        or item.get("title")
                        or item.get("specimen", {}).get("name", "")
                    )
                else:
                    name = str(item)
                if name:
                    # Flatten lists (MorphoSource wraps values in arrays)
                    if isinstance(name, list):
                        name = name[0] if name else ""
                    if name:
                        lines.append(f"  - {name}")
    lines += [
        "",
        "## Recommendations",
        "",
        "- Review the datasets listed above for relevance to your research.",
        "- Consider broadening searches that returned zero results.",
        "- Use the MorphoSource web interface for detailed specimen inspection.",
        "",
        "## Conclusion",
        "",
    ]
    if reason and reason != "no_api_key":
        lines.append(
            "AI-powered synthesis was attempted but encountered an issue: "
            f"{reason}. Review the data summary above for research guidance."
        )
    else:
        lines.append(
            "See the data summary above. To enable AI-powered synthesis, "
            "ensure the `OPENAI_API_KEY` secret is configured in your "
            "repository settings."
        )
    return {"status": "fallback", "report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_research(topic, media_id=None):
    """End-to-end research pipeline: seed -> decompose -> search -> refine -> synthesize.

    Posts progress updates to the GitHub issue at each stage.

    Parameters
    ----------
    topic : str
        The research topic or goal.
    media_id : str or None
        Optional MorphoSource media ID to seed the research.
    """
    log.info("AutoResearchClaw — starting research on: %s", topic)
    if media_id:
        log.info("Seed media ID: %s", media_id)

    seed_data = None
    seed_context = None

    # ------------------------------------------------------------------
    # Stage 0: Fetch seed media (if provided)
    # ------------------------------------------------------------------
    if media_id:
        log.info("=" * 60)
        log.info("STAGE 0: Fetching seed media record")
        log.info("=" * 60)
        seed_data = fetch_seed_media(media_id)
        if seed_data:
            seed_context = _summarize_seed(seed_data)
            padded = media_id.strip().lstrip("0").zfill(9)
            ms_url = f"https://www.morphosource.org/concern/media/{padded}"
            _post_to_issue(
                f"### Stage 0: Seed Media Record\n\n"
                f"Fetched **[media {padded}]({ms_url})** as research seed:\n\n"
                f"{seed_context}\n\n"
                f"_Using this record to guide query decomposition…_"
            )
            log.info("Seed summary:\n%s", seed_context)
        else:
            _post_to_issue(
                f"### Stage 0: Seed Media Record\n\n"
                f"Could not fetch media `{media_id}` from MorphoSource. "
                f"Proceeding with topic-only research."
            )
            log.warning("Seed media fetch failed, continuing without seed")

    # ------------------------------------------------------------------
    # Stage 1: Decompose
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("STAGE 1: Decomposing research topic")
    log.info("=" * 60)
    queries = decompose_topic(topic, seed_context=seed_context)
    log.info("Generated %d sub-queries", len(queries))

    query_lines = "\n".join(
        f"  {i}. **{q['query']}** — {q.get('rationale', '')}"
        for i, q in enumerate(queries, 1)
    )
    _post_to_issue(
        f"### Stage 1: Topic Decomposition\n\n"
        f"Decomposed research topic into **{len(queries)}** sub-queries:\n\n"
        f"{query_lines}\n\n"
        f"_Proceeding to MorphoSource searches…_"
    )

    # ------------------------------------------------------------------
    # Stage 2: Execute searches
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("STAGE 2: Executing MorphoSource searches")
    log.info("=" * 60)
    search_results = execute_searches(queries)
    total_hits = sum(r["result_count"] for r in search_results)
    log.info("Total: %d results across %d queries", total_hits, len(search_results))

    # ------------------------------------------------------------------
    # Stage 2b: Iterative refinement
    # ------------------------------------------------------------------
    for iteration in range(1, MAX_REFINEMENT_ROUNDS + 1):
        zero_count = sum(1 for r in search_results if r["result_count"] == 0)
        if zero_count == 0:
            break
        log.info(
            "Refinement round %d: %d zero-result queries to retry",
            iteration,
            zero_count,
        )
        new_results, had_refinements = refine_searches(search_results)
        if not had_refinements:
            break
        improved = [r for r in new_results if r["result_count"] > 0]
        if improved:
            search_results.extend(improved)
            log.info("%d refined queries returned results", len(improved))
        else:
            log.info("No improvement from refinement round %d", iteration)
            break

    total_hits = sum(r["result_count"] for r in search_results)
    log.info(
        "Final: %d total results across %d queries", total_hits, len(search_results)
    )

    search_summary_lines = []
    for r in search_results:
        icon = "+" if r["result_count"] > 0 else "0"
        search_summary_lines.append(
            f"  - [{icon}] **{r['query']}** → "
            f"{r['result_count']} result(s) via `{r['api_endpoint']}`"
        )
    _post_to_issue(
        f"### Stage 2: MorphoSource Search Results\n\n"
        f"**{total_hits}** total results across **{len(search_results)}** queries:\n\n"
        + "\n".join(search_summary_lines)
        + "\n\n_Proceeding to synthesis…_"
    )

    # ------------------------------------------------------------------
    # Stage 3: Synthesize report
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("STAGE 3: Synthesizing research report")
    log.info("=" * 60)
    report = synthesize_report(topic, search_results, seed_context=seed_context)
    log.info("Report generated (status: %s)", report["status"])

    _post_to_issue(
        f"### Stage 3: Research Report\n\n"
        f"**Status:** {report['status']}\n\n"
        f"---\n\n"
        f"{report.get('report', '_No report generated._')}"
    )

    result = {
        "topic": topic,
        "queries": queries,
        "search_results": [
            {k: v for k, v in r.items() if k != "result_data"}
            for r in search_results
        ],
        "report": report,
    }
    if media_id:
        result["seed_media_id"] = media_id
    if seed_context:
        result["seed_summary"] = seed_context
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    """CLI entry point for the research agent."""
    global _reporter

    import argparse

    parser = argparse.ArgumentParser(
        description="AutoResearchClaw — autonomous MorphoSource research agent",
    )
    parser.add_argument("topic", help="Research topic or goal to investigate")
    parser.add_argument(
        "--media-id",
        default=None,
        help="MorphoSource media ID to use as seed (e.g. 000038412)",
    )
    args = parser.parse_args()

    topic = args.topic
    media_id = args.media_id

    # Initialize GitHub Issue reporter
    _reporter = GitHubIssueReporter()

    log.info("=" * 60)
    log.info("AutoResearchClaw starting")
    log.info("Topic: %s", topic)
    log.info("Seed media ID: %s", media_id or "none")
    log.info("Model: %s", OPENAI_MODEL)
    log.info("OpenAI key: %s", "set" if os.environ.get("OPENAI_API_KEY") else "NOT SET")
    log.info("GitHub reporting: %s", "enabled" if _reporter.enabled else "disabled")
    log.info("=" * 60)

    try:
        result = run_research(topic, media_id=media_id)
    except Exception as exc:
        log.error("Research pipeline failed: %s", exc)
        log.error(traceback.format_exc())
        _post_to_issue(
            f"### AutoResearchClaw Error\n\n"
            f"The research pipeline encountered a fatal error:\n\n"
            f"```\n{traceback.format_exc()}\n```\n\n"
            f"Please check the workflow logs for details."
        )
        if _reporter:
            _reporter.update_labels(["research-agent", "error"])
        sys.exit(1)

    # Write output files
    with open("research_report.json", "w") as f:
        json.dump(result, f, indent=2)

    report_text = result["report"].get("report", "")
    if report_text:
        with open("research_report.md", "w") as f:
            f.write(report_text)

    # Set GitHub Actions outputs
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"report_status={result['report']['status']}\n")
            f.write(f"query_count={len(result['queries'])}\n")
            total = sum(r["result_count"] for r in result["search_results"])
            f.write(f"total_results={total}\n")

    if _reporter:
        _reporter.update_labels(["research-agent", "completed"])

    log.info("Research complete — see research_report.json / research_report.md")


if __name__ == "__main__":
    main()
