#!/usr/bin/env python3
"""Convert a labelmap NIfTI (union or multi-label) to a web-friendly .glb."""

from __future__ import annotations

import argparse
import colorsys
import json
import sys
from pathlib import Path

import numpy as np


def _label_color(label: int) -> np.ndarray:
    """Stable distinct RGBA per label id (1-based)."""
    hue = (label * 0.61803398875) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.72, 0.95)
    return np.array([int(r * 255), int(g * 255), int(b * 255), 220], dtype=np.uint8)


def _vtk_binary_surface(arr: np.ndarray, spacing, origin, decimate: float):
    import vtk
    from vtk.util import numpy_support

    if arr.sum() == 0:
        return None
    vtk_img = vtk.vtkImageData()
    vtk_img.SetDimensions(arr.shape[2], arr.shape[1], arr.shape[0])
    vtk_img.SetSpacing(float(spacing[0]), float(spacing[1]), float(spacing[2]))
    vtk_img.SetOrigin(float(origin[0]), float(origin[1]), float(origin[2]))
    flat = arr.astype(np.uint8).ravel(order="C")
    vtk_arr = numpy_support.numpy_to_vtk(flat, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
    vtk_img.GetPointData().SetScalars(vtk_arr)

    mc = vtk.vtkFlyingEdges3D()
    mc.SetInputData(vtk_img)
    mc.SetValue(0, 0.5)
    mc.Update()
    poly = mc.GetOutput()
    if poly.GetNumberOfPolys() == 0:
        return None

    if decimate > 0:
        dec = vtk.vtkDecimatePro()
        dec.SetInputData(poly)
        dec.SetTargetReduction(min(max(decimate, 0.0), 0.99))
        dec.PreserveTopologyOn()
        dec.Update()
        poly = dec.GetOutput()

    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(poly)
    clean.Update()
    norms = vtk.vtkPolyDataNormals()
    norms.SetInputConnection(clean.GetOutputPort())
    norms.ConsistencyOn()
    norms.SplittingOff()
    norms.Update()
    return norms.GetOutput()


def _poly_to_trimesh(poly, trimesh, numpy_support):
    import vtk

    pts = numpy_support.vtk_to_numpy(poly.GetPoints().GetData())
    cells = poly.GetPolys()
    cells.InitTraversal()
    faces = []
    id_list = vtk.vtkIdList()
    while cells.GetNextCell(id_list):
        if id_list.GetNumberOfIds() == 3:
            faces.append([id_list.GetId(i) for i in range(3)])
    if not faces:
        return None
    return trimesh.Trimesh(vertices=pts, faces=np.asarray(faces, dtype=np.int64), process=True)


def _load_nifti(path: Path):
    try:
        import SimpleITK as sitk

        img = sitk.ReadImage(str(path))
        arr = sitk.GetArrayFromImage(img)  # z, y, x
        spacing = img.GetSpacing()  # x, y, z
        origin = img.GetOrigin()
        return arr, spacing, origin
    except ImportError:
        import nibabel as nib

        img = nib.load(str(path))
        arr = np.asanyarray(img.dataobj)
        zooms = img.header.get_zooms()[:3]
        spacing = (float(zooms[0]), float(zooms[1]), float(zooms[2]))
        origin = tuple(float(x) for x in img.affine[:3, 3])
        return arr, spacing, origin


def convert(labelmap: Path, out_glb: Path, *, decimate: float = 0.85,
            per_segment: bool = True, max_segments: int = 0) -> dict:
    import trimesh
    from vtk.util import numpy_support

    arr, spacing, origin = _load_nifti(labelmap)
    labels = sorted(int(v) for v in np.unique(arr) if int(v) > 0)
    if not labels:
        raise SystemExit(f"empty labelmap: {labelmap}")
    if max_segments > 0:
        labels = labels[:max_segments]

    scene = trimesh.Scene()
    stats = {"labels": [], "triangles": 0, "meshes": 0}

    if not per_segment or len(labels) == 1:
        binary = (arr > 0).astype(np.uint8)
        poly = _vtk_binary_surface(binary, spacing, origin, decimate)
        if poly is None:
            raise SystemExit("no surface generated")
        mesh = _poly_to_trimesh(poly, trimesh, numpy_support)
        if mesh is None:
            raise SystemExit("mesh conversion failed")
        mesh.visual.vertex_colors = _label_color(1)
        scene.add_geometry(mesh, node_name="segmentation")
        stats["labels"] = [0 if len(labels) > 1 else labels[0]]
        stats["triangles"] = int(mesh.faces.shape[0])
        stats["meshes"] = 1
    else:
        for lab in labels:
            binary = (arr == lab).astype(np.uint8)
            poly = _vtk_binary_surface(binary, spacing, origin, decimate)
            if poly is None:
                continue
            mesh = _poly_to_trimesh(poly, trimesh, numpy_support)
            if mesh is None:
                continue
            rgba = _label_color(lab)
            mesh.visual.vertex_colors = np.tile(rgba, (len(mesh.vertices), 1))
            scene.add_geometry(mesh, node_name=f"Segment_{lab}")
            stats["labels"].append(lab)
            stats["triangles"] += int(mesh.faces.shape[0])
            stats["meshes"] += 1

    if stats["meshes"] == 0:
        raise SystemExit("no meshes exported")

    out_glb.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(out_glb))
    stats["glb_bytes"] = out_glb.stat().st_size
    stats["glb_path"] = str(out_glb)
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--decimate", type=float, default=0.85,
                   help="VTK decimation fraction 0–0.99 (default 0.85)")
    p.add_argument("--union-only", action="store_true",
                   help="single mesh for all labels > 0")
    p.add_argument("--max-segments", type=int, default=0)
    p.add_argument("--manifest", type=Path, default=None)
    args = p.parse_args(argv)

    stats = convert(
        args.input, args.output,
        decimate=args.decimate,
        per_segment=not args.union_only,
        max_segments=args.max_segments,
    )
    print(json.dumps(stats, indent=2))
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
