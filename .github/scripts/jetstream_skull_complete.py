#!/usr/bin/env python3
"""Run a fixture-hosted skull through bright-seed + optional Dice vs GT.

Default fixture: ``data/sample/colors_of_skull_urls.json`` (Crotalus 000445108).

  1. Load CT from GitHub raw URL into Jetstream Slicer (skip if already present).
  2. Bright-seed with ``--max-steps`` = max(budgets), ``--no-stop-rules``.
  3. Post-hoc union composites at each budget and score vs local GT labelmap.

The Mac mini is the driver (HTTP to Jetstream). GT must exist under
``data/sample/`` (``make colors-skull-gt-labelmap``).

Usage::

    set -a && source .env && set +a
    python3 .github/scripts/jetstream_skull_complete.py \\
        --fixture data/sample/colors_of_skull_urls.json
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

DEFAULT_FIXTURE = REPO_ROOT / "data/sample/colors_of_skull_urls.json"


def _raw_url(repo: str, ref: str, relpath: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{relpath.lstrip('/')}"


def _load_fixture(path: Path) -> dict:
    raw = json.loads(path.read_text())
    repo = os.environ.get("GITHUB_REPO", raw["github_repo"])
    ref = os.environ.get("GITHUB_REF", raw["github_ref"])
    files = raw.get("files") or {}
    gt_rel = files.get("gt_labelmap_nrrd")
    return {
        "ct_url": _raw_url(repo, ref, files["ct_nrrd"]),
        "gt_path": REPO_ROOT / gt_rel if gt_rel else None,
        "volume_name": raw.get("slicer_volume_name") or Path(files["ct_nrrd"]).stem,
        "slug": raw.get("slug", "skull"),
        "pilot_defaults": raw.get("pilot_defaults") or {},
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    p.add_argument("--budgets", type=str, default=None)
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--gt-path", type=Path, default=None)
    p.add_argument("--volume-name", type=str, default=None)
    p.add_argument("--skip-load", action="store_true")
    p.add_argument("--skip-metrics", action="store_true")
    p.add_argument("--no-reset", action="store_true")
    p.add_argument("--no-screenshots", action="store_true")
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args(argv)

    fix_path = args.fixture if args.fixture.is_absolute() else REPO_ROOT / args.fixture
    fix = _load_fixture(fix_path)
    defaults = fix["pilot_defaults"]
    budgets = [
        int(x) for x in (args.budgets or ",".join(str(b) for b in defaults.get("budgets", [10]))).split(",")
        if x.strip()
    ]
    max_steps = args.max_steps or int(defaults.get("max_steps", max(budgets)))
    intensity = float(defaults.get("intensity_percentile", 99.0))
    volume_name = args.volume_name or fix["volume_name"]
    gt_path = args.gt_path or fix["gt_path"]
    label = fix["slug"]

    base_url = os.environ.get("SLICER_WEBSERVER_URL", "").strip()
    if not base_url:
        print("ERROR: set SLICER_WEBSERVER_URL", file=sys.stderr)
        return 2

    out_dir = args.out_dir or (
        REPO_ROOT / "runs" / f"{label}_complete_{time.strftime('%Y%m%dT%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    bright_dir = out_dir / "bright_seed"

    print(f"Fixture   : {fix_path}")
    print(f"Slicer    : {base_url}")
    print(f"CT URL    : {fix['ct_url']}")
    print(f"Volume    : {volume_name}")
    print(f"max_steps : {max_steps}")
    print(f"budgets   : {budgets}")
    print(f"out_dir   : {out_dir}")
    if gt_path:
        print(f"GT path   : {gt_path}  (exists={gt_path.exists()})")

    if not args.skip_load:
        from jetstream_unshelve_start import load_sample_in_slicer

        loaded = load_sample_in_slicer(base_url, fix["ct_url"], volume_name)
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
        stop = summary.get("stop_reason") or {}
        if stop.get("reason") == "resource_exhausted":
            print(f"Stopped early: {stop.get('kind')} at step {stop.get('step')}")

    per_seg = _per_segment_paths_in_click_order(bright_dir)
    print(f"Per-segment artifacts: {len(per_seg)}")
    if not per_seg:
        print("ERROR: no per-segment NIfTIs", file=sys.stderr)
        return 5

    if args.skip_metrics:
        print("Skipping Dice metrics (--skip-metrics).")
        return 0

    if not gt_path or not gt_path.exists():
        print(
            "WARNING: GT labelmap missing; bright-seed done but no Dice scores.\n"
            "  Run: make colors-skull-gt-labelmap\n"
            "  Then re-run with --skip-load.",
            file=sys.stderr,
        )
        return 0

    rows = post_hoc_metrics_for_specimen(
        specimen_dir=out_dir,
        gt_path=gt_path,
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
