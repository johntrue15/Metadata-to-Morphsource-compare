#!/usr/bin/env python3
"""
Auto-derive bright-seed parameters from a CT volume's intensity histogram.

The goal is "one knob": you point this at a CT and it returns a complete
parameter set that the bright-seed runner can use without any
hand-tuning. The same knob should work across uint8 soft-tissue micro-CTs
(IMPC mouse embryo), Hounsfield-unit clinical-style scans
(Felis catus 3DAS:00001), and uint16 micro-CT skulls (tuatara) without
any per-specimen overrides.

The interesting design choice is ``intensity_drop_floor_frac``, the
auto-saturate stopping rule. For wide bright tails (Hounsfield bone
spans ~1500 HU between threshold and peak) ``0.5`` is right --
the IMPC mouse takes 8-10 clicks before its clicked intensity drops
below ``threshold + 0.5*(peak-threshold)``. For narrow micro-CT bone
tails (uint16 tuatara: most bone in a tight ~3000-count band, with
isolated max-intensity outliers) ``0.5`` would stop after click 1.

We pick the floor automatically by measuring the bright tail's
``(p99.9 - threshold) / (max - threshold)`` ratio. Wide tail -> ratio
near 1 -> strict floor. Narrow tail with outlier peak -> ratio near 0
-> permissive floor.

This module has no GPU/torch/nninteractive dependency: it only uses
numpy + SimpleITK so the sweep harness + routing logic can call it
without paying the 10s nnInteractive cold-start cost.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("auto_params")


# Caps + floors picked empirically on the IMPC + Felis + tuatara test
# cases. Adjust here, not at call sites, so every consumer (autopilot
# runner, sweep seeder, dashboard) stays in sync.
_DEFAULT_PERCENTILE = 99.0
_DEFAULT_MAX_STEPS = 500
_DEFAULT_MIN_LOCAL_DENSITY = 0.4
_DEFAULT_NEIGHBORHOOD_RADIUS = 2
_DEFAULT_MIN_CLICKS_BEFORE_DROP_STOP = 5

# floor_frac bounds: 0.1 = very permissive (uint16 micro-CT skull), 0.5
# = strict (mouse / Hounsfield bone). Anything outside this range was
# either too eager to exit (>0.5) or never exited (<0.1) on the test
# specimens.
_MIN_FLOOR_FRAC = 0.10
_MAX_FLOOR_FRAC = 0.50

# Per-volume scaling for segment-size caps and candidate caps. min
# segment voxels filters dust; max segment voxels kills runaway clicks;
# max candidates caps the sorted candidate list so the loop stays fast
# on big skull CTs.
_MIN_SEGMENT_VOXEL_FRAC = 1e-5
_MIN_SEGMENT_VOXELS_FLOOR = 200
_MAX_SEGMENT_VOXEL_FRAC = 0.30
_MAX_CANDIDATES_FRAC = 0.01
_MAX_CANDIDATES_FLOOR = 50_000
_MAX_CANDIDATES_CEIL = 500_000


@dataclass
class AutoParams:
    """Output bundle. ``meta`` holds the volume stats we used to derive
    the recommendations so a caller can audit the choice or sweep
    around it."""
    percentile: float
    intensity_drop_floor_frac: float
    min_segment_voxels: int
    max_segment_voxels: int
    max_candidates: int
    max_steps: int
    min_local_density: float
    neighborhood_radius: int
    min_clicks_before_drop_stop: int
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def derive_from_array(arr, *, voxel_count: Optional[int] = None,
                      rng_seed: int = 42,
                      sample_cap: int = 5_000_000) -> AutoParams:
    """Derive parameters from a numpy volume array.

    The histogram math runs on a uniform random sample of up to
    ``sample_cap`` voxels (5M default). On a 22M-voxel tuatara CT this
    cuts the percentile pass from ~600ms to ~120ms with no measurable
    drift in the recommended floor_frac.
    """
    import numpy as np

    if voxel_count is None:
        voxel_count = int(arr.size)

    flat = arr.ravel()
    if flat.size > sample_cap:
        rng = np.random.default_rng(rng_seed)
        idx = rng.choice(flat.size, sample_cap, replace=False)
        flat = flat[idx]

    p50, p90, p95, p99, p99_5, p99_9 = np.percentile(
        flat, [50.0, 90.0, 95.0, 99.0, 99.5, 99.9]
    )
    vmin = float(flat.min())
    vmax = float(flat.max())

    percentile = _DEFAULT_PERCENTILE
    threshold = float(p99)

    if vmax > threshold:
        # (p99.9 - threshold) / (max - threshold). Wide spread keeps
        # the floor strict; outlier peaks (one or two voxels at
        # uint16 max) collapse it toward the permissive end.
        tail_ratio = (float(p99_9) - threshold) / (vmax - threshold)
        floor_frac = float(np.clip(
            tail_ratio, _MIN_FLOOR_FRAC, _MAX_FLOOR_FRAC
        ))
    else:
        # Degenerate: every voxel >= threshold. Use permissive default
        # so we don't quit on click 1.
        floor_frac = _MIN_FLOOR_FRAC

    min_segment_voxels = max(
        _MIN_SEGMENT_VOXELS_FLOOR,
        int(voxel_count * _MIN_SEGMENT_VOXEL_FRAC),
    )
    max_segment_voxels = int(voxel_count * _MAX_SEGMENT_VOXEL_FRAC)
    max_candidates = max(
        _MAX_CANDIDATES_FLOOR,
        min(_MAX_CANDIDATES_CEIL,
            int(voxel_count * _MAX_CANDIDATES_FRAC)),
    )

    meta = {
        "voxel_count": int(voxel_count),
        "dtype": str(arr.dtype),
        "intensity_min": vmin,
        "intensity_max": vmax,
        "p50": float(p50),
        "p90": float(p90),
        "p95": float(p95),
        "p99": float(p99),
        "p99_5": float(p99_5),
        "p99_9": float(p99_9),
        "threshold": threshold,
        "tail_ratio": (
            (float(p99_9) - threshold) / (vmax - threshold)
            if vmax > threshold else None
        ),
        "sampled_voxels": int(flat.size),
    }
    return AutoParams(
        percentile=percentile,
        intensity_drop_floor_frac=floor_frac,
        min_segment_voxels=min_segment_voxels,
        max_segment_voxels=max_segment_voxels,
        max_candidates=max_candidates,
        max_steps=_DEFAULT_MAX_STEPS,
        min_local_density=_DEFAULT_MIN_LOCAL_DENSITY,
        neighborhood_radius=_DEFAULT_NEIGHBORHOOD_RADIUS,
        min_clicks_before_drop_stop=_DEFAULT_MIN_CLICKS_BEFORE_DROP_STOP,
        meta=meta,
    )


def derive_from_path(path: str | Path, **kwargs) -> AutoParams:
    """Convenience: read a CT volume + derive parameters.

    Uses SimpleITK so it works on the .nrrd / .nii.gz / .nii files the
    rest of the pipeline produces.
    """
    import SimpleITK as sitk

    img = sitk.ReadImage(str(path))
    arr = sitk.GetArrayFromImage(img)
    params = derive_from_array(arr, **kwargs)
    params.meta["input_path"] = str(path)
    params.meta["shape_kji"] = list(arr.shape)
    params.meta["spacing_xyz"] = list(img.GetSpacing())
    return params


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Derive bright-seed parameters from a CT volume's "
            "intensity histogram. Prints JSON on stdout so the "
            "autopilot runner / sweep harness can consume it."
        ),
    )
    p.add_argument("--input", required=True,
                   help="CT volume (.nrrd / .nii.gz / .nii)")
    p.add_argument("--sample-cap", type=int, default=5_000_000,
                   help="Random-sample cap for percentile estimation "
                        "(default 5M voxels)")
    p.add_argument("--rng-seed", type=int, default=42)
    return p


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    params = derive_from_path(
        args.input,
        rng_seed=args.rng_seed,
        sample_cap=args.sample_cap,
    )
    print(json.dumps(params.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
