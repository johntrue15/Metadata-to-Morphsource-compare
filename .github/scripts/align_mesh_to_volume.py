"""
Translate a mesh into the world frame of a reference volume so subsequent
crop / voxelize / metrics operate on a meaningful spatial overlap.

Background
----------
MorphoSource derivative-mesh projects (e.g. project 000358382
"Colors of Skull Anatomy") frequently ship .ply / .stl files in the
modeller's local coordinate frame: the mesh is centered at the origin of
its own bbox instead of preserving the CT scanner's world coordinates.
The Felis catus pair (000362550 vs 000362581) puts the mesh at
[5,0,0]..[70,70,127] mm while the CT lives at [-80,-80,-138]..[0,0,0]:
zero world-space overlap. ``crop_around_mesh.py`` then dies with
"Mesh bbox does not intersect the reference volume."

When the two objects represent the same physical specimen (a cat's skull
in both cases) at the same scale & orientation, a pure translation
suffices. This script provides three alignment methods:

- ``centroid`` : translate so bbox centroids coincide. Always applied.
- ``auto``     : skip when bboxes already overlap by >50% of the smaller
                  bbox volume; otherwise apply ``centroid``.
- (extension)  : ``pca`` / ``icp`` could be added later for cases where
                  rotation is also wrong. For project 358382, centroid
                  alone has been sufficient.

Output: a transformed copy of the input mesh + a sidecar JSON summary.

Runs inside the nnInteractive venv (needs vtk + numpy + SimpleITK).

Usage::

    python align_mesh_to_volume.py \\
        --reference-volume /tmp/.../ct.nii.gz \\
        --mesh /tmp/.../mesh.stl \\
        --output /tmp/.../mesh_aligned.stl \\
        --method centroid \\
        --summary /tmp/.../mesh_aligned.align.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("align_mesh")


def _import_deps():
    try:
        import numpy as np
        import SimpleITK as sitk
        import vtk
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Run inside the nnInteractive venv.",
              file=sys.stderr)
        sys.exit(1)
    return np, sitk, vtk


def _mesh_reader_writer(suffix: str, vtk_):
    s = suffix.lower()
    if s == ".ply":
        return vtk_.vtkPLYReader, vtk_.vtkPLYWriter
    if s == ".stl":
        return vtk_.vtkSTLReader, vtk_.vtkSTLWriter
    if s == ".obj":
        return vtk_.vtkOBJReader, vtk_.vtkSTLWriter  # write STL fallback
    return None, None


def _read_mesh_bounds(mesh_path: Path, np_, vtk_):
    reader_cls, _ = _mesh_reader_writer(mesh_path.suffix, vtk_)
    if reader_cls is None:
        raise RuntimeError(f"Unsupported mesh format: {mesh_path.suffix}")
    reader = reader_cls()
    reader.SetFileName(str(mesh_path))
    reader.Update()
    poly = reader.GetOutput()
    b = poly.GetBounds()  # (xmin,xmax,ymin,ymax,zmin,zmax)
    mins = np_.array([b[0], b[2], b[4]])
    maxs = np_.array([b[1], b[3], b[5]])
    return mins, maxs, reader, poly


def _read_ct_bounds(volume_path: Path, np_, sitk_):
    if volume_path.is_dir():
        reader = sitk_.ImageSeriesReader()
        files = reader.GetGDCMSeriesFileNames(str(volume_path))
        if not files:
            raise RuntimeError(f"No DICOM files in {volume_path}")
        reader.SetFileNames(files)
        img = reader.Execute()
    else:
        img = sitk_.ReadImage(str(volume_path))
    size = img.GetSize()
    corners = []
    for i in (0, size[0] - 1):
        for j in (0, size[1] - 1):
            for k in (0, size[2] - 1):
                corners.append(img.TransformIndexToPhysicalPoint((i, j, k)))
    arr = np_.array(corners)
    return arr.min(axis=0), arr.max(axis=0)


def _overlap_frac(min_a, max_a, min_b, max_b, np_):
    inter_min = np_.maximum(min_a, min_b)
    inter_max = np_.minimum(max_a, max_b)
    inter = np_.maximum(inter_max - inter_min, 0.0).prod()
    vol_a = (max_a - min_a).prod()
    vol_b = (max_b - min_b).prod()
    return float(inter / max(min(vol_a, vol_b), 1e-9))


def align(mesh_path: Path, reference_volume: Path, output_path: Path,
          method: str = "centroid",
          summary_path: Path | None = None) -> dict:
    np_, sitk_, vtk_ = _import_deps()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("Reading mesh: %s", mesh_path)
    m_min, m_max, reader, poly = _read_mesh_bounds(mesh_path, np_, vtk_)
    m_center = (m_min + m_max) / 2.0
    log.info("Mesh world bbox: min=%s max=%s center=%s",
             m_min.tolist(), m_max.tolist(), m_center.tolist())

    log.info("Reading reference volume: %s", reference_volume)
    c_min, c_max = _read_ct_bounds(reference_volume, np_, sitk_)
    c_center = (c_min + c_max) / 2.0
    log.info("CT world bbox: min=%s max=%s center=%s",
             c_min.tolist(), c_max.tolist(), c_center.tolist())

    overlap_before = _overlap_frac(m_min, m_max, c_min, c_max, np_)
    log.info("Pre-alignment overlap fraction: %.3f", overlap_before)

    summary = {
        "method": method,
        "applied": False,
        "ct_bbox": {"min": c_min.tolist(), "max": c_max.tolist(),
                     "center": c_center.tolist()},
        "mesh_bbox_before": {"min": m_min.tolist(), "max": m_max.tolist(),
                              "center": m_center.tolist()},
        "overlap_frac_before": overlap_before,
        "output_path": str(output_path),
        "mesh_path": str(mesh_path),
        "reference_volume": str(reference_volume),
    }

    if method == "auto" and overlap_before > 0.5:
        log.info("auto mode + overlap %.3f > 0.5 -> pass-through (copy original)",
                 overlap_before)
        import shutil
        shutil.copy2(mesh_path, output_path)
        if summary_path:
            summary_path.write_text(json.dumps(summary, indent=2))
        return summary

    effective = "centroid" if method in {"centroid", "auto"} else method
    if effective != "centroid":
        return {**summary, "error": f"unknown alignment method: {method}"}

    translation = (c_center - m_center).astype(float)
    log.info("Applying centroid translation: %s mm", translation.tolist())

    tf = vtk_.vtkTransform()
    tf.Translate(float(translation[0]), float(translation[1]),
                 float(translation[2]))
    tff = vtk_.vtkTransformPolyDataFilter()
    tff.SetTransform(tf)
    tff.SetInputData(poly)
    tff.Update()

    _, writer_cls = _mesh_reader_writer(output_path.suffix, vtk_)
    if writer_cls is None:
        raise RuntimeError(f"Unsupported output mesh format: {output_path.suffix}")
    writer = writer_cls()
    writer.SetFileTypeToBinary()
    writer.SetFileName(str(output_path))
    writer.SetInputData(tff.GetOutput())
    writer.Write()

    out_poly = tff.GetOutput()
    b = out_poly.GetBounds()
    after_min = np_.array([b[0], b[2], b[4]])
    after_max = np_.array([b[1], b[3], b[5]])
    overlap_after = _overlap_frac(after_min, after_max, c_min, c_max, np_)
    log.info("Wrote %s (%d points, %d cells); post-alignment overlap=%.3f",
             output_path, out_poly.GetNumberOfPoints(),
             out_poly.GetNumberOfCells(), overlap_after)

    summary.update({
        "applied": True,
        "translation": translation.tolist(),
        "mesh_bbox_after": {
            "min": after_min.tolist(), "max": after_max.tolist(),
            "center": ((after_min + after_max) / 2.0).tolist(),
        },
        "overlap_frac_after": overlap_after,
        "output_n_points": int(out_poly.GetNumberOfPoints()),
        "output_n_cells": int(out_poly.GetNumberOfCells()),
    })

    if summary_path:
        summary_path.write_text(json.dumps(summary, indent=2))
        log.info("Summary -> %s", summary_path)
    return summary


def _parse_args():
    p = argparse.ArgumentParser(
        description="Align a mesh to a reference volume's world frame.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--reference-volume", required=True,
                   help="CT volume (NIfTI/NRRD/MHA) or DICOM series dir")
    p.add_argument("--mesh", required=True,
                   help="Input mesh (PLY/STL/OBJ)")
    p.add_argument("--output", required=True,
                   help="Output mesh path (same format inferred from suffix)")
    p.add_argument("--method", default="centroid",
                   choices=["centroid", "auto"],
                   help="Alignment method (default: centroid)")
    p.add_argument("--summary", default="",
                   help="Path for JSON summary (default: <output>.align.json)")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    args = _parse_args()
    output = Path(args.output)
    summary_path = (Path(args.summary)
                    if args.summary
                    else output.with_suffix("").with_suffix(".align.json"))
    try:
        result = align(
            mesh_path=Path(args.mesh),
            reference_volume=Path(args.reference_volume),
            output_path=output,
            method=args.method,
            summary_path=summary_path,
        )
    except Exception as exc:
        log.error("Alignment failed: %s", exc, exc_info=True)
        return 1
    if "error" in result:
        log.error("Alignment returned error: %s", result["error"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
