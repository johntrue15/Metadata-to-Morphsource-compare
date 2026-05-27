#!/usr/bin/env python3
"""Tiny stdlib-only dashboard for the project-358382 skull batch.

Reads orchestrator state from disk:

* ``runs/skull_batch_358382/orchestrator.pid``  - liveness
* ``runs/skull_batch_358382/orchestrator.log``  - tail + currently-running slug
* ``runs/skull_batch_358382/progress.csv``      - one row per attempt
* ``Tests/fixtures/nninteractive_compare/_manifest_358382.json`` - the queue
* ``Tests/fixtures/nninteractive_compare/<slug>/baseline_metrics.json`` -
  cached metrics per completed fixture

Serves:

* ``GET /``           - single-page HTML/CSS/JS dashboard (auto-refresh)
* ``GET /api/status`` - JSON snapshot of everything
* ``GET /api/log?n=N`` - last N orchestrator log lines (default 200)

No dependencies beyond Python stdlib. Bind defaults to 127.0.0.1:7860.

Usage::

    python scripts/dev/dashboard.py            # opens browser to localhost:7860
    python scripts/dev/dashboard.py --port 8765
    python scripts/dev/dashboard.py --no-browser
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dashboard")

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "runs" / "skull_batch_358382"
FIXTURE_ROOT = REPO_ROOT / "Tests" / "fixtures" / "nninteractive_compare"
MANIFEST_PATH = FIXTURE_ROOT / "_manifest_358382.json"
PID_FILE = RUNS_DIR / "orchestrator.pid"
LOG_FILE = RUNS_DIR / "orchestrator.log"
PROGRESS_CSV = RUNS_DIR / "progress.csv"

# The autopilot sweep harness writes its state under
# paper_artifacts/sweep/ (see .github/scripts/sweep_harness.py). We
# surface a summary card in this dashboard so the same browser tab
# covers both the legacy colors-of-skull batch and the new 24/7
# sweep.
SWEEP_DIR = REPO_ROOT / "paper_artifacts" / "sweep"
SWEEP_QUEUE = SWEEP_DIR / "sweep_queue.jsonl"
SWEEP_RESULTS = SWEEP_DIR / "sweep_results.jsonl"
SWEEP_INFLIGHT_DIR = SWEEP_DIR / "in_flight"
SWEEP_DAEMON_PID = SWEEP_DIR / "sweep_daemon.pid"
SWEEP_DAEMON_LOG = SWEEP_DIR / "sweep_daemon.log"

GH_REPO = os.environ.get("GH_REPO", "johntrue15/MorphoClaw")


# ---------------------------------------------------------------------------
# State collection
# ---------------------------------------------------------------------------

def _is_pid_alive(pid: int) -> bool:
    """Cross-platform liveness check."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        exit_code = ctypes.c_ulong()
        try:
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                h, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_pid() -> Optional[int]:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _orchestrator_state() -> dict:
    pid = _read_pid()
    alive = bool(pid and _is_pid_alive(pid))
    started_mtime: Optional[float] = None
    if PID_FILE.exists():
        try:
            started_mtime = PID_FILE.stat().st_mtime
        except OSError:
            pass
    return {
        "pid": pid,
        "alive": alive,
        "started_unix": started_mtime,
        "started_iso": (datetime.fromtimestamp(started_mtime, tz=timezone.utc)
                        .isoformat() if started_mtime else None),
        "uptime_s": (time.time() - started_mtime) if started_mtime else None,
        "log_size_bytes": (LOG_FILE.stat().st_size if LOG_FILE.exists() else 0),
        "progress_rows": (sum(1 for _ in PROGRESS_CSV.open()) - 1
                          if PROGRESS_CSV.exists() else 0),
    }


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"pairs": [], "project_id": "?", "project_title": "?"}
    return json.loads(MANIFEST_PATH.read_text())


def _load_progress_rows() -> list[dict]:
    if not PROGRESS_CSV.exists():
        return []
    with PROGRESS_CSV.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_log_tail(n: int = 200) -> list[str]:
    if not LOG_FILE.exists():
        return []
    # Cheap tail: read whole file (it's bounded by orchestrator output volume).
    # The orchestrator logs roughly 30 lines per specimen, so even 17 specimens
    # is < 1000 lines.
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:]


