#!/usr/bin/env python3
"""Batch runner for Colors of Skull Anatomy pairs on Jetstream ECU.

Modes:
1) prefetch: download CT + mesh media for all pairs to a shared cache.
2) run-all: run nninteractive_compare sequentially for each pair.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from morphosource_api_download import download_media  # noqa: E402


def _load_pairs(path: Path) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError(f"Expected list in {path}")
    out = []
    for row in rows:
        ct = str(row.get("ct_media_id", "")).strip()
        mesh = str(row.get("mesh_media_id", "")).strip()
        if not ct or not mesh:
            continue
        out.append(
            {
                "physical_object_id": str(row.get("physical_object_id", "")).strip(),
                "physical_object_title": str(row.get("physical_object_title", "")).strip(),
                "taxonomy": str(row.get("taxonomy", "")).strip(),
                "ct_media_id": ct,
                "mesh_media_id": mesh,
                "ct_title": str(row.get("ct_title", "")).strip(),
                "mesh_title": str(row.get("mesh_title", "")).strip(),
            }
        )
    return out


def _slug_pair(pair: dict[str, Any]) -> str:
    specimen = pair.get("physical_object_id") or "unknown"
    tax = (pair.get("taxonomy") or "taxon").replace(" ", "_")
    return f"{specimen}__{tax}__{pair['ct_media_id']}__{pair['mesh_media_id']}"


def run_prefetch(pairs: list[dict[str, Any]], cache_root: Path) -> dict[str, Any]:
    cache_root.mkdir(parents=True, exist_ok=True)
    results = []
    for idx, pair in enumerate(pairs, start=1):
        print(f"[prefetch {idx}/{len(pairs)}] specimen={pair['physical_object_id']} "
              f"ct={pair['ct_media_id']} mesh={pair['mesh_media_id']}")
        ct_dir = cache_root / pair["ct_media_id"]
        gt_dir = cache_root / pair["mesh_media_id"]
        ct = download_media(pair["ct_media_id"], str(ct_dir))
        gt = download_media(pair["mesh_media_id"], str(gt_dir))
        rec = {"pair": pair, "ct": ct, "gt": gt}
        results.append(rec)
    ok = sum(1 for r in results if r["ct"].get("success") and r["gt"].get("success"))
    return {"mode": "prefetch", "total_pairs": len(pairs), "ok_pairs": ok, "results": results}


def _run_compare(pair: dict[str, Any], out_dir: Path, max_steps: int, goal: str) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "nninteractive_compare.py"),
        "--ct-media-id",
        pair["ct_media_id"],
        "--gt-media-id",
        pair["mesh_media_id"],
        "--goal",
        goal,
        "--max-steps",
        str(max_steps),
        "--output-dir",
        str(out_dir),
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = round(time.time() - t0, 2)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "seconds": elapsed,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "command": cmd,
    }


def run_all_pairs(
    pairs: list[dict[str, Any]],
    out_root: Path,
    *,
    max_steps: int,
    goal: str,
    continue_on_error: bool,
) -> dict[str, Any]:
    runs_dir = out_root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for idx, pair in enumerate(pairs, start=1):
        slug = _slug_pair(pair)
        pair_out = runs_dir / slug
        print(f"[run {idx}/{len(pairs)}] {slug}")
        run = _run_compare(pair, pair_out, max_steps=max_steps, goal=goal)
        row = {"pair": pair, "out_dir": str(pair_out), "run": run}
        rows.append(row)
        print(f"  -> rc={run['returncode']} sec={run['seconds']}")
        if not run["ok"]:
            err = (run.get("stderr_tail") or "").strip()
            out = (run.get("stdout_tail") or "").strip()
            if err:
                print("  stderr tail:")
                print(err[-1200:])
            elif out:
                print("  stdout tail:")
                print(out[-1200:])
        if not run["ok"] and not continue_on_error:
            break
    ok = sum(1 for r in rows if r["run"]["ok"])
    return {
        "mode": "run-all",
        "total_pairs": len(pairs),
        "attempted": len(rows),
        "ok_pairs": ok,
        "results": rows,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "Tests" / "fixtures" / "project358382_pilot3.json",
    )
    p.add_argument(
        "--mode",
        choices=("prefetch", "run-all", "prefetch-and-run"),
        default="prefetch-and-run",
    )
    p.add_argument("--output-root", type=Path, default=REPO_ROOT / "runs" / f"colors_skull_batch_{time.strftime('%Y%m%dT%H%M%S')}")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--goal", default="Segment the cranial bone (skull).")
    p.add_argument("--continue-on-error", action="store_true")
    p.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="1-based index into manifest list; use 2 to skip first pair.",
    )
    args = p.parse_args(argv)

    manifest = args.manifest if args.manifest.is_absolute() else REPO_ROOT / args.manifest
    pairs = _load_pairs(manifest)
    if not pairs:
        print(f"ERROR: no valid pairs found in {manifest}", file=sys.stderr)
        return 2
    if args.start_index < 1 or args.start_index > len(pairs):
        print(
            f"ERROR: --start-index must be in [1, {len(pairs)}], got {args.start_index}",
            file=sys.stderr,
        )
        return 2
    pairs = pairs[args.start_index - 1:]

    out_root = args.output_root.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    cache_root = out_root / "download_cache"
    summary: dict[str, Any] = {
        "manifest": str(manifest),
        "output_root": str(out_root),
        "mode": args.mode,
        "start_index": args.start_index,
        "max_steps": args.max_steps,
        "goal": args.goal,
    }

    if args.mode in ("prefetch", "prefetch-and-run"):
        pre = run_prefetch(pairs, cache_root)
        summary["prefetch"] = pre
        (out_root / "prefetch_summary.json").write_text(json.dumps(pre, indent=2), encoding="utf-8")
        print(f"[prefetch] ok_pairs={pre['ok_pairs']}/{pre['total_pairs']}")
        if args.mode == "prefetch":
            (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return 0

    run = run_all_pairs(
        pairs,
        out_root,
        max_steps=args.max_steps,
        goal=args.goal,
        continue_on_error=args.continue_on_error,
    )
    summary["run_all"] = run
    (out_root / "run_all_summary.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[run-all] ok_pairs={run['ok_pairs']}/{run['attempted']} attempted")
    return 0 if run["ok_pairs"] == run["attempted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

