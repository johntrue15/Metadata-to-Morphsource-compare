#!/usr/bin/env python3
"""
AutoResearchClaw — autonomous iterative MorphoSource research agent.

Inspired by Karpathy's auto-research paradigm: give the agent a research
topic, a program.md strategy file, and a number of iterations.  It
autonomously decomposes, searches MorphoSource, evaluates what it found,
decides what to try next, and repeats — building up memory across
iterations.  Each iteration creates a GitHub issue with its findings.
After N iterations you wake up to a full research log.

Usage:
    python research_agent.py "topic" --iterations 5 --media-id 000038412
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
        from dotenv import load_dotenv  # type: ignore
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

SCRIPT_DIR = Path(__file__).resolve().parent

log.info("Model: %s | Debug: %s", OPENAI_MODEL, _debug)


# ---------------------------------------------------------------------------
# GitHub Issue Reporter — can comment AND create new issues
# ---------------------------------------------------------------------------


class GitHubIssueReporter:
    """Manages GitHub issue interactions: create issues, post comments, update labels."""

    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.issue_number = os.environ.get("ISSUE_NUMBER")
        self.enabled = bool(self.token and self.repo)
        if self.enabled:
            log.info(
                "GitHub reporting enabled (repo=%s, parent_issue=#%s)",
                self.repo,
                self.issue_number or "none",
            )
        else:
            log.info("GitHub reporting disabled (missing GITHUB_TOKEN or GITHUB_REPOSITORY)")

    def _api(self, method, path, payload=None):
        """Make a GitHub API request. Returns (status_code, response_dict)."""
        url = f"https://api.github.com/repos/{self.repo}/{path}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.getcode(), body
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
            log.warning("GitHub API %s %s → HTTP %d: %s", method, path, exc.code, body_text)
            return exc.code, {}
        except Exception as exc:
            log.warning("GitHub API %s %s failed: %s", method, path, exc)
            return 0, {}

    def create_issue(self, title, body, labels=None):
        """Create a new issue. Returns the issue number or None."""
        if not self.enabled:
            return None
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        status, data = self._api("POST", "issues", payload)
        if status == 201:
            num = data.get("number")
            log.info("Created issue #%s: %s", num, title)
            return num
        return None

    def post_comment(self, body, issue_number=None):
        """Post a comment on an issue."""
        issue_number = issue_number or self.issue_number
        if not self.enabled or not issue_number:
            return False
        status, _ = self._api("POST", f"issues/{issue_number}/comments", {"body": body})
        if status == 201:
            log.info("Posted comment to issue #%s", issue_number)
            return True
        return False

    def update_labels(self, labels, issue_number=None):
        """Replace labels on an issue."""
        issue_number = issue_number or self.issue_number
        if not self.enabled or not issue_number:
            return False
        status, _ = self._api("PUT", f"issues/{issue_number}/labels", {"labels": labels})
        return status == 200


_reporter: GitHubIssueReporter | None = None


def _post_to_issue(body, issue_number=None):
    """Post a comment to a GitHub issue if reporting is active."""
    if _reporter:
        _reporter.post_comment(body, issue_number=issue_number)


# ---------------------------------------------------------------------------
# Load program.md
# ---------------------------------------------------------------------------


def _load_program(path=None):
    """Load the research program definition from a Markdown file."""
    if path:
        p = Path(path)
    else:
        p = SCRIPT_DIR / "program.md"
    if p.is_file():
        text = p.read_text(encoding="utf-8")
        log.info("Loaded program.md (%d chars) from %s", len(text), p)
        return text
    log.info("No program.md found at %s", p)
    return ""


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------


def _call_llm(messages, max_tokens=2000, json_mode=False, label="LLM"):
    """Call OpenAI chat completions with logging and auto parameter adaptation."""
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

    log.debug("[%s] model=%s, max_tokens=%d, json=%s", label, OPENAI_MODEL, max_tokens, json_mode)

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as first_err:
        err_str = str(first_err).lower()
        if "max_tokens" in err_str or "not supported" in err_str:
            log.info("[%s] Retrying with max_completion_tokens", label)
            del kwargs["max_tokens"]
            kwargs["max_completion_tokens"] = max_tokens
            kwargs.pop("temperature", None)
            kwargs.pop("response_format", None)
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as retry_err:
                log.error("[%s] Retry failed: %s", label, retry_err)
                return None
        else:
            log.error("[%s] API call failed: %s", label, first_err)
            return None

    choice = response.choices[0] if response.choices else None
    content = choice.message.content if choice else None
    usage = response.usage

    log.info(
        "[%s] finish=%s | len=%s | usage=%s",
        label,
        choice.finish_reason if choice else "none",
        len(content) if content else 0,
        f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "N/A",
    )

    if not content:
        log.warning("[%s] Empty content (finish=%s)", label, choice.finish_reason if choice else "?")

    return (content or "").strip()


# ---------------------------------------------------------------------------
# Stage 0: Fetch seed media
# ---------------------------------------------------------------------------


def fetch_seed_media(media_id):
    """Fetch a single media record from MorphoSource."""
    media_id = media_id.strip().lstrip("0") or "0"
    padded_id = media_id.zfill(9)
    url = f"{MORPHOSOURCE_API_BASE}/media/{padded_id}"
    log.info("Fetching seed media: %s", url)

    headers = {"Accept": "application/json"}
    api_key = os.environ.get("MORPHOSOURCE_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        if _requests:
            resp = _requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.getcode() == 200:
                    return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.error("Failed to fetch seed media: %s", exc)
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
        return v[0] if isinstance(v, list) and v else v

    parts = []
    for label, key in [
        ("Title", "title"), ("Type/Modality", "media_type"),
        ("Modality", "modality"), ("Taxonomy", "physical_object_taxonomy_name"),
        ("Organization", "physical_object_organization"),
        ("Specimen", "physical_object_title"), ("Device", "device"),
        ("Description", "short_description"),
    ]:
        val = _val(record, key)
        if val:
            parts.append(f"**{label}:** {val}")
    return "\n".join(parts) if parts else json.dumps(record, indent=2)[:800]


# ---------------------------------------------------------------------------
# Decompose topic into search queries
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = (
    "You are a research planning assistant for MorphoSource (a database of 3-D "
    "specimen scans, media and physical objects). Decompose the user's research "
    "goal into 3-5 specific search queries.\n\n"
    "Return a JSON object: {\"queries\": [{\"query\": \"...\", \"rationale\": \"...\"}]}\n"
    "Return ONLY the JSON object."
)

_DECOMPOSE_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "for", "on", "by",
    "is", "are", "was", "were", "be", "been", "with", "at", "from", "as",
    "it", "its", "that", "this", "these", "those", "how", "what", "which",
    "identify", "analyze", "analyse", "find", "show", "most", "promising",
    "research", "pathways", "opportunities", "ecosystem", "let", "lets", "about",
}

_CONCEPT_QUERIES = {
    "specimen": {"query": "specimen", "rationale": "Browse physical specimen records."},
    "specimens": {"query": "specimen", "rationale": "Browse physical specimen records."},
    "ct": {"query": "CT scan", "rationale": "Browse CT scan media records."},
    "x-ray": {"query": "CT scan", "rationale": "Browse X-ray / CT media records."},
    "scan": {"query": "CT scan", "rationale": "Browse 3-D scan media."},
    "scans": {"query": "CT scan", "rationale": "Browse 3-D scan media."},
    "metadata": {"query": "metadata", "rationale": "Search records by metadata fields."},
    "3d": {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
    "mesh": {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
}


def _heuristic_decompose(topic):
    import re as _re
    queries, seen = [], set()
    lower = topic.lower()
    for kw, entry in _CONCEPT_QUERIES.items():
        if kw in lower and entry["query"] not in seen:
            queries.append(dict(entry))
            seen.add(entry["query"])
    stop = {"Analyze", "Analysis", "Identify", "Data", "Database", "MorphoSource",
            "Research", "Pathways", "Promising", "Ecosystem", "Show", "Find", "Browse"}
    for tok in _re.findall(r"\b([A-Z][a-z]{3,})\b", topic):
        if tok not in stop:
            qt = f"{tok} specimens"
            if qt not in seen:
                queries.append({"query": qt, "rationale": f"Search for {tok} records."})
                seen.add(qt)
    if not queries:
        words = _re.findall(r"[A-Za-z][A-Za-z'-]+", topic)
        kws = [w for w in words if w.lower() not in _DECOMPOSE_STOPWORDS and len(w) > 2]
        queries.append({"query": " ".join(kws[:3]) or topic, "rationale": "Broad search."})
    return queries[:MAX_QUERIES]


def _parse_decompose(text):
    if not text:
        return None

    def _try(s):
        try:
            p = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(p, dict) and isinstance(p.get("queries"), list) and p["queries"]:
            return p["queries"]
        if isinstance(p, list) and p:
            return p
        return None

    if "```" in text:
        for part in text.split("```")[1::2]:
            c = part[4:].strip() if part.startswith("json") else part.strip()
            r = _try(c)
            if r:
                return r
    r = _try(text.strip())
    if r:
        return r
    for o, c in [("{", "}"), ("[", "]")]:
        s, e = text.find(o), text.rfind(c)
        if s != -1 and e > s:
            r = _try(text[s:e + 1])
            if r:
                return r
    return None


def decompose_topic(topic, seed_context=None, memory_context=None):
    """Decompose a research topic into MorphoSource search queries."""
    user_parts = [topic]
    if seed_context:
        user_parts.append(
            f"\nSeed media record to anchor queries:\n{seed_context}"
        )
    if memory_context:
        user_parts.append(
            f"\nPrevious research memory — avoid repeating failed queries "
            f"and build on successful leads:\n{memory_context}"
        )
    user_content = "\n".join(user_parts)

    for attempt in range(1, _LLM_RETRIES + 1):
        content = _call_llm(
            [{"role": "system", "content": _DECOMPOSE_SYSTEM},
             {"role": "user", "content": user_content}],
            max_tokens=2000, json_mode=True, label=f"Decompose-{attempt}",
        )
        if content is None:
            break
        queries = _parse_decompose(content)
        if queries:
            return queries[:MAX_QUERIES]
        log.warning("Decompose attempt %d: unparseable", attempt)
        if attempt < _LLM_RETRIES:
            time.sleep(min(2 ** (attempt - 1), 4))

    return _heuristic_decompose(topic)


# ---------------------------------------------------------------------------
# Execute MorphoSource searches
# ---------------------------------------------------------------------------


def execute_searches(queries):
    results = []
    for i, item in enumerate(queries, 1):
        qt = item["query"]
        log.info("Search %d/%d: %s", i, len(queries), qt)
        try:
            fmt = query_formatter.format_query(qt)
            params = fmt.get("api_params", {"q": qt, "per_page": 10})
            sr = morphosource_api.search_morphosource(
                params, fmt.get("formatted_query", qt), query_info=fmt,
            )
            cnt = sr.get("summary", {}).get("count", 0)
            results.append({
                "query": qt,
                "rationale": item.get("rationale", ""),
                "formatted_query": fmt.get("formatted_query") or qt,
                "api_endpoint": fmt.get("api_endpoint") or "media",
                "result_count": cnt,
                "result_status": sr.get("summary", {}).get("status", "unknown"),
                "result_data": sr.get("full_data", {}),
            })
        except Exception as exc:
            log.error("Search '%s' failed: %s", qt, exc)
            results.append({
                "query": qt, "rationale": item.get("rationale", ""),
                "formatted_query": qt, "api_endpoint": "unknown",
                "result_count": 0, "result_status": f"error: {exc}", "result_data": {},
            })
    return results


def refine_searches(search_results):
    import re as _re
    refined, seen = [], set()
    for r in search_results:
        if r["result_count"] > 0:
            continue
        words = [w for w in _re.findall(r"[A-Za-z][A-Za-z'-]+", r["query"])
                 if w.lower() not in _DECOMPOSE_STOPWORDS and len(w) > 2]
        for w in words:
            if w.lower() not in seen:
                refined.append({"query": w, "rationale": f"Simplified retry of '{r['query']}'."})
                seen.add(w.lower())
    if not refined:
        return [], False
    log.info("Refining %d zero-result queries", len(refined))
    return execute_searches(refined[:MAX_QUERIES]), True


# ---------------------------------------------------------------------------
# Synthesize report
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = (
    "You are a research synthesis assistant. Write a Markdown report with:\n"
    "## Research Topic\n## Available Data on MorphoSource\n"
    "## Recommendations\n(Data to analyse, Additional data collection, "
    "Analysis methods, Next steps)\n## Conclusion\n\n"
    "Be specific, cite result counts and MorphoSource endpoints."
)


def synthesize_report(topic, search_results, seed_context=None, memory_context=None):
    items = []
    for r in search_results:
        entry = {k: r[k] for k in ("query", "rationale", "formatted_query",
                                     "api_endpoint", "result_count", "result_status")}
        data = r.get("result_data", {})
        for key in ("media", "physical_objects"):
            recs = data.get(key, [])
            if not recs and isinstance(data.get("response"), dict):
                recs = data["response"].get(key, [])
            if recs:
                entry["sample_records"] = recs[:3]
                break
        items.append(entry)

    extras = ""
    if seed_context:
        extras += f"\nSeed media record:\n{seed_context}"
    if memory_context:
        extras += f"\nAccumulated research memory:\n{memory_context}"

    user_msg = f"Research topic: {topic}{extras}\n\nSearch results:\n{json.dumps(items, indent=2)}"

    for attempt in range(1, _LLM_RETRIES + 1):
        report = _call_llm(
            [{"role": "system", "content": _SYNTHESIS_SYSTEM},
             {"role": "user", "content": user_msg}],
            max_tokens=4000, label=f"Synthesize-{attempt}",
        )
        if report is None:
            break
        if report:
            return {"status": "success", "report": report}
        if attempt < _LLM_RETRIES:
            time.sleep(min(2 ** (attempt - 1), 4))

    lines = [f"## Research Topic\n\n{topic}\n\n## Available Data\n"]
    for r in search_results:
        lines.append(f"- **{r['query']}** — {r['result_count']} result(s)")
    lines.append("\n## Conclusion\n\nSee data above. Set OPENAI_API_KEY for AI synthesis.")
    return {"status": "fallback", "report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Evaluation step — the Karpathy "did it improve?" check
# ---------------------------------------------------------------------------

_EVALUATE_SYSTEM = (
    "You are evaluating one iteration of an autonomous MorphoSource research agent. "
    "Given the research topic, the iteration's search results, and accumulated memory "
    "from prior iterations, produce a JSON evaluation.\n\n"
    "Return a JSON object with these keys:\n"
    '  "score": integer 1-10 (how productive was this iteration),\n'
    '  "discoveries": list of strings (key new findings),\n'
    '  "dead_ends": list of strings (queries/approaches to avoid),\n'
    '  "next_directions": list of 3-5 strings (specific queries or angles for next iteration),\n'
    '  "summary": string (2-3 paragraph running summary of ALL findings so far)\n\n'
    "Return ONLY the JSON object."
)


def evaluate_iteration(topic, search_results, memory, program=""):
    """LLM evaluates the iteration and decides what to explore next."""
    context_parts = [f"Research topic: {topic}"]
    if program:
        context_parts.append(f"Research program strategy:\n{program[:2000]}")

    results_summary = []
    for r in search_results:
        results_summary.append({
            "query": r["query"], "result_count": r["result_count"],
            "status": r["result_status"],
        })
    context_parts.append(f"This iteration's results:\n{json.dumps(results_summary, indent=2)}")

    if memory:
        prev = memory.get("summary", "")
        tried = memory.get("queries_tried", [])
        context_parts.append(f"Previous summary:\n{prev}")
        if tried:
            context_parts.append(f"Previously tried queries: {json.dumps(tried)}")

    user_msg = "\n\n".join(context_parts)

    content = _call_llm(
        [{"role": "system", "content": _EVALUATE_SYSTEM},
         {"role": "user", "content": user_msg}],
        max_tokens=2000, json_mode=True, label="Evaluate",
    )

    if content:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                log.info("Evaluation score: %s/10", parsed.get("score", "?"))
                return parsed
        except (json.JSONDecodeError, ValueError):
            log.warning("Could not parse evaluation JSON")

    return {
        "score": 5,
        "discoveries": [],
        "dead_ends": [r["query"] for r in search_results if r["result_count"] == 0],
        "next_directions": ["Try broader taxonomy searches", "Explore different modalities"],
        "summary": "Evaluation unavailable — continuing with heuristic directions.",
    }


# ---------------------------------------------------------------------------
# Memory management
# ---------------------------------------------------------------------------


def _build_memory(iteration, search_results, evaluation, prev_memory):
    """Build the accumulated memory object for the next iteration."""
    prev_tried = prev_memory.get("queries_tried", []) if prev_memory else []
    prev_dead = prev_memory.get("dead_ends", []) if prev_memory else []
    prev_discoveries = prev_memory.get("all_discoveries", []) if prev_memory else []

    new_tried = [{"query": r["query"], "count": r["result_count"]} for r in search_results]
    new_discoveries = evaluation.get("discoveries", [])
    new_dead = evaluation.get("dead_ends", [])

    return {
        "iteration": iteration,
        "queries_tried": prev_tried + new_tried,
        "dead_ends": list(set(prev_dead + new_dead)),
        "all_discoveries": prev_discoveries + new_discoveries,
        "next_directions": evaluation.get("next_directions", []),
        "summary": evaluation.get("summary", ""),
        "score": evaluation.get("score", 5),
    }


def _format_memory_for_llm(memory):
    """Format memory into a concise string for LLM context."""
    if not memory:
        return ""
    parts = [f"Iterations completed: {memory.get('iteration', 0)}"]
    parts.append(f"Overall score so far: {memory.get('score', '?')}/10")

    tried = memory.get("queries_tried", [])
    if tried:
        tried_lines = [f"  - \"{t['query']}\" → {t['count']} results" for t in tried[-15:]]
        parts.append("Queries tried:\n" + "\n".join(tried_lines))

    dead = memory.get("dead_ends", [])
    if dead:
        parts.append(f"Dead ends (DO NOT retry): {', '.join(dead[-10:])}")

    discoveries = memory.get("all_discoveries", [])
    if discoveries:
        parts.append("Key discoveries:\n" + "\n".join(f"  - {d}" for d in discoveries[-10:]))

    nexts = memory.get("next_directions", [])
    if nexts:
        parts.append("Suggested next directions:\n" + "\n".join(f"  - {n}" for n in nexts))

    summary = memory.get("summary", "")
    if summary:
        parts.append(f"Running summary:\n{summary}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Single iteration pipeline
# ---------------------------------------------------------------------------


def run_single_iteration(topic, iteration, total, memory, seed_context=None, program=""):
    """Execute one iteration of the research loop.

    Returns (iteration_result_dict, updated_memory).
    """
    log.info("=" * 60)
    log.info("ITERATION %d/%d", iteration, total)
    log.info("=" * 60)

    memory_context = _format_memory_for_llm(memory) if memory else None

    # Decompose
    queries = decompose_topic(
        topic,
        seed_context=seed_context if iteration == 1 else None,
        memory_context=memory_context,
    )

    # Search
    search_results = execute_searches(queries)

    # Refine zero-result queries
    for _ in range(MAX_REFINEMENT_ROUNDS):
        zeros = sum(1 for r in search_results if r["result_count"] == 0)
        if zeros == 0:
            break
        new, had = refine_searches(search_results)
        if not had:
            break
        improved = [r for r in new if r["result_count"] > 0]
        if improved:
            search_results.extend(improved)
        else:
            break

    total_hits = sum(r["result_count"] for r in search_results)
    log.info("Iteration %d: %d results across %d queries", iteration, total_hits, len(search_results))

    # Evaluate
    evaluation = evaluate_iteration(topic, search_results, memory, program)

    # Synthesize report for this iteration
    report = synthesize_report(topic, search_results, seed_context=seed_context, memory_context=memory_context)

    # Build memory for next iteration
    new_memory = _build_memory(iteration, search_results, evaluation, memory)

    result = {
        "iteration": iteration,
        "total_iterations": total,
        "queries": queries,
        "search_results": [
            {k: v for k, v in r.items() if k != "result_data"}
            for r in search_results
        ],
        "total_hits": total_hits,
        "evaluation": evaluation,
        "report": report,
    }

    return result, new_memory


# ---------------------------------------------------------------------------
# Orchestrator — the Karpathy-style iteration loop
# ---------------------------------------------------------------------------


def run_research_program(topic, iterations=1, media_id=None, program=""):
    """Run the full iterative research program.

    Each iteration:
      1. Decomposes the topic (informed by memory from prior iterations)
      2. Searches MorphoSource
      3. Evaluates what was found (score, discoveries, dead ends)
      4. Decides what to explore next
      5. Creates a GitHub issue with the iteration's findings
      6. Passes memory forward to the next iteration

    After all iterations, posts a final synthesis to the parent issue.
    """
    log.info("Starting research program: %d iteration(s)", iterations)

    seed_context = None
    if media_id:
        log.info("Fetching seed media: %s", media_id)
        seed_data = fetch_seed_media(media_id)
        if seed_data:
            seed_context = _summarize_seed(seed_data)
            padded = media_id.strip().lstrip("0").zfill(9)
            _post_to_issue(
                f"### Seed Media Record\n\n"
                f"Fetched **[media {padded}]"
                f"(https://www.morphosource.org/concern/media/{padded})** "
                f"as research seed:\n\n{seed_context}"
            )

    memory = None
    all_results = []
    iteration_issues = []

    for i in range(1, iterations + 1):
        result, memory = run_single_iteration(
            topic, i, iterations, memory,
            seed_context=seed_context, program=program,
        )
        all_results.append(result)

        # --- Create a GitHub issue for this iteration ---
        score = result["evaluation"].get("score", "?")
        hits = result["total_hits"]
        issue_title = f"Research [{i}/{iterations}]: {topic[:80]}"
        discoveries = result["evaluation"].get("discoveries", [])
        next_dirs = result["evaluation"].get("next_directions", [])

        disc_lines = "\n".join(f"- {d}" for d in discoveries) if discoveries else "_None_"
        next_lines = "\n".join(f"- {n}" for n in next_dirs) if next_dirs else "_None_"
        report_text = result["report"].get("report", "_No report._")

        issue_body = (
            f"## Iteration {i}/{iterations} — Score: {score}/10\n\n"
            f"**Topic:** {topic}\n"
            f"**Results:** {hits} total hits\n\n"
            f"### Key Discoveries\n{disc_lines}\n\n"
            f"### Next Directions\n{next_lines}\n\n"
            f"---\n\n"
            f"### Full Report\n\n{report_text}\n\n"
            f"---\n"
            f"_AutoResearchClaw iteration {i}/{iterations} • "
            f"Score: {score}/10 • {hits} results_"
        )

        if _reporter:
            issue_num = _reporter.create_issue(
                issue_title, issue_body,
                labels=["research-agent", f"iteration-{i}"],
            )
            if issue_num:
                iteration_issues.append(issue_num)
                _reporter.update_labels(
                    ["research-agent", f"iteration-{i}", "completed"],
                    issue_number=issue_num,
                )

        # Post progress summary to parent issue
        issue_link = f"#{iteration_issues[-1]}" if iteration_issues else f"Iteration {i}"
        _post_to_issue(
            f"### Iteration {i}/{iterations} complete\n\n"
            f"**Score:** {score}/10 | **Results:** {hits}\n\n"
            f"**Discoveries:** {', '.join(discoveries[:3]) if discoveries else 'none'}\n\n"
            f"**Next:** {', '.join(next_dirs[:3]) if next_dirs else 'continuing...'}\n\n"
            f"Full report: {issue_link}"
        )

        log.info(
            "Iteration %d/%d complete — score=%s, hits=%d",
            i, iterations, score, hits,
        )

    # --- Final synthesis across all iterations ---
    log.info("=" * 60)
    log.info("FINAL SYNTHESIS across %d iterations", iterations)
    log.info("=" * 60)

    if iterations > 1 and memory:
        final_summary = memory.get("summary", "")
        all_discoveries = memory.get("all_discoveries", [])
        total_queries = len(memory.get("queries_tried", []))

        disc_text = "\n".join(f"- {d}" for d in all_discoveries) if all_discoveries else "_None_"
        issue_links = " ".join(f"#{n}" for n in iteration_issues) if iteration_issues else "N/A"

        final_body = (
            f"## Final Research Summary\n\n"
            f"**Topic:** {topic}\n"
            f"**Iterations:** {iterations}\n"
            f"**Total queries executed:** {total_queries}\n"
            f"**Iteration issues:** {issue_links}\n\n"
            f"### All Discoveries\n{disc_text}\n\n"
            f"### Summary\n{final_summary}\n\n"
            f"---\n\n"
            f"_To continue this research, comment on this issue with new "
            f"directions or questions. The issue-comment-reply workflow "
            f"will process follow-ups._"
        )
        _post_to_issue(final_body)

    return {
        "topic": topic,
        "iterations_completed": iterations,
        "iteration_issues": iteration_issues,
        "final_memory": memory,
        "all_results": all_results,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    global _reporter

    import argparse

    parser = argparse.ArgumentParser(
        description="AutoResearchClaw — autonomous iterative MorphoSource research agent",
    )
    parser.add_argument("topic", help="Research topic or goal to investigate")
    parser.add_argument(
        "--media-id", default=None,
        help="MorphoSource media ID to seed the research",
    )
    parser.add_argument(
        "--iterations", type=int, default=1,
        help="Number of autonomous research iterations (default: 1)",
    )
    parser.add_argument(
        "--program", default=None,
        help="Path to program.md strategy file (default: auto-detect)",
    )
    args = parser.parse_args()

    _reporter = GitHubIssueReporter()
    program = _load_program(args.program)

    log.info("=" * 60)
    log.info("AutoResearchClaw starting")
    log.info("Topic: %s", args.topic)
    log.info("Iterations: %d", args.iterations)
    log.info("Seed media: %s", args.media_id or "none")
    log.info("Model: %s", OPENAI_MODEL)
    log.info("Program: %d chars loaded", len(program))
    log.info("OpenAI key: %s", "set" if os.environ.get("OPENAI_API_KEY") else "NOT SET")
    log.info("GitHub: %s", "enabled" if _reporter.enabled else "disabled")
    log.info("=" * 60)

    try:
        result = run_research_program(
            args.topic,
            iterations=args.iterations,
            media_id=args.media_id,
            program=program,
        )
    except Exception:
        log.error("Research program failed:\n%s", traceback.format_exc())
        _post_to_issue(
            f"### AutoResearchClaw Error\n\n```\n{traceback.format_exc()}\n```"
        )
        if _reporter:
            _reporter.update_labels(["research-agent", "error"])
        sys.exit(1)

    # Write output files
    output = {
        "topic": result["topic"],
        "iterations_completed": result["iterations_completed"],
        "iteration_issues": result["iteration_issues"],
        "final_memory": result["final_memory"],
    }
    with open("research_report.json", "w") as f:
        json.dump(output, f, indent=2)

    last_result = result["all_results"][-1] if result["all_results"] else {}
    report_text = last_result.get("report", {}).get("report", "")
    if report_text:
        with open("research_report.md", "w") as f:
            f.write(report_text)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"iterations_completed={result['iterations_completed']}\n")
            f.write(f"report_status={last_result.get('report', {}).get('status', 'unknown')}\n")

    if _reporter:
        _reporter.update_labels(["research-agent", "completed"])

    log.info("Research program complete — %d iterations", result["iterations_completed"])


if __name__ == "__main__":
    main()
