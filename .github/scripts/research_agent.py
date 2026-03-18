#!/usr/bin/env python3
"""
AutoResearchClaw research agent for MorphoSource.

Decomposes a research topic into targeted MorphoSource queries, iteratively
searches for relevant specimen data, and synthesizes findings into a
research report with actionable recommendations.
"""

import json
import os
import sys
import time
import importlib.util

if 'openai' in sys.modules:
    OpenAI = getattr(sys.modules['openai'], 'OpenAI', None)  # type: ignore
else:
    _openai_spec = importlib.util.find_spec("openai")
    if _openai_spec:
        from openai import OpenAI  # type: ignore
    else:
        OpenAI = None  # type: ignore

import query_formatter
import morphosource_api


# ---------------------------------------------------------------------------
# Stage 1: Decompose research topic into MorphoSource search queries
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM_PROMPT = (
    "You are a research planning assistant specializing in biological specimen data. "
    "The user will provide a high-level research goal. Your job is to decompose it "
    "into 3-5 specific search queries that should be run against MorphoSource "
    "(a database of 3-D specimen scans, media and physical objects).\n\n"
    "Return ONLY a JSON array of objects, each with:\n"
    '  "query": a concise natural-language search phrase,\n'
    '  "rationale": one sentence explaining why this query helps the research.\n\n'
    "Example output:\n"
    '[{"query": "CT scans of Anolis lizards", '
    '"rationale": "Retrieve available micro-CT data for the focal genus."}]\n'
    "Do NOT include any text outside the JSON array."
)

# Words to skip when extracting subject terms from a research topic
_DECOMPOSE_STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'for', 'on', 'by',
    'is', 'are', 'was', 'were', 'be', 'been', 'with', 'at', 'from', 'as',
    'it', 'its', 'that', 'this', 'these', 'those', 'how', 'what', 'which',
    'identify', 'analyze', 'analyse', 'find', 'show', 'most', 'promising',
    'research', 'pathways', 'opportunities', 'wedges', 'commercialization',
    'ecosystem', 'starting', 'let', 'lets', 'rethink', 'about',
}