_RUN_DISPATCH_RE = re.compile(
    r"-> dispatched run (\d+) for (\S+)")
_RUN_STATUS_RE = re.compile(
    r"run (\d+): status=(\S+) conclusion=(\S*)")


def _current_run_from_log() -> Optional[dict]:
    """Find the most recently dispatched run that hasn't yet completed."""
    lines = _read_log_tail(500)
    last_dispatch: Optional[dict] = None
    last_status: dict[str, dict] = {}  # run_id -> {status, conclusion}
    for ln in lines:
        m = _RUN_DISPATCH_RE.search(ln)
        if m:
            last_dispatch = {"run_id": m.group(1), "slug": m.group(2),
                             "line": ln}
        m = _RUN_STATUS_RE.search(ln)
        if m:
            last_status[m.group(1)] = {
                "status": m.group(2), "conclusion": m.group(3) or None}
    if not last_dispatch:
        return None
    rid = last_dispatch["run_id"]
    if rid in last_status:
        last_dispatch.update(last_status[rid])
    return last_dispatch


def _fixture_status(slug: str) -> dict:
    """Inspect Tests/fixtures/.../<slug>/ on disk for a completed bundle."""
    fx = FIXTURE_ROOT / slug
    if not fx.is_dir():
        return {"fixture_present": False}
    metrics_path = fx / "baseline_metrics.json"
    if not metrics_path.exists():
        return {"fixture_present": True, "metrics_present": False}
    try:
        m = json.loads(metrics_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"fixture_present": True, "metrics_present": False}
    size_bytes = sum(p.stat().st_size for p in fx.iterdir() if p.is_file())
    return {
        "fixture_present": True,
        "metrics_present": True,
        "dice": m.get("dice"),
        "iou": m.get("iou"),
        "precision": m.get("precision"),
        "recall": m.get("recall"),
        "voxel_count_pred": m.get("voxel_count_pred"),
        "voxel_count_gt": m.get("voxel_count_gt"),
        "volume_mm3_pred": m.get("volume_mm3_pred"),
        "volume_mm3_gt": m.get("volume_mm3_gt"),
        "image_shape_zyx": m.get("image_shape_zyx"),
        "fixture_size_bytes": size_bytes,
    }


def _latest_progress_for_slug(rows: list[dict], slug: str) -> Optional[dict]:
    matches = [r for r in rows if r.get("slug") == slug]
    if not matches:
        return None
    # Order-preserving last row is the most recent
    return matches[-1]


