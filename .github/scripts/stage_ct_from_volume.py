#!/usr/bin/env python3
"""Stage a GitHub-loadable CT NRRD by downsampling an existing volume.

A lightweight alternative to ``stage_morphosource_sample.py --phase ct-only``
for the *segmentation* path: it does NOT need the GT mesh (so it sidesteps
mesh<->CT bbox-alignment failures) and reuses a CT volume already converted on
the box (e.g. ``runs/<...>/ct_<media>.nii.gz``). It bin-shrinks the volume so the
largest axis is <= ``--max-axis``, preserving spacing/origin, and writes a gzipped
NRRD under ``data/sample/`` plus a small provenance sidecar.

Use the canonical staging tool when you also need a GT labelmap on the same grid.

Example::

    stage_ct_from_volume.py \
        --in-volume runs/chameleon_.../ct_000408242.nii.gz \
        --slug chameleon_skull_000408235 --max-axis 384
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]


def main(argv=None) -> int:
    import SimpleITK as sitk

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in-volume", required=True,
                   help="source CT volume (.nii.gz/.nrrd) with correct spacing")
    p.add_argument("--slug", required=True, help="output file-name prefix")
    p.add_argument("--out-dir", default="data/sample")
    p.add_argument("--max-axis", type=int, default=384,
                   help="max voxels along any axis after downsample (default 384)")
    p.add_argument("--ct-media-id", default=None)
    p.add_argument("--mesh-media-id", default=None)
    args = p.parse_args(argv)

    in_path = Path(args.in_volume)
    if not in_path.is_absolute():
        in_path = REPO_ROOT / in_path
    if not in_path.exists():
        raise SystemExit(f"ERROR: input volume not found: {in_path}")

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.slug}_ct.nrrd"
    prov_path = out_dir / f"{args.slug}.provenance.json"

    print(f"Reading {in_path} …", flush=True)
    im = sitk.ReadImage(str(in_path))
    size = list(im.GetSize())
    spacing = list(im.GetSpacing())
    factor = max(1, math.ceil(max(size) / args.max_axis))
    print(f"  size={size} spacing={[round(s,4) for s in spacing]} "
          f"-> shrink factor {factor}", flush=True)

    if factor > 1:
        shrink = sitk.BinShrinkImageFilter()
        shrink.SetShrinkFactors([factor, factor, factor])
        im = shrink.Execute(im)

    # Keep CT intensities as 16-bit (source is uint16/int16).
    if im.GetPixelID() not in (sitk.sitkUInt16, sitk.sitkInt16):
        im = sitk.Cast(im, sitk.sitkInt16)

    print(f"  writing {out_path}  (size={list(im.GetSize())} "
          f"spacing={[round(s,4) for s in im.GetSpacing()]}) …", flush=True)
    sitk.WriteImage(im, str(out_path), useCompression=True)

    prov = {
        "slug": args.slug,
        "source_volume": str(in_path.relative_to(REPO_ROOT))
        if str(in_path).startswith(str(REPO_ROOT)) else str(in_path),
        "ct_media_id": args.ct_media_id,
        "mesh_media_id": args.mesh_media_id,
        "method": "stage_ct_from_volume.py BinShrink",
        "shrink_factor": factor,
        "native_size": size,
        "native_spacing_mm": [round(s, 6) for s in spacing],
        "output_size": list(im.GetSize()),
        "output_spacing_mm": [round(s, 6) for s in im.GetSpacing()],
        "output_bytes": out_path.stat().st_size,
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    prov_path.write_text(json.dumps(prov, indent=2) + "\n")
    print(f"  provenance -> {prov_path}", flush=True)
    print(f"DONE: {out_path} ({out_path.stat().st_size/1e6:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
