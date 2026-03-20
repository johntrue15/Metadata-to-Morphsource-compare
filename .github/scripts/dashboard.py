#!/usr/bin/env python3
"""
AutoResearchClaw local dashboard.

Reads JSONL logs from ~/.autoresearchclaw/logs/ and serves a live
monitoring UI at http://localhost:5001

Run:  python3 dashboard.py
"""

import json
import os
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string

app = Flask(__name__)
LOG_DIR = Path.home() / ".autoresearchclaw" / "logs"

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _list_runs():
    """Return all runs sorted newest-first."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    runs = []
    for meta_path in sorted(LOG_DIR.glob("*_meta.json"), reverse=True):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            jsonl = LOG_DIR / f"{meta['run_id']}.jsonl"
            meta["has_log"] = jsonl.exists()
            meta["cycle_count"] = sum(1 for _ in open(jsonl) if jsonl.exists()) if jsonl.exists() else 0
            runs.append(meta)
        except Exception:
            continue
    return runs


def _read_events(run_id):
    """Read all JSONL events for a run."""
    jsonl = LOG_DIR / f"{run_id}.jsonl"
    if not jsonl.exists():
        return []
    events = []
    for line in jsonl.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _read_meta(run_id):
    """Read metadata for a run."""
    meta_path = LOG_DIR / f"{run_id}_meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    return None


def _extract_detail_data(events):
    """Extract score trend, queries, and discoveries from raw events."""
    scores = []
    queries = []
    discoveries = []
    for e in events:
        stage = e.get("stage", "")
        cycle = e.get("cycle", 0)
        if stage == "evaluate" and "score" in e:
            scores.append({"cycle": cycle, "score": e["score"]})
            for d in e.get("discoveries", []):
                discoveries.append({"cycle": cycle, "text": d})
        if stage == "decompose" and "queries" in e:
            for q in e["queries"]:
                queries.append({"cycle": cycle, "query": q, "hits": None})
        if stage == "search":
            hits = e.get("total_hits", 0)
            n_queries = e.get("queries_run", 0)
            if queries:
                last_decompose = [q for q in queries if q["hits"] is None]
                if last_decompose and n_queries > 0:
                    per_q = hits // max(n_queries, 1)
                    for q in last_decompose:
                        q["hits"] = per_q
    for q in queries:
        if q["hits"] is None:
            q["hits"] = 0
    return scores, queries, discoveries


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoResearchClaw Dashboard</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9;
          --accent: #58a6ff; --green: #3fb950; --yellow: #d29922; --red: #f85149; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); color: var(--text); padding: 20px; }
  h1 { color: var(--accent); margin-bottom: 8px; font-size: 1.6em; }
  h2 { color: var(--accent); margin-bottom: 12px; font-size: 1.3em; }
  .subtitle { color: #8b949e; margin-bottom: 24px; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .runs { display: flex; flex-direction: column; gap: 12px; }
  .run-card { background: var(--card); border: 1px solid var(--border);
              border-radius: 8px; padding: 16px; transition: border-color 0.2s; }
  .run-card:hover { border-color: var(--accent); }
  .run-header { display: flex; justify-content: space-between; align-items: center; }
  .run-topic { font-weight: 600; font-size: 1.05em; }
  .badge { padding: 2px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 600; }
  .badge-running { background: #1f3a1f; color: var(--green); }
  .badge-completed { background: #1a2a1a; color: var(--green); }
  .badge-error { background: #2a1a1a; color: var(--red); }
  .run-meta { color: #8b949e; font-size: 0.85em; margin-top: 6px; }
  .run-meta span { margin-right: 16px; }
  .empty { text-align: center; padding: 60px; color: #8b949e; }

  /* Detail page */
  .detail-header { margin-bottom: 20px; }
  .stats { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
  .stat { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 12px 20px; min-width: 120px; }
  .stat-value { font-size: 1.8em; font-weight: 700; color: var(--accent); }
  .stat-label { color: #8b949e; font-size: 0.85em; }
  .section { margin-bottom: 24px; }
  .section h2 { margin-bottom: 12px; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
  @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }
  .chart-box { background: var(--card); border: 1px solid var(--border);
               border-radius: 8px; padding: 16px; }
  .chart-box h3 { color: var(--accent); font-size: 1em; margin-bottom: 10px; }
  svg.trend { width: 100%; height: 120px; }
  .trend-line { fill: none; stroke: var(--accent); stroke-width: 2; }
  .trend-dot { fill: var(--accent); }
  .trend-area { fill: var(--accent); opacity: 0.08; }
  .trend-grid { stroke: var(--border); stroke-width: 0.5; }
  .trend-label { fill: #8b949e; font-size: 10px; font-family: inherit; }
  .query-table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
  .query-table th { text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--border);
                    color: #8b949e; font-weight: 600; }
  .query-table td { padding: 6px 10px; border-bottom: 1px solid var(--border); }
  .query-table tr:last-child td { border-bottom: none; }
  .disc-item { padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 0.9em; }
  .disc-item:last-child { border-bottom: none; }
  .disc-cycle { color: var(--yellow); font-weight: 600; margin-right: 8px; }
  .issue-link { display: inline-block; background: #1a2a1a; color: var(--green);
                padding: 4px 12px; border-radius: 14px; margin: 4px 4px 4px 0;
                font-size: 0.9em; font-weight: 600; }
  .issue-link:hover { background: #243a24; text-decoration: none; }
  .events { font-family: 'SF Mono', Monaco, monospace; font-size: 0.85em;
            background: var(--card); border: 1px solid var(--border);
            border-radius: 8px; padding: 16px; max-height: 600px;
            overflow-y: auto; line-height: 1.6; }
  .event { border-bottom: 1px solid var(--border); padding: 6px 0; }
  .event:last-child { border-bottom: none; }
  .event-time { color: #8b949e; }
  .event-cycle { color: var(--yellow); font-weight: 600; }
  .event-stage { color: var(--green); }
  .score-bar { display: inline-block; width: 60px; height: 8px; background: var(--border);
               border-radius: 4px; vertical-align: middle; margin-left: 6px; }
  .score-fill { height: 100%; border-radius: 4px; background: var(--green); }
  .back { margin-bottom: 16px; display: inline-block; }
</style>
</head>
<body>
{% if run %}
  <a href="/" class="back">&larr; All Runs</a>
  <div class="detail-header">
    <h1>{{ run.topic[:80] }}</h1>
    <div class="subtitle">Run ID: {{ run.run_id }} &middot;
      <span class="badge badge-{{ run.status }}">{{ run.status }}</span>
    </div>
  </div>
  <div class="stats">
    <div class="stat">
      <div class="stat-value">{{ run.research_depth }}</div>
      <div class="stat-label">Depth</div>
    </div>
    <div class="stat">
      <div class="stat-value">{{ run.github_issues }}</div>
      <div class="stat-label">GH Issues</div>
    </div>
    <div class="stat">
      <div class="stat-value">{{ run.model }}</div>
      <div class="stat-label">Model</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="cycle-count">{{ events|length }}</div>
      <div class="stat-label">Events</div>
    </div>
    {% if latest_score is not none %}
    <div class="stat">
      <div class="stat-value">{{ latest_score }}/10</div>
      <div class="stat-label">Score</div>
    </div>
    {% endif %}
  </div>

  {% if issue_links %}
  <div class="section">
    <h2>GitHub Issues</h2>
    <div>
    {% for link in issue_links %}
      {% if repo %}
      <a class="issue-link" href="https://github.com/{{ repo }}/issues/{{ link[1:] }}" target="_blank">{{ link }}</a>
      {% else %}
      <span class="issue-link">{{ link }}</span>
      {% endif %}
    {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="two-col">
    <div class="chart-box">
      <h3>Score Trend</h3>
      <div id="score-chart">
      {% if scores|length > 1 %}
        <svg class="trend" viewBox="0 0 400 120" preserveAspectRatio="none">
          <line x1="0" y1="0" x2="0" y2="120" class="trend-grid"/>
          {% for i in range(11) %}<line x1="0" y1="{{ i * 12 }}" x2="400" y2="{{ i * 12 }}" class="trend-grid"/>{% endfor %}
          {% set ns = namespace(points='', area='') %}
          {% for s in scores %}
            {% set x = (loop.index0 / (scores|length - 1)) * 380 + 10 %}
            {% set y = 114 - (s.score / 10) * 108 %}
            {% set ns.points = ns.points ~ x|string ~ ',' ~ y|string ~ ' ' %}
          {% endfor %}
          <polyline points="10,114 {{ ns.points }} {{ 380 * ((scores|length - 1) / (scores|length - 1)) + 10 }},114" class="trend-area"/>
          <polyline points="{{ ns.points }}" class="trend-line"/>
          {% for s in scores %}
            {% set x = (loop.index0 / (scores|length - 1)) * 380 + 10 %}
            {% set y = 114 - (s.score / 10) * 108 %}
            <circle cx="{{ x }}" cy="{{ y }}" r="3.5" class="trend-dot"/>
            <text x="{{ x }}" y="{{ y - 8 }}" text-anchor="middle" class="trend-label">{{ s.score }}</text>
          {% endfor %}
        </svg>
      {% elif scores|length == 1 %}
        <div style="text-align:center;padding:20px;color:var(--accent);font-size:2em;font-weight:700;">{{ scores[0].score }}/10</div>
      {% else %}
        <div style="padding:20px;color:#8b949e;text-align:center;">No scores yet</div>
      {% endif %}
      </div>
    </div>
    <div class="chart-box">
      <h3>Discoveries Over Time</h3>
      <div id="disc-list" style="max-height:200px;overflow-y:auto;">
      {% if discoveries %}
        {% for d in discoveries %}
        <div class="disc-item"><span class="disc-cycle">C{{ d.cycle }}</span>{{ d.text }}</div>
        {% endfor %}
      {% else %}
        <div style="padding:20px;color:#8b949e;text-align:center;">No discoveries yet</div>
      {% endif %}
      </div>
    </div>
  </div>

  {% if queries %}
  <div class="section">
    <h2>Queries Tried</h2>
    <div class="chart-box">
      <table class="query-table" id="query-table">
        <thead><tr><th>Cycle</th><th>Query</th><th>Hits</th></tr></thead>
        <tbody>
        {% for q in queries %}
          <tr><td>C{{ q.cycle }}</td><td>{{ q.query }}</td><td>{{ q.hits }}</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}

  <h2>Live Event Log</h2>
  <div class="events" id="events">
    {% for e in events %}
    <div class="event">
      <span class="event-time">{{ e.timestamp[11:19] }}</span>
      <span class="event-cycle">C{{ e.cycle }}</span>
      <span class="event-stage">{{ e.stage }}</span>
      {% if e.total_hits is defined %} &middot; {{ e.total_hits }} hits{% endif %}
      {% if e.score is defined %} &middot; score={{ e.score }}
        <span class="score-bar"><span class="score-fill" style="width:{{ e.score * 10 }}%"></span></span>
      {% endif %}
      {% if e.queries is defined %} &middot; queries: {{ e.queries|join(', ') }}{% endif %}
      {% if e.discoveries is defined and e.discoveries %} &middot; found: {{ e.discoveries|join('; ')|truncate(120) }}{% endif %}
      {% if e.issue_num is defined %} &middot; <strong>GitHub issue #{{ e.issue_num }}</strong>{% endif %}
      {% if e.duration_ms is defined %} &middot; {{ e.duration_ms }}ms{% endif %}
    </div>
    {% endfor %}
  </div>
  <script>
    const runId = "{{ run.run_id }}";
    const eventsDiv = document.getElementById("events");
    let lastCount = {{ events|length }};
    let allEvents = {{ events|tojson }};

    function rebuildExtras() {
      const scores = [], queries = [], discs = [];
      for (const e of allEvents) {
        if (e.stage === "evaluate" && e.score !== undefined) {
          scores.push({cycle: e.cycle, score: e.score});
          for (const d of (e.discoveries || [])) discs.push({cycle: e.cycle, text: d});
        }
        if (e.stage === "decompose" && e.queries)
          for (const q of e.queries) queries.push({cycle: e.cycle, query: q, hits: null});
        if (e.stage === "search") {
          const h = e.total_hits || 0, n = e.queries_run || 0;
          const pending = queries.filter(q => q.hits === null);
          const perQ = n > 0 ? Math.floor(h / n) : 0;
          pending.forEach(q => q.hits = perQ);
        }
      }
      queries.forEach(q => { if (q.hits === null) q.hits = 0; });

      const chart = document.getElementById("score-chart");
      if (chart && scores.length > 1) {
        const W = 400, H = 120;
        let pts = scores.map((s, i) => {
          const x = (i / (scores.length - 1)) * 380 + 10;
          const y = 114 - (s.score / 10) * 108;
          return {x, y, score: s.score};
        });
        let svg = `<svg class="trend" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
        for (let i = 0; i <= 10; i++) svg += `<line x1="0" y1="${i*12}" x2="${W}" y2="${i*12}" class="trend-grid"/>`;
        const areaP = "10,114 " + pts.map(p => p.x+","+p.y).join(" ") + ` ${pts[pts.length-1].x},114`;
        svg += `<polyline points="${areaP}" class="trend-area"/>`;
        svg += `<polyline points="${pts.map(p=>p.x+","+p.y).join(" ")}" class="trend-line"/>`;
        pts.forEach(p => {
          svg += `<circle cx="${p.x}" cy="${p.y}" r="3.5" class="trend-dot"/>`;
          svg += `<text x="${p.x}" y="${p.y-8}" text-anchor="middle" class="trend-label">${p.score}</text>`;
        });
        svg += `</svg>`;
        chart.innerHTML = svg;
      } else if (chart && scores.length === 1) {
        chart.innerHTML = `<div style="text-align:center;padding:20px;color:var(--accent);font-size:2em;font-weight:700;">${scores[0].score}/10</div>`;
      }

      const discList = document.getElementById("disc-list");
      if (discList && discs.length) {
        discList.innerHTML = discs.map(d =>
          `<div class="disc-item"><span class="disc-cycle">C${d.cycle}</span>${d.text}</div>`
        ).join("");
      }

      const qt = document.getElementById("query-table");
      if (qt && queries.length) {
        const tbody = qt.querySelector("tbody");
        if (tbody) tbody.innerHTML = queries.map(q =>
          `<tr><td>C${q.cycle}</td><td>${q.query}</td><td>${q.hits}</td></tr>`
        ).join("");
      }
    }

    async function poll() {
      try {
        const resp = await fetch(`/api/run/${runId}/events`);
        const data = await resp.json();
        if (data.length > lastCount) {
          const newEvents = data.slice(lastCount);
          allEvents = data;
          for (const e of newEvents) {
            const div = document.createElement("div");
            div.className = "event";
            let html = `<span class="event-time">${(e.timestamp||'').substring(11,19)}</span>
              <span class="event-cycle">C${e.cycle}</span>
              <span class="event-stage">${e.stage}</span>`;
            if (e.total_hits !== undefined) html += ` &middot; ${e.total_hits} hits`;
            if (e.score !== undefined) html += ` &middot; score=${e.score}
              <span class="score-bar"><span class="score-fill" style="width:${e.score*10}%"></span></span>`;
            if (e.queries) html += ` &middot; queries: ${e.queries.join(', ')}`;
            if (e.discoveries && e.discoveries.length) html += ` &middot; found: ${e.discoveries.join('; ').substring(0,120)}`;
            if (e.issue_num !== undefined) html += ` &middot; <strong>GitHub issue #${e.issue_num}</strong>`;
            if (e.duration_ms !== undefined) html += ` &middot; ${e.duration_ms}ms`;
            div.innerHTML = html;
            eventsDiv.appendChild(div);
          }
          lastCount = data.length;
          document.getElementById("cycle-count").textContent = lastCount;
          eventsDiv.scrollTop = eventsDiv.scrollHeight;
          rebuildExtras();
        }
      } catch(err) {}
      setTimeout(poll, 3000);
    }
    poll();
    eventsDiv.scrollTop = eventsDiv.scrollHeight;
  </script>

{% else %}
  <h1>AutoResearchClaw Dashboard</h1>
  <div class="subtitle">Monitoring research runs on this Mac mini</div>
  <div class="runs">
  {% if runs %}
    {% for r in runs %}
    <a href="/run/{{ r.run_id }}" style="text-decoration:none;color:inherit;">
    <div class="run-card">
      <div class="run-header">
        <span class="run-topic">{{ r.topic[:90] }}</span>
        <span class="badge badge-{{ r.status }}">{{ r.status }}</span>
      </div>
      <div class="run-meta">
        <span>{{ r.start_time[:16].replace('T',' ') }}</span>
        <span>Depth: {{ r.research_depth }}</span>
        <span>Issues: {{ r.github_issues }}</span>
        <span>Model: {{ r.model }}</span>
        {% if r.issue_links %}<span>GH: {{ r.issue_links|join(' ') }}</span>{% endif %}
      </div>
    </div>
    </a>
    {% endfor %}
  {% else %}
    <div class="empty">
      <p>No research runs yet.</p>
      <p>Trigger a workflow dispatch to start one.</p>
    </div>
  {% endif %}
  </div>
{% endif %}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    runs = _list_runs()
    return render_template_string(INDEX_HTML, runs=runs, run=None, events=None)


@app.route("/run/<run_id>")
def run_detail(run_id):
    meta = _read_meta(run_id)
    if not meta:
        return "Run not found", 404
    events = _read_events(run_id)
    scores, queries, discoveries = _extract_detail_data(events)
    latest_score = scores[-1]["score"] if scores else None
    issue_links = meta.get("issue_links", [])
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return render_template_string(
        INDEX_HTML, run=meta, events=events, runs=None,
        latest_score=latest_score, scores=scores, queries=queries,
        discoveries=discoveries, issue_links=issue_links, repo=repo,
    )


@app.route("/api/runs")
def api_runs():
    return jsonify(_list_runs())


@app.route("/api/run/<run_id>/events")
def api_events(run_id):
    return jsonify(_read_events(run_id))


@app.route("/api/run/<run_id>/meta")
def api_meta(run_id):
    meta = _read_meta(run_id)
    return jsonify(meta) if meta else ("Not found", 404)


@app.route("/api/run/<run_id>/stream")
def api_stream(run_id):
    """Server-Sent Events stream for live tailing."""
    jsonl = LOG_DIR / f"{run_id}.jsonl"

    def generate():
        if not jsonl.exists():
            yield f"data: {json.dumps({'error': 'not found'})}\n\n"
            return
        with open(jsonl, "r", encoding="utf-8") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line.strip():
                    yield f"data: {line.strip()}\n\n"
                else:
                    time.sleep(1)
                    meta = _read_meta(run_id)
                    if meta and meta.get("status") != "running":
                        yield f"data: {json.dumps({'stage': 'done', 'status': meta['status']})}\n\n"
                        return

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"AutoResearchClaw Dashboard")
    print(f"Log directory: {LOG_DIR}")
    print(f"Open http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