def _classify(specimen: dict) -> str:
    if not specimen.get("eligible", True):
        return "skipped_too_large"
    if specimen.get("metrics_present"):
        return "completed"
    if specimen.get("is_running"):
        return "in_progress"
    last_status = (specimen.get("last_progress") or {}).get("status")
    if last_status in {"workflow_failed", "timeout", "dispatch_failed",
                       "artifact_dl_failed", "stage_failed", "git_failed"}:
        return "failed"
    if last_status == "success":
        # success in CSV but fixture not yet on disk -> staging in flight
        return "completed"
    if last_status == "skipped_cached":
        return "completed"
    return "queued"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _sweep_state() -> dict:
    """Summarise the autopilot sweep harness (paper_artifacts/sweep/).

    Returns a dict the dashboard JSON / HTML render directly:
    queue/done/in-flight counts, daemon liveness, the N most recent
    results, and the currently-running job (if any).
    """
    queue = _read_jsonl(SWEEP_QUEUE)
    results = _read_jsonl(SWEEP_RESULTS)
    results_by_id = {r.get("job_id"): r for r in results}
    in_flight_ids: list[str] = []
    if SWEEP_INFLIGHT_DIR.exists():
        in_flight_ids = sorted(
            p.stem for p in SWEEP_INFLIGHT_DIR.glob("*.lock")
        )
    in_flight_set = set(in_flight_ids)

    n_done = sum(1 for j in queue if j.get("job_id") in results_by_id)
    n_in_flight = sum(1 for j in queue if j.get("job_id") in in_flight_set)
    n_pending = max(0, len(queue) - n_done - n_in_flight)

    daemon_pid: Optional[int] = None
    if SWEEP_DAEMON_PID.exists():
        try:
            daemon_pid = int(SWEEP_DAEMON_PID.read_text().strip() or 0)
        except ValueError:
            daemon_pid = None
    daemon_alive = bool(daemon_pid) and _is_pid_alive(daemon_pid)

    recent = results[-10:] if results else []
    # Mirror render_status's headline fields. Each entry already has
    # status / duration / clicks / union / stop_reason from
    # sweep_harness._summarise_run.
    recent_view = [
        {
            "job_id": r.get("job_id"),
            "status": r.get("status"),
            "duration_s": r.get("duration_s"),
            "n_clicks": r.get("n_clicks"),
            "union_voxels": r.get("union_voxels"),
            "stop_reason": r.get("stop_reason"),
            "timed_out": r.get("timed_out"),
            "params_override": r.get("params_override"),
            "media_id": r.get("media_id"),
        }
        for r in recent
    ]

    current_job: Optional[dict] = None
    for jid in in_flight_ids:
        # The lock file holds a small JSON blob with claimed_at + pid.
        lock = SWEEP_INFLIGHT_DIR / f"{jid}.lock"
        meta = {}
        try:
            meta = json.loads(lock.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        current_job = {
            "job_id": jid,
            "claimed_at": meta.get("claimed_at"),
            "claimed_by_pid": meta.get("pid"),
        }
        break  # one daemon -> at most one in-flight job

    # Per-specimen mini summary (e.g. "mouse" / "felis"): how many
    # configurations completed, mean duration, count failed.
    by_media: dict[str, dict] = {}
    for r in results:
        mid = r.get("media_id") or "unknown"
        bucket = by_media.setdefault(mid, {
            "completed": 0, "failed": 0, "timed_out": 0,
            "total_duration_s": 0.0,
        })
        if r.get("status") == "success":
            bucket["completed"] += 1
        else:
            bucket["failed"] += 1
            if r.get("timed_out"):
                bucket["timed_out"] += 1
        bucket["total_duration_s"] += float(r.get("duration_s") or 0)

    return {
        "state_dir": str(SWEEP_DIR),
        "queue_size": len(queue),
        "done": n_done,
        "in_flight": n_in_flight,
        "pending": n_pending,
        "daemon_pid": daemon_pid,
        "daemon_alive": daemon_alive,
        "current_job": current_job,
        "recent_results": recent_view,
        "by_media": by_media,
    }


def _collect_state() -> dict:
    manifest = _load_manifest()
    progress = _load_progress_rows()
    current = _current_run_from_log()
    orch = _orchestrator_state()
    sweep = _sweep_state()

    # Map slug -> all specs needed
    pairs = manifest.get("pairs", [])
    # Queue order = smallest CT first, matching the orchestrator
    eligible = [p for p in pairs if p.get("eligible", True)]
    eligible.sort(key=lambda p: int(p.get("ct_file_size") or 0))
    non_eligible = [p for p in pairs if not p.get("eligible", True)]
    ordered = eligible + non_eligible

    specimens: list[dict] = []
    n_completed = 0
    n_failed = 0
    n_in_progress = 0
    n_queued = 0
    n_skipped = 0
    dice_values: list[float] = []
    fixture_bytes_total = 0

    for i, p in enumerate(ordered):
        slug = p["slug"]
        fx = _fixture_status(slug)
        last = _latest_progress_for_slug(progress, slug)
        is_running = (current is not None
                      and current.get("slug") == slug
                      and not fx.get("metrics_present")
                      and orch["alive"])

        spec = {
            "slug": slug,
            "queue_position": i + 1,
            "physical_object_id": p.get("physical_object_id"),
            "taxonomy": p.get("taxonomy"),
            "title": p.get("title"),
            "ct_media_id": p.get("ct_media_id"),
            "mesh_media_id": p.get("mesh_media_id"),
            "ct_file_size_gb": round(
                int(p.get("ct_file_size") or 0) / (1 << 30), 3),
            "mesh_file_size_mb": round(
                int(p.get("mesh_file_size") or 0) / (1 << 20), 2),
            "eligible": p.get("eligible", True),
            "skip_reason": p.get("skip_reason"),
            "is_running": is_running,
            "current_run": current if is_running else None,
            "last_progress": last,
            **fx,
        }
        spec["status"] = _classify(spec)

        if spec["status"] == "completed":
            n_completed += 1
            if spec.get("dice") is not None:
                dice_values.append(float(spec["dice"]))
            if spec.get("fixture_size_bytes"):
                fixture_bytes_total += spec["fixture_size_bytes"]
        elif spec["status"] == "failed":
            n_failed += 1
        elif spec["status"] == "in_progress":
            n_in_progress += 1
        elif spec["status"] == "skipped_too_large":
            n_skipped += 1
        else:
            n_queued += 1

        specimens.append(spec)

    return {
        "now_iso": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "project": {
            "id": manifest.get("project_id"),
            "title": manifest.get("project_title"),
            "url": manifest.get("project_url"),
            "total_pairs": len(pairs),
            "eligible": len(eligible),
            "ineligible": len(non_eligible),
        },
        "orchestrator": orch,
        "summary": {
            "completed": n_completed,
            "failed": n_failed,
            "in_progress": n_in_progress,
            "queued": n_queued,
            "skipped_too_large": n_skipped,
            "total_eligible": len(eligible),
            "mean_dice": (round(sum(dice_values) / len(dice_values), 4)
                          if dice_values else None),
            "max_dice": (round(max(dice_values), 4) if dice_values else None),
            "fixture_size_mb_total": round(
                fixture_bytes_total / (1 << 20), 2),
        },
        "specimens": specimens,
        "current_run": current,
        "sweep": sweep,
        "github": {
            "repo": GH_REPO,
            "actions_url": f"https://github.com/{GH_REPO}/actions",
        },
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MorphoClaw 358382 skull batch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --border: #30363d;
    --fg: #c9d1d9;
    --muted: #8b949e;
    --link: #58a6ff;
    --green: #2ea043;
    --yellow: #d29922;
    --red: #f85149;
    --blue: #1f6feb;
    --grey: #6e7681;
    --purple: #a371f7;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    background: var(--bg); color: var(--fg);
    font-size: 14px; line-height: 1.4;
  }
  a { color: var(--link); text-decoration: none; }
  a:hover { text-decoration: underline; }
  header {
    padding: 18px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
    display: flex; align-items: baseline; gap: 24px;
    flex-wrap: wrap;
  }
  header h1 {
    margin: 0; font-size: 18px; font-weight: 600;
  }
  header .small { color: var(--muted); font-size: 12px; }
  main {
    padding: 18px 24px;
    max-width: 1400px;
    margin: 0 auto;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 18px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }
  .card .label {
    color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.04em;
    margin-bottom: 4px;
  }
  .card .value {
    font-size: 22px; font-weight: 600;
  }
  .card .subtitle {
    color: var(--muted); font-size: 12px; margin-top: 2px;
  }
  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .pill-completed  { background: rgba(46,160,67,.18);  color: var(--green); }
  .pill-in_progress{ background: rgba(31,111,235,.18); color: var(--link);
                     animation: pulse 1.6s infinite; }
  .pill-failed     { background: rgba(248,81,73,.18);  color: var(--red); }
  .pill-queued     { background: rgba(110,118,129,.18);color: var(--grey); }
  .pill-skipped_too_large {
                     background: rgba(163,113,247,.18);color: var(--purple); }
  .pill-alive      { background: rgba(46,160,67,.18);  color: var(--green); }
  .pill-down       { background: rgba(248,81,73,.18);  color: var(--red); }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.55; }
  }

  table {
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  th, td {
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    vertical-align: middle;
  }
  th {
    background: #1c2128;
    color: var(--muted);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  tr:last-child td { border-bottom: none; }
  tr.running { background: rgba(31,111,235,.06); }
  tr.failed  { background: rgba(248,81,73,.04); }
  tr.completed { background: rgba(46,160,67,.04); }

  .mono { font-family: "JetBrains Mono", ui-monospace, SFMono-Regular,
                      Menlo, Consolas, monospace;
          font-size: 12px; }
  .muted { color: var(--muted); }
  .right { text-align: right; }

  .progress-bar {
    height: 8px;
    background: var(--border);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 8px;
  }
  .progress-bar > div {
    height: 100%;
    background: linear-gradient(90deg, var(--green), var(--link));
    transition: width 0.4s ease;
  }

  .log-pane {
    background: #010409;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 14px;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
    line-height: 1.5;
    color: #c9d1d9;
    max-height: 360px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .log-pane .ts { color: var(--muted); }
  .log-pane .info { color: var(--fg); }
  .log-pane .warn { color: var(--yellow); }
  .log-pane .err  { color: var(--red); }

  .section-title {
    margin: 28px 0 10px;
    font-size: 14px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  .dice-bar {
    display: inline-block;
    width: 60px; height: 6px;
    background: var(--border);
    border-radius: 3px;
    overflow: hidden;
    vertical-align: middle;
    margin-right: 6px;
  }
  .dice-bar > div {
    height: 100%;
    background: var(--green);
  }
  .ago { color: var(--muted); font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>MorphoClaw &mdash; project <span id="project-id">358382</span> skull batch</h1>
  <div class="small" id="header-meta"></div>
  <div class="small" style="margin-left:auto">
    <a id="actions-link" href="#" target="_blank">GitHub Actions &rarr;</a>
    &middot;
    <span class="muted">refresh: <span id="refresh-counter">--</span>s</span>
  </div>
</header>
<main>
  <div class="grid" id="summary-cards"></div>

  <div class="section-title">Currently running</div>
  <div class="card" id="current-card">
    <span class="muted">Nothing in flight.</span>
  </div>

  <div class="section-title">
    Autopilot sweep
    <span class="muted" style="text-transform:none;letter-spacing:normal;font-weight:normal;font-size:12px">
      &mdash; bright-seed parameter exploration on Dell GPU
    </span>
  </div>
  <div class="grid" id="sweep-cards"></div>
  <div class="card" id="sweep-current-card" style="margin-bottom:18px">
    <span class="muted">No sweep job currently in flight.</span>
  </div>
  <table id="sweep-results-table" style="margin-bottom:18px">
    <thead><tr>
      <th>Job</th><th>Status</th>
      <th class="right">Duration</th>
      <th class="right">Clicks</th>
      <th class="right">Union voxels</th>
      <th>Stop reason</th>
    </tr></thead>
    <tbody></tbody>
  </table>

  <div class="section-title">Specimens</div>
  <table id="specimen-table">
    <thead><tr>
      <th>#</th><th>Status</th><th>Taxonomy</th><th>Slug</th>
      <th class="right">CT</th><th class="right">Mesh</th>
      <th class="right">Dice</th><th class="right">Voxels p / GT</th>
      <th class="right">Fixture</th>
      <th>Run</th>
    </tr></thead>
    <tbody></tbody>
  </table>

  <div class="section-title">Orchestrator log (tail)</div>
  <div class="log-pane" id="log-pane">loading...</div>
</main>

<script>
const REFRESH_S = 10;
let countdown = REFRESH_S;

function pill(status) {
  const labels = {
    completed: "completed", in_progress: "in progress",
    failed: "failed", queued: "queued",
    skipped_too_large: "skipped",
  };
  return `<span class="pill pill-${status}">${labels[status] || status}</span>`;
}
function fmtBytes(b) {
  if (b === null || b === undefined) return "&mdash;";
  if (b >= 1<<30) return (b / (1<<30)).toFixed(2) + " GB";
  if (b >= 1<<20) return (b / (1<<20)).toFixed(2) + " MB";
  if (b >= 1<<10) return (b / (1<<10)).toFixed(1) + " KB";
  return b + " B";
}
function fmtDuration(s) {
  if (s === null || s === undefined) return "&mdash;";
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${ss}s`;
  return `${ss}s`;
}
function fmtAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  const dt = (Date.now() - t) / 1000;
  return fmtDuration(dt) + " ago";
}
function diceCell(s) {
  if (s.dice === null || s.dice === undefined) return '<span class="muted">&mdash;</span>';
  const v = Number(s.dice);
  const pct = Math.max(0, Math.min(1, v)) * 100;
  return `<span class="dice-bar"><div style="width:${pct}%"></div></span>` +
         `<span class="mono">${v.toFixed(4)}</span>`;
}

async function refresh() {
  let st;
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    st = await r.json();
  } catch (e) {
    document.getElementById("header-meta").textContent =
      "ERROR fetching /api/status: " + e;
    return;
  }

  // --- header ---
  document.getElementById("project-id").textContent = st.project.id || "?";
  document.getElementById("actions-link").href = st.github.actions_url;
  const orchStatus = st.orchestrator.alive
    ? `<span class="pill pill-alive">orchestrator alive</span>`
    : `<span class="pill pill-down">orchestrator stopped</span>`;
  document.getElementById("header-meta").innerHTML =
    `${orchStatus} &nbsp; pid ${st.orchestrator.pid || "-"} &middot; ` +
    `up ${fmtDuration(st.orchestrator.uptime_s)} &middot; ` +
    `log ${fmtBytes(st.orchestrator.log_size_bytes)} &middot; ` +
    `last refresh ${new Date(st.now_iso).toLocaleTimeString()}`;

  // --- summary cards ---
  const s = st.summary;
  const total = s.total_eligible || 1;
  const pct = ((s.completed / total) * 100).toFixed(1);
  const cards = [
    {label: "Progress", value: `${s.completed} / ${total}`,
     bar: s.completed / total,
     subtitle: `${pct}% of eligible done`},
    {label: "In flight",  value: s.in_progress,
     subtitle: s.in_progress ? "GPU runner busy" : "idle"},
    {label: "Failed",     value: s.failed,
     subtitle: s.failed ? "needs attention" : "none"},
    {label: "Queued",     value: s.queued,
     subtitle: s.queued ? "waiting" : "queue empty"},
    {label: "Skipped",    value: s.skipped_too_large,
     subtitle: "CT > 5 GB"},
    {label: "Mean dice",  value: s.mean_dice === null ? "&mdash;" : s.mean_dice.toFixed(4),
     subtitle: s.max_dice === null ? "" : `max ${s.max_dice.toFixed(4)}`},
    {label: "Cache size", value: fmtBytes(s.fixture_size_mb_total * (1<<20)),
     subtitle: `${s.completed} fixture(s) on disk`},
  ];
  document.getElementById("summary-cards").innerHTML =
    cards.map(c => `
      <div class="card">
        <div class="label">${c.label}</div>
        <div class="value">${c.value}</div>
        ${c.subtitle ? `<div class="subtitle">${c.subtitle}</div>` : ""}
        ${c.bar !== undefined ? `<div class="progress-bar"><div style="width:${(c.bar*100).toFixed(1)}%"></div></div>` : ""}
      </div>`).join("");

  // --- current card ---
  const cur = st.current_run;
  const curSpec = st.specimens.find(x => x.is_running);
  if (cur && curSpec) {
    const runUrl = `https://github.com/${st.github.repo}/actions/runs/${cur.run_id}`;
    document.getElementById("current-card").innerHTML = `
      <div style="display:flex; gap:18px; align-items:center; flex-wrap:wrap">
        ${pill("in_progress")}
        <div>
          <div style="font-size:16px; font-weight:600">
            ${curSpec.taxonomy || curSpec.slug}
          </div>
          <div class="muted mono">${curSpec.slug}</div>
        </div>
        <div class="muted">CT&nbsp;<b>${curSpec.ct_file_size_gb} GB</b> &middot;
                           mesh&nbsp;<b>${curSpec.mesh_file_size_mb} MB</b></div>
        <div class="muted">step ${curSpec.queue_position} / ${st.project.eligible}</div>
        <div style="margin-left:auto">
          <a href="${runUrl}" target="_blank">run ${cur.run_id} &rarr;</a>
          <div class="muted">status: ${cur.status || "?"}</div>
        </div>
      </div>`;
  } else if (st.orchestrator.alive) {
    document.getElementById("current-card").innerHTML =
      `<span class="muted">Orchestrator alive; no run currently dispatched.</span>`;
  } else {
    document.getElementById("current-card").innerHTML =
      `<span class="muted">Orchestrator not running.</span>`;
  }

  // --- sweep cards ---
  const sw = st.sweep || {};
  const sweepCards = [
    {label: "Sweep progress",
     value: `${sw.done || 0} / ${sw.queue_size || 0}`,
     bar: sw.queue_size ? (sw.done / sw.queue_size) : 0,
     subtitle: `${sw.pending || 0} pending`},
    {label: "Daemon",
     value: sw.daemon_alive
       ? `<span class="pill pill-alive">alive</span>`
       : `<span class="pill pill-down">down</span>`,
     subtitle: sw.daemon_pid ? `pid ${sw.daemon_pid}` : "no pid file"},
    {label: "In flight",
     value: sw.in_flight || 0,
     subtitle: sw.in_flight ? "GPU busy" : "idle"},
    {label: "Recent failures",
     value: (sw.recent_results || [])
       .filter(r => r.status !== "success").length,
     subtitle: "in last 10"},
  ];
  // Per-media badges (mouse / felis / ...).
  const byMedia = sw.by_media || {};
  for (const [mid, b] of Object.entries(byMedia)) {
    const total = (b.completed || 0) + (b.failed || 0);
    const mean = total ? (b.total_duration_s / total) : 0;
    sweepCards.push({
      label: mid,
      value: `${b.completed || 0} ok`,
      subtitle: `${b.failed || 0} failed` +
                (b.timed_out ? ` (${b.timed_out} timeout)` : "") +
                ` &middot; avg ${fmtDuration(mean)}`,
    });
  }
  document.getElementById("sweep-cards").innerHTML =
    sweepCards.map(c => `
      <div class="card">
        <div class="label">${c.label}</div>
        <div class="value">${c.value}</div>
        ${c.subtitle ? `<div class="subtitle">${c.subtitle}</div>` : ""}
        ${c.bar !== undefined ? `<div class="progress-bar"><div style="width:${(c.bar*100).toFixed(1)}%"></div></div>` : ""}
      </div>`).join("");

  // --- sweep current job ---
  if (sw.current_job) {
    const cj = sw.current_job;
    document.getElementById("sweep-current-card").innerHTML = `
      <div style="display:flex; gap:18px; align-items:center; flex-wrap:wrap">
        ${pill("in_progress")}
        <div class="mono" style="font-size:13px">${cj.job_id}</div>
        <div class="muted">claimed ${fmtAgo(cj.claimed_at)}</div>
        <div class="muted">pid ${cj.claimed_by_pid || "?"}</div>
      </div>`;
  } else {
    document.getElementById("sweep-current-card").innerHTML =
      `<span class="muted">No sweep job currently in flight.</span>`;
  }

  // --- sweep results table (most recent first) ---
  const swTbody = document.querySelector("#sweep-results-table tbody");
  const recent = (sw.recent_results || []).slice().reverse();
  if (!recent.length) {
    swTbody.innerHTML = `<tr><td colspan="6" class="muted">No sweep results yet.</td></tr>`;
  } else {
    swTbody.innerHTML = recent.map(r => {
      const status = r.status === "success" ? "completed" :
                     (r.timed_out ? "failed" : "failed");
      const rowCls = r.status === "success" ? "completed" : "failed";
      return `
        <tr class="${rowCls}">
          <td class="mono" style="font-size:11px">${r.job_id || ""}</td>
          <td>${pill(status)}</td>
          <td class="right mono">${fmtDuration(r.duration_s)}</td>
          <td class="right mono">${r.n_clicks === null || r.n_clicks === undefined ? '<span class="muted">&mdash;</span>' : r.n_clicks}</td>
          <td class="right mono">${r.union_voxels ? Number(r.union_voxels).toLocaleString() : '<span class="muted">&mdash;</span>'}</td>
          <td class="muted" style="font-size:12px">${r.stop_reason || (r.timed_out ? "timeout" : "")}</td>
        </tr>`;
    }).join("");
  }

  // --- specimens table ---
  const tbody = document.querySelector("#specimen-table tbody");
  tbody.innerHTML = st.specimens.map(s => {
    const runUrl = s.last_progress && s.last_progress.run_id
      ? `https://github.com/${st.github.repo}/actions/runs/${s.last_progress.run_id}`
      : null;
    const rowCls = s.is_running ? "running" :
                   s.status === "failed" ? "failed" :
                   s.status === "completed" ? "completed" : "";
    return `
      <tr class="${rowCls}">
        <td class="mono muted">${s.queue_position}</td>
        <td>${pill(s.status)}</td>
        <td><b>${s.taxonomy || "?"}</b><br>
            <span class="muted" style="font-size:11px">${s.title || ""}</span></td>
        <td class="mono" style="font-size:11px">${s.slug}</td>
        <td class="right mono">${s.ct_file_size_gb} GB</td>
        <td class="right mono">${s.mesh_file_size_mb} MB</td>
        <td class="right">${diceCell(s)}</td>
        <td class="right mono">${
          s.voxel_count_pred !== undefined && s.voxel_count_pred !== null
            ? s.voxel_count_pred.toLocaleString() + " / " +
              (s.voxel_count_gt || 0).toLocaleString()
            : '<span class="muted">&mdash;</span>'
        }</td>
        <td class="right mono">${
          s.fixture_size_bytes ? fmtBytes(s.fixture_size_bytes)
                               : '<span class="muted">&mdash;</span>'
        }</td>
        <td>${runUrl
          ? `<a href="${runUrl}" target="_blank" class="mono" style="font-size:11px">${s.last_progress.run_id}</a>`
          : '<span class="muted">&mdash;</span>'}</td>
      </tr>`;
  }).join("");

  // --- log pane ---
  try {
    const r = await fetch("/api/log?n=120", {cache: "no-store"});
    const lines = (await r.json()).lines;
    document.getElementById("log-pane").innerHTML = lines.map(ln => {
      const cls = /\[ERROR\]/.test(ln) ? "err"
                : /\[WARN/.test(ln)    ? "warn"
                : /\[INFO\]/.test(ln)  ? "info" : "";
      return `<span class="${cls}">${ln.replace(/</g, "&lt;")}</span>`;
    }).join("\n");
    const pane = document.getElementById("log-pane");
    pane.scrollTop = pane.scrollHeight;
  } catch (e) {
    document.getElementById("log-pane").textContent = "ERROR loading log: " + e;
  }
}

function tick() {
  countdown -= 1;
  if (countdown <= 0) {
    countdown = REFRESH_S;
    refresh();
  }
  document.getElementById("refresh-counter").textContent = countdown;
}

refresh();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MorphoClawDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - " + fmt, self.address_string(), *args)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, code: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        try:
            if path == "/" or path == "/index.html":
                self._send_html(INDEX_HTML)
            elif path == "/api/status":
                self._send_json(_collect_state())
            elif path == "/api/log":
                try:
                    n = int(q.get("n", ["200"])[0])
                except ValueError:
                    n = 200
                n = max(1, min(n, 2000))
                self._send_json({"lines": _read_log_tail(n)})
            elif path == "/healthz":
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found", "path": path}, code=404)
        except Exception as exc:  # noqa: BLE001
            log.exception("error handling %s", path)
            self._send_json({"error": repr(exc)}, code=500)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--no-browser", action="store_true",
                   help="Don't try to launch a browser tab")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"==> Dashboard serving at {url}")
    print(f"    repo state: {REPO_ROOT}")
    print(f"    orchestrator state dir: {RUNS_DIR}")
    print(f"    fixture root: {FIXTURE_ROOT}")
    print(f"    Ctrl+C to stop.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n==> Stopped.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
