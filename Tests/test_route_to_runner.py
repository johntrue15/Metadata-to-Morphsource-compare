"""Unit tests for route_to_runner.decide.

The decide() function is pure (no I/O), so the tests just drive it
with concrete (voxel_count, dtype_bytes, free_vram_bytes) triples and
pin down the branch choice. Three representative cases:

* IMPC mouse (5.6M uint8 voxels) on an 8 GB GPU: comfortably fits.
* Felis fixture (18M int16 voxels) on an 8 GB GPU: still fits.
* Full-resolution tuatara (5 GB raw): the routing must say jetstream.

Plus the obvious branches: no GPU, forced override, min-free floor,
and the "either" borderline band.
"""

import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent.parent / ".github" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import route_to_runner as r2r  # noqa: E402


GB = 1024 ** 3


def test_mouse_on_4gb_dell_is_borderline():
    # IMPC mouse: 259*258*421 ~ 5.6 M voxels, uint8. On the Dell XPS
    # 4 GB GPU (3.7 GB free in practice) the mouse lands at the top
    # of the "either" band (estimate ~1.76 GB, *1.5 = 2.64 GB ~ 71%
    # of free). The operator can either let it run locally or push
    # it to Jetstream. This is the realistic Dell-XPS scenario.
    d = r2r.decide(
        voxel_count=5_600_000,
        dtype_bytes=1,
        free_vram_bytes=int(3.7 * GB),
    )
    assert d.runner == "either"


def test_mouse_fits_on_24gb_dell():
    d = r2r.decide(
        voxel_count=5_600_000,
        dtype_bytes=1,
        free_vram_bytes=int(22.0 * GB),
    )
    assert d.runner == "dell"


def test_felis_borderline_on_4gb_dell():
    # Felis fixture: ~18M int16 voxels. Estimate = 1.2 GB + 18M*100 =
    # 3.0 GB, with 1.5x headroom = 4.5 GB > 3.7 GB free -> jetstream.
    # This is the borderline case where the Dell XPS GPU is too small
    # but a fatter local GPU (RTX 3090, 24 GB) would take it.
    d = r2r.decide(
        voxel_count=18_000_000,
        dtype_bytes=2,
        free_vram_bytes=int(3.7 * GB),
    )
    assert d.runner == "jetstream"
    assert "exceeds" in d.reason


def test_felis_fits_on_24gb_dell():
    d = r2r.decide(
        voxel_count=18_000_000,
        dtype_bytes=2,
        free_vram_bytes=int(22.0 * GB),
    )
    assert d.runner == "dell"


def test_full_tuatara_blows_any_gpu():
    # ~5 GB raw tuatara, ~2.5 G uint16 voxels. Even with the fixed
    # overhead, 2.5G*100 = 250 GB raw estimate -> jetstream
    # regardless of the local GPU size.
    d = r2r.decide(
        voxel_count=2_500_000_000,
        dtype_bytes=2,
        free_vram_bytes=int(22.0 * GB),
    )
    assert d.runner == "jetstream"


def test_no_gpu_routes_to_jetstream():
    d = r2r.decide(
        voxel_count=5_600_000, dtype_bytes=1,
        free_vram_bytes=None,
    )
    assert d.runner == "jetstream"
    assert "no Dell GPU" in d.reason


def test_min_free_floor_overrides_estimate():
    d = r2r.decide(
        voxel_count=5_600_000, dtype_bytes=1,
        free_vram_bytes=int(2.0 * GB),
        min_free_bytes=int(4.0 * GB),
    )
    assert d.runner == "jetstream"
    assert "min-free-gb" in d.reason


def test_force_dell_bypasses_estimate():
    d = r2r.decide(
        voxel_count=2_500_000_000, dtype_bytes=2,
        free_vram_bytes=int(7 * GB),
        force="dell",
    )
    assert d.runner == "dell"
    assert "forced" in d.reason


def test_force_jetstream_bypasses_estimate():
    d = r2r.decide(
        voxel_count=10_000, dtype_bytes=1,
        free_vram_bytes=int(22 * GB),
        force="jetstream",
    )
    assert d.runner == "jetstream"
    assert "forced" in d.reason


def test_either_band_when_borderline():
    # Construct a case where (overhead + voxels*bpv) * headroom is
    # ~55% of free. With defaults (overhead=1.2 GB, bpv=100,
    # headroom=1.5):
    #   (1.2GB + voxels*100) * 1.5 = 0.55 * free
    # -> voxels = (0.55*free/1.5 - 1.2GB) / 100
    free = int(10 * GB)
    target = 0.55 * free / 1.5
    voxels = int((target - 1.2 * GB) / 100)
    d = r2r.decide(
        voxel_count=voxels, dtype_bytes=2,
        free_vram_bytes=free,
    )
    assert d.runner == "either"


def test_to_dict_is_json_serializable():
    import json as _json
    d = r2r.decide(
        voxel_count=5_600_000, dtype_bytes=1,
        free_vram_bytes=int(7 * GB),
    )
    blob = _json.dumps(d.to_dict())
    parsed = _json.loads(blob)
    assert parsed["runner"] in {"dell", "jetstream", "either"}
    assert parsed["voxel_count"] == 5_600_000


def test_custom_bytes_per_voxel_changes_decision():
    # With a much higher coefficient, the mouse should NOT fit.
    d = r2r.decide(
        voxel_count=5_600_000, dtype_bytes=1,
        free_vram_bytes=int(7 * GB),
        bytes_per_voxel=2000.0,  # exaggerated
    )
    assert d.runner == "jetstream"


def test_higher_fixed_overhead_pushes_to_jetstream():
    # If we overestimate the model's fixed cost (e.g. an experimental
    # model release with much heavier weights), even the mouse should
    # route to jetstream on the small Dell GPU.
    d = r2r.decide(
        voxel_count=5_600_000, dtype_bytes=1,
        free_vram_bytes=int(3.7 * GB),
        fixed_overhead_bytes=int(3.5 * GB),
    )
    assert d.runner == "jetstream"


def test_headroom_floor_at_1_keeps_decision():
    # With headroom=1.0, mouse should fit even more easily.
    d = r2r.decide(
        voxel_count=5_600_000, dtype_bytes=1,
        free_vram_bytes=int(7 * GB),
        headroom=1.0,
    )
    assert d.runner == "dell"
