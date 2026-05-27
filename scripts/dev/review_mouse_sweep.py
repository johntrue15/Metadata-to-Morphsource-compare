#!/usr/bin/env python3
"""Review mouse sweep results vs Jetstream reference and export STLs.

For each successful impc_mouse sweep job:
  * compute Dice / IoU / coverage vs the Jetstream saturation reference
  * rank configs by a composite score (quality + efficiency)
  * export union + per-segment meshes as .stl for manual inspection

Usage::

    python scripts/dev/review_mouse_sweep.py
    python scripts/dev/review_mouse_sweep.py --export-all
    python scripts/dev/review_mouse_sweep.py --top-n 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

log = logging.getLogger("review_mouse_sweep")

SWEEP_RESULTS = REPO_ROOT / "paper_artifacts" / "sweep" / "sweep_results.jsonl"
JETSTREAM_REF = (
    REPO_ROOT / "paper_artifacts" / "mouse_skull_session_001" / "composite.nii.gz"
)
DEFAULT_OUT = REPO_ROOT / "paper_artifacts" / "mouse_review"


@dataclass
class RunReview:
    job_id: str
    status: str
    params: dict
    n_clicks: Optional[int]
    union_voxels: Optional[int]
    duration_s: Optional[float]
    stop_reason: Optional[str]
    dice: Optional[float]
    iou: Optional[float]
    pred_voxels: Optional[int]
    ref_voxels: Optional[int]
    coverage_vs_ref: Optional[float]  # |pred ∩ ref| / |ref|
    composite_score: Optional[float]
    output_dir: str
    labelmap_path: Optional[str]
    multilabel_path: Optional[str]


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            rows.append(json.loads(ln))
    return rows


def _resample_to_reference(moving_path: Path, fixed_img, *, binary: bool = True) -> np.ndarray:
    """Resample *moving_path* onto the grid of *fixed_img* (nearest neighbor)."""
    import SimpleITK as sitk

    moving = sitk.ReadImage(str(moving_path))
    if (moving.GetSize() == fixed_img.GetSize()
            and moving.GetSpacing() == fixed_img.GetSpacing()
            and moving.GetOrigin() == fixed_img.GetOrigin()
            and moving.GetDirection() == fixed_img.GetDirection()):
        arr = sitk.GetArrayFromImage(moving)
        return ((arr > 0) if binary else arr).astype(np.uint8)

    resampler = sitk.ResampleImageFilter()
    resampler.SetReferenceImage(fixed_img)
    resampler.SetInterpolator(sitk.sitkNearestNeighbor)
    resampler.SetDefaultPixelValue(0)
    resampled = resampler.Execute(moving)
    arr = sitk.GetArrayFromImage(resampled)
    return ((arr > 0) if binary else arr).astype(np.uint8)


def _load_binary_mask(path: Path) -> tuple[np.ndarray, object]:
    import SimpleITK as sitk

    img = sitk.ReadImage(str(path))
    arr = sitk.GetArrayViewFromImage(img)
    mask = (arr > 0).astype(np.uint8)
    return mask, img


def _dice_iou(pred: np.ndarray, ref: np.ndarray) -> tuple[float, float, int, int, int]:
    p = pred.astype(bool)
    r = ref.astype(bool)
    inter = int(np.logical_and(p, r).sum())
    n_p = int(p.sum())
    n_r = int(r.sum())
    dice = (2.0 * inter / (n_p + n_r)) if (n_p + n_r) else 0.0
    union = int(np.logical_or(p, r).sum())
    iou = (inter / union) if union else 0.0
    return dice, iou, n_p, n_r, inter


def _composite_score(*, dice: float, iou: float, coverage: float,
                     n_clicks: int, duration_s: float) -> float:
    """Higher is better. Weight quality over speed; penalise under-coverage."""
    quality = 0.55 * dice + 0.30 * iou + 0.15 * coverage
    click_penalty = min(n_clicks / 50.0, 1.0) * 0.08
    time_penalty = min(duration_s / 7200.0, 1.0) * 0.07
    return round(quality - click_penalty - time_penalty, 6)


def _labelmap_to_stl(labelmap_path: Path, out_stl: Path, *,
                     label_value: int = 1,
                     decimate: float = 0.0) -> dict:
    """Marching-cubes a binary (or single-label) mask to STL."""
    import SimpleITK as sitk
    import vtk
    from vtk.util import numpy_support

    img = sitk.ReadImage(str(labelmap_path))
    arr = sitk.GetArrayFromImage(img)
    if label_value == 0:
        binary = (arr > 0).astype(np.uint8)
    else:
        binary = (arr == label_value).astype(np.uint8)

    if binary.sum() == 0:
        return {"skipped": True, "reason": "empty_mask"}

    spacing = img.GetSpacing()
    origin = img.GetOrigin()
    # VTK expects z,y,x; SITK array is z,y,x already.
    vtk_img = vtk.vtkImageData()
    vtk_img.SetDimensions(binary.shape[2], binary.shape[1], binary.shape[0])
    vtk_img.SetSpacing(spacing[0], spacing[1], spacing[2])
    vtk_img.SetOrigin(origin[0], origin[1], origin[2])
    flat = binary.ravel(order="C")
    vtk_arr = numpy_support.numpy_to_vtk(flat, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
    vtk_img.GetPointData().SetScalars(vtk_arr)

    mc = vtk.vtkFlyingEdges3D()
    mc.SetInputData(vtk_img)
    mc.SetValue(0, 0.5)
    mc.Update()
    poly = mc.GetOutput()

    if decimate > 0 and poly.GetNumberOfPolys() > 0:
        dec = vtk.vtkDecimatePro()
        dec.SetInputData(poly)
        dec.SetTargetReduction(decimate)
        dec.PreserveTopologyOn()
        dec.Update()
        poly = dec.GetOutput()

    # Clean + compute normals for nicer viewing in MeshLab/Blender.
    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(poly)
    clean.Update()
    norms = vtk.vtkPolyDataNormals()
    norms.SetInputConnection(clean.GetOutputPort())
    norms.ConsistencyOn()
    norms.SplittingOff()
    norms.Update()
    poly = norms.GetOutput()

    out_stl.parent.mkdir(parents=True, exist_ok=True)
    writer = vtk.vtkSTLWriter()
    writer.SetFileName(str(out_stl))
    writer.SetInputData(poly)
    writer.Write()

    return {
        "skipped": False,
        "triangles": int(poly.GetNumberOfPolys()),
        "points": int(poly.GetNumberOfPoints()),
        "stl_path": str(out_stl),
    }


def _export_run_stls(job: dict, out_root: Path, *, per_segment: bool = True) -> dict:
    out_dir = Path(job["output_dir"])
    media_id = job.get("media_id", "impc_mouse")
    job_slug = job["job_id"]
    stl_root = out_root / "stl" / job_slug
    stl_root.mkdir(parents=True, exist_ok=True)

    labelmap = out_dir / f"{media_id}_nni_labelmap.nii.gz"
    multilabel = out_dir / f"{media_id}_nni_multilabel.nii.gz"
    exports: dict = {"job_id": job_slug, "union": None, "segments": []}

    if labelmap.exists():
        union_stl = stl_root / "union.stl"
        exports["union"] = _labelmap_to_stl(labelmap, union_stl, label_value=0)

    if per_segment and multilabel.exists():
        import SimpleITK as sitk

        arr = sitk.GetArrayFromImage(sitk.ReadImage(str(multilabel)))
        labels = sorted(int(v) for v in np.unique(arr) if v > 0)
        seg_dir = stl_root / "segments"
        for lbl in labels:
            seg_stl = seg_dir / f"segment_{lbl:03d}.stl"
            info = _labelmap_to_stl(multilabel, seg_stl, label_value=lbl)
            if not info.get("skipped"):
                exports["segments"].append({"label": lbl, **info})

    return exports


def review_runs(*, ref_path: Path = JETSTREAM_REF) -> list[RunReview]:
    if not ref_path.exists():
        raise FileNotFoundError(f"Jetstream reference missing: {ref_path}")

    import SimpleITK as sitk

    ref_img = sitk.ReadImage(str(ref_path))
    # Use the source volume grid as the comparison frame so cropped
    # Jetstream exports align correctly.
    vol_path = REPO_ROOT / ".local" / "impc_data" / "IMPC_sample_data.nrrd"
    grid_img = sitk.ReadImage(str(vol_path)) if vol_path.exists() else ref_img
    ref_mask = _resample_to_reference(ref_path, grid_img, binary=True)
    n_ref = int(ref_mask.sum())
    log.info("Reference %s resampled to volume grid: %d foreground voxels",
             ref_path.name, n_ref)

    reviews: list[RunReview] = []
    for row in _read_jsonl(SWEEP_RESULTS):
        if row.get("media_id") != "impc_mouse":
            continue

        review = RunReview(
            job_id=row["job_id"],
            status=row.get("status", "?"),
            params=row.get("params_override") or {},
            n_clicks=row.get("n_clicks"),
            union_voxels=row.get("union_voxels"),
            duration_s=row.get("duration_s"),
            stop_reason=row.get("stop_reason"),
            dice=None,
            iou=None,
            pred_voxels=None,
            ref_voxels=n_ref,
            coverage_vs_ref=None,
            composite_score=None,
            output_dir=row.get("output_dir") or "",
            labelmap_path=None,
            multilabel_path=None,
        )

        if row.get("status") != "success":
            reviews.append(review)
            continue

        out_dir = Path(row["output_dir"])
        labelmap = out_dir / "impc_mouse_nni_labelmap.nii.gz"
        multilabel = out_dir / "impc_mouse_nni_multilabel.nii.gz"
        review.labelmap_path = str(labelmap) if labelmap.exists() else None
        review.multilabel_path = str(multilabel) if multilabel.exists() else None

        if not labelmap.exists():
            reviews.append(review)
            continue

        pred_mask, _ = _load_binary_mask(labelmap)
        if pred_mask.shape != ref_mask.shape:
            pred_mask = _resample_to_reference(labelmap, grid_img, binary=True)
        dice, iou, n_p, n_r, inter = _dice_iou(pred_mask, ref_mask)
        coverage = (inter / n_r) if n_r else 0.0
        review.dice = round(dice, 6)
        review.iou = round(iou, 6)
        review.pred_voxels = n_p
        review.coverage_vs_ref = round(coverage, 6)
        review.composite_score = _composite_score(
            dice=dice, iou=iou, coverage=coverage,
            n_clicks=int(row.get("n_clicks") or 0),
            duration_s=float(row.get("duration_s") or 0),
        )
        reviews.append(review)

    return reviews


def _unique_param_groups(successful: list[RunReview]) -> list[RunReview]:
    """Collapse density duplicates — density had no effect on mouse runs."""
    seen: dict[str, RunReview] = {}
    for r in successful:
        key = json.dumps({
            "p": r.params.get("intensity_percentile"),
            "f": r.params.get("intensity_drop_floor_frac"),
        }, sort_keys=True)
        prev = seen.get(key)
        if prev is None or (r.composite_score or 0) > (prev.composite_score or 0):
            seen[key] = r
    return list(seen.values())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", default=str(DEFAULT_OUT))
    p.add_argument("--top-n", type=int, default=5,
                   help="Export STLs for the top N configs by composite score.")
    p.add_argument("--export-all", action="store_true",
                   help="Export STLs for every successful unique param group.")
    p.add_argument("--export-reference", action="store_true", default=True)
    p.add_argument("--no-export-reference", action="store_false",
                   dest="export_reference")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out_root = Path(args.out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    reviews = review_runs()
    successful = [r for r in reviews if r.status == "success" and r.dice is not None]
    successful.sort(key=lambda r: r.composite_score or 0, reverse=True)
    unique = _unique_param_groups(successful)
    unique.sort(key=lambda r: r.composite_score or 0, reverse=True)

    # Persist ranked table.
    report = {
        "reference": str(JETSTREAM_REF),
        "ref_voxels": successful[0].ref_voxels if successful else None,
        "n_successful": len(successful),
        "n_unique_param_groups": len(unique),
        "ranked_unique": [asdict(r) for r in unique],
        "all_runs": [asdict(r) for r in reviews],
    }
    report_path = out_root / "mouse_sweep_review.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Human-readable summary.
    lines = [
        "# Mouse sweep review vs Jetstream reference",
        "",
        f"Reference: `{JETSTREAM_REF.relative_to(REPO_ROOT)}`",
        f"Reference voxels: {successful[0].ref_voxels if successful else '?'}",
        "",
        "## Ranked unique configs (density collapsed)",
        "",
        "| Rank | p | floor | clicks | union | Dice | IoU | coverage | score | dur |",
        "|------|---|-------|--------|-------|------|-----|----------|-------|-----|",
    ]
    for i, r in enumerate(unique, 1):
        pctl = r.params.get("intensity_percentile")
        floor = r.params.get("intensity_drop_floor_frac")
        dur = f"{r.duration_s:.0f}s" if r.duration_s else "?"
        lines.append(
            f"| {i} | {pctl} | {floor} | {r.n_clicks} | {r.union_voxels:,} "
            f"| {r.dice:.4f} | {r.iou:.4f} | {r.coverage_vs_ref:.4f} "
            f"| {r.composite_score:.4f} | {dur} |"
        )
    lines += [
        "",
        "## Failed / timed out",
        "",
    ]
    for r in reviews:
        if r.status != "success":
            lines.append(f"- `{r.job_id}` — {r.status}")

    md_path = out_root / "mouse_sweep_review.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Console summary.
    print("\n=== Mouse sweep ranking (vs Jetstream reference) ===\n")
    if not unique:
        print("No successful runs with metrics.")
        return 1

    best = unique[0]
    print(f"BEST: p={best.params.get('intensity_percentile')} "
          f"floor={best.params.get('intensity_drop_floor_frac')}")
    print(f"  Dice={best.dice:.4f}  IoU={best.iou:.4f}  "
          f"coverage={best.coverage_vs_ref:.4f}")
    print(f"  clicks={best.n_clicks}  union={best.union_voxels:,}  "
          f"score={best.composite_score:.4f}  dur={best.duration_s:.0f}s")
    print()
    for i, r in enumerate(unique[:10], 1):
        print(f"  {i:2d}. p={r.params.get('intensity_percentile'):4.1f} "
              f"floor={r.params.get('intensity_drop_floor_frac')}  "
              f"dice={r.dice:.4f}  iou={r.iou:.4f}  "
              f"clicks={r.n_clicks}  score={r.composite_score:.4f}")

    # Export STLs.
    export_jobs = unique if args.export_all else unique[: max(1, args.top_n)]
    export_manifest: list[dict] = []

    if args.export_reference:
        ref_stl = out_root / "stl" / "_jetstream_reference" / "union.stl"
        log.info("Exporting Jetstream reference STL (resampled to volume grid)...")
        import SimpleITK as sitk

        vol_path = REPO_ROOT / ".local" / "impc_data" / "IMPC_sample_data.nrrd"
        grid_img = sitk.ReadImage(str(vol_path))
        ref_on_grid = out_root / "_jetstream_reference_on_volume.nii.gz"
        ref_arr = _resample_to_reference(JETSTREAM_REF, grid_img, binary=False)
        out_img = sitk.GetImageFromArray(ref_arr)
        out_img.CopyInformation(grid_img)
        sitk.WriteImage(out_img, str(ref_on_grid))
        ref_info = _labelmap_to_stl(ref_on_grid, ref_stl, label_value=0)
        export_manifest.append({"job_id": "_jetstream_reference", "union": ref_info})

    # Per-segment reference STLs from the resampled grid labelmap.
    ref_seg_dir = out_root / "stl" / "_jetstream_reference" / "segments"
    ref_on_grid_path = out_root / "_jetstream_reference_on_volume.nii.gz"
    if ref_on_grid_path.exists():
        import SimpleITK as sitk
        ref_arr = sitk.GetArrayFromImage(sitk.ReadImage(str(ref_on_grid_path)))
        for lbl in sorted(int(v) for v in np.unique(ref_arr) if v > 0):
            seg_stl = ref_seg_dir / f"segment_{lbl:03d}.stl"
            info = _labelmap_to_stl(ref_on_grid_path, seg_stl, label_value=lbl)
            if not info.get("skipped"):
                export_manifest.append({"job_id": "_jetstream_reference",
                                        "segment": lbl, **info})

    for r in export_jobs:
        row = next(x for x in _read_jsonl(SWEEP_RESULTS) if x["job_id"] == r.job_id)
        log.info("Exporting STLs for %s...", r.job_id)
        info = _export_run_stls(row, out_root)
        export_manifest.append(info)

    manifest_path = out_root / "stl_export_manifest.json"
    manifest_path.write_text(json.dumps(export_manifest, indent=2), encoding="utf-8")

    print(f"\nReports written to:\n  {report_path}\n  {md_path}")
    print(f"STLs written under:\n  {out_root / 'stl'}")
    print(f"\nOpen in MeshLab, Blender, Windows 3D Viewer, etc.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
