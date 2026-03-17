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

MAX_QUERIES = 5


def decompose_topic(topic):
    """Break a research topic into specific MorphoSource search queries.

    Returns a list of dicts with ``query`` and ``rationale`` keys, or a
    single-element fallback list when the LLM is unavailable.
    """
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key or OpenAI is None:
        return [{"query": topic, "rationale": "Direct search (LLM unavailable)."}]

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
        text = response.choices[0].message.content.strip()
        # Strip markdown fences if present
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
            text = text.strip()

        queries = json.loads(text)
        if not isinstance(queries, list) or not queries:
            return [{"query": topic, "rationale": "Fallback (unexpected LLM format)."}]
        return queries[:MAX_QUERIES]
    except Exception as exc:
        print(f"⚠ Decomposition failed: {exc}")
        return [{"query": topic, "rationale": "Fallback (decomposition error)."}]


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
        return _fallback_report(topic, search_results)

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
        report = response.choices[0].message.content.strip()
        return {"status": "success", "report": report}
    except Exception as exc:
        print(f"⚠ Synthesis failed: {exc}")
        return _fallback_report(topic, search_results)


def _fallback_report(topic, search_results):
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
        status = "✅" if r["result_count"] > 0 else "⚠️"
        lines.append(
            f"- {status} **{r['query']}** — {r['result_count']} result(s) "
            f"via `{r['api_endpoint']}` endpoint ({r['result_status']})"
        )
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
        "See the data summary above. A more detailed report requires an "
        "OpenAI API key for AI-powered synthesis.",
    ]
    return {"status": "fallback", "report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_research(topic):
    """End-to-end research pipeline: decompose → search → synthesize.

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

    # Stage 3
    print("\n📝 Stage 3: Synthesizing research report …")
    report = synthesize_report(topic, search_results)
    print("   → Report generated")

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
