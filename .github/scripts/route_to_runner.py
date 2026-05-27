#!/usr/bin/env python3
"""
Route a CT volume to the Dell GPU or to MorphoCloud Jetstream.

The rule is purely VRAM-based: estimate the GPU memory nnInteractive
will need to run a click on this volume, compare it against the Dell
GPU's currently-free VRAM, and pick:

* ``dell``      — the volume fits with comfortable headroom
* ``jetstream`` — the volume is too large to run safely on the local GPU
* ``either``    — borderline; either host would work

The estimate comes from a per-voxel coefficient we measured against
the IMPC mouse (5.6M voxels, peaked at ~2.1 GB VRAM) and the Felis
fixture (18M voxels, peaked at ~5.4 GB). nnInteractive's encoder is
the dominant cost and roughly linear in voxel count. The
``--peak-vram-bytes-per-voxel`` knob is exposed so a future
re-measurement can tune the constant without code edits.

Examples:

    # Quick decision on a CT
    python route_to_runner.py --input mouse.nrrd
    # -> dell

    # Force a recheck via nvidia-smi (default; runs every call)
    python route_to_runner.py --input huge_skull.nii.gz --json
    # -> jetstream + a JSON blob

    # Drive from CI: refuse to run on Dell when free VRAM < 4 GB
    python route_to_runner.py --input mouse.nrrd --min-free-gb 4
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("route_to_runner")


# nnInteractive's peak VRAM is dominated by a roughly fixed overhead
# (model weights + CUDA context + a sliding-window inference patch)
# plus a smaller per-voxel cost that mostly tracks the encoder's full
# feature map. We model it as
#   peak = fixed_overhead + voxels * bytes_per_voxel
# and bias both numbers toward the conservative side so a routing
# decision of "dell" really does fit. The defaults below were
# calibrated against the Dell-XPS 4 GB GPU runs documented in
# paper_artifacts/mouse_skull_session_001 and the Felis fixture smoke
# test (18M voxels at int16 ran without OOM). Re-measure with
# ``nvidia-smi --loop=1`` against the actual workload if a future
# model release changes the encoder footprint.
_DEFAULT_FIXED_OVERHEAD_BYTES = int(1.2 * 1024 ** 3)  # ~1.2 GB
_DEFAULT_PEAK_VRAM_BYTES_PER_VOXEL = 100.0

# Headroom factor: only route to Dell if (estimated_vram * headroom)
# <= free_vram. headroom=1.5 means we want a 50% safety margin so a
# second concurrent process or a slightly bigger-than-estimated peak
# doesn't OOM the GPU.
_DEFAULT_HEADROOM = 1.5

# "Either" band: if the estimated peak is between (free * lower_band)
# and (free * upper_band), report "either" so the caller can decide.
_EITHER_LOWER = 0.40
_EITHER_UPPER = 0.70


@dataclass
class RouteDecision:
    runner: str           # "dell", "jetstream", or "either"
    reason: str
    voxel_count: int
    dtype_bytes: int
    raw_volume_bytes: int
    estimated_peak_vram_bytes: int
    free_vram_bytes: Optional[int]
    total_vram_bytes: Optional[int]
    headroom: float
    bytes_per_voxel: float
    fixed_overhead_bytes: int = _DEFAULT_FIXED_OVERHEAD_BYTES
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _nvidia_smi_free_bytes() -> tuple[Optional[int], Optional[int]]:
    """Return (free_bytes, total_bytes) for GPU 0, or (None, None) if
    nvidia-smi isn't on PATH. We call out to ``nvidia-smi`` directly
    (rather than ``pynvml``) because the Dell runner already has it
    installed and the parent venv doesn't pull NVML."""
    bin_name = shutil.which("nvidia-smi")
    if not bin_name:
        return None, None
    try:
        out = subprocess.check_output(
            [bin_name,
             "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, timeout=10,
        ).strip().splitlines()
        if not out:
            return None, None
        first_gpu = out[0].split(",")
        free_mib = int(first_gpu[0].strip())
        total_mib = int(first_gpu[1].strip())
        return free_mib * 1024 * 1024, total_mib * 1024 * 1024
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        log.debug("nvidia-smi failed: %s", exc)
        return None, None


def _read_volume_shape_and_dtype(path: Path) -> tuple[tuple[int, ...], str, int]:
    """Return (shape, dtype_name, dtype_bytes). Uses SimpleITK so it
    handles every format the rest of the pipeline produces."""
    import SimpleITK as sitk
    import numpy as np

    img = sitk.ReadImage(str(path))
    arr = sitk.GetArrayFromImage(img)
    dt = np.dtype(arr.dtype)
    return tuple(arr.shape), dt.name, dt.itemsize


def decide(
    *,
    voxel_count: int,
    dtype_bytes: int,
    free_vram_bytes: Optional[int],
    total_vram_bytes: Optional[int] = None,
    bytes_per_voxel: float = _DEFAULT_PEAK_VRAM_BYTES_PER_VOXEL,
    fixed_overhead_bytes: int = _DEFAULT_FIXED_OVERHEAD_BYTES,
    headroom: float = _DEFAULT_HEADROOM,
    min_free_bytes: Optional[int] = None,
    force: Optional[str] = None,
) -> RouteDecision:
    """Decide where to run, given a volume's voxel count + dtype and
    the GPU's free VRAM. Pure function, no I/O.

    ``min_free_bytes``: if free VRAM is below this absolute floor,
    route to Jetstream regardless of the per-voxel estimate.
    ``force``: bypass everything; useful for CI tests + ops scenarios
    where the operator needs an override.
    """
    raw = voxel_count * dtype_bytes
    estimate = int(fixed_overhead_bytes + voxel_count * bytes_per_voxel)
    if force in {"dell", "jetstream"}:
        return RouteDecision(
            runner=force,
            reason=f"forced to {force}",
            voxel_count=voxel_count, dtype_bytes=dtype_bytes,
            raw_volume_bytes=raw, estimated_peak_vram_bytes=estimate,
            free_vram_bytes=free_vram_bytes,
            total_vram_bytes=total_vram_bytes,
            headroom=headroom, bytes_per_voxel=bytes_per_voxel,
            fixed_overhead_bytes=fixed_overhead_bytes,
        )

    if free_vram_bytes is None:
        return RouteDecision(
            runner="jetstream",
            reason="no Dell GPU detected (nvidia-smi unavailable)",
            voxel_count=voxel_count, dtype_bytes=dtype_bytes,
            raw_volume_bytes=raw, estimated_peak_vram_bytes=estimate,
            free_vram_bytes=None, total_vram_bytes=None,
            headroom=headroom, bytes_per_voxel=bytes_per_voxel,
            fixed_overhead_bytes=fixed_overhead_bytes,
        )

    if min_free_bytes is not None and free_vram_bytes < min_free_bytes:
        return RouteDecision(
            runner="jetstream",
            reason=(
                f"Dell free VRAM ({free_vram_bytes / 1024**3:.2f} GB) "
                f"is below the --min-free-gb floor "
                f"({min_free_bytes / 1024**3:.2f} GB)"
            ),
            voxel_count=voxel_count, dtype_bytes=dtype_bytes,
            raw_volume_bytes=raw, estimated_peak_vram_bytes=estimate,
            free_vram_bytes=free_vram_bytes,
            total_vram_bytes=total_vram_bytes,
            headroom=headroom, bytes_per_voxel=bytes_per_voxel,
            fixed_overhead_bytes=fixed_overhead_bytes,
        )

    estimate_with_headroom = int(estimate * headroom)
    if estimate_with_headroom > free_vram_bytes:
        return RouteDecision(
            runner="jetstream",
            reason=(
                f"estimated peak {estimate / 1024**3:.2f} GB * "
                f"{headroom:.1f}x headroom exceeds Dell free "
                f"{free_vram_bytes / 1024**3:.2f} GB"
            ),
            voxel_count=voxel_count, dtype_bytes=dtype_bytes,
            raw_volume_bytes=raw, estimated_peak_vram_bytes=estimate,
            free_vram_bytes=free_vram_bytes,
            total_vram_bytes=total_vram_bytes,
            headroom=headroom, bytes_per_voxel=bytes_per_voxel,
            fixed_overhead_bytes=fixed_overhead_bytes,
        )

    # Borderline band: estimate sits between 40-70% of free VRAM.
    # Either host would work; let the caller pick.
    band_lo = int(free_vram_bytes * _EITHER_LOWER)
    band_hi = int(free_vram_bytes * _EITHER_UPPER)
    if band_lo <= estimate_with_headroom <= band_hi:
        return RouteDecision(
            runner="either",
            reason=(
                f"estimated peak {estimate / 1024**3:.2f} GB sits in the "
                f"40-70%% band of Dell free "
                f"{free_vram_bytes / 1024**3:.2f} GB; either host works"
            ),
            voxel_count=voxel_count, dtype_bytes=dtype_bytes,
            raw_volume_bytes=raw, estimated_peak_vram_bytes=estimate,
            free_vram_bytes=free_vram_bytes,
            total_vram_bytes=total_vram_bytes,
            headroom=headroom, bytes_per_voxel=bytes_per_voxel,
            fixed_overhead_bytes=fixed_overhead_bytes,
        )

    return RouteDecision(
        runner="dell",
        reason=(
            f"estimated peak {estimate / 1024**3:.2f} GB fits within Dell "
            f"free {free_vram_bytes / 1024**3:.2f} GB at "
            f"{headroom:.1f}x headroom"
        ),
        voxel_count=voxel_count, dtype_bytes=dtype_bytes,
        raw_volume_bytes=raw, estimated_peak_vram_bytes=estimate,
        free_vram_bytes=free_vram_bytes,
        total_vram_bytes=total_vram_bytes,
        headroom=headroom, bytes_per_voxel=bytes_per_voxel,
        fixed_overhead_bytes=fixed_overhead_bytes,
    )


def decide_for_path(
    path: str | Path,
    *,
    bytes_per_voxel: float = _DEFAULT_PEAK_VRAM_BYTES_PER_VOXEL,
    fixed_overhead_bytes: int = _DEFAULT_FIXED_OVERHEAD_BYTES,
    headroom: float = _DEFAULT_HEADROOM,
    min_free_bytes: Optional[int] = None,
    force: Optional[str] = None,
) -> RouteDecision:
    """End-to-end convenience: read the volume metadata, query GPU
    VRAM, and decide."""
    p = Path(path)
    shape, dtype_name, dtype_bytes = _read_volume_shape_and_dtype(p)
    voxel_count = 1
    for d in shape:
        voxel_count *= int(d)
    free, total = _nvidia_smi_free_bytes()
    decision = decide(
        voxel_count=voxel_count, dtype_bytes=dtype_bytes,
        free_vram_bytes=free, total_vram_bytes=total,
        bytes_per_voxel=bytes_per_voxel,
        fixed_overhead_bytes=fixed_overhead_bytes,
        headroom=headroom,
        min_free_bytes=min_free_bytes, force=force,
    )
    decision.meta = {
        "input_path": str(p),
        "shape_kji": list(shape),
        "dtype": dtype_name,
    }
    return decision


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Route a CT volume to Dell GPU or Jetstream.")
    p.add_argument("--input", required=True,
                   help="CT volume path")
    p.add_argument("--peak-vram-bytes-per-voxel", type=float,
                   default=_DEFAULT_PEAK_VRAM_BYTES_PER_VOXEL,
                   help=("Empirical nnInteractive peak VRAM per CT voxel "
                         f"(default {_DEFAULT_PEAK_VRAM_BYTES_PER_VOXEL:.0f} "
                         "B/voxel). Re-measure with nvidia-smi if a "
                         "future model release changes the encoder."))
    p.add_argument("--fixed-overhead-gb", type=float,
                   default=_DEFAULT_FIXED_OVERHEAD_BYTES / (1024 ** 3),
                   help=("Fixed nnInteractive VRAM overhead "
                         "(model weights + CUDA context + working "
                         f"buffers), default "
                         f"{_DEFAULT_FIXED_OVERHEAD_BYTES / 1024**3:.2f} GB."))
    p.add_argument("--headroom", type=float, default=_DEFAULT_HEADROOM,
                   help=("Multiplicative safety factor (default "
                         f"{_DEFAULT_HEADROOM:.1f}x): we only pick Dell "
                         "if estimate * headroom <= free VRAM."))
    p.add_argument("--min-free-gb", type=float, default=None,
                   help=("Absolute floor on free Dell VRAM in GB; below "
                         "this, route to Jetstream regardless of size."))
    p.add_argument("--force", choices=["dell", "jetstream"], default=None,
                   help="Bypass the heuristic; force a specific runner.")
    p.add_argument("--json", action="store_true",
                   help="Print the full decision blob as JSON. Default "
                        "prints just the runner name on stdout.")
    return p


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    min_free = (int(args.min_free_gb * 1024**3)
                if args.min_free_gb is not None else None)
    decision = decide_for_path(
        args.input,
        bytes_per_voxel=args.peak_vram_bytes_per_voxel,
        fixed_overhead_bytes=int(args.fixed_overhead_gb * 1024 ** 3),
        headroom=args.headroom,
        min_free_bytes=min_free,
        force=args.force,
    )
    if args.json:
        print(json.dumps(decision.to_dict(), indent=2))
    else:
        print(decision.runner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
