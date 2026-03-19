#!/usr/bin/env python3
"""
AutoResearchClaw — autonomous iterative MorphoSource research agent.

Two-loop architecture:
  - Inner loop (research_depth): fast autonomous cycles that decompose,
    search MorphoSource, evaluate, and build memory.  Logged locally as
    JSONL for the dashboard.
  - Outer loop (github_issues): creates GitHub issues at regular intervals
    aggregating the inner cycles for human review.

Usage:
    python research_agent.py "topic" --research-depth 10 --github-issues 3
"""

import json
import logging
import os
import re
import sys
import time
import traceback
import importlib.util
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env for local development
# ---------------------------------------------------------------------------


def _load_dotenv():
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


_load_dotenv()

# ---------------------------------------------------------------------------
# Console logging
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

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
MAX_QUERIES = 5
_LLM_RETRIES = 3
MAX_REFINEMENT_ROUNDS = 2
MORPHOSOURCE_API_BASE = "https://www.morphosource.org/api"
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = Path.home() / ".autoresearchclaw" / "logs"

log.info("Model: %s | Debug: %s", OPENAI_MODEL, _debug)


# ---------------------------------------------------------------------------
# Structured JSONL logging for the dashboard
# ---------------------------------------------------------------------------


class RunLogger:
    """Writes structured JSONL logs and run metadata for the local dashboard."""

    def __init__(self, topic, research_depth, github_issues, media_id=None):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", topic.lower())[:40].strip("_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{ts}_{slug}"
        self.jsonl_path = LOG_DIR / f"{self.run_id}.jsonl"
        self.meta_path = LOG_DIR / f"{self.run_id}_meta.json"
        self.start_time = datetime.now(timezone.utc).isoformat()

        meta = {
            "run_id": self.run_id,
            "topic": topic,
            "research_depth": research_depth,
            "github_issues": github_issues,
            "media_id": media_id,
            "model": OPENAI_MODEL,
            "start_time": self.start_time,
            "status": "running",
            "issue_links": [],
        }
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        log.info("Run log: %s", self.jsonl_path)

    def event(self, cycle, stage, **kwargs):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cycle": cycle,
            "stage": stage,
            **kwargs,
        }
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def update_meta(self, **kwargs):
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta.update(kwargs)
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def finish(self, status="completed", issue_links=None):
        self.update_meta(
            status=status,
            end_time=datetime.now(timezone.utc).isoformat(),
            issue_links=issue_links or [],
        )


_run_logger: RunLogger | None = None


# ---------------------------------------------------------------------------
# GitHub Issue Reporter
# ---------------------------------------------------------------------------


class GitHubIssueReporter:
    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.issue_number = os.environ.get("ISSUE_NUMBER")
        self.enabled = bool(self.token and self.repo)
        if self.enabled:
            log.info("GitHub reporting enabled (repo=%s, parent=#%s)",
                     self.repo, self.issue_number or "none")

    def _api(self, method, path, payload=None):
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
            log.warning("GitHub API %s %s -> %d", method, path, exc.code)
            return exc.code, {}
        except Exception as exc:
            log.warning("GitHub API %s %s failed: %s", method, path, exc)
            return 0, {}

    def create_issue(self, title, body, labels=None):
        if not self.enabled:
            return None
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        status, data = self._api("POST", "issues", payload)
        if status == 201:
            num = data.get("number")
            log.info("Created issue #%s", num)
            return num
        return None

    def post_comment(self, body, issue_number=None):
        issue_number = issue_number or self.issue_number
        if not self.enabled or not issue_number:
            return False
        status, _ = self._api("POST", f"issues/{issue_number}/comments", {"body": body})
        return status == 201

    def update_labels(self, labels, issue_number=None):
        issue_number = issue_number or self.issue_number
        if not self.enabled or not issue_number:
            return False
        status, _ = self._api("PUT", f"issues/{issue_number}/labels", {"labels": labels})
        return status == 200


_reporter: GitHubIssueReporter | None = None


def _post_to_issue(body, issue_number=None):
    if _reporter:
        _reporter.post_comment(body, issue_number=issue_number)


# ---------------------------------------------------------------------------
# Load program.md
# ---------------------------------------------------------------------------


