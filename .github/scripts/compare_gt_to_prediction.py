#!/usr/bin/env python3
"""Compare a ground-truth mesh (or labelmap) against an exported prediction volume.

This is the *segmentation-free* comparison module. It takes a prediction
labelmap that was already produced by a clicks-to-completion run (e.g. the
``artifacts/composite.nii.gz`` exported by ``slicer_remote_bright_seed.py``)
and scores it against a MorphoSource ground-truth ``.ply`` mesh — without
running nnInteractive at all.

It is pure orchestration over three existing standalone modules:

    1. mesh_ct_alignment.align_mesh_to_reference_volume
         — orient the GT mesh into the prediction's world frame
           (48 signed axis permutations x origin conventions).
    2. voxelize_mesh_vtk.voxelize
         — rasterize the aligned mesh onto the prediction's EXACT voxel grid
           (same size/spacing/origin/direction), so the two labelmaps are
           directly comparable.
    3. segmentation_metrics.compare_labelmaps / render_overlay_panel
         — Dice / IoU / Hausdorff / volume agreement + a 3x3 overlay panel.

Because step 2 voxelizes onto the prediction's own grid, you can run the
clicks-to-completion segmentation FIRST and only voxelize the GT here, after
you have a volume to compare against.

Inputs
------
--prediction       Exported prediction labelmap (NIfTI/NRRD). Defines the grid.
--gt-mesh          GT surface mesh (.ply/.stl/.obj) to voxelize, OR
--gt-labelmap      A GT labelmap already voxelized onto the prediction grid
                   (skips alignment + voxelization).
--reference-volume (optional) CT volume for the overlay's grayscale background.
                   Defaults to the prediction file if omitted.
--output-dir       Where to write gt_voxelized.nii.gz, metrics.json, overlay.png.

Run inside the nnInteractive venv ($NNINTERACTIVE_HOME/bin/python) so vtk +
SimpleITK + matplotlib are available::

    "$NNINTERACTIVE_HOME/bin/python" .github/scripts/compare_gt_to_prediction.py \
        --prediction runs/<slug>/artifacts/composite.nii.gz \
        --gt-mesh data/morphosource-download-000691954/mesh.ply \
        --reference-volume data/sample/crotalus_skull_000445108_ct.nrrd \
        --output-dir runs/<slug>/compare

Exit codes: 0 success, 1 on any error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

log = logging.getLogger("compare_gt")


def _align_gt_mesh(mesh_path: Path, reference_volume: Path,
                   output_dir: Path, *, mesh_axis_perm: str,
                   min_overlap_ratio: float,
                   force_search: bool = True) -> dict:
    """Orient the GT mesh into the prediction/CT world frame.

    Returns the alignment summary dict from mesh_ct_alignment. On success the
    aligned mesh path is under ``alignment["output_path"]``.

    With ``force_search`` (default), the full 48 signed-axis-permutation x
    origin-convention search is always run and the BEST-overlap orientation
    is chosen. Without it, ``align_mesh_to_reference_volume`` short-circuits on
    the first identity match above ``min_overlap_ratio`` — which silently
    accepts a mis-registered axis order when the mesh's long axis doesn't line
    up with the CT's long axis (observed on Crotalus: identity overlap 0.44 but
    the mesh y-extent spilled far outside the grid).
    """
    import mesh_ct_alignment as mca

    aligned = output_dir / f"gt_aligned{mesh_path.suffix.lower()}"
    summary = mca.align_mesh_to_reference_volume(
        mesh_path=mesh_path,
        volume_path=reference_volume,
        output_mesh_path=aligned,
        min_overlap_ratio=min_overlap_ratio,
        mesh_axis_perm=mesh_axis_perm,
        force=force_search,
    )
    return summary


def compare(
    prediction: Path,
    output_dir: Path,
    *,
    gt_mesh: Optional[Path] = None,
    gt_labelmap: Optional[Path] = None,
    reference_volume: Optional[Path] = None,
    mesh_axis_perm: str = "auto",
    min_overlap_ratio: float = 0.05,
    force_register: bool = True,
    compute_surface: bool = True,
    overlay: bool = True,
) -> dict:
    """Score a prediction labelmap against a GT mesh (or labelmap).

    The GT mesh is voxelized onto the prediction's exact grid so the two
    labelmaps share geometry, then segmentation_metrics scores them.
    """
    import voxelize_mesh_vtk
    import segmentation_metrics as sm

    prediction = Path(prediction)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not prediction.exists():
        return {"success": False, "stage": "locate_prediction",
                "error": f"prediction not found: {prediction}"}

    result: dict = {
        "success": False,
        "prediction": str(prediction),
        "output_dir": str(output_dir),
    }

    # ---- 1. Resolve the GT labelmap on the prediction's grid ----
    if gt_labelmap is not None:
        gt_labelmap = Path(gt_labelmap)
        if not gt_labelmap.exists():
            return {"success": False, "stage": "locate_gt_labelmap",
                    "error": f"gt-labelmap not found: {gt_labelmap}"}
        log.info("Using pre-voxelized GT labelmap: %s", gt_labelmap)
        result["gt_labelmap"] = str(gt_labelmap)
        result["gt_source"] = "labelmap"
    else:
        if gt_mesh is None:
            return {"success": False, "stage": "args",
                    "error": "provide either --gt-mesh or --gt-labelmap"}
        gt_mesh = Path(gt_mesh)
        if not gt_mesh.exists():
            return {"success": False, "stage": "locate_gt_mesh",
                    "error": f"gt-mesh not found: {gt_mesh}"}

        # Voxelize onto the PREDICTION's grid so geometry matches exactly.
        # Align the mesh into that frame first.
        log.info("Aligning GT mesh to prediction grid: %s", gt_mesh)
        align_summary = _align_gt_mesh(
            gt_mesh, prediction, output_dir,
            mesh_axis_perm=mesh_axis_perm,
            min_overlap_ratio=min_overlap_ratio,
            force_search=force_register,
        )
        log.info("Alignment chosen: axes=%s origin=%s overlap=%s",
                 align_summary.get("mesh_M_label"),
                 align_summary.get("origin_convention"),
                 align_summary.get("overlap_ratio"))
        result["alignment"] = align_summary
        if "error" in align_summary:
            result.update(stage="align_mesh")
            return result
        aligned_mesh = Path(align_summary.get("output_path", gt_mesh))

        gt_labelmap = output_dir / "gt_voxelized.nii.gz"
        log.info("Voxelizing aligned GT mesh onto prediction grid -> %s",
                 gt_labelmap)
        try:
            vox_summary = voxelize_mesh_vtk.voxelize(
                reference_volume=prediction,
                mesh_path=aligned_mesh,
                output_path=gt_labelmap,
                fill_value=1,
                summary_path=output_dir / "gt_voxelized.voxelize.json",
            )
        except Exception as exc:
            log.exception("Voxelization failed")
            result.update(stage="voxelize", error=repr(exc))
            return result
        result["voxelize"] = vox_summary
        result["gt_labelmap"] = str(gt_labelmap)
        result["gt_source"] = "mesh"
        if not vox_summary.get("foreground_voxels"):
            result.update(
                stage="voxelize",
                error="GT voxelization produced 0 foreground voxels; "
                      "mesh/CT frames likely incompatible (check alignment).",
            )
            return result

    # ---- 2. Score prediction vs GT labelmap ----
    log.info("Scoring prediction vs GT labelmap")
    try:
        metrics = sm.compare_labelmaps(
            str(prediction), str(gt_labelmap),
            compute_surface_distances=compute_surface,
        )
    except Exception as exc:
        log.exception("Metric computation failed")
        result.update(stage="metrics", error=repr(exc))
        return result

    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics.to_dict(), indent=2, default=str))
    result["metrics"] = metrics.to_dict()
    result["metrics_path"] = str(metrics_path)
    log.info("Dice=%.4f IoU=%.4f precision=%.4f recall=%.4f",
             metrics.dice, metrics.iou, metrics.precision, metrics.recall)

    # ---- 3. Overlay panel ----
    if overlay:
        bg = Path(reference_volume) if reference_volume else prediction
        overlay_path = output_dir / "overlay.png"
        try:
            out = sm.render_overlay_panel(
                str(bg), str(prediction), str(gt_labelmap), str(overlay_path),
                title=f"Dice={metrics.dice:.3f}  IoU={metrics.iou:.3f}",
            )
            if out:
                result["overlay_path"] = out
                log.info("Wrote overlay: %s", out)
        except Exception as exc:
            log.warning("Overlay rendering failed: %s", exc)

    result["success"] = True
    return result


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare a GT mesh/labelmap against an exported "
                    "prediction volume (no segmentation).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--prediction", required=True,
                   help="Exported prediction labelmap (NIfTI/NRRD). "
                        "Defines the comparison grid.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--gt-mesh",
                   help="GT surface mesh (.ply/.stl/.obj) to voxelize.")
    g.add_argument("--gt-labelmap",
                   help="GT labelmap already on the prediction grid "
                        "(skips align + voxelize).")
    p.add_argument("--reference-volume", default="",
                   help="CT volume for overlay background "
                        "(defaults to the prediction file).")
    p.add_argument("--output-dir", required=True,
                   help="Directory for gt_voxelized.nii.gz, metrics.json, "
                        "overlay.png.")
    p.add_argument("--mesh-axis-perm", default="auto",
                   help="Force a signed axis permutation (e.g. +x-z+y) or "
                        "'auto' to search all 48 (default: auto).")
    p.add_argument("--min-overlap-ratio", type=float, default=0.05,
                   help="Minimum mesh/CT bbox overlap to accept an alignment "
                        "(default: 0.05).")
    p.add_argument("--trust-identity-orientation", action="store_true",
                   help="Skip the full orientation search and accept the "
                        "identity axis order if it clears --min-overlap-ratio. "
                        "By default the module searches all 48 signed axis "
                        "permutations x origin conventions and picks the best.")
    p.add_argument("--no-surface", action="store_true",
                   help="Skip the (expensive) Hausdorff/surface metrics.")
    p.add_argument("--no-overlay", action="store_true",
                   help="Skip rendering the overlay PNG.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    args = _parse_args(argv)

    t0 = time.time()
    result = compare(
        prediction=Path(args.prediction),
        output_dir=Path(args.output_dir),
        gt_mesh=Path(args.gt_mesh) if args.gt_mesh else None,
        gt_labelmap=Path(args.gt_labelmap) if args.gt_labelmap else None,
        reference_volume=(Path(args.reference_volume)
                          if args.reference_volume else None),
        mesh_axis_perm=args.mesh_axis_perm,
        min_overlap_ratio=args.min_overlap_ratio,
        force_register=not args.trust_identity_orientation,
        compute_surface=not args.no_surface,
        overlay=not args.no_overlay,
    )

    # Always emit a machine-readable result summary.
    summary_path = Path(args.output_dir) / "compare_result.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(result, indent=2, default=str))

    if not result.get("success"):
        log.error("Comparison failed at stage=%s: %s",
                  result.get("stage"), result.get("error"))
        return 1

    m = result["metrics"]
    print(json.dumps({
        "dice": m["dice"],
        "iou": m["iou"],
        "precision": m["precision"],
        "recall": m["recall"],
        "volume_mm3_pred": m["volume_mm3_pred"],
        "volume_mm3_gt": m["volume_mm3_gt"],
        "metrics_path": result["metrics_path"],
        "overlay_path": result.get("overlay_path"),
    }, indent=2))
    log.info("Done in %.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
