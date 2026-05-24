"""
Resample a (CT, GT-labelmap) pair so no axis exceeds ``--max-axis`` voxels,
in place. The CT uses linear interpolation; the GT labelmap uses nearest
neighbour so its binary value space stays exact. Both end on the same
grid (origin/direction preserved, spacing scaled).

Used by ``nninteractive_compare.py`` to keep fixture bundles manageable on
whole-skull specimens (a 50 mm skull at 0.05 mm spacing + 5 mm crop margin
otherwise produces a 1200³ grid -> ~250 MB nii.gz per specimen).

Adapted from ``eval_project358382_pilot._resample_cap_axis``.

Runs inside the nnInteractive venv (needs SimpleITK + numpy).

Usage::

    python resample_volume_pair.py \\
        --ct /tmp/.../ct_cropped.nii.gz \\
        --gt /tmp/.../gt_voxelized.nii.gz \\
        --max-axis 384 \\
        --summary /tmp/.../resample.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("resample_pair")


def _import_deps():
    try:
        import numpy as np
        import SimpleITK as sitk
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Run inside the nnInteractive venv.",
              file=sys.stderr)
        sys.exit(1)
    return np, sitk


def resample_pair(ct_path: Path, gt_path: Path, max_axis: int,
                  summary_path: Path | None = None) -> dict:
    if max_axis <= 0:
        log.info("max_axis=%d (disabled); no-op", max_axis)
        return {"resampled": False, "max_axis_cap": max_axis}

    np_, sitk = _import_deps()

    img = sitk.ReadImage(str(ct_path))
    size = img.GetSize()
    if max(size) <= max_axis:
        log.info("CT max axis %d <= cap %d (no resample)", max(size), max_axis)
        return {"resampled": False, "size_before": list(size),
                "max_axis_cap": max_axis}

    scale = max_axis / float(max(size))
    new_size = [max(1, int(round(s * scale))) for s in size]
    spacing = img.GetSpacing()
    new_spacing = [
        spacing[i] * (size[i] / float(new_size[i])) for i in range(3)
    ]
    log.info("CT %s: %s spacing=%s -> %s spacing=%s (cap=%d, scale=%.3f)",
             ct_path.name, list(size), [round(s, 5) for s in spacing],
             new_size, [round(s, 5) for s in new_spacing], max_axis, scale)

    def _resample(src, interp):
        ref = sitk.Image(new_size, src.GetPixelID())
        ref.SetSpacing(new_spacing)
        ref.SetOrigin(src.GetOrigin())
        ref.SetDirection(src.GetDirection())
        return sitk.Resample(src, ref, sitk.Transform(), interp,
                             0, src.GetPixelID())

    ct_new = _resample(img, sitk.sitkLinear)
    sitk.WriteImage(ct_new, str(ct_path))
    log.info("Wrote %s (%d bytes)", ct_path.name, ct_path.stat().st_size)

    gt = sitk.ReadImage(str(gt_path))
    gt_new = _resample(gt, sitk.sitkNearestNeighbor)
    sitk.WriteImage(gt_new, str(gt_path))
    log.info("Wrote %s (%d bytes)", gt_path.name, gt_path.stat().st_size)

    summary = {
        "resampled": True,
        "ct_path": str(ct_path),
        "gt_path": str(gt_path),
        "size_before": list(size),
        "size_after": list(new_size),
        "spacing_before": [float(s) for s in spacing],
        "spacing_after": [float(s) for s in new_spacing],
        "max_axis_cap": max_axis,
        "scale": float(scale),
        "ct_bytes_after": ct_path.stat().st_size,
        "gt_bytes_after": gt_path.stat().st_size,
    }
    if summary_path:
        summary_path.write_text(json.dumps(summary, indent=2))
    return summary


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ct", required=True,
                   help="CT volume to resample in-place")
    p.add_argument("--gt", required=True,
                   help="GT labelmap to resample in-place (must share grid)")
    p.add_argument("--max-axis", type=int, required=True,
                   help="Max voxels per axis after resampling")
    p.add_argument("--summary", default="",
                   help="Optional JSON summary path")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    args = _parse_args()
    try:
        result = resample_pair(
            ct_path=Path(args.ct),
            gt_path=Path(args.gt),
            max_axis=args.max_axis,
            summary_path=Path(args.summary) if args.summary else None,
        )
    except Exception as exc:
        log.error("Resample failed: %s", exc, exc_info=True)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