def _load_program(path=None):
    p = Path(path) if path else SCRIPT_DIR / "program.md"
    if p.is_file():
        text = p.read_text(encoding="utf-8")
        log.info("Loaded program.md (%d chars)", len(text))
        return text
    return ""


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------


def _is_reasoning_model(model_name):
    m = model_name.lower()
    return m.startswith(("o1", "o3", "o4", "gpt-5"))


def _call_llm(messages, max_tokens=2000, json_mode=False, label="LLM"):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("[%s] OPENAI_API_KEY not set", label)
        return None
    if OpenAI is None:
        log.warning("[%s] openai package not installed", label)
        return None

    client = OpenAI(api_key=api_key)
    reasoning = _is_reasoning_model(OPENAI_MODEL)
    kwargs = {"model": OPENAI_MODEL, "messages": messages}

    if reasoning:
        kwargs["max_completion_tokens"] = max_tokens
        if json_mode:
            msgs = list(messages)
            if msgs and msgs[0]["role"] == "system":
                msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\nRespond with valid JSON only."}
            kwargs["messages"] = msgs
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0.7
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

    log.debug("[%s] model=%s reasoning=%s tokens=%d", label, OPENAI_MODEL, reasoning, max_tokens)

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as first_err:
        err_str = str(first_err).lower()
        if not reasoning and ("max_tokens" in err_str or "unsupported" in err_str):
            log.info("[%s] Retrying as reasoning model", label)
            kwargs.pop("max_tokens", None)
            kwargs.pop("temperature", None)
            kwargs.pop("response_format", None)
            kwargs["max_completion_tokens"] = max_tokens
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
    log.info("[%s] finish=%s len=%s usage=%s", label,
             choice.finish_reason if choice else "none",
             len(content) if content else 0,
             f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "N/A")
    return (content or "").strip()


# ---------------------------------------------------------------------------
# Seed media
# ---------------------------------------------------------------------------


def fetch_seed_media(media_id):
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
        log.error("Seed media fetch failed: %s", exc)
    return None


def _summarize_seed(seed_data):
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
        ("Title", "title"), ("Type", "media_type"), ("Modality", "modality"),
        ("Taxonomy", "physical_object_taxonomy_name"),
        ("Organization", "physical_object_organization"),
        ("Specimen", "physical_object_title"), ("Device", "device"),
        ("Description", "short_description"),
    ]:
        val = _val(record, key)
        if val:
            parts.append(f"**{label}:** {val}")
    return "\n".join(parts) if parts else json.dumps(record, indent=2)[:800]


# ---------------------------------------------------------------------------
# Decompose
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = (
    "You are a research planning assistant for MorphoSource (a database of 3-D "
    "specimen scans, media and physical objects). Decompose the user's research "
    "goal into 3-5 specific search queries.\n\n"
    "Return a JSON object: {\"queries\": [{\"query\": \"...\", \"rationale\": \"...\"}]}\n"
    "Return ONLY the JSON object."
)

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "for", "on", "by",
    "is", "are", "was", "were", "be", "been", "with", "at", "from", "as",
    "it", "its", "that", "this", "how", "what", "which", "identify",
    "analyze", "find", "show", "most", "promising", "research", "pathways",
    "ecosystem", "let", "lets", "about", "develop", "follow", "paper",
    "titled", "new", "twist", "evolution", "uncovers", "extremely",
    "specialized", "morphology",
}

