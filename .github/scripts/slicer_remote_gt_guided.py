#!/usr/bin/env python3
"""GT-guided nnInteractive click loop on a remote Jetstream Slicer server.

Uses a mesh-voxelized labelmap (same grid as the loaded CT) as the
teacher: each step exports the live segmentation, scores Dice locally,
picks the next click in the largest false-negative region, and prompts
nnInteractive on the server.

This is the per-scan training driver for ``metadata_to_morphsource.seg_train``:
logged runs can be fed into ``seg_train round`` to train a student model.

Usage (Mac driver, Slicer + nnInteractive on Jetstream)::

    set -a && source .env && set +a
    export SLICER_WEBSERVER_URL=https://http-149-165-155-127-2016.proxy-js2-iu.exosphere.app/

    # 1) Ensure GT labelmap exists (GPU host or Mac override):
    make stage-sample-gt

    python3 .github/scripts/slicer_remote_gt_guided.py \\
        --volume tuatara_skull_000358663_ct \\
        --gt-path data/sample/tuatara_skull_000358663_gt_labelmap.nrrd \\
        --ct-path data/sample/tuatara_skull_000358663_ct.nrrd \\
        --max-steps 50 \\
        --paper-tag tuatara_skull_v1 \\
        --out-dir runs/tuatara_gt_guided
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from run_telemetry import (  # noqa: E402
    CAPTURE_REMOTE_ENV_SRC,
    EXPORT_SEGMENTATION_SRC,
    HASH_ACTIVE_VOLUME_SRC,
    RunLogger,
)
from slicer_remote_bright_seed import (  # noqa: E402
    ENABLE_VISIBILITY_SRC,
    RESET_SEGMENTATION_SRC,
    SET_ACTIVE_VOLUME_SRC_TEMPLATE,
    post_python,
    _read_url,
)
from replay_session import APPLY_CLICK_SRC_TEMPLATE  # noqa: E402

from metadata_to_morphsource.seg_train.gt_guided_clicker import (  # noqa: E402
    pick_next_click,
    mask_from_sitk_array,
)


def _load_gt_mask(gt_path: Path) -> tuple:
    import SimpleITK as sitk
    import numpy as np

    gt_img = sitk.ReadImage(str(gt_path))
    gt_arr = sitk.GetArrayFromImage(gt_img)
    spacing = tuple(float(s) for s in gt_img.GetSpacing())
    return mask_from_sitk_array(gt_arr), spacing, gt_arr.shape


def _decode_composite_b64(export: dict, out_path: Path) -> Path:
    comp = export.get("composite") or {}
    b64 = comp.get("data_b64")
    if not b64:
        raise RuntimeError(f"export missing composite: {export.get('status')}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(b64))
    return out_path


def _pred_mask_from_export(export: dict, gt_shape: tuple, tmp_dir: Path) -> "np.ndarray":
    import SimpleITK as sitk
    import numpy as np

    comp_path = _decode_composite_b64(export, tmp_dir / "composite.nii.gz")
    pred_img = sitk.ReadImage(str(comp_path))
    pred_arr = sitk.GetArrayFromImage(pred_img)
    if tuple(pred_arr.shape) != tuple(gt_shape):
        # Union per-segment exports if composite empty/mismatched
        pred = np.zeros(gt_shape, dtype=bool)
        for seg in export.get("per_segment") or []:
            sb64 = seg.get("data_b64")
            if not sb64:
                continue
            sp = tmp_dir / seg.get("filename", "seg.nii.gz")
            sp.write_bytes(base64.b64decode(sb64))
            a = sitk.GetArrayFromImage(sitk.ReadImage(str(sp)))
            if a.shape == gt_shape:
                pred |= (a > 0)
        return pred
    return mask_from_sitk_array(pred_arr)


def _dice_np(pred, gt) -> float:
    inter = (pred & gt).sum()
    denom = int(pred.sum()) + int(gt.sum())
    return float(2.0 * inter / denom) if denom else 1.0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--volume", required=True, help="Slicer scalar volume node name")
    p.add_argument("--gt-path", type=Path, required=True,
                   help="Mesh-voxelized GT labelmap NRRD (same grid as CT)")
    p.add_argument("--ct-path", type=Path, default=None,
                   help="Reference CT NRRD (provenance only)")
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--target-dice", type=float, default=0.92,
                   help="Stop early when composite Dice reaches this value")
    p.add_argument("--no-negative-clicks", action="store_true")
    p.add_argument("--reset-first", action="store_true")
    p.add_argument("--no-new-segment-per-click", action="store_true",
                   help="Refine same segment instead of one segment per click")
    p.add_argument("--paper-tag", default="gt_guided")
    p.add_argument("--run-id", default="")
    p.add_argument("--out-dir", type=Path,
                   default=Path("runs") / time.strftime("gt_guided_%Y%m%d_%H%M%S"))
    p.add_argument("--append-ledger", type=Path, default=None,
                   help="Append final episode to seg_train ledger.jsonl")
    args = p.parse_args(argv)

    if not args.gt_path.exists():
        print(f"ERROR: GT not found: {args.gt_path}", file=sys.stderr)
        print("Run: make stage-sample-gt", file=sys.stderr)
        return 2

    base_url = _read_url()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    gt_mask, gt_spacing, gt_shape = _load_gt_mask(args.gt_path)

    logger = RunLogger.start(
        root=args.out_dir,
        args={k: (str(v) if isinstance(v, Path) else v)
              for k, v in vars(args).items()},
        label=args.paper_tag,
    )
    logger.log("=== GT-guided remote segmentation ===")
    logger.log(f"server    : {base_url}")
    logger.log(f"volume    : {args.volume}")
    logger.log(f"gt_path   : {args.gt_path}")
    logger.log(f"gt_shape  : {gt_shape}")
    logger.log(f"max_steps : {args.max_steps}")

    try:
        remote_env = post_python(base_url, CAPTURE_REMOTE_ENV_SRC, timeout=60)
        logger.record_remote_env(remote_env)
        logger.log(f"nnInteractive: {remote_env.get('nninteractive_version')}")
    except Exception as exc:
        logger.log(f"remote env capture failed: {exc!r}")

    r = post_python(
        base_url,
        SET_ACTIVE_VOLUME_SRC_TEMPLATE.format(target_name=args.volume),
        timeout=20,
    )
    if r.get("status") != "ok":
        logger.finalize(stop_reason={"reason": "volume_not_found", "details": r})
        return 3

    if args.reset_first:
        post_python(base_url, RESET_SEGMENTATION_SRC, timeout=120)
    post_python(base_url, ENABLE_VISIBILITY_SRC, timeout=30)

    vol_meta = post_python(base_url, HASH_ACTIVE_VOLUME_SRC, timeout=120)
    logger.record_inputs(vol_meta)

    history = []
    stop_reason = None
    new_seg = not args.no_new_segment_per_click

    with tempfile.TemporaryDirectory(prefix="gt_guided_") as tmp:
        tmp_path = Path(tmp)
        pred = __import__("numpy").zeros(gt_shape, dtype=bool)

        for step in range(args.max_steps):
            export = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=300)
            if export.get("status") == "no_segmentation":
                pred = __import__("numpy").zeros(gt_shape, dtype=bool)
            elif export.get("status") != "ok" and not export.get("composite"):
                logger.log(f"export failed: {export}")
                stop_reason = {"reason": "export_failed", "step": step}
                break
            else:
                try:
                    pred = _pred_mask_from_export(export, gt_shape, tmp_path)
                except Exception as exc:
                    logger.log(f"mask decode failed: {exc!r}")
                    stop_reason = {"reason": "mask_decode", "error": repr(exc)}
                    break

            dice = _dice_np(pred, gt_mask)
            plan = pick_next_click(
                pred, gt_mask,
                allow_negative=not args.no_negative_clicks,
            )
            logger.log(
                f"step {step:02d}  dice={dice:.4f}  "
                f"fn={plan.fn_voxels if plan else 0:,}  "
                f"fp={plan.fp_voxels if plan else 0:,}"
            )
            history.append({
                "step": step,
                "dice": round(dice, 6),
                "plan": None if plan is None else {
                    "i": plan.i, "j": plan.j, "k": plan.k,
                    "positive": plan.positive,
                    "reason": plan.reason,
                },
            })
            logger.event("gt_guided_step", step=step, dice=dice,
                         plan=history[-1]["plan"])

            if dice >= args.target_dice:
                stop_reason = {"reason": "target_dice", "dice": dice}
                break
            if plan is None:
                stop_reason = {"reason": "masks_agree", "dice": dice}
                break

            click_src = APPLY_CLICK_SRC_TEMPLATE.format(
                i=plan.i, j=plan.j, k=plan.k,
                click_positive=str(plan.positive).lower(),
                make_new_segment=str((new_seg and step > 0).lower()),
            )
            cr = post_python(base_url, click_src, timeout=240)
            if cr.get("status") != "ok":
                logger.log(f"click failed: {cr}")
                stop_reason = {"reason": "click_failed", "step": step, "details": cr}
                break
            history[-1]["click"] = {
                "delta": cr.get("delta"),
                "voxels_after": cr.get("voxels_after"),
            }

        # Final export + metrics
        export = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=300)
        final_pred_path = args.out_dir / "prediction_composite.nii.gz"
        try:
            _decode_composite_b64(export, final_pred_path)
            from segmentation_metrics import compare_labelmaps
            metrics = compare_labelmaps(
                str(final_pred_path), str(args.gt_path),
                compute_surface_distances=True,
            )
            metrics_dict = metrics.to_dict() if hasattr(metrics, "to_dict") else dict(metrics)
        except Exception as exc:
            metrics_dict = {"error": repr(exc)}

        summary = {
            "volume": args.volume,
            "gt_path": str(args.gt_path),
            "ct_path": str(args.ct_path) if args.ct_path else "",
            "steps_run": len(history),
            "stop_reason": stop_reason,
            "history": history,
            "final_metrics": metrics_dict,
        }
        (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        logger.log(f"Final Dice: {metrics_dict.get('dice', metrics_dict)}")
        logger.finalize(stop_reason=stop_reason or {"reason": "completed"})

        if args.append_ledger:
            _append_ledger(args, metrics_dict, final_pred_path)

    print(f"Done. {args.out_dir / 'summary.json'}")
    return 0


def _append_ledger(args, metrics: dict, pred_path: Path) -> None:
    from metadata_to_morphsource.seg_train.experiment_ledger import (
        ExperimentLedger,
        EpisodeRecord,
        SegmentationMode,
    )

    ledger = ExperimentLedger(args.append_ledger.parent)
    rec = EpisodeRecord(
        run_id=args.run_id or args.out_dir.name,
        paper_tag=args.paper_tag,
        media_id="000011009",
        physical_object_id="tuatara_skull_000358663",
        taxonomy="Sphenodon punctatus",
        volume_path=str(args.ct_path or ""),
        ground_truth_path=str(args.gt_path),
        prediction_path=str(pred_path),
        mode=SegmentationMode.NNINTERACTIVE.value,
        operator="gt_guided_clicker",
        goal="mesh-derived GT guided clicks",
        n_prompts=len(json.loads((args.out_dir / "summary.json").read_text()).get("history", [])),
        dice=metrics.get("dice"),
        iou=metrics.get("iou"),
        precision=metrics.get("precision"),
        recall=metrics.get("recall"),
    )
    ledger.record(rec)


if __name__ == "__main__":
    raise SystemExit(main())
