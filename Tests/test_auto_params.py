"""Unit tests for auto_params.derive_from_array.

These tests pin down the parameter recommendations on the three
representative histogram shapes we've observed in the wild:

* ``uint8`` soft-tissue micro-CT (IMPC mouse embryo): wide bright tail
  spanning ~half the dynamic range, threshold and peak well-separated.
* Hounsfield-unit clinical-style CT (Felis catus): wide tail with the
  bright peak (dense bone / teeth) clearly above the threshold.
* ``uint16`` micro-CT skull (tuatara): narrow bright tail clustered
  tightly near the threshold with isolated outlier max voxels.

The autopilot reduces to one number for the saturation stop --
``intensity_drop_floor_frac`` -- so we lock its behavior on these
three shapes plus a degenerate edge case.
"""

import sys
from pathlib import Path

import numpy as np
import pytest


SCRIPT_DIR = Path(__file__).resolve().parent.parent / ".github" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import auto_params as ap  # noqa: E402


def _wide_tail_uint8(n: int = 200_000) -> np.ndarray:
    """Synthetic IMPC-mouse-like histogram: most voxels in 0-100 (soft
    tissue), thin bright tail 100-255 (organs / dense regions).
    Reproduces the mouse's tail_ratio ~ 0.4-0.5 range."""
    rng = np.random.default_rng(0)
    bulk = rng.integers(0, 90, size=int(n * 0.93), dtype=np.uint8)
    tail = rng.integers(100, 256, size=int(n * 0.07), dtype=np.uint8)
    return np.concatenate([bulk, tail])


def _felis_like_int16(n: int = 200_000) -> np.ndarray:
    """Hounsfield-style CT: air at -1000, soft tissue around 0, bone
    in the 800-2500 range with the densest bone (teeth) at the top.
    p99 lands near 1000, p99.9 near 1700, max near 2500."""
    rng = np.random.default_rng(1)
    air = rng.normal(-1000, 50, size=int(n * 0.30)).astype(np.int16)
    soft = rng.normal(0, 60, size=int(n * 0.65)).astype(np.int16)
    bone = rng.uniform(800, 2500, size=int(n * 0.05)).astype(np.int16)
    return np.concatenate([air, soft, bone])


def _tuatara_like_uint16(n: int = 200_000) -> np.ndarray:
    """Narrow bright-tail histogram: bulk soft-tissue intensities
    clustered low, a tight bone band near 42000, and a TRULY sparse
    handful (<0.01%) of voxels reaching the uint16 max. Mirrors the
    real tuatara: p99=41.6k, p99.9=44.8k, max=65.5k -- the gap from
    p99.9 to max is dominated by scan artifact / detector outliers,
    not a smooth bright continuum, so the tail_ratio collapses to
    ~0.13 and we want a permissive floor."""
    rng = np.random.default_rng(2)
    n_outliers = max(5, int(n * 0.00005))  # ~10 voxels out of 200k
    n_bone = int(n * 0.05)
    n_bulk = n - n_bone - n_outliers
    bulk = rng.integers(8000, 13000, size=n_bulk, dtype=np.uint16)
    bone = rng.integers(41000, 46000, size=n_bone, dtype=np.uint16)
    outliers = rng.integers(60000, 65535, size=n_outliers,
                            dtype=np.uint16)
    return np.concatenate([bulk, bone, outliers])


def test_wide_tail_picks_strict_floor():
    arr = _wide_tail_uint8()
    p = ap.derive_from_array(arr)
    assert p.percentile == pytest.approx(99.0)
    # Wide tail -> floor close to 0.5 (strict; matches IMPC mouse run).
    assert 0.35 <= p.intensity_drop_floor_frac <= 0.50
    # Sanity: tail ratio should land in the wide regime.
    assert p.meta["tail_ratio"] is not None and p.meta["tail_ratio"] > 0.35


