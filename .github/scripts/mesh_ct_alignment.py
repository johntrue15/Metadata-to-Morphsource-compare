"""
Align a surface mesh to a reference CT/MRI volume when coordinate frames differ.

MorphoSource CT stacks and derivative meshes often disagree on axis order
(RAS vs slice index) or origin convention (voxel corner vs volume center).
``crop_around_mesh`` assumes the mesh bbox lies inside the volume in physical
space; this module picks a signed axis permutation (and optional origin
reinterpretation) that maximizes mesh-in-volume overlap, then writes an
aligned mesh copy for cropping / voxelization.
"""

from __future__ import annotations

import itertools
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

log = logging.getLogger("mesh_ct_alignment")

Bounds6 = Tuple[float, float, float, float, float, float]


def signed_permutations(np):
    """Yield (label, 3x3 int8 matrix) for 48 signed axis permutations."""
    axis_chars = "xyz"
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            M = np.zeros((3, 3), dtype=np.int8)
            for i_out, j_in in enumerate(perm):
                M[i_out, j_in] = signs[i_out]
            label = "".join(
                ("+" if signs[i] > 0 else "-") + axis_chars[perm[i]]
                for i in range(3)
            )
            yield label, M


def transform_bbox(np, M, bounds: Bounds6) -> Bounds6:
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    corners = np.array([
        [xmin, ymin, zmin], [xmax, ymin, zmin],
        [xmin, ymax, zmin], [xmax, ymax, zmin],
        [xmin, ymin, zmax], [xmax, ymin, zmax],
        [xmin, ymax, zmax], [xmax, ymax, zmax],
    ], dtype=np.float64)
    new = corners @ np.asarray(M, dtype=np.float64).T
    return (float(new[:, 0].min()), float(new[:, 0].max()),
            float(new[:, 1].min()), float(new[:, 1].max()),
            float(new[:, 2].min()), float(new[:, 2].max()))


def bbox_overlap_vol(b1: Bounds6, b2: Bounds6) -> float:
    ix = max(0.0, min(b1[1], b2[1]) - max(b1[0], b2[0]))
    iy = max(0.0, min(b1[3], b2[3]) - max(b1[2], b2[2]))
    iz = max(0.0, min(b1[5], b2[5]) - max(b1[4], b2[4]))
    return ix * iy * iz


def bbox_volume(b: Bounds6) -> float:
    return (max(0.0, b[1] - b[0])
            * max(0.0, b[3] - b[2])
            * max(0.0, b[5] - b[4]))


def ct_world_extent(origin, size_xyz, spacing_xyz) -> Bounds6:
    ox, oy, oz = origin
    nx, ny, nz = size_xyz
    sx, sy, sz = spacing_xyz
    return (ox, ox + (nx - 1) * sx,
            oy, oy + (ny - 1) * sy,
            oz, oz + (nz - 1) * sz)


def find_best_mesh_orientation(
    mesh_bounds: Bounds6,
    size_xyz: Sequence[int],
    spacing_xyz: Sequence[float],
    *,
    volume_origin: Optional[Sequence[float]] = None,
    origin_convention: str = "auto",
    mesh_axis_perm: str = "auto",
) -> Dict[str, Any]:
    """Pick mesh signed-permutation maximizing overlap with CT extent."""
    import numpy as np

    nx, ny, nz = (int(size_xyz[0]), int(size_xyz[1]), int(size_xyz[2]))
    sx, sy, sz = (float(spacing_xyz[0]), float(spacing_xyz[1]), float(spacing_xyz[2]))

    origins: list[tuple[str, tuple[float, float, float]]] = []
    if volume_origin is not None:
        origins.append(("volume", tuple(float(x) for x in volume_origin)))
    if origin_convention in ("auto", "zero"):
        origins.append(("zero", (0.0, 0.0, 0.0)))
    if origin_convention in ("auto", "centered"):
        origins.append((
            "centered",
            (-0.5 * (nx - 1) * sx,
             -0.5 * (ny - 1) * sy,
             -0.5 * (nz - 1) * sz),
        ))

    if mesh_axis_perm == "auto":
        candidates = list(signed_permutations(np))
    else:
        match = None
        for label, M in signed_permutations(np):
            if label == mesh_axis_perm:
                match = (label, M)
                break
        if match is None:
            raise ValueError(f"Unknown mesh_axis_perm: {mesh_axis_perm!r}")
        candidates = [match]

    mesh_vol = bbox_volume(mesh_bounds)
    if mesh_vol <= 0:
        raise RuntimeError(f"Mesh bbox has zero volume: {mesh_bounds}")

    best = None
    for origin_label, origin in origins:
        ct_extent = ct_world_extent(origin, (nx, ny, nz), (sx, sy, sz))
        for label, M in candidates:
            new_bounds = transform_bbox(np, M, mesh_bounds)
            new_vol = bbox_volume(new_bounds)
            overlap = bbox_overlap_vol(new_bounds, ct_extent)
            ratio = overlap / new_vol if new_vol > 0 else 0.0
            if best is None or ratio > best[0]:
                best = (ratio, label, M, origin_label, origin, new_bounds)

    assert best is not None
    ratio, label, M, origin_label, origin, new_bounds = best
    return {
        "overlap_ratio": round(ratio, 4),
        "mesh_M": [list(row) for row in np.asarray(M).tolist()],
        "mesh_M_label": label,
        "origin_convention": origin_label,
        "volume_origin": list(origin),
        "transformed_bounds": list(new_bounds),
    }