_CONCEPT_QUERIES = {
    "specimen": {"query": "specimen", "rationale": "Browse physical specimen records."},
    "ct": {"query": "CT scan", "rationale": "Browse CT scan media records."},
    "scan": {"query": "CT scan", "rationale": "Browse 3-D scan media."},
    "metadata": {"query": "metadata", "rationale": "Search records by metadata fields."},
    "mesh": {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
}


def _heuristic_decompose(topic):
    queries, seen = [], set()
    lower = topic.lower()
    for kw, entry in _CONCEPT_QUERIES.items():
        if kw in lower and entry["query"] not in seen:
            queries.append(dict(entry))
            seen.add(entry["query"])
    stop = {"Analyze", "Analysis", "Identify", "Data", "Database", "MorphoSource",
            "Research", "Pathways", "Promising", "Ecosystem", "Show", "Find", "Browse",
            "Develop", "Follow", "Paper", "Titled", "Evolution", "Uncovers", "Extremely",
            "Specialized", "Morphology", "Twist", "New"}
    for tok in re.findall(r"\b([A-Z][a-z]{3,})\b", topic):
        if tok not in stop:
            qt = f"{tok} specimens"
            if qt not in seen:
                queries.append({"query": qt, "rationale": f"Search for {tok} records."})
                seen.add(qt)
    if not queries:
        words = re.findall(r"[A-Za-z][A-Za-z'-]+", topic)
        kws = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 2]
        queries.append({"query": " ".join(kws[:4]) or topic, "rationale": "Broad search."})
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
    user_parts = [topic]
    if seed_context:
        user_parts.append(f"\nSeed media record:\n{seed_context}")
    if memory_context:
        user_parts.append(
            f"\nPrevious research memory — avoid repeating failed queries, "
            f"build on successful leads:\n{memory_context}"
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
        if attempt < _LLM_RETRIES:
            time.sleep(min(2 ** (attempt - 1), 4))

    return _heuristic_decompose(topic)


# ---------------------------------------------------------------------------
# Search
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
                params, fmt.get("formatted_query", qt), query_info=fmt)
            cnt = sr.get("summary", {}).get("count", 0)
            results.append({
                "query": qt, "rationale": item.get("rationale", ""),
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
    refined, seen = [], set()
    for r in search_results:
        if r["result_count"] > 0:
            continue
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", r["query"])
                 if w.lower() not in _STOPWORDS and len(w) > 2]
        for w in words:
            if w.lower() not in seen:
                refined.append({"query": w, "rationale": f"Simplified retry of '{r['query']}'."})
                seen.add(w.lower())
    if not refined:
        return [], False
    return execute_searches(refined[:MAX_QUERIES]), True


# ---------------------------------------------------------------------------
# Synthesize
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
        lines.append(f"- **{r['query']}** -- {r['result_count']} result(s)")
    lines.append("\n## Conclusion\n\nSee data above.")
    return {"status": "fallback", "report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

_EVALUATE_SYSTEM = (
    "You are evaluating one iteration of an autonomous MorphoSource research agent. "
    "Given the research topic, the iteration's search results, and accumulated memory "
    "from prior iterations, produce a JSON evaluation.\n\n"
    "Return a JSON object with these keys:\n"
    '  "score": integer 1-10,\n'
    '  "discoveries": list of strings,\n'
    '  "dead_ends": list of strings,\n'
    '  "next_directions": list of 3-5 strings,\n'
    '  "summary": string (2-3 paragraph running summary)\n\n'
    "Return ONLY the JSON object."
)


def evaluate_iteration(topic, search_results, memory, program=""):
    context_parts = [f"Research topic: {topic}"]
    if program:
        context_parts.append(f"Research program strategy:\n{program[:2000]}")
    results_summary = [{"query": r["query"], "result_count": r["result_count"],
                        "status": r["result_status"]} for r in search_results]
    context_parts.append(f"This cycle's results:\n{json.dumps(results_summary, indent=2)}")
    if memory:
        prev = memory.get("summary", "")
        tried = memory.get("queries_tried", [])
        if prev:
            context_parts.append(f"Previous summary:\n{prev}")
        if tried:
            context_parts.append(f"Previously tried queries: {json.dumps(tried[-20:])}")

    content = _call_llm(
        [{"role": "system", "content": _EVALUATE_SYSTEM},
         {"role": "user", "content": "\n\n".join(context_parts)}],
        max_tokens=2000, json_mode=True, label="Evaluate",
    )
    if content:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                log.info("Evaluation score: %s/10", parsed.get("score", "?"))
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "score": 5, "discoveries": [],
        "dead_ends": [r["query"] for r in search_results if r["result_count"] == 0],
        "next_directions": ["Try broader taxonomy searches", "Explore different modalities"],
        "summary": "Evaluation unavailable.",
    }


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _build_memory(cycle, search_results, evaluation, prev_memory):
    prev_tried = prev_memory.get("queries_tried", []) if prev_memory else []
    prev_dead = prev_memory.get("dead_ends", []) if prev_memory else []
    prev_disc = prev_memory.get("all_discoveries", []) if prev_memory else []
    return {
        "iteration": cycle,
        "queries_tried": prev_tried + [{"query": r["query"], "count": r["result_count"]}
                                        for r in search_results],
        "dead_ends": list(set(prev_dead + evaluation.get("dead_ends", []))),
        "all_discoveries": prev_disc + evaluation.get("discoveries", []),
        "next_directions": evaluation.get("next_directions", []),
        "summary": evaluation.get("summary", ""),
        "score": evaluation.get("score", 5),
    }


def _format_memory_for_llm(memory):
    if not memory:
        return ""
    parts = [f"Cycles completed: {memory.get('iteration', 0)}",
             f"Score: {memory.get('score', '?')}/10"]
    tried = memory.get("queries_tried", [])
    if tried:
        parts.append("Queries tried:\n" + "\n".join(
            f'  - "{t["query"]}" -> {t["count"]} results' for t in tried[-15:]))
    dead = memory.get("dead_ends", [])
    if dead:
        parts.append(f"Dead ends (DO NOT retry): {', '.join(dead[-10:])}")
    disc = memory.get("all_discoveries", [])
    if disc:
        parts.append("Discoveries:\n" + "\n".join(f"  - {d}" for d in disc[-10:]))
    nexts = memory.get("next_directions", [])
    if nexts:
        parts.append("Next directions:\n" + "\n".join(f"  - {n}" for n in nexts))
    summary = memory.get("summary", "")
    if summary:
        parts.append(f"Running summary:\n{summary}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Single research cycle (inner loop unit)
# ---------------------------------------------------------------------------


def run_single_cycle(topic, cycle, total_depth, memory, seed_context=None, program=""):
    """One fast cycle: decompose -> search -> refine -> evaluate -> memory."""
    t0 = time.time()
    log.info("--- Cycle %d/%d ---", cycle, total_depth)

    memory_context = _format_memory_for_llm(memory) if memory else None

    queries = decompose_topic(
        topic,
        seed_context=seed_context if cycle == 1 else None,
        memory_context=memory_context,
    )
    if _run_logger:
        _run_logger.event(cycle, "decompose", queries=[q["query"] for q in queries])

    search_results = execute_searches(queries)
    for _ in range(MAX_REFINEMENT_ROUNDS):
        if not any(r["result_count"] == 0 for r in search_results):
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
    if _run_logger:
        _run_logger.event(cycle, "search", total_hits=total_hits,
                          queries_run=len(search_results))

    evaluation = evaluate_iteration(topic, search_results, memory, program)
    new_memory = _build_memory(cycle, search_results, evaluation, memory)

    duration_ms = int((time.time() - t0) * 1000)
    if _run_logger:
        _run_logger.event(cycle, "evaluate",
                          score=evaluation.get("score", 0),
                          discoveries=evaluation.get("discoveries", []),
                          duration_ms=duration_ms,
                          total_hits=total_hits,
                          memory_summary=evaluation.get("summary", "")[:300])

    log.info("Cycle %d: score=%s/10, hits=%d, %dms",
             cycle, evaluation.get("score", "?"), total_hits, duration_ms)

    result = {
        "cycle": cycle,
        "queries": queries,
        "search_results": [{k: v for k, v in r.items() if k != "result_data"}
                           for r in search_results],
        "total_hits": total_hits,
        "evaluation": evaluation,
    }
    return result, new_memory, search_results


# ---------------------------------------------------------------------------
# Build a GitHub issue from aggregated cycles
# ---------------------------------------------------------------------------


def _build_issue_report(topic, cycle_results, memory, seed_context, issue_num, total_issues):
    """Synthesize a report from multiple cycles and post as a GitHub issue."""
    all_search_results = []
    for cr in cycle_results:
        all_search_results.extend(cr.get("search_results", []))

    memory_context = _format_memory_for_llm(memory)
    report = synthesize_report(topic, all_search_results,
                               seed_context=seed_context, memory_context=memory_context)

    cycles_covered = [cr["cycle"] for cr in cycle_results]
    total_hits = sum(cr["total_hits"] for cr in cycle_results)
    latest_score = memory.get("score", "?") if memory else "?"
    discoveries = memory.get("all_discoveries", [])[-10:] if memory else []
    next_dirs = memory.get("next_directions", []) if memory else []

    disc_lines = "\n".join(f"- {d}" for d in discoveries) if discoveries else "_None yet_"
    next_lines = "\n".join(f"- {n}" for n in next_dirs) if next_dirs else "_Continuing..._"
    report_text = report.get("report", "_No report._")

    issue_body = (
        f"## Research Issue {issue_num}/{total_issues}\n\n"
        f"**Topic:** {topic}\n"
        f"**Cycles covered:** {cycles_covered[0]}-{cycles_covered[-1]}\n"
        f"**Total hits this batch:** {total_hits}\n"
        f"**Current score:** {latest_score}/10\n\n"
        f"### Key Discoveries\n{disc_lines}\n\n"
        f"### Next Directions\n{next_lines}\n\n"
        f"---\n\n### Full Report\n\n{report_text}\n\n"
        f"---\n_AutoResearchClaw issue {issue_num}/{total_issues}_"
    )

    if _run_logger:
        _run_logger.event(cycles_covered[-1], "github_issue",
                          issue_num=issue_num, total_issues=total_issues)

    return issue_body, report


# ---------------------------------------------------------------------------
# Orchestrator — two-loop architecture
# ---------------------------------------------------------------------------


def run_research_program(topic, research_depth=10, github_issues=3,
                         media_id=None, program=""):
    """Two-loop research program.

    Inner loop (research_depth): fast cycles building memory.
    Outer loop (github_issues): periodic GitHub issue creation.
    """
    log.info("Starting: %d cycles, %d GitHub issues", research_depth, github_issues)

    seed_context = None
    if media_id:
        seed_data = fetch_seed_media(media_id)
        if seed_data:
            seed_context = _summarize_seed(seed_data)
            padded = media_id.strip().lstrip("0").zfill(9)
            _post_to_issue(
                f"### Seed Media Record\n\n"
                f"Fetched **[media {padded}]"
                f"(https://www.morphosource.org/concern/media/{padded})** "
                f"as research seed:\n\n{seed_context}")

    _post_to_issue(
        f"### Research Program Started\n\n"
        f"**Depth:** {research_depth} cycles | "
        f"**Issues:** {github_issues} reports\n\n"
        f"Dashboard: `http://localhost:5001`")

    github_issues = min(github_issues, research_depth)
    issue_interval = max(1, research_depth // github_issues)

    memory = None
    all_cycle_results = []
    pending_cycles = []
    iteration_issues = []
    issue_counter = 0

    for cycle in range(1, research_depth + 1):
        result, memory, _raw = run_single_cycle(
            topic, cycle, research_depth, memory,
            seed_context=seed_context, program=program,
        )
        all_cycle_results.append(result)
        pending_cycles.append(result)

        at_issue_boundary = (cycle % issue_interval == 0) or (cycle == research_depth)
        issues_remaining = github_issues - issue_counter

        if at_issue_boundary and issues_remaining > 0:
            issue_counter += 1
            issue_title = f"Research [{issue_counter}/{github_issues}]: {topic[:80]}"

            issue_body, _ = _build_issue_report(
                topic, pending_cycles, memory, seed_context,
                issue_counter, github_issues,
            )

            gh_num = None
            if _reporter:
                gh_num = _reporter.create_issue(
                    issue_title, issue_body,
                    labels=["research-agent", f"issue-{issue_counter}"],
                )
                if gh_num:
                    iteration_issues.append(gh_num)
                    _reporter.update_labels(
                        ["research-agent", f"issue-{issue_counter}", "completed"],
                        issue_number=gh_num,
                    )

            issue_link = f"#{gh_num}" if gh_num else f"Issue {issue_counter}"
            score = memory.get("score", "?") if memory else "?"
            hits = sum(c["total_hits"] for c in pending_cycles)
            _post_to_issue(
                f"### Issue {issue_counter}/{github_issues} posted\n\n"
                f"**Cycles:** {pending_cycles[0]['cycle']}-{pending_cycles[-1]['cycle']} | "
                f"**Score:** {score}/10 | **Hits:** {hits}\n\n"
                f"Report: {issue_link}")

            log.info("GitHub issue %d/%d posted (cycles %d-%d)",
                     issue_counter, github_issues,
                     pending_cycles[0]["cycle"], pending_cycles[-1]["cycle"])

            if _run_logger:
                _run_logger.update_meta(issue_links=[f"#{n}" for n in iteration_issues])

            pending_cycles = []

    # --- Final synthesis ---
    log.info("=" * 60)
    log.info("FINAL SYNTHESIS across %d cycles", research_depth)
    log.info("=" * 60)

    if memory:
        all_disc = memory.get("all_discoveries", [])
        total_queries = len(memory.get("queries_tried", []))
        issue_links = " ".join(f"#{n}" for n in iteration_issues) if iteration_issues else "N/A"

        disc_text = "\n".join(f"- {d}" for d in all_disc) if all_disc else "_None_"
        final_body = (
            f"## Final Research Summary\n\n"
            f"**Topic:** {topic}\n"
            f"**Cycles:** {research_depth} | **Issues:** {len(iteration_issues)}\n"
            f"**Total queries:** {total_queries}\n"
            f"**Issue links:** {issue_links}\n\n"
            f"### All Discoveries\n{disc_text}\n\n"
            f"### Summary\n{memory.get('summary', '')}\n\n"
            f"---\n\n_Comment on this issue to continue the research._"
        )
        _post_to_issue(final_body)

    if _run_logger:
        _run_logger.finish(status="completed", issue_links=[f"#{n}" for n in iteration_issues])

    return {
        "topic": topic,
        "research_depth": research_depth,
        "github_issues_created": len(iteration_issues),
        "iteration_issues": iteration_issues,
        "final_memory": memory,
        "all_results": all_cycle_results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    global _reporter, _run_logger
    import argparse

    parser = argparse.ArgumentParser(
        description="AutoResearchClaw — autonomous MorphoSource research agent")
    parser.add_argument("topic", help="Research topic or goal")
    parser.add_argument("--media-id", default=None, help="Seed media ID")
    parser.add_argument("--research-depth", type=int, default=10,
                        help="Internal research cycles (default: 10)")
    parser.add_argument("--github-issues", type=int, default=3,
                        help="GitHub issues to create (default: 3)")
    parser.add_argument("--program", default=None, help="Path to program.md")
    args = parser.parse_args()

    _reporter = GitHubIssueReporter()
    _run_logger = RunLogger(args.topic, args.research_depth, args.github_issues, args.media_id)
    program = _load_program(args.program)

    log.info("=" * 60)
    log.info("AutoResearchClaw starting")
    log.info("Topic: %s", args.topic)
    log.info("Research depth: %d cycles", args.research_depth)
    log.info("GitHub issues: %d", args.github_issues)
    log.info("Seed media: %s", args.media_id or "none")
    log.info("Model: %s", OPENAI_MODEL)
    log.info("Run ID: %s", _run_logger.run_id)
    log.info("Log file: %s", _run_logger.jsonl_path)
    log.info("=" * 60)

    try:
        result = run_research_program(
            args.topic,
            research_depth=args.research_depth,
            github_issues=args.github_issues,
            media_id=args.media_id,
            program=program,
        )
    except Exception:
        log.error("Research program failed:\n%s", traceback.format_exc())
        _post_to_issue(f"### Error\n\n```\n{traceback.format_exc()}\n```")
        if _reporter:
            _reporter.update_labels(["research-agent", "error"])
        if _run_logger:
            _run_logger.finish(status="error")
        sys.exit(1)

    output = {
        "topic": result["topic"],
        "research_depth": result["research_depth"],
        "github_issues_created": result["github_issues_created"],
        "iteration_issues": result["iteration_issues"],
        "final_memory": result["final_memory"],
        "run_id": _run_logger.run_id if _run_logger else None,
    }
    with open("research_report.json", "w") as f:
        json.dump(output, f, indent=2)

    last = result["all_results"][-1] if result["all_results"] else {}
    report_text = last.get("report", {}).get("report", "") if isinstance(last.get("report"), dict) else ""
    if report_text:
        with open("research_report.md", "w") as f:
            f.write(report_text)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"research_depth={result['research_depth']}\n")
            f.write(f"github_issues_created={result['github_issues_created']}\n")

    if _reporter:
        _reporter.update_labels(["research-agent", "completed"])

    log.info("Complete: %d cycles, %d issues", result["research_depth"], result["github_issues_created"])


if __name__ == "__main__":
    main()
