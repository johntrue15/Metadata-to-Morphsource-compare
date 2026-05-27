#!/usr/bin/env python3
"""Prepare the PCB ``TI tiff stack`` for local nnInteractive testing.

Stacks ``TI###.tif`` into NIfTI without loading the full 2.6B-voxel volume
into RAM: slices are read one-by-one and decimated so the longest axis is
<= ``--max-axis`` (default 384, safe on a 4 GB GPU).

Spacing defaults to 1 mm isotropic unless ``--spacing-xyz`` is set.

Usage (nnInteractive venv, from repo root)::

    python scripts/dev/prepare_pcb_volume.py
    python scripts/dev/prepare_pcb_volume.py --max-axis 384 --spacing-xyz 0.05 0.05 0.05
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

log = logging.getLogger("prepare_pcb_volume")

DEFAULT_STACK = Path(
    r"/mnt/c/Users/DELL_/OneDrive/Documents/PCB/TI tiff stack"
)
DEFAULT_OUT_DIR = REPO_ROOT / ".local" / "pcb_data"


def _stride_plan(full_size_xyz: tuple[int, int, int], max_axis: int):
    """Return (stride_x, stride_y, stride_z), out_size_xyz."""
    longest = max(full_size_xyz)
    if max_axis <= 0 or longest <= max_axis:
        return (1, 1, 1), full_size_xyz
    scale = max_axis / float(longest)
    out = tuple(max(1, int(round(s * scale))) for s in full_size_xyz)
    strides = tuple(
        max(1, int(math.ceil(full / out)))
        for full, out in zip(full_size_xyz, out)
    )
    return strides, out


def _to_2d(arr, np):
    """Collapse multi-component / RGB slices to a single 2D plane."""
    if arr.ndim == 2:
        return arr
    if arr.ndim == 3:
        if arr.shape[0] <= 4 and arr.shape[1] > 4:
            return np.max(arr, axis=0)
        if arr.shape[-1] <= 4:
            return np.max(arr, axis=-1)
        return arr[arr.shape[0] // 2]
    raise ValueError(f"Unexpected slice shape {arr.shape}")


def _stack_strided(files: list[Path], strides: tuple[int, int, int], sitk, np):
    """Build a decimated 3D volume (z, y, x) numpy array."""
    sx, sy, sz = strides
    z_indices = list(range(0, len(files), sz))
    if not z_indices:
        raise RuntimeError("No slices after Z decimation")

    first = _to_2d(
        sitk.GetArrayFromImage(sitk.ReadImage(str(files[z_indices[0]]))), np
    )
    plane = first[::sy, ::sx]
    vol = np.empty((len(z_indices), plane.shape[0], plane.shape[1]),
                   dtype=first.dtype)
    vol[0] = plane
    for zi, fi in enumerate(z_indices[1:], start=1):
        if zi % 20 == 0:
            log.info("  slice %d / %d", zi, len(z_indices) - 1)
        sl = _to_2d(sitk.GetArrayFromImage(sitk.ReadImage(str(files[fi]))), np)
        vol[zi] = sl[::sy, ::sx]
    return vol, z_indices


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", type=Path, default=DEFAULT_STACK)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument(
        "--spacing-xyz", nargs=3, type=float, default=None,
        metavar=("SX", "SY", "SZ"),
    )
    p.add_argument("--max-axis", type=int, default=384)
    p.add_argument("--slug", default="pcb_ti")
    args = p.parse_args()

    import numpy as np
    import SimpleITK as sitk
    from tiff_stack_to_nifti import _list_tiffs

    args.output_dir.mkdir(parents=True, exist_ok=True)
    final_path = args.output_dir / f"{args.slug}.nii.gz"

    spacing_in = tuple(args.spacing_xyz) if args.spacing_xyz else (1.0, 1.0, 1.0)
    files = _list_tiffs(args.input_dir, recursive=False)
    if not files:
        raise SystemExit(f"No TIFF slices in {args.input_dir}")

    probe = sitk.ReadImage(str(files[0]))
    full_xyz = probe.GetSize()  # (x, y) for one slice; z = n files
    full_size_xyz = (full_xyz[0], full_xyz[1], len(files))
    strides, out_size_xyz = _stride_plan(full_size_xyz, args.max_axis)

    log.info(
        "Full stack ~%s (%.0fM voxels); decimate strides=%s -> ~%s",
        full_size_xyz,
        full_size_xyz[0] * full_size_xyz[1] * full_size_xyz[2] / 1e6,
        strides,
        out_size_xyz,
    )

    vol_zyx, z_indices = _stack_strided(files, strides, sitk, np)
    # SimpleITK image from array is (z,y,x); SetSpacing order is (x,y,z).
    spacing_out = (
        spacing_in[0] * strides[0],
        spacing_in[1] * strides[1],
        spacing_in[2] * strides[2],
    )
    img = sitk.GetImageFromArray(vol_zyx)
    img.SetSpacing(spacing_out)
    size_xyz = img.GetSize()
    origin_final = (
        -0.5 * (size_xyz[0] - 1) * spacing_out[0],
        -0.5 * (size_xyz[1] - 1) * spacing_out[1],
        -0.5 * (size_xyz[2] - 1) * spacing_out[2],
    )
    img.SetOrigin(origin_final)

    sitk.WriteImage(img, str(final_path), useCompression=True)
    voxels = int(vol_zyx.size)
    meta = {
        "n_slices": len(files),
        "z_indices_used": len(z_indices),
        "full_size_xyz": list(full_size_xyz),
        "strides_xyz": list(strides),
        "size_after_decimate_xyz": list(size_xyz),
        "spacing_in": list(spacing_in),
        "spacing_out": list(spacing_out),
        "origin": list(origin_final),
        "final_path": str(final_path),
        "voxel_count": voxels,
    }
    meta_path = args.output_dir / f"{args.slug}.prepare.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d voxels, %.1f MB)", final_path, voxels,
             final_path.stat().st_size / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
