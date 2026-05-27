#!/usr/bin/env python3
"""Run the tuatara sample through a full pilot-style bright-seed + Dice scoring.

Mirrors ``eval_project358382_pilot`` for a single URL-hosted specimen:

  1. Load CT from GitHub raw URL into Jetstream Slicer (skip if already present).
  2. Bright-seed with ``--max-steps`` = max(budgets), ``--no-stop-rules``.
  3. Post-hoc union composites at each budget and score vs local GT labelmap.

The Mac mini is the driver (HTTP to Jetstream only). GT voxelization must
exist at ``data/sample/tuatara_skull_000358663_gt_labelmap.nrrd`` (run
``make stage-sample-gt`` on a GPU host, or ``make stage-sample-gt`` here
with ``MORPHOCLAW_FORCE_MAC_VOXELIZE=1``).

Usage::

    set -a && source .env && set +a
    export SLICER_WEBSERVER_URL=https://http-149-165-155-127-2016.proxy-js2-iu.exosphere.app/
    python3 .github/scripts/jetstream_tuatara_complete.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE = REPO_ROOT / "data/sample/tuatara_sample_urls.json"
DEFAULT_GT = REPO_ROOT / "data/sample/tuatara_skull_000358663_gt_labelmap.nrrd"


def _raw_url(repo: str, ref: str, relpath: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{relpath.lstrip('/')}"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixture", type=Path, default=FIXTURE)
    p.add_argument("--budgets", type=str, default="10,25,50,100")
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--gt-path", type=Path, default=DEFAULT_GT)
    p.add_argument("--volume-name", type=str, default=None,
                   help="Slicer volume node name (default: fixture name; use if "
                        "Slicer suffixed e.g. *_ct_1)")
    p.add_argument("--skip-load", action="store_true",
                   help="CT already in Slicer scene; only run bright-seed + metrics")
    p.add_argument("--skip-metrics", action="store_true")
    p.add_argument("--no-reset", action="store_true",
                   help="Do not clear segmentation before clicking (continue)")
    p.add_argument("--no-screenshots", action="store_true")
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args(argv)

    raw = json.loads(args.fixture.read_text())
    repo = os.environ.get("GITHUB_REPO", raw["github_repo"])
    ref = os.environ.get("GITHUB_REF", raw["github_ref"])
    ct_url = _raw_url(repo, ref, raw["files"]["ct_nrrd"])
    volume_name = args.volume_name or raw["slicer_volume_name"]
    defaults = raw.get("pilot_defaults") or {}
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    max_steps = args.max_steps or int(defaults.get("max_steps", max(budgets)))
    intensity = float(defaults.get("intensity_percentile", 99.0))
    label = raw.get("slug", "tuatara")

    base_url = os.environ.get("SLICER_WEBSERVER_URL", "").strip()
    if not base_url:
        print("ERROR: set SLICER_WEBSERVER_URL", file=sys.stderr)
        return 2

    out_dir = args.out_dir or (
        REPO_ROOT / "runs" / f"tuatara_complete_{time.strftime('%Y%m%dT%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    bright_dir = out_dir / "bright_seed"

    print(f"Slicer     : {base_url}")
    print(f"CT URL     : {ct_url}")
    print(f"Volume     : {volume_name}")
    print(f"max_steps  : {max_steps}")
    print(f"budgets    : {budgets}")
    print(f"out_dir    : {out_dir}")
    print(f"GT path    : {args.gt_path}  (exists={args.gt_path.exists()})")

    if not args.skip_load:
        from jetstream_unshelve_start import load_sample_in_slicer

        loaded = load_sample_in_slicer(base_url, ct_url, volume_name)
        if loaded.get("status") != "ok":
            print(f"ERROR: load failed: {loaded!r}", file=sys.stderr)
            return 4
        volume_name = loaded.get("volume_name") or volume_name
        print(f"Active volume name: {volume_name}")

    os.environ["SLICER_WEBSERVER_URL"] = base_url
    from eval_project358382_pilot import (
        run_bright_seed,
        _per_segment_paths_in_click_order,
        post_hoc_metrics_for_specimen,
    )

    rc = run_bright_seed(
        volume_name=volume_name,
        out_dir=bright_dir,
        label=label,
        max_steps=max_steps,
        intensity_percentile=intensity,
        no_screenshots=args.no_screenshots,
        reset_first=not args.no_reset,
    )
    if rc != 0:
        print(f"bright_seed failed with exit {rc}", file=sys.stderr)
        return rc

    summary_path = bright_dir / "summary.json"
    actual_clicks = 0
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        actual_clicks = int(summary.get("steps", 0))
        (out_dir / "bright_seed_summary.json").write_text(
            json.dumps(summary, indent=2)
        )

    per_seg = _per_segment_paths_in_click_order(bright_dir)
    print(f"Per-segment artifacts: {len(per_seg)}")
    if not per_seg:
        print("ERROR: no per-segment NIfTIs", file=sys.stderr)
        return 5

    if args.skip_metrics:
        print("Skipping Dice metrics (--skip-metrics).")
        return 0

    if not args.gt_path.exists():
        print(
            "WARNING: GT labelmap missing; bright-seed done but no Dice scores.\n"
            "  Run: make stage-sample-gt\n"
            "  Then re-run with --skip-load --out-dir <same> or score manually.",
            file=sys.stderr,
        )
        return 0

    rows = post_hoc_metrics_for_specimen(
        specimen_dir=out_dir,
        gt_path=args.gt_path,
        per_segment_paths_sorted=per_seg,
        budgets=budgets,
        actual_clicks=actual_clicks,
        logger=None,
    )
    results_csv = out_dir / "results.csv"
    with results_csv.open("w") as fh:
        fh.write("budget,K_used,dice,iou,actual_clicks\n")
        for row in rows:
            m = row.get("metrics") or {}
            fh.write(
                f"{row.get('budget')},{row.get('K_used')},"
                f"{m.get('dice', '')},{m.get('iou', '')},{actual_clicks}\n"
            )
    (out_dir / "metrics_rows.json").write_text(
        json.dumps(rows, indent=2, default=str)
    )
    print(f"Wrote {results_csv}")
    for row in rows:
        m = row.get("metrics") or {}
        if "error" in m:
            print(f"  budget={row.get('budget')}  ERROR: {m['error']}")
        else:
            print(
                f"  budget={row.get('budget'):>3d}  "
                f"dice={m.get('dice', 0):.4f}  iou={m.get('iou', 0):.4f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
