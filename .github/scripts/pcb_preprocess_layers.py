#!/usr/bin/env python3
"""
PCB preprocessing pipeline with dewarp/stitch/flatten hooks.

First-pass implementation keeps dewarp/stitch as explicit modular stages with
identity defaults and outputs deterministic manifests/artifacts so stronger
methods can be dropped in later without changing orchestration.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]


def _require_sitk():
    try:
        import SimpleITK as sitk  # noqa: F401
    except Exception as exc:
        raise RuntimeError("pcb_preprocess_layers needs SimpleITK installed") from exc


def _run_prepare_stack(input_dir: Path, out_dir: Path, max_axis: int, spacing_xyz: tuple[float, float, float] | None) -> Path:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "dev" / "prepare_pcb_volume.py"),
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(out_dir),
        "--max-axis",
        str(max_axis),
        "--slug",
        "pcb_ti_preprocessed",
    ]
    if spacing_xyz is not None:
        cmd.extend(["--spacing-xyz", str(spacing_xyz[0]), str(spacing_xyz[1]), str(spacing_xyz[2])])
    subprocess.run(cmd, check=True)
    return out_dir / "pcb_ti_preprocessed.nii.gz"


def _identity_dewarp(volume):
    return volume


def _normalize_dewarp(volume):
    import SimpleITK as sitk

    arr = sitk.GetArrayFromImage(volume).astype(np.float32)
    p1, p99 = np.percentile(arr, [1.0, 99.0])
    if p99 > p1:
        arr = np.clip((arr - p1) / (p99 - p1), 0.0, 1.0)
    else:
        arr.fill(0.0)
    out = sitk.GetImageFromArray(arr)
    out.CopyInformation(volume)
    return out


def _identity_stitch(volume):
    return volume


def _pick_layer_axis(dims_ijk: tuple[int, int, int]) -> int:
    return int(np.argmin(np.asarray(dims_ijk, dtype=np.int64)))


def _flatten_layers(volume, n_layers: int):
    import SimpleITK as sitk
    from PIL import Image

    arr = sitk.GetArrayFromImage(volume)  # z,y,x
    dims_ijk = volume.GetSize()
    axis = _pick_layer_axis(dims_ijk)
    n_axis = int(dims_ijk[axis])
    if n_axis < 1:
        raise RuntimeError("Invalid axis size")
    idxs = np.linspace(0, n_axis - 1, num=max(1, int(n_layers)), dtype=np.int64)

    layer_imgs = []
    for idx in idxs.tolist():
        if axis == 2:
            plane = arr[idx, :, :]
        elif axis == 1:
            plane = arr[:, idx, :]
        else:
            plane = arr[:, :, idx]
        pf = plane.astype(np.float32)
        p1, p99 = np.percentile(pf, [1, 99])
        if p99 > p1:
            pf = np.clip((pf - p1) / (p99 - p1), 0.0, 1.0)
        else:
            pf.fill(0.0)
        layer_imgs.append((idx, (pf * 255).astype(np.uint8)))
    return axis, layer_imgs


def _write_layer_pngs(layer_imgs: list[tuple[int, np.ndarray]], out_dir: Path) -> list[dict[str, Any]]:
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    layers = []
    for idx, arr in layer_imgs:
        p = out_dir / f"layer_{idx:03d}.png"
        Image.fromarray(arr, mode="L").save(p)
        layers.append({"index": int(idx), "png": str(p)})
    return layers


def _parse_spacing(values: list[float] | None) -> tuple[float, float, float] | None:
    if not values:
        return None
    if len(values) != 3:
        raise SystemExit("--spacing-xyz requires exactly 3 values")
    return float(values[0]), float(values[1]), float(values[2])


def main(argv: list[str] | None = None) -> int:
    _require_sitk()
    import SimpleITK as sitk

    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--input-volume", type=Path,
                   help="Existing NIfTI/NRRD volume to preprocess")
    g.add_argument("--input-stack-dir", type=Path,
                   help="Directory with TIFF stack (uses prepare_pcb_volume.py)")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--max-axis", type=int, default=512)
    p.add_argument("--spacing-xyz", nargs=3, type=float, default=None)
    p.add_argument("--dewarp-mode", choices=("identity", "normalize"), default="identity")
    p.add_argument("--stitch-mode", choices=("identity",), default="identity")
    p.add_argument("--flatten-layers", type=int, default=4,
                   help="Number of flattened layer views to export")
    args = p.parse_args(argv)

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    spacing = _parse_spacing(args.spacing_xyz)
    if args.input_stack_dir:
        source_volume = _run_prepare_stack(args.input_stack_dir.resolve(), out_dir, args.max_axis, spacing)
    else:
        source_volume = args.input_volume.resolve()
    if not source_volume.is_file():
        raise SystemExit(f"Missing input volume: {source_volume}")

    vol = sitk.ReadImage(str(source_volume))

    if args.dewarp_mode == "identity":
        vol_dewarp = _identity_dewarp(vol)
    else:
        vol_dewarp = _normalize_dewarp(vol)

    if args.stitch_mode == "identity":
        vol_stitched = _identity_stitch(vol_dewarp)
    else:
        raise SystemExit(f"Unsupported stitch mode: {args.stitch_mode}")

    preprocessed_path = out_dir / "pcb_preprocessed.nii.gz"
    sitk.WriteImage(vol_stitched, str(preprocessed_path), useCompression=True)

    layer_axis, layer_imgs = _flatten_layers(vol_stitched, n_layers=args.flatten_layers)
    flatten_dir = out_dir / "flattened_layers"
    layer_entries = _write_layer_pngs(layer_imgs, flatten_dir)

    manifest = {
        "version": 1,
        "source_volume": str(source_volume),
        "preprocessed_volume": str(preprocessed_path),
        "stages": {
            "dewarp_mode": args.dewarp_mode,
            "stitch_mode": args.stitch_mode,
            "flatten_layers": int(args.flatten_layers),
        },
        "geometry": {
            "size_ijk": list(vol_stitched.GetSize()),
            "spacing_xyz_mm": list(vol_stitched.GetSpacing()),
            "origin_xyz_mm": list(vol_stitched.GetOrigin()),
            "direction_lps": list(vol_stitched.GetDirection()),
            "layer_axis_ijk": int(layer_axis),
        },
        "layer_views": layer_entries,
    }
    manifest_path = out_dir / "preprocess_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

