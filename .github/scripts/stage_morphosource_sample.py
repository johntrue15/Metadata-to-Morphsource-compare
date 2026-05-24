#!/usr/bin/env python3
"""Download a MorphoSource (CT, mesh) pair and stage a tiny, URL-loadable
NRRD test asset pair in ``data/sample/``.

Defaults reproduce the *Tuatara skull* sample:

    parent CT      : media 000011009  (~5 GB TIFF z-stack, isotropic 0.0298 mm)
    derivative mesh: media 000358663  (~700 MB ``.ply``, derived from the CT)

Outputs (gzip-encoded single-file NRRDs, alongside a provenance JSON)::

    data/sample/tuatara_skull_000358663_ct.nrrd
    data/sample/tuatara_skull_000358663_gt_labelmap.nrrd
    data/sample/tuatara_skull_000358663.provenance.json

These mirror the convention of
``https://github.com/SlicerMorph/SampleData/blob/master/IMPC_sample_data.nrrd``
so 3D Slicer (and the Jetstream Slicer remote in this repo) can pull either
NRRD directly via *Add data from URL*.

Implementation notes:

- Downloads are cached at ``data/morphosource-download-<id>/`` (which is
  already gitignored). Re-runs reuse the cache. Set ``--force-download`` to
  redownload.
- The CT TIFF stack is **never fully loaded into RAM**: only the slices
  inside the mesh bounding box (plus ``--margin-mm``) are read, and each
  slice is cropped in XY and stride-downsampled to keep the largest axis
  ``<= --max-axis`` voxels (default 512). For a tuatara skull at 30 µm
  this yields a ~30–80 MB gzipped NRRD.
- The mesh is voxelized onto the *same* downsampled grid with VTK
  (``vtkPolyDataToImageStencil``), so the labelmap and CT overlay exactly.
- Two coordinate-frame mismatches are auto-corrected:
    1. **Origin convention** — the script tries both ``(0, 0, 0)`` (TIFF
       voxel-corner) and ``-0.5 * (N-1) * spacing`` (volume-centered).
    2. **Axis orientation** — all 48 signed permutations of the mesh
       axes are evaluated, and the one whose bbox lands most inside the
       CT extent is picked. This handles the common case where the mesh
       was exported in a Slicer-RAS frame (rostral-caudal = +Y) but the
       TIFF stack has rostral-caudal = +Z.

Usage::

    set -a && source .env && set +a
    "$HOME/.autoresearchclaw/nninteractive/bin/python" \\
        .github/scripts/stage_morphosource_sample.py
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _helpers import load_dotenv, safe_first  # noqa: E402
from morphosource_client import MorphoSourceClient  # noqa: E402
from morphosource_api_download import download_media  # noqa: E402

log = logging.getLogger("stage_sample")


TIFF_EXTS = {".tif", ".tiff"}
MESH_EXTS = {".ply", ".stl", ".obj"}
MIN_TIFF_STACK = 10


def _import_deps():
    try:
        import numpy as np
        import SimpleITK as sitk
        import vtk
        from vtk.util import numpy_support
        from PIL import Image
    except ImportError as exc:
        print(
            f"Missing dependency: {exc}. Run inside the nnInteractive venv "
            "(`$HOME/.autoresearchclaw/nninteractive/bin/python`).",
            file=sys.stderr,
        )
        sys.exit(1)
    Image.MAX_IMAGE_PIXELS = None
    return np, sitk, vtk, numpy_support, Image


# ---------------------------------------------------------------------------
# MorphoSource lookup helpers
# ---------------------------------------------------------------------------


def _fetch_metadata(client: MorphoSourceClient, media_id: str) -> dict:
    rec = client.get_media(media_id)
    if rec.error:
        raise RuntimeError(f"MorphoSource lookup failed for {media_id}: {rec.error}")
    data = rec.data or {}
    m = data.get("response", data)
    if isinstance(m, dict):
        m = m.get("media", m)
    if not isinstance(m, dict):
        raise RuntimeError(f"Unexpected metadata shape for {media_id}")
    return m


def _voxel_spacing_from_meta(meta: dict) -> Optional[Tuple[float, float, float]]:
    sx = safe_first(meta.get("x_pixel_spacing"))
    sy = safe_first(meta.get("y_pixel_spacing"))
    sz = safe_first(meta.get("z_pixel_spacing")) or safe_first(meta.get("slice_thickness"))
    try:
        sx = float(sx) if sx else None
        sy = float(sy) if sy else sx
        sz = float(sz) if sz else sx
        if sx is None:
            return None
        return (sx, sy, sz)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cached downloads
# ---------------------------------------------------------------------------


def _download_cached(media_id: str, dest_dir: Path, force: bool = False) -> dict:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not force:
        zips = list(dest_dir.glob("morphosource_media-id-*.zip"))
        extracted = [z for z in zips
                     if (z.parent / z.stem).is_dir()
                     and any((z.parent / z.stem).iterdir())]
        if extracted:
            log.info("Cache hit for media %s — %s", media_id, dest_dir)
            return {
                "success": True, "media_id": media_id,
                "downloaded_file": str(extracted[0]),
                "file_size": extracted[0].stat().st_size,
                "download_dir": str(dest_dir),
                "from_cache": True,
            }
    log.info("Downloading media %s -> %s", media_id, dest_dir)
    return download_media(media_id, str(dest_dir))


# ---------------------------------------------------------------------------
# TIFF z-stack discovery (mirrors nninteractive_compare._find_ct_volume)
# ---------------------------------------------------------------------------


def _natural_key(p: Path):
    import re
    return [int(x) if x.isdigit() else x.lower()
            for x in re.split(r"(\d+)", p.name)]


def _find_tiff_stack(root: Path) -> Tuple[Path, list[Path]]:
    """Return (stack_dir, sorted_tiff_paths). Largest stack wins on ties."""
    best: tuple[Path, list[Path], int] = (Path(), [], -1)
    for sub in root.rglob("*"):
        if not sub.is_dir():
            continue
        try:
            tifs = [e for e in sub.iterdir()
                    if e.is_file() and e.suffix.lower() in TIFF_EXTS]
        except (OSError, PermissionError):
            continue
        if len(tifs) < MIN_TIFF_STACK:
            continue
        total = sum(t.stat().st_size for t in tifs)
        if total > best[2]:
            tifs_sorted = sorted(tifs, key=_natural_key)
            best = (sub, tifs_sorted, total)
    if best[2] < 0:
        raise RuntimeError(f"No TIFF z-stack found under {root}")
    log.info("Picked TIFF stack: %s (%d slices, %.1f MB)",
             best[0], len(best[1]), best[2] / 1e6)
    return best[0], best[1]


def _find_mesh(root: Path) -> Path:
    candidates: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in MESH_EXTS:
            candidates.append(p)
    if not candidates:
        raise RuntimeError(f"No mesh found under {root}")
    candidates.sort(key=lambda f: f.stat().st_size, reverse=True)
    log.info("Picked mesh: %s (%.1f MB)",
             candidates[0], candidates[0].stat().st_size / 1e6)
    return candidates[0]


# ---------------------------------------------------------------------------
# Mesh bounds (VTK)
# ---------------------------------------------------------------------------


def _read_mesh_bounds_and_poly(mesh_path: Path):
    _, _, vtk, _, _ = _import_deps()
    suffix = mesh_path.suffix.lower()
    if suffix == ".ply":
        reader = vtk.vtkPLYReader()
    elif suffix == ".stl":
        reader = vtk.vtkSTLReader()
    elif suffix == ".obj":
        reader = vtk.vtkOBJReader()
    else:
        raise ValueError(f"Unsupported mesh format: {suffix}")
    reader.SetFileName(str(mesh_path))
    reader.Update()
    poly = reader.GetOutput()
    if not poly or poly.GetNumberOfPoints() == 0:
        raise RuntimeError(f"Mesh has no points: {mesh_path}")
    bounds = tuple(float(b) for b in poly.GetBounds())  # (xmin,xmax,ymin,ymax,zmin,zmax)
    log.info("Mesh world bounds (mm): "
             "x=[%.3f, %.3f]  y=[%.3f, %.3f]  z=[%.3f, %.3f]",
             *bounds)
    return poly, bounds


# ---------------------------------------------------------------------------
# Streaming TIFF -> cropped, downsampled volume
# ---------------------------------------------------------------------------


@dataclass
class GridSpec:
    """Image grid after crop + stride-downsample."""
    size_xyz: Tuple[int, int, int]
    spacing_xyz: Tuple[float, float, float]
    origin_xyz: Tuple[float, float, float]
    crop_index_min: Tuple[int, int, int]  # (ix0, iy0, iz0) into native grid
    crop_index_max: Tuple[int, int, int]  # (ix1, iy1, iz1) inclusive
    stride: int
    full_size_xyz: Tuple[int, int, int]
    origin_convention: str
    mesh_M: Tuple[Tuple[float, ...], ...]  # 3x3 signed-permutation applied to mesh
    mesh_M_label: str


# ---------------------------------------------------------------------------
# Mesh orientation auto-detection
# ---------------------------------------------------------------------------


def _signed_permutations(np):
    """Yield (label, 3x3 np.int8 matrix) for the 48 signed-permutation
    rotation/reflection matrices of an axis-aligned cube."""
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


def _transform_bbox(np, M, bounds_xyzxyz):
    """Apply a 3x3 signed-permutation matrix to a (xmin,xmax,ymin,ymax,zmin,zmax)
    axis-aligned bbox; result is still axis-aligned."""
    xmin, xmax, ymin, ymax, zmin, zmax = bounds_xyzxyz
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


def _bbox_overlap_vol(b1, b2) -> float:
    ix = max(0.0, min(b1[1], b2[1]) - max(b1[0], b2[0]))
    iy = max(0.0, min(b1[3], b2[3]) - max(b1[2], b2[2]))
    iz = max(0.0, min(b1[5], b2[5]) - max(b1[4], b2[4]))
    return ix * iy * iz


def _bbox_volume(b) -> float:
    return (max(0.0, b[1] - b[0])
            * max(0.0, b[3] - b[2])
            * max(0.0, b[5] - b[4]))


def _ct_world_extent(origin, size, spacing):
    return (origin[0], origin[0] + (size[0] - 1) * spacing[0],
            origin[1], origin[1] + (size[1] - 1) * spacing[1],
            origin[2], origin[2] + (size[2] - 1) * spacing[2])


def _find_best_orientation(np, mesh_bounds, full_size_xyz, spacing_xyz,
                           origin_convention: str,
                           mesh_axis_perm: str = "auto"):
    """Pick the (signed-permutation, origin-convention) pair that maximizes
    overlap of the transformed mesh bbox with the CT world extent.

    Returns (M_3x3, label, origin_convention, origin_xyz, overlap_ratio,
    transformed_bounds).
    """
    nx, ny, nz = full_size_xyz
    sx, sy, sz = spacing_xyz
    origins = []
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
        candidates = list(_signed_permutations(np))
    else:
        # Find exact match.
        match = None
        for label, M in _signed_permutations(np):
            if label == mesh_axis_perm:
                match = (label, M)
                break
        if match is None:
            raise ValueError(
                f"Unknown --mesh-axis-perm value: {mesh_axis_perm!r}. "
                "Use e.g. '+x+y+z' (identity) or '+x+z+y' (swap Y/Z)."
            )
        candidates = [match]

    mesh_vol = _bbox_volume(mesh_bounds)
    if mesh_vol <= 0:
        raise RuntimeError(f"Mesh bbox has zero volume: {mesh_bounds}")

    best = None  # (overlap_ratio, label, M, origin_label, origin, new_bounds)
    for origin_label, origin in origins:
        ct_extent = _ct_world_extent(origin, full_size_xyz, spacing_xyz)
        for label, M in candidates:
            new_bounds = _transform_bbox(np, M, mesh_bounds)
            new_vol = _bbox_volume(new_bounds)
            overlap = _bbox_overlap_vol(new_bounds, ct_extent)
            ratio = overlap / new_vol if new_vol > 0 else 0.0
            if best is None or ratio > best[0]:
                best = (ratio, label, M, origin_label, origin, new_bounds)

    assert best is not None
    return best


def _apply_signed_permutation_to_poly(np, vtk, poly, M):
    M4 = np.eye(4, dtype=np.float64)
    M4[:3, :3] = np.asarray(M, dtype=np.float64)
    vmat = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            vmat.SetElement(r, c, float(M4[r, c]))
    xform = vtk.vtkTransform()
    xform.SetMatrix(vmat)
    tf = vtk.vtkTransformPolyDataFilter()
    tf.SetTransform(xform)
    tf.SetInputData(poly)
    tf.Update()
    return tf.GetOutput()


def _compute_grid(full_size_xyz: Tuple[int, int, int],
                  spacing_xyz: Tuple[float, float, float],
                  mesh_bounds: Sequence[float],
                  margin_mm: float,
                  max_axis: int,
                  origin_convention: str = "auto",
                  mesh_axis_perm: str = "auto",
                  min_overlap: float = 0.9) -> GridSpec:
    """Pick the mesh-axis-permutation + origin-convention pair that lands
    the mesh inside the CT extent, then derive crop indices + downsample
    stride."""
    np, _, _, _, _ = _import_deps()

    nx, ny, nz = full_size_xyz
    sx, sy, sz = spacing_xyz

    overlap, perm_label, M, origin_label, origin, new_bounds = \
        _find_best_orientation(np, mesh_bounds, full_size_xyz, spacing_xyz,
                               origin_convention, mesh_axis_perm)
    log.info("Best mesh orientation: axes=%s, origin=%s, overlap=%.3f",
             perm_label, origin_label, overlap)
    log.info("Mesh bounds after permutation (mm): "
             "x=[%.3f, %.3f]  y=[%.3f, %.3f]  z=[%.3f, %.3f]",
             *new_bounds)
    if overlap < min_overlap:
        raise RuntimeError(
            f"Best mesh orientation only matches {overlap:.1%} of the "
            f"mesh bbox to the CT extent (threshold={min_overlap:.1%}). "
            "The mesh world bounds may be incompatible with the TIFF stack. "
            "Try overriding with --mesh-axis-perm or --origin-convention."
        )

    xmin_m, xmax_m, ymin_m, ymax_m, zmin_m, zmax_m = new_bounds
    ox, oy, oz = origin
    ix0 = int(np.floor((xmin_m - margin_mm - ox) / sx))
    ix1 = int(np.ceil((xmax_m + margin_mm - ox) / sx))
    iy0 = int(np.floor((ymin_m - margin_mm - oy) / sy))
    iy1 = int(np.ceil((ymax_m + margin_mm - oy) / sy))
    iz0 = int(np.floor((zmin_m - margin_mm - oz) / sz))
    iz1 = int(np.ceil((zmax_m + margin_mm - oz) / sz))

    ix0c, ix1c = max(0, ix0), min(nx - 1, ix1)
    iy0c, iy1c = max(0, iy0), min(ny - 1, iy1)
    iz0c, iz1c = max(0, iz0), min(nz - 1, iz1)
    if ix1c < ix0c or iy1c < iy0c or iz1c < iz0c:
        raise RuntimeError(
            f"Empty crop after clamping: native size={full_size_xyz}, "
            f"requested index range x={ix0}..{ix1} y={iy0}..{iy1} z={iz0}..{iz1}"
        )

    crop_dims = (ix1c - ix0c + 1, iy1c - iy0c + 1, iz1c - iz0c + 1)
    stride = max(1, int(np.ceil(max(crop_dims) / max_axis)))

    new_nx = (crop_dims[0] + stride - 1) // stride
    new_ny = (crop_dims[1] + stride - 1) // stride
    new_nz = (crop_dims[2] + stride - 1) // stride

    new_spacing = (sx * stride, sy * stride, sz * stride)
    new_origin = (
        origin[0] + ix0c * sx,
        origin[1] + iy0c * sy,
        origin[2] + iz0c * sz,
    )

    log.info("Origin convention: %s -> origin=%s", origin_label, origin)
    log.info("Crop index range (native): x=[%d, %d] y=[%d, %d] z=[%d, %d]",
             ix0c, ix1c, iy0c, iy1c, iz0c, iz1c)
    log.info("Crop voxel dims (native): %s -> downsample stride=%d -> output dims %s",
             crop_dims, stride, (new_nx, new_ny, new_nz))
    log.info("Output spacing (mm): %s, origin (mm): %s", new_spacing, new_origin)

    M_tuple = tuple(tuple(float(v) for v in row) for row in np.asarray(M))
    return GridSpec(
        size_xyz=(new_nx, new_ny, new_nz),
        spacing_xyz=new_spacing,
        origin_xyz=new_origin,
        crop_index_min=(ix0c, iy0c, iz0c),
        crop_index_max=(ix1c, iy1c, iz1c),
        stride=stride,
        full_size_xyz=full_size_xyz,
        origin_convention=origin_label,
        mesh_M=M_tuple,
        mesh_M_label=perm_label,
    )


def _peek_tiff_dims(Image, tiff_files: list[Path], np) -> Tuple[int, int, str]:
    with Image.open(tiff_files[0]) as im:
        nx, ny = im.size  # (width, height)
        # Force-load mode to detect dtype.
        sample = np.asarray(im)
    dtype_name = str(sample.dtype)
    return nx, ny, dtype_name


def _stream_load_volume(tiff_files: list[Path], grid: GridSpec) -> "np.ndarray":  # type: ignore
    np, _, _, _, Image = _import_deps()
    ix0, iy0, iz0 = grid.crop_index_min
    ix1, iy1, iz1 = grid.crop_index_max
    stride = grid.stride

    z_indices = list(range(iz0, iz1 + 1, stride))
    log.info("Streaming %d / %d TIFF slices (stride=%d)",
             len(z_indices), len(tiff_files), stride)

    slices_out: list = []
    last_log = time.time()
    for n, k in enumerate(z_indices):
        with Image.open(tiff_files[k]) as im:
            arr = np.asarray(im)
        if arr.ndim != 2:
            raise RuntimeError(f"Unexpected TIFF shape {arr.shape} in {tiff_files[k]}")
        crop = arr[iy0:iy1 + 1:stride, ix0:ix1 + 1:stride]
        slices_out.append(np.ascontiguousarray(crop))
        if time.time() - last_log > 5.0:
            log.info("  z slice %d/%d", n + 1, len(z_indices))
            last_log = time.time()
    volume = np.stack(slices_out, axis=0)
    log.info("Assembled volume shape (z, y, x) = %s, dtype=%s",
             volume.shape, volume.dtype)
    return volume


# ---------------------------------------------------------------------------
# NRRD writing
# ---------------------------------------------------------------------------


def _write_nrrd(np, sitk, volume_zyx, spacing_xyz, origin_xyz,
                output_path: Path, pixel_type=None):
    img = sitk.GetImageFromArray(volume_zyx)
    img.SetSpacing(tuple(float(s) for s in spacing_xyz))
    img.SetOrigin(tuple(float(o) for o in origin_xyz))
    # Identity direction (LPS = MorphoSource TIFF stack convention).
    img.SetDirection((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
    if pixel_type is not None:
        img = sitk.Cast(img, pixel_type)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sitk.WriteImage(img, str(output_path), useCompression=True)
    log.info("Wrote %s (%.1f MB)",
             output_path, output_path.stat().st_size / 1e6)
    return img


# ---------------------------------------------------------------------------
# Voxelization onto a precomputed grid (no SITK reference image needed)
# ---------------------------------------------------------------------------


def _voxelize_onto_grid(poly, grid: GridSpec,
                        output_path: Path, fill_value: int = 1) -> dict:
    np, sitk, vtk, numpy_support, _ = _import_deps()

    size = grid.size_xyz
    spacing = grid.spacing_xyz
    origin = grid.origin_xyz
    direction = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    # Build a world -> voxel-index VTK transform.
    D = np.eye(3, dtype=np.float64)
    invD = np.eye(3, dtype=np.float64)
    inv_spacing = 1.0 / np.asarray(spacing, dtype=np.float64)
    M = np.eye(4, dtype=np.float64)
    M[:3, :3] = (np.diag(inv_spacing) @ invD)
    M[:3, 3] = -M[:3, :3] @ np.asarray(origin, dtype=np.float64)
    vmat = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            vmat.SetElement(r, c, float(M[r, c]))
    xform = vtk.vtkTransform()
    xform.SetMatrix(vmat)

    tfilter = vtk.vtkTransformPolyDataFilter()
    tfilter.SetTransform(xform)
    tfilter.SetInputData(poly)
    tfilter.Update()
    poly_idx = tfilter.GetOutput()
    log.info("Mesh in voxel-index space, bounds=%s",
             poly_idx.GetBounds())

    white = vtk.vtkImageData()
    white.SetSpacing(1.0, 1.0, 1.0)
    white.SetOrigin(0.0, 0.0, 0.0)
    white.SetExtent(0, size[0] - 1, 0, size[1] - 1, 0, size[2] - 1)
    white.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
    white.GetPointData().GetScalars().Fill(int(fill_value))

    stencil_src = vtk.vtkPolyDataToImageStencil()
    stencil_src.SetInputData(poly_idx)
    stencil_src.SetOutputOrigin(0.0, 0.0, 0.0)
    stencil_src.SetOutputSpacing(1.0, 1.0, 1.0)
    stencil_src.SetOutputWholeExtent(0, size[0] - 1, 0, size[1] - 1, 0, size[2] - 1)
    stencil_src.Update()

    stencil = vtk.vtkImageStencil()
    stencil.SetInputData(white)
    stencil.SetStencilConnection(stencil_src.GetOutputPort())
    stencil.ReverseStencilOff()
    stencil.SetBackgroundValue(0)
    stencil.Update()
    masked = stencil.GetOutput()

    arr = numpy_support.vtk_to_numpy(masked.GetPointData().GetScalars())
    arr = arr.reshape(size[2], size[1], size[0]).astype(np.uint8)

    img = sitk.GetImageFromArray(arr)
    img.SetOrigin(origin)
    img.SetSpacing(spacing)
    img.SetDirection(direction)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sitk.WriteImage(img, str(output_path), useCompression=True)

    fg = int((arr > 0).sum())
    voxel_volume = float(spacing[0] * spacing[1] * spacing[2])
    log.info("Foreground voxels: %d (%.2f mm³), file size %.1f MB",
             fg, fg * voxel_volume,
             output_path.stat().st_size / 1e6)
    return {
        "output_path": str(output_path),
        "foreground_voxels": fg,
        "foreground_volume_mm3": round(fg * voxel_volume, 4),
        "fill_value": int(fill_value),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def stage_sample(ct_media_id: str, mesh_media_id: str, out_dir: Path,
                 slug: str, max_axis: int, margin_mm: float,
                 origin_convention: str, mesh_axis_perm: str,
                 ct_dtype: str, force_download: bool,
                 download_root: Path) -> dict:
    np, sitk, vtk, _, Image = _import_deps()
    client = MorphoSourceClient()

    log.info("Resolving metadata for CT %s and mesh %s",
             ct_media_id, mesh_media_id)
    ct_meta = _fetch_metadata(client, ct_media_id)
    mesh_meta = _fetch_metadata(client, mesh_media_id)
    if safe_first(ct_meta.get("visibility")).lower() != "open":
        raise RuntimeError(
            f"CT media {ct_media_id} visibility is "
            f"{safe_first(ct_meta.get('visibility'))!r} -- must be 'open' to download"
        )
    if safe_first(mesh_meta.get("visibility")).lower() != "open":
        raise RuntimeError(
            f"Mesh media {mesh_media_id} visibility is "
            f"{safe_first(mesh_meta.get('visibility'))!r} -- must be 'open' to download"
        )

    spacing = _voxel_spacing_from_meta(ct_meta)
    if spacing is None:
        raise RuntimeError(
            f"CT media {ct_media_id} does not expose voxel spacing in the "
            "MorphoSource API. Pass --spacing-xyz to override."
        )
    log.info("Voxel spacing (mm) from API: %s", spacing)

    ct_dir = download_root / f"morphosource-download-{ct_media_id}"
    mesh_dir = download_root / f"morphosource-download-{mesh_media_id}"

    ct_dl = _download_cached(ct_media_id, ct_dir, force=force_download)
    if not ct_dl.get("success"):
        raise RuntimeError(f"CT download failed: {ct_dl.get('error')}")
    mesh_dl = _download_cached(mesh_media_id, mesh_dir, force=force_download)
    if not mesh_dl.get("success"):
        raise RuntimeError(f"Mesh download failed: {mesh_dl.get('error')}")

    # Locate files.
    tiff_dir, tiff_files = _find_tiff_stack(ct_dir)
    mesh_path = _find_mesh(mesh_dir)

    # Sanity-check slice dims.
    nx, ny, dtype_name = _peek_tiff_dims(Image, tiff_files, np)
    nz = len(tiff_files)
    log.info("Native CT extent: %d x %d x %d voxels (%s); "
             "world: %.2f x %.2f x %.2f mm",
             nx, ny, nz, dtype_name,
             nx * spacing[0], ny * spacing[1], nz * spacing[2])

    # Read mesh bounds before any heavy lifting.
    poly, mesh_bounds = _read_mesh_bounds_and_poly(mesh_path)

    grid = _compute_grid(
        full_size_xyz=(nx, ny, nz),
        spacing_xyz=spacing,
        mesh_bounds=mesh_bounds,
        margin_mm=margin_mm,
        max_axis=max_axis,
        origin_convention=origin_convention,
        mesh_axis_perm=mesh_axis_perm,
    )

    # Apply the auto-detected axis permutation to the mesh polydata so the
    # voxelizer can use the same world frame as the CT.
    if grid.mesh_M_label != "+x+y+z":
        log.info("Applying signed permutation %s to mesh polydata",
                 grid.mesh_M_label)
        poly_oriented = _apply_signed_permutation_to_poly(np, vtk, poly, grid.mesh_M)
    else:
        poly_oriented = poly

    # Stream CT slices into a small numpy volume.
    volume_zyx = _stream_load_volume(tiff_files, grid)
    if ct_dtype == "uint8":
        # Robustly rescale to 0..255 using the 0.5/99.5 percentile range so
        # one or two voxels don't crush contrast. Useful for keeping the
        # NRRD small enough for GitHub even at ~512^3.
        lo = float(np.percentile(volume_zyx, 0.5))
        hi = float(np.percentile(volume_zyx, 99.5))
        rng = max(1e-6, hi - lo)
        scaled = np.clip((volume_zyx.astype(np.float32) - lo) * (255.0 / rng),
                         0.0, 255.0)
        volume_zyx = scaled.astype(np.uint8)
        log.info("Cast CT to uint8 using window [%.1f, %.1f]", lo, hi)
    elif ct_dtype == "int16":
        # Source TIFFs are usually uint16 in [0, 65535]; int16 keeps full
        # range up to 32767, so we cap above that.
        volume_zyx = np.clip(volume_zyx.astype(np.int32), -32768, 32767).astype(np.int16)
    elif ct_dtype != "uint16":
        raise ValueError(f"Unknown --ct-dtype: {ct_dtype!r}")

    # Write CT NRRD.
    ct_path = out_dir / f"{slug}_ct.nrrd"
    ct_img = _write_nrrd(np, sitk, volume_zyx,
                         spacing_xyz=grid.spacing_xyz,
                         origin_xyz=grid.origin_xyz,
                         output_path=ct_path)
    log.info("CT NRRD: size=%s spacing=%s origin=%s dtype=%s",
             ct_img.GetSize(), ct_img.GetSpacing(), ct_img.GetOrigin(),
             volume_zyx.dtype)

    # Voxelize mesh onto the same downsampled grid.
    gt_path = out_dir / f"{slug}_gt_labelmap.nrrd"
    vox_summary = _voxelize_onto_grid(poly_oriented, grid, gt_path, fill_value=1)

    # Provenance.
    provenance = {
        "tool": "stage_morphosource_sample.py",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ct_media": {
            "id": ct_media_id,
            "title": safe_first(ct_meta.get("title")),
            "taxonomy": safe_first(ct_meta.get("physical_object_taxonomy_name")),
            "physical_object_id": safe_first(ct_meta.get("physical_object_id")),
            "physical_object_title": safe_first(ct_meta.get("physical_object_title")),
            "doi": safe_first(ct_meta.get("doi")),
            "ark": safe_first(ct_meta.get("ark")),
            "visibility": safe_first(ct_meta.get("visibility")),
            "x_pixel_spacing_mm": spacing[0],
            "y_pixel_spacing_mm": spacing[1],
            "z_pixel_spacing_mm": spacing[2],
            "native_voxel_dims_xyz": [nx, ny, nz],
            "tiff_stack_root": str(tiff_dir),
        },
        "mesh_media": {
            "id": mesh_media_id,
            "title": safe_first(mesh_meta.get("title")),
            "media_parent_id": safe_first(mesh_meta.get("media_parent_id")),
            "doi": safe_first(mesh_meta.get("doi")),
            "ark": safe_first(mesh_meta.get("ark")),
            "visibility": safe_first(mesh_meta.get("visibility")),
            "file": str(mesh_path),
            "world_bounds_xyz_mm": list(mesh_bounds),
        },
        "outputs": {
            "ct_nrrd": {
                "path": str(ct_path),
                "size_bytes": ct_path.stat().st_size,
                "sha256": _sha256(ct_path),
                "voxel_dims_xyz": list(grid.size_xyz),
                "spacing_xyz_mm": list(grid.spacing_xyz),
                "origin_xyz_mm": list(grid.origin_xyz),
                "dtype": dtype_name,
            },
            "gt_labelmap_nrrd": {
                "path": str(gt_path),
                "size_bytes": gt_path.stat().st_size,
                "sha256": _sha256(gt_path),
                **vox_summary,
            },
        },
        "grid": {
            "max_axis_target": max_axis,
            "margin_mm": margin_mm,
            "stride": grid.stride,
            "origin_convention": grid.origin_convention,
            "crop_index_min": list(grid.crop_index_min),
            "crop_index_max": list(grid.crop_index_max),
            "mesh_axis_perm": grid.mesh_M_label,
            "mesh_axis_matrix": [list(row) for row in grid.mesh_M],
            "ct_dtype": ct_dtype,
        },
    }
    prov_path = out_dir / f"{slug}.provenance.json"
    prov_path.write_text(json.dumps(provenance, indent=2))
    log.info("Provenance: %s", prov_path)
    return provenance


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args():
    p = argparse.ArgumentParser(
        description="Download a MorphoSource (CT, mesh) pair and stage as "
                    "URL-loadable NRRD test assets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--ct-media-id", default="000011009",
                   help="MorphoSource media ID for the CT volume (TIFF z-stack).")
    p.add_argument("--mesh-media-id", default="000358663",
                   help="MorphoSource media ID for the surface mesh.")
    p.add_argument("--out-dir", default="data/sample",
                   help="Directory to stage the NRRDs in (committed to git).")
    p.add_argument("--slug", default="tuatara_skull_000358663",
                   help="File-name prefix for the staged outputs.")
    p.add_argument("--max-axis", type=int, default=384,
                   help="Maximum voxel count along any axis in the output. "
                        "Drives the downsample stride. Default keeps the "
                        "CT NRRD under GitHub's 100 MB hard limit.")
    p.add_argument("--margin-mm", type=float, default=2.0,
                   help="Padding around the mesh bbox before cropping (mm).")
    p.add_argument("--origin-convention", default="auto",
                   choices=["auto", "zero", "centered"],
                   help="How to align the CT TIFF stack to the mesh world frame.")
    p.add_argument("--mesh-axis-perm", default="auto",
                   help="Force a specific signed axis permutation on the mesh, "
                        "e.g. '+x+y+z' (identity) or '+x+z+y' (swap Y/Z). "
                        "Default 'auto' picks the best of 48 candidates.")
    p.add_argument("--ct-dtype", default="uint16",
                   choices=["uint16", "int16", "uint8"],
                   help="Output dtype for the CT NRRD. uint8 is much smaller "
                        "but lossy (8-bit window over the 0.5/99.5 percentile).")
    p.add_argument("--download-root", default="data",
                   help="Where to cache MorphoSource downloads (gitignored).")
    p.add_argument("--force-download", action="store_true",
                   help="Ignore download cache and refetch from MorphoSource.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    load_dotenv()
    if not os.environ.get("MORPHOSOURCE_API_KEY"):
        log.error("MORPHOSOURCE_API_KEY is not set. Cannot fetch media.")
        return 2

    t0 = time.time()
    try:
        stage_sample(
            ct_media_id=args.ct_media_id,
            mesh_media_id=args.mesh_media_id,
            out_dir=Path(args.out_dir),
            slug=args.slug,
            max_axis=args.max_axis,
            margin_mm=args.margin_mm,
            origin_convention=args.origin_convention,
            mesh_axis_perm=args.mesh_axis_perm,
            ct_dtype=args.ct_dtype,
            force_download=args.force_download,
            download_root=Path(args.download_root),
        )
    except Exception as exc:
        log.error("Staging failed: %s", exc, exc_info=True)
        return 1
    log.info("Done in %.1f s", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
