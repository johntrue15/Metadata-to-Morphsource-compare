"""Unit tests for mesh / CT coordinate alignment helpers."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

align = importlib.import_module("mesh_ct_alignment")


def test_bbox_overlap_full_containment() -> None:
    inner = (1.0, 2.0, 1.0, 2.0, 1.0, 2.0)
    outer = (0.0, 10.0, 0.0, 10.0, 0.0, 10.0)
    assert align.bbox_overlap_vol(inner, outer) == align.bbox_volume(inner)


def test_find_best_mesh_orientation_picks_identity_when_aligned() -> None:
    mesh_bounds = (10.0, 20.0, 10.0, 20.0, 10.0, 20.0)
    size_xyz = (100, 100, 100)
    spacing_xyz = (1.0, 1.0, 1.0)
    result = align.find_best_mesh_orientation(
        mesh_bounds, size_xyz, spacing_xyz,
        volume_origin=(0.0, 0.0, 0.0),
        origin_convention="volume",
        mesh_axis_perm="auto",
    )
    assert result["overlap_ratio"] >= 0.99
    assert result["mesh_M_label"] == "+x+y+z"


def test_signed_permutations_count() -> None:
    import numpy as np

    perms = list(align.signed_permutations(np))
    assert len(perms) == 48


def test_transform_bbox_swaps_axes() -> None:
    import numpy as np

    bounds = (0.0, 1.0, 0.0, 2.0, 0.0, 3.0)
    M = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=np.int8)
    out = align.transform_bbox(np, M, bounds)
    assert abs((out[1] - out[0]) - 3.0) < 1e-6
    assert abs((out[3] - out[2]) - 2.0) < 1e-6
    assert abs((out[5] - out[4]) - 1.0) < 1e-6


if __name__ == "__main__":
    test_bbox_overlap_full_containment()
    test_find_best_mesh_orientation_picks_identity_when_aligned()
    test_signed_permutations_count()
    test_transform_bbox_swaps_axes()
    print("OK: mesh_ct_alignment tests passed")