def test_narrow_tail_picks_permissive_floor():
    arr = _tuatara_like_uint16()
    p = ap.derive_from_array(arr)
    # Narrow tail with outlier max -> floor should be permissive
    # (close to the 0.10 lower bound, definitely well below 0.5).
    assert p.intensity_drop_floor_frac <= 0.30
    assert p.intensity_drop_floor_frac >= ap._MIN_FLOOR_FRAC
    # Outlier max produces a small tail_ratio.
    assert p.meta["tail_ratio"] is not None and p.meta["tail_ratio"] < 0.25


def test_felis_like_lands_in_middle():
    arr = _felis_like_int16()
    p = ap.derive_from_array(arr)
    # Hounsfield bone has a moderate-to-wide tail (close to 0.5).
    assert 0.30 <= p.intensity_drop_floor_frac <= 0.50


def test_floor_clamped_to_min_when_no_signal():
    # All-constant volume: max == threshold == p99. The "degenerate"
    # branch should kick in and return the minimum permissive floor
    # so the caller doesn't divide by zero or exit on click 1.
    arr = np.full((100, 100, 100), 1500, dtype=np.int16)
    p = ap.derive_from_array(arr)
    assert p.intensity_drop_floor_frac == pytest.approx(ap._MIN_FLOOR_FRAC)
    assert p.meta["tail_ratio"] is None


def test_segment_caps_scale_with_volume():
    small = np.zeros((10, 10, 10), dtype=np.uint8)
    big = np.zeros((1000, 1000, 1000), dtype=np.uint8)
    big[..., :10] = 200  # add a bright shell so percentiles are nontrivial

    small_p = ap.derive_from_array(small)
    big_p = ap.derive_from_array(big)

    # min_segment_voxels floors at 200 even for tiny volumes.
    assert small_p.min_segment_voxels == ap._MIN_SEGMENT_VOXELS_FLOOR
    # 1e9 voxels * 1e-5 = 10000 min, scaled up.
    assert big_p.min_segment_voxels > small_p.min_segment_voxels
    # max_segment_voxels = 30% of volume; ratio holds proportionally.
    assert big_p.max_segment_voxels == int(big.size * 0.30)


def test_max_candidates_respects_floor_and_ceil():
    small = np.zeros((10, 10, 10), dtype=np.uint8)
    huge = np.zeros((512, 512, 512), dtype=np.uint8)  # 134M voxels
    huge[..., :50] = 200

    assert ap.derive_from_array(small).max_candidates == \
        ap._MAX_CANDIDATES_FLOOR
    assert ap.derive_from_array(huge).max_candidates == \
        ap._MAX_CANDIDATES_CEIL


def test_constants_are_passed_through():
    arr = _wide_tail_uint8()
    p = ap.derive_from_array(arr)
    assert p.max_steps == ap._DEFAULT_MAX_STEPS
    assert p.min_local_density == ap._DEFAULT_MIN_LOCAL_DENSITY
    assert p.neighborhood_radius == ap._DEFAULT_NEIGHBORHOOD_RADIUS
    assert p.min_clicks_before_drop_stop == \
        ap._DEFAULT_MIN_CLICKS_BEFORE_DROP_STOP


def test_to_dict_is_json_serializable():
    import json as _json
    p = ap.derive_from_array(_wide_tail_uint8())
    blob = _json.dumps(p.to_dict())
    parsed = _json.loads(blob)
    assert "intensity_drop_floor_frac" in parsed
    assert "meta" in parsed
    assert parsed["meta"]["voxel_count"] == _wide_tail_uint8().size


def test_sampling_does_not_change_recommendation_significantly():
    # Two seeds on the same (big) array should land within +/- 0.05.
    arr = _felis_like_int16(n=2_000_000)
    p1 = ap.derive_from_array(arr, rng_seed=1,
                              sample_cap=1_000_000)
    p2 = ap.derive_from_array(arr, rng_seed=99,
                              sample_cap=1_000_000)
    assert abs(p1.intensity_drop_floor_frac - p2.intensity_drop_floor_frac) < 0.05
