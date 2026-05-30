#!/usr/bin/env python3
"""Select completed Jetstream skull jobs that should get a 3D-visual release.

Scans ``jobs/status/*.json`` for jobs in state ``done`` and resolves the
composite labelmap committed under ``results/<run_dir>/``. Emits one JSON
object per line (JSONL) on stdout::

    {"id": "...", "run_dir": "...", "composite": "...", "summary": "...",
     "steps": N, "union_voxels": N, "stop_reason": {...}}

Used by the release_skull_visuals workflow. Filtering of jobs that already have
a GitHub Release happens in the workflow (it needs the ``gh`` CLI).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATUS_DIR = REPO / "jobs" / "status"


def _composite_for(run_dir: str) -> str | None:
    base = REPO / "results" / run_dir / "bright_seed"
    for rel in ("artifacts/composite.nii.gz",
                "checkpoint/composite_latest.nrrd"):
        p = base / rel
        if p.exists():
            return str(p.relative_to(REPO))
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--job-id", default=None,
                   help="restrict to a single job id (jobs/status/<id>.json)")
    args = p.parse_args(argv)

    if not STATUS_DIR.exists():
        return 0

    if args.job_id:
        status_files = [STATUS_DIR / f"{args.job_id}.json"]
    else:
        status_files = sorted(STATUS_DIR.glob("*.json"))

    n = 0
    for sf in status_files:
        if not sf.exists():
            print(f"WARN: no status file {sf.name}", file=sys.stderr)
            continue
        try:
            st = json.loads(sf.read_text())
        except Exception as e:
            print(f"WARN: bad status {sf.name}: {e!r}", file=sys.stderr)
            continue
        if st.get("state") != "done":
            continue
        run_dir = st.get("run_dir")
        if not run_dir:
            print(f"WARN: {sf.name} has no run_dir", file=sys.stderr)
            continue
        composite = _composite_for(run_dir)
        if not composite:
            print(f"WARN: no composite labelmap for {run_dir}", file=sys.stderr)
            continue
        summary = REPO / "results" / run_dir / "bright_seed" / "summary.json"
        rec = {
            "id": st.get("id") or sf.stem,
            "run_dir": run_dir,
            "composite": composite,
            "summary": str(summary.relative_to(REPO)) if summary.exists() else "",
            "steps": st.get("steps"),
            "union_voxels": st.get("union_voxels"),
            "n_segments": st.get("n_segments"),
            "stop_reason": st.get("stop_reason"),
        }
        print(json.dumps(rec))
        n += 1

    if n == 0:
        print("no completed jobs to release", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
