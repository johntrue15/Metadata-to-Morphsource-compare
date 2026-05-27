#!/usr/bin/env python3
"""Export the *current* Jetstream Slicer segmentation (no reset, no new clicks).

Use after a manual or bright-seed session when the scene already has N
segments. Downloads the composite + per-segment labelmaps, scores against
a mesh-voxelized GT, and writes pilot-style budget metrics.

Usage::

    set -a && source .env && set +a
    export SLICER_WEBSERVER_URL=https://http-149-165-155-127-2016.proxy-js2-iu.exosphere.app/

    python3 .github/scripts/slicer_remote_score_scene.py \\
        --gt-path data/sample/tuatara_skull_000358663_gt_labelmap.nrrd \\
        --ct-path data/sample/tuatara_skull_000358663_ct.nrrd \\
        --budgets 10,25,50,100 \\
        --out-dir runs/tuatara_100click_vs_gt
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from run_telemetry import EXPORT_SEGMENTATION_SRC  # noqa: E402
from slicer_remote_bright_seed import post_python, _read_url  # noqa: E402


LIST_VOLUMES_SRC = """\
import slicer
names = []
for n in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
    names.append(n.GetName())
__execResult = {"status": "ok", "volumes": names}
"""


def _decode_segments(export: dict, out_dir: Path) -> list[Path]:
    seg_dir = out_dir / "artifacts" / "per_segment"
    seg_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for seg in export.get("per_segment") or []:
        b64 = seg.get("data_b64")
        if not b64:
            continue
        fname = seg.get("filename") or f"{seg.get('sid', 'seg')}.nii.gz"
        p = seg_dir / fname
        p.write_bytes(base64.b64decode(b64))
        paths.append(p)
    return paths


def _segment_sort_key(p: Path) -> tuple:
    m = re.search(r"Segment[_]?(\d+)", p.stem, re.I)
    if m:
        return (0, int(m.group(1)))
    return (1, p.stem)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gt-path", type=Path, required=True)
    p.add_argument("--ct-path", type=Path, default=None)
    p.add_argument("--budgets", default="10,25,50,100")
    p.add_argument("--out-dir", type=Path,
                   default=Path("runs") / f"tuatara_score_{time.strftime('%Y%m%dT%H%M%S')}")
    p.add_argument("--list-volumes-only", action="store_true")
    p.add_argument("--export-only", action="store_true",
                   help="Download masks only; skip GT metrics")
    args = p.parse_args(argv)

    if not args.export_only and not args.gt_path.exists():
        print(f"ERROR: GT missing: {args.gt_path}", file=sys.stderr)
        print("Run: make tuatara-gt-labelmap", file=sys.stderr)
        return 2

    base_url = _read_url()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.list_volumes_only:
        r = post_python(base_url, LIST_VOLUMES_SRC, timeout=30)
        print(json.dumps(r, indent=2))
        return 0

    print("Exporting live segmentation from Jetstream (no reset, no clicks)…")
    export = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=600)
    # Save slim export metadata (payloads are on disk as NIfTI)
    meta = {
        "status": export.get("status"),
        "n_per_segment": len(export.get("per_segment") or []),
        "composite_size": (export.get("composite") or {}).get("size_bytes"),
    }
    (args.out_dir / "export_meta.json").write_text(json.dumps(meta, indent=2))

    if export.get("status") == "no_segmentation":
        print("ERROR: no segmentation in Slicer scene.", file=sys.stderr)
        return 3

    comp = export.get("composite") or {}
    if comp.get("data_b64"):
        comp_path = args.out_dir / "composite_live.nii.gz"
        comp_path.write_bytes(base64.b64decode(comp["data_b64"]))
        print(f"Wrote {comp_path} ({comp_path.stat().st_size:,} bytes)")
    else:
        comp_path = None

    per_seg = _decode_segments(export, args.out_dir)
    per_seg = sorted(per_seg, key=_segment_sort_key)
    print(f"Per-segment exports: {len(per_seg)}")

    if args.export_only:
        print(f"Export-only complete. {len(per_seg)} segments under {args.out_dir}")
        return 0

    sys.path.insert(0, str(_SCRIPT_DIR))
    from eval_project358382_pilot import compose_union, score_against_gt

    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    rows = []
    metrics_dir = args.out_dir / "metrics"
    composites_dir = args.out_dir / "composites"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    composites_dir.mkdir(parents=True, exist_ok=True)

    if per_seg:
        for k in budgets:
            comp_k = composites_dir / f"composite_at_{k:03d}.nii.gz"
            summary = compose_union(per_seg, k, comp_k)
            if "error" in summary:
                rows.append({"budget": k, "error": summary["error"]})
                continue
            m = score_against_gt(comp_k, args.gt_path)
            row = {
                "budget": k,
                "K_used": summary["K_used"],
                "composite_path": str(comp_k),
                "composite_voxels_set": summary.get("voxels_set"),
                "metrics": m,
            }
            rows.append(row)
            (metrics_dir / f"metrics_at_{k:03d}.json").write_text(
                json.dumps(row, indent=2, default=str)
            )
            if "error" not in m:
                print(f"  budget={k:>3d}  K={summary['K_used']:>3d}  "
                      f"dice={m.get('dice', 0):.4f}  iou={m.get('iou', 0):.4f}")
    elif comp_path:
        m = score_against_gt(comp_path, args.gt_path)
        rows.append({"budget": "live_composite", "metrics": m})
        print(f"  live composite  dice={m.get('dice', 0):.4f}  iou={m.get('iou', 0):.4f}")

    results = {
        "gt_path": str(args.gt_path),
        "ct_path": str(args.ct_path) if args.ct_path else "",
        "n_segments_exported": len(per_seg),
        "budgets": budgets,
        "rows": rows,
    }
    (args.out_dir / "results.json").write_text(json.dumps(results, indent=2, default=str))

    csv_path = args.out_dir / "results.csv"
    with csv_path.open("w") as fh:
        fh.write("budget,K_used,dice,iou,precision,recall,voxels_pred\n")
        for row in rows:
            m = row.get("metrics") or {}
            fh.write(
                f"{row.get('budget')},{row.get('K_used','')},"
                f"{m.get('dice','')},{m.get('iou','')},"
                f"{m.get('precision','')},{m.get('recall','')},"
                f"{row.get('composite_voxels_set','')}\n"
            )
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