def read_mesh_bounds(mesh_path: Path) -> Bounds6:
    """Return (xmin,xmax,ymin,ymax,zmin,zmax) from a mesh file."""
    suffix = mesh_path.suffix.lower()
    import vtk  # type: ignore

    if suffix == ".ply":
        r = vtk.vtkPLYReader()
    elif suffix == ".stl":
        r = vtk.vtkSTLReader()
    elif suffix == ".obj":
        r = vtk.vtkOBJReader()
    else:
        raise ValueError(f"Unsupported mesh format: {suffix}")

    r.SetFileName(str(mesh_path))
    r.Update()
    poly = r.GetOutput()
    if not poly or poly.GetNumberOfPoints() == 0:
        raise RuntimeError(f"Empty mesh: {mesh_path}")
    return tuple(poly.GetBounds())  # type: ignore[return-value]


def apply_orientation_to_mesh(mesh_path: Path, output_path: Path,
                              M: Sequence[Sequence[int]]) -> Path:
    """Write a copy of *mesh_path* with 3x3 signed-permutation *M* applied."""
    import numpy as np
    import vtk  # type: ignore

    suffix = mesh_path.suffix.lower()
    if suffix == ".ply":
        r = vtk.vtkPLYReader()
    elif suffix == ".stl":
        r = vtk.vtkSTLReader()
    elif suffix == ".obj":
        r = vtk.vtkOBJReader()
    else:
        raise ValueError(f"Unsupported mesh format: {suffix}")

    r.SetFileName(str(mesh_path))
    r.Update()
    poly = r.GetOutput()

    M4 = np.eye(4, dtype=np.float64)
    M4[:3, :3] = np.asarray(M, dtype=np.float64)
    vmat = vtk.vtkMatrix4x4()
    for row in range(4):
        for col in range(4):
            vmat.SetElement(row, col, float(M4[row, col]))
    xform = vtk.vtkTransform()
    xform.SetMatrix(vmat)
    tf = vtk.vtkTransformPolyDataFilter()
    tf.SetTransform(xform)
    tf.SetInputData(poly)
    tf.Update()
    out_poly = tf.GetOutput()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".ply":
        w = vtk.vtkPLYWriter()
    elif suffix == ".stl":
        w = vtk.vtkSTLWriter()
    else:
        w = vtk.vtkOBJWriter()
    w.SetFileName(str(output_path))
    w.SetInputData(out_poly)
    w.Write()
    return output_path


def align_mesh_to_reference_volume(
    mesh_path: Path,
    volume_path: Path,
    output_mesh_path: Path,
    *,
    min_overlap_ratio: float = 0.05,
    origin_convention: str = "auto",
    mesh_axis_perm: str = "auto",
    force: bool = False,
) -> Dict[str, Any]:
    """Align *mesh_path* to *volume_path* and write to *output_mesh_path*."""
    import SimpleITK as sitk

    mesh_path = Path(mesh_path)
    volume_path = Path(volume_path)
    output_mesh_path = Path(output_mesh_path)

    if output_mesh_path.exists() and output_mesh_path.stat().st_size > 0 and not force:
        return {"output_path": str(output_mesh_path), "from_cache": True}

    img = sitk.ReadImage(str(volume_path))
    size_xyz = img.GetSize()
    spacing_xyz = img.GetSpacing()
    volume_origin = img.GetOrigin()

    mesh_bounds = read_mesh_bounds(mesh_path)

    identity_extent = ct_world_extent(volume_origin, size_xyz, spacing_xyz)
    identity_overlap = bbox_overlap_vol(mesh_bounds, identity_extent)
    mesh_vol = bbox_volume(mesh_bounds)
    identity_ratio = identity_overlap / mesh_vol if mesh_vol > 0 else 0.0

    if identity_ratio >= min_overlap_ratio and mesh_axis_perm == "auto" and not force:
        log.info("Mesh already overlaps volume (ratio=%.3f); copying without re-orient",
                 identity_ratio)
        output_mesh_path.parent.mkdir(parents=True, exist_ok=True)
        if mesh_path.resolve() != output_mesh_path.resolve():
            shutil.copy2(mesh_path, output_mesh_path)
        return {
            "output_path": str(output_mesh_path),
            "overlap_ratio": round(identity_ratio, 4),
            "mesh_M_label": "+x+y+z",
            "skipped_reorientation": True,
        }

    orient = find_best_mesh_orientation(
        mesh_bounds, size_xyz, spacing_xyz,
        volume_origin=volume_origin,
        origin_convention=origin_convention,
        mesh_axis_perm=mesh_axis_perm,
    )
    if orient["overlap_ratio"] < min_overlap_ratio:
        return {
            "error": "insufficient_overlap",
            "overlap_ratio": orient["overlap_ratio"],
            "min_overlap_ratio": min_overlap_ratio,
            "details": orient,
        }

    apply_orientation_to_mesh(mesh_path, output_mesh_path, orient["mesh_M"])
    log.info("Aligned mesh %s -> %s (axes=%s, overlap=%.3f)",
             mesh_path.name, output_mesh_path.name,
             orient["mesh_M_label"], orient["overlap_ratio"])
    orient["output_path"] = str(output_mesh_path)
    return orient
