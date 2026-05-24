"""Tiny deterministic CT + mesh fixtures for the replay tier.

Why this exists
---------------
The local specimen cache is **mesh-only** (no CT volumes / DICOM /
TIFF stacks anywhere under ``~/.autoresearchclaw/specimens/``). To
exercise the project-358382 pilot end-to-end offline, the replay test
needs *some* CT input that:

1. Is small enough to commit / regenerate on every run (the replay
   fixtures themselves stay JSONL-only).
2. Has at least one bright sphere so the bright-seed greedy
   percentile picker finds a seed point. Otherwise the pilot exits
   early with "no_seed_found" and we don't actually exercise the
   record/replay loop.
3. Has a matching mesh (PLY) that the orchestrator can voxelize back
   into the same grid as the CT for Dice scoring.

This module produces both, on demand, with a fixed seed.

Outputs
-------
``make_fixture_specimen(out_dir, *, slug, shape, seed)`` writes::

    <out_dir>/ct_volume.nii.gz       - tiny synthetic CT (~few KB gzipped)
    <out_dir>/mesh.ply               - sphere mesh covering the bright spot
    <out_dir>/expected.json          - voxel-count + sphere centre metadata

Both files are reproducible bit-for-bit when the same seed/shape are
passed. The caller can then feed them into ``run_specimen`` via the
orchestrator's normal caching paths.

Heavy imports (numpy / SimpleITK / vtk) are lazy so importing this
module on a Python that lacks them — like the parent CI bootstrap —
still succeeds.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class FixtureBundle:
    """Paths + ground-truth metadata for one synthetic specimen."""

    slug: str
    ct_path: Path
    mesh_path: Path
    gt_label_path: Optional[Path]
    sphere_center_lps_mm: tuple[float, float, float]
    sphere_radius_mm: float
    bright_voxel_count: int
    shape_ijk: tuple[int, int, int]
    spacing_mm: tuple[float, float, float]
    fixture_seed: int

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "ct_path": str(self.ct_path),
            "mesh_path": str(self.mesh_path),
            "gt_label_path": (
                str(self.gt_label_path) if self.gt_label_path else None
            ),
            "sphere_center_lps_mm": list(self.sphere_center_lps_mm),
            "sphere_radius_mm": self.sphere_radius_mm,
            "bright_voxel_count": self.bright_voxel_count,
            "shape_ijk": list(self.shape_ijk),
            "spacing_mm": list(self.spacing_mm),
            "fixture_seed": self.fixture_seed,
        }


def _require_numpy_sitk():
    import numpy as np
    import SimpleITK as sitk
    return np, sitk


def make_fixture_specimen(
    out_dir: Path,
    *,
    slug: str = "fixture_specimen",
    shape: tuple[int, int, int] = (48, 48, 48),
    spacing_mm: tuple[float, float, float] = (1.0, 1.0, 1.0),
    sphere_center_voxels: Optional[tuple[float, float, float]] = None,
    sphere_radius_voxels: float = 6.0,
    background_intensity: float = 100.0,
    sphere_intensity: float = 3000.0,
    noise_amplitude: float = 5.0,
    seed: int = 0,
    write_gt: bool = True,
) -> FixtureBundle:
    """Build a tiny CT + matching mesh in *out_dir*.

    The CT is a uint16 volume with a uniform low background + a
    bright sphere of higher intensity. The mesh is the surface of the
    same sphere in LPS millimetres, written as ASCII PLY so it stays
    deterministic and human-readable.

    The optional ``gt.nii.gz`` is the binary mask of the sphere on
    the same grid — useful when a test wants to short-circuit the
    voxelization step.
    """
    np, sitk = _require_numpy_sitk()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nx, ny, nz = (int(s) for s in shape)
    sx, sy, sz = (float(s) for s in spacing_mm)
    cx_v, cy_v, cz_v = (
        sphere_center_voxels
        if sphere_center_voxels is not None
        else (nx / 2.0, ny / 2.0, nz / 2.0)
    )
    r = float(sphere_radius_voxels)

    rng = np.random.default_rng(seed)
    # Build coordinate grids in voxel space.
    xv = np.arange(nx)
    yv = np.arange(ny)
    zv = np.arange(nz)
    Z, Y, X = np.meshgrid(zv, yv, xv, indexing="ij")
    dist2 = (X - cx_v) ** 2 + (Y - cy_v) ** 2 + (Z - cz_v) ** 2
    inside = dist2 <= r * r

    arr = np.full((nz, ny, nx), background_intensity, dtype=np.float32)
    arr[inside] = sphere_intensity
    if noise_amplitude > 0:
        arr = arr + rng.normal(0, noise_amplitude, size=arr.shape).astype(np.float32)
    arr = np.clip(arr, 0, 65535).astype(np.uint16)

    img = sitk.GetImageFromArray(arr)
    img.SetSpacing((sx, sy, sz))
    # Place origin so the sphere centre is at a known LPS coord.
    img.SetOrigin((0.0, 0.0, 0.0))
    img.SetDirection((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))

    ct_path = out_dir / "ct_volume.nii.gz"
    sitk.WriteImage(img, str(ct_path), useCompression=True)

    sphere_center_lps_mm = (cx_v * sx, cy_v * sy, cz_v * sz)
    sphere_radius_mm = r * min(sx, sy, sz)

    mesh_path = out_dir / "mesh.ply"
    _write_sphere_ply(
        mesh_path,
        center=sphere_center_lps_mm,
        radius=sphere_radius_mm,
        n_subdiv=2,
    )

    gt_path: Optional[Path] = None
    if write_gt:
        gt_arr = np.where(inside, 1, 0).astype(np.uint8)
        gt_img = sitk.GetImageFromArray(gt_arr)
        gt_img.CopyInformation(img)
        gt_path = out_dir / "gt_voxelized.nii.gz"
        sitk.WriteImage(gt_img, str(gt_path), useCompression=True)

    bundle = FixtureBundle(
        slug=slug,
        ct_path=ct_path,
        mesh_path=mesh_path,
        gt_label_path=gt_path,
        sphere_center_lps_mm=tuple(sphere_center_lps_mm),
        sphere_radius_mm=sphere_radius_mm,
        bright_voxel_count=int(inside.sum()),
        shape_ijk=(nx, ny, nz),
        spacing_mm=(sx, sy, sz),
        fixture_seed=seed,
    )
    (out_dir / "expected.json").write_text(json.dumps(bundle.to_dict(), indent=2))
    return bundle


def _write_sphere_ply(
    path: Path,
    *,
    center: tuple[float, float, float],
    radius: float,
    n_subdiv: int = 2,
) -> Path:
    """Write a deterministic icosphere PLY centred at ``center``.

    No external libs required — we build the icosahedron and subdivide
    a fixed number of times. ``n_subdiv=2`` yields 162 vertices / 320
    faces, which is plenty for voxelization tests.
    """
    import math

    phi = (1.0 + math.sqrt(5.0)) / 2.0

    # Initial icosahedron vertices (12), then normalise to unit sphere.
    verts: list[list[float]] = [
        [-1,  phi, 0], [ 1,  phi, 0], [-1, -phi, 0], [ 1, -phi, 0],
        [0, -1,  phi], [0,  1,  phi], [0, -1, -phi], [0,  1, -phi],
        [ phi, 0, -1], [ phi, 0,  1], [-phi, 0, -1], [-phi, 0,  1],
    ]
    faces: list[tuple[int, int, int]] = [
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
    ]

    def _normalise(v):
        n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        return [v[0] / n, v[1] / n, v[2] / n]

    verts = [_normalise(v) for v in verts]

    midpoint_cache: dict[tuple[int, int], int] = {}

    def _midpoint(a: int, b: int) -> int:
        key = (a, b) if a < b else (b, a)
        if key in midpoint_cache:
            return midpoint_cache[key]
        pa = verts[a]
        pb = verts[b]
        m = _normalise([(pa[0] + pb[0]) / 2.0,
                        (pa[1] + pb[1]) / 2.0,
                        (pa[2] + pb[2]) / 2.0])
        verts.append(m)
        midpoint_cache[key] = len(verts) - 1
        return len(verts) - 1

    for _ in range(n_subdiv):
        new_faces: list[tuple[int, int, int]] = []
        for a, b, c in faces:
            ab = _midpoint(a, b)
            bc = _midpoint(b, c)
            ca = _midpoint(c, a)
            new_faces.extend([(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)])
        faces = new_faces

    cx, cy, cz = center
    out_lines = [
        "ply",
        "format ascii 1.0",
        "comment fixture sphere generated by jetstream_replay.synthetic",
        f"element vertex {len(verts)}",
        "property float x",
        "property float y",
        "property float z",
        f"element face {len(faces)}",
        "property list uchar int vertex_indices",
        "end_header",
    ]
    for v in verts:
        out_lines.append(
            f"{cx + v[0] * radius:.6f} "
            f"{cy + v[1] * radius:.6f} "
            f"{cz + v[2] * radius:.6f}"
        )
    for a, b, c in faces:
        out_lines.append(f"3 {a} {b} {c}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out_lines) + "\n")
    return path


def fingerprint(bundle: FixtureBundle) -> str:
    """Short stable hash of a fixture bundle for assertions."""
    blob = json.dumps(bundle.to_dict(), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