# Known MorphoSource-relevant concepts to look for in research topics
_CONCEPT_QUERIES = {
    'specimen': {"query": "specimen", "rationale": "Browse physical specimen records."},
    'specimens': {"query": "specimen", "rationale": "Browse physical specimen records."},
    'ct': {"query": "CT scan", "rationale": "Browse CT scan media records."},
    'x-ray': {"query": "CT scan", "rationale": "Browse X-ray / CT media records."},
    'xray': {"query": "CT scan", "rationale": "Browse X-ray / CT media records."},
    'micro-ct': {"query": "CT scan", "rationale": "Browse microCT scan records."},
    'scan': {"query": "CT scan", "rationale": "Browse 3-D scan media."},
    'scans': {"query": "CT scan", "rationale": "Browse 3-D scan media."},
    'metadata': {"query": "metadata", "rationale": "Search records by metadata fields."},
    '3d': {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
    '3-d': {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
    'mesh': {"query": "3D mesh", "rationale": "Browse 3-D mesh and surface models."},
}


def _heuristic_decompose(topic):
    """Extract concrete, searchable sub-queries from a research topic.

    Used when the LLM is unavailable or returns an unparseable response.
    Scans the topic for known MorphoSource-relevant concepts and generates
    up to MAX_QUERIES targeted queries.
    """
    import re as _re

    queries = []
    seen_query_texts = set()
    lower_topic = topic.lower()

    # 1. Detect known concepts (CT, specimen, metadata, etc.)
    for keyword, entry in _CONCEPT_QUERIES.items():
        if keyword in lower_topic and entry["query"] not in seen_query_texts:
            queries.append(dict(entry))
            seen_query_texts.add(entry["query"])

    # 2. Extract biological / taxonomic terms (capitalized, Latin-looking)
    tokens = _re.findall(r"\b([A-Z][a-z]{3,})\b", topic)
    taxon_stopwords = {
        'Analyze', 'Analysis', 'Identify', 'Data', 'Database',
        'MorphoSource', 'Morphosource', 'Research', 'Pathways',
        'Opportunities', 'Wedges', 'Commercialization', 'Promising',
        'Ecosystem', 'Starting', 'Show', 'Find', 'List', 'Browse',
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

    # 3. If still nothing, fall back to a broad browse query
    if not queries:
        # Extract a few non-stop keywords from the topic
        words = _re.findall(r"[A-Za-z][A-Za-z'-]+", topic)
        keywords = [w for w in words if w.lower() not in _DECOMPOSE_STOPWORDS and len(w) > 2]
        q_text = ' '.join(keywords[:3]) if keywords else topic
        queries.append({
            "query": q_text,
            "rationale": "Broad keyword search (heuristic fallback).",
        })

    return queries[:MAX_QUERIES]


MAX_QUERIES = 5
_LLM_RETRIES = 3


def _parse_decompose_response(text):
    """Parse an LLM decomposition response into a list of query dicts.

    Handles markdown-fenced JSON (even when preceded by other text),
    JSON arrays embedded in surrounding prose, and plain JSON arrays.
    Returns ``None`` when the text cannot be parsed into a non-empty list.
    """
    if not text:
        return None

    # Try markdown-fenced blocks anywhere in the text
    if '```' in text:
        parts = text.split('```')
        for part in parts[1::2]:  # odd-indexed parts are inside fences
            candidate = part
            if candidate.startswith('json'):
                candidate = candidate[4:]
            candidate = candidate.strip()
            if candidate:
                try:
                    queries = json.loads(candidate)
                    if isinstance(queries, list) and queries:
                        return queries
                except (json.JSONDecodeError, ValueError):
                    continue

    # Try to parse the whole (stripped) text as JSON
    stripped = text.strip()
    if not stripped:
        return None

    try:
        queries = json.loads(stripped)
        if isinstance(queries, list) and queries:
            return queries
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to extract a JSON array embedded in surrounding text
    start = stripped.find('[')
    end = stripped.rfind(']')
    if start != -1 and end > start:
        try:
            queries = json.loads(stripped[start:end + 1])
            if isinstance(queries, list) and queries:
                return queries
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def decompose_topic(topic):
    """Break a research topic into specific MorphoSource search queries.

    Returns a list of dicts with ``query`` and ``rationale`` keys, or a
    heuristic decomposition when the LLM is unavailable.
    """
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key or OpenAI is None:
        return _heuristic_decompose(topic)

    last_exc = None
    for attempt in range(1, _LLM_RETRIES + 1):
        try:
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-5",
                messages=[
                    {"role": "system", "content": _DECOMPOSE_SYSTEM_PROMPT},
                    {"role": "user", "content": topic},
                ],
                max_completion_tokens=2000,
            )
            content = response.choices[0].message.content
            text = (content or "").strip()
            queries = _parse_decompose_response(text)
            if queries is not None:
                return queries[:MAX_QUERIES]
            # Empty / unparseable — retry if attempts remain
            if attempt < _LLM_RETRIES:
                print(f"⚠ Decomposition attempt {attempt}: LLM returned empty/unparseable response, retrying …")
            else:
                print(f"⚠ Decomposition attempt {attempt}: LLM returned empty/unparseable response")
        except Exception as exc:
            last_exc = exc
            print(f"⚠ Decomposition attempt {attempt} failed: {exc}")

        if attempt < _LLM_RETRIES:
            time.sleep(min(2 ** (attempt - 1), 4))

    if last_exc:
        print(f"⚠ Decomposition failed after {_LLM_RETRIES} attempts, using heuristic fallback")
    else:
        print("⚠ LLM returned empty content, using heuristic fallback")
    return _heuristic_decompose(topic)


# ---------------------------------------------------------------------------
# Stage 2: Execute MorphoSource searches for each sub-query
# ---------------------------------------------------------------------------

def execute_searches(queries):
    """Run each sub-query through the existing query pipeline.

    Returns a list of result dicts, one per query.
    """
    results = []
    for item in queries:
        query_text = item["query"]
        print(f"\n🔍 Searching MorphoSource: {query_text}")

        formatted = query_formatter.format_query(query_text)
        api_params = formatted.get("api_params", {"q": query_text, "per_page": 10})

        search_result = morphosource_api.search_morphosource(
            api_params,
            formatted.get("formatted_query", query_text),
            query_info=formatted,
        )

        results.append({
            "query": query_text,
            "rationale": item.get("rationale", ""),
            "formatted_query": formatted.get("formatted_query", query_text),
            "api_endpoint": formatted.get("api_endpoint", "media"),
            "result_count": search_result.get("summary", {}).get("count", 0),
            "result_status": search_result.get("summary", {}).get("status", "unknown"),
            "result_data": search_result.get("full_data", {}),
        })

    return results


# ---------------------------------------------------------------------------
# Stage 2b: Iterative refinement for zero-result queries
# ---------------------------------------------------------------------------

MAX_REFINEMENT_ROUNDS = 2


def _refine_zero_result_queries(search_results):
    """Generate simplified queries for searches that returned zero results.

    Returns a list of new query dicts to retry.
    """
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

        # Strategy 1: try individual significant words
        for word in meaningful:
            if word.lower() not in seen:
                refined.append({
                    "query": word,
                    "rationale": f"Simplified retry of '{original}' (zero results).",
                })
                seen.add(word.lower())

        # Strategy 2: if multi-word, try shorter combinations
        if len(meaningful) >= 2:
            shorter = ' '.join(meaningful[:2])
            if shorter.lower() not in seen:
                refined.append({
                    "query": shorter,
                    "rationale": f"Paired-term retry of '{original}'.",
                })
                seen.add(shorter.lower())

    return refined[:MAX_QUERIES]


def refine_searches(search_results):
    """Run a refinement pass: re-query zero-result searches with simplified terms.

    Returns (new_results, had_refinements) where new_results is a list of
    results from the refined queries and had_refinements is a boolean.
    """
    refined_queries = _refine_zero_result_queries(search_results)
    if not refined_queries:
        return [], False

    print(f"\n🔄 Refining {len(refined_queries)} zero-result queries …")
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


def synthesize_report(topic, search_results):
    """Generate a Markdown research report from collected search results.

    Returns a dict with ``status`` and ``report`` keys.
    """
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key or OpenAI is None:
        return _fallback_report(topic, search_results, reason="no_api_key")

    # Build a compact context payload for the LLM
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
        # Include a slice of actual data so the LLM can see specimen names
        data = r.get("result_data", {})
        for key in ("media", "physical_objects"):
            items = data.get(key, [])
            if not items and isinstance(data.get("response"), dict):
                items = data["response"].get(key, [])
            if items:
                entry["sample_records"] = items[:3]
                break
        context_items.append(entry)

    user_message = (
        f"Research topic: {topic}\n\n"
        f"MorphoSource search results:\n{json.dumps(context_items, indent=2)}"
    )

    last_exc = None
    for attempt in range(1, _LLM_RETRIES + 1):
        try:
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-5",
                messages=[
                    {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                max_completion_tokens=4000,
            )
            content = response.choices[0].message.content
            report = (content or "").strip()
            if report:
                return {"status": "success", "report": report}
            if attempt < _LLM_RETRIES:
                print(f"⚠ Synthesis attempt {attempt}: LLM returned empty report, retrying …")
            else:
                print(f"⚠ Synthesis attempt {attempt}: LLM returned empty report")
        except Exception as exc:
            last_exc = exc
            print(f"⚠ Synthesis attempt {attempt} failed: {exc}")

        if attempt < _LLM_RETRIES:
            time.sleep(min(2 ** (attempt - 1), 4))

    reason = str(last_exc) if last_exc else "LLM returned empty response"
    print(f"⚠ Synthesis failed after {_LLM_RETRIES} attempts, using fallback")
    return _fallback_report(topic, search_results, reason=reason)


def _fallback_report(topic, search_results, reason=None):
    """Build a plain-text report when the LLM is unavailable.

    Parameters
    ----------
    topic : str
        The original research topic.
    search_results : list
        Collected search results from MorphoSource.
    reason : str or None
        Why AI synthesis was not used.  ``"no_api_key"`` when the key is
        missing, any other non-empty string for an error description, or
        ``None`` which defaults to the same behaviour as ``"no_api_key"``.
    """
    lines = [
        "## Research Topic",
        "",
        topic,
        "",
        "## Available Data on MorphoSource",
        "",
    ]
    for r in search_results:
        status = "✅" if r["result_count"] > 0 else "⚠️"
        lines.append(
            f"- {status} **{r['query']}** — {r['result_count']} result(s) "
            f"via `{r['api_endpoint']}` endpoint ({r['result_status']})"
        )
        # Include sample records when available
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

def run_research(topic):
    """End-to-end research pipeline: decompose → search → refine → synthesize.

    Returns a dict with ``topic``, ``queries``, ``search_results``, and
    ``report`` keys.
    """
    print(f"🧪 AutoResearchClaw — starting research on: {topic}")

    # Stage 1
    print("\n📋 Stage 1: Decomposing research topic …")
    queries = decompose_topic(topic)
    print(f"   → {len(queries)} sub-queries generated")

    # Stage 2
    print("\n🔎 Stage 2: Executing MorphoSource searches …")
    search_results = execute_searches(queries)
    total_hits = sum(r["result_count"] for r in search_results)
    print(f"   → {total_hits} total results across {len(search_results)} queries")

    # Stage 2b: Iterative refinement for zero-result queries
    for iteration in range(1, MAX_REFINEMENT_ROUNDS + 1):
        zero_count = sum(1 for r in search_results if r["result_count"] == 0)
        if zero_count == 0:
            break
        print(f"\n🔄 Refinement round {iteration}: "
              f"{zero_count} zero-result queries to retry …")
        new_results, had_refinements = refine_searches(search_results)
        if not had_refinements:
            break
        # Keep only new results that improved on zero-result originals
        improved = [r for r in new_results if r["result_count"] > 0]
        if improved:
            search_results.extend(improved)
            print(f"   → {len(improved)} refined queries returned results")
        else:
            print("   → No improvement from refinement")
            break

    total_hits = sum(r["result_count"] for r in search_results)
    print(f"   → Final: {total_hits} total results across "
          f"{len(search_results)} queries")

    # Stage 3
    print("\n📝 Stage 3: Synthesizing research report …")
    report = synthesize_report(topic, search_results)
    print(f"   → Report generated (status: {report['status']})")

    return {
        "topic": topic,
        "queries": queries,
        "search_results": [
            {k: v for k, v in r.items() if k != "result_data"}
            for r in search_results
        ],
        "report": report,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for the research agent."""
    if len(sys.argv) < 2:
        print("Usage: research_agent.py '<research_topic>'")
        sys.exit(1)

    topic = sys.argv[1]
    result = run_research(topic)

    with open("research_report.json", "w") as f:
        json.dump(result, f, indent=2)

    # Write the Markdown report for easy reading
    report_text = result["report"].get("report", "")
    if report_text:
        with open("research_report.md", "w") as f:
            f.write(report_text)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"report_status={result['report']['status']}\n")
            f.write(f"query_count={len(result['queries'])}\n")
            total = sum(r["result_count"] for r in result["search_results"])
            f.write(f"total_results={total}\n")

    print("\n✅ Research complete — see research_report.json / research_report.md")


if __name__ == "__main__":
    main()
