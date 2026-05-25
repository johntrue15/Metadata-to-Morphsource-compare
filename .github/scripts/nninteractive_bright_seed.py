#!/usr/bin/env python3
"""
Local bright-spot greedy nnInteractive segmentation.

This is a port of ``slicer_remote_bright_seed.py`` that drives the
LOCAL ``nninteractive_segment.Segmenter`` (headless, GPU/CUDA) instead
of a remote 3D Slicer instance. The deterministic part of the
algorithm is exactly the same:

  1. Read the CT volume.
  2. Threshold at the ``--intensity-percentile`` of the volume's
     intensity distribution (default 99th = top 1%).
  3. Collect every voxel above the threshold, sort by intensity
     descending, cap at ``--max-candidates`` (default 200k).
  4. Loop up to ``--max-steps`` clicks:
       a. Read the current segmentation mask from the Segmenter.
       b. Skim past any candidate already inside the mask.
       c. Click the next candidate as a POSITIVE point.
       d. Optionally early-stop on patience/min_delta or runaway
          explosions (a single click adding more than
          ``--max-explosion-frac`` of the volume).

Why this exists
---------------
The LLM-driven ``nninteractive_loop.py`` keeps mis-localising on
thin-cortical-bone CTs (Felis catus / Crotalus intermedius) because the
vision model has to *guess* spatial coordinates from PNG previews and
it consistently clicks the dark cranial cavity instead of the bright
bone walls. The mouse-skull saturation session in
``paper_artifacts/mouse_skull_session_001/`` skipped the LLM entirely
and produced 80 clean segments with 113 deterministic clicks. This
script makes that same deterministic strategy available against the
local nnInteractive backend so we can isolate "LLM is the failure
mode" from "nnInteractive itself can't handle this anatomy".

Outputs
-------
``<output-dir>/`` contains:
  - ``<media-id>_bright_labelmap.nii.gz`` — final binary mask
  - ``<media-id>_bright_summary.json``    — per-step trail + stats
  - ``<media-id>_bright_clicks.jsonl``    — one JSON dict per click
  - ``<media-id>_step{NN}_before_*.png``  — preview before each click
  - ``<media-id>_step{NN}_after_*.png``   — preview after each click
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
sys.path.insert(0, str(SCRIPT_DIR))

from nninteractive_segment import (  # noqa: E402
    NNInteractiveUnavailable, SegmenterConfig, make_segmenter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("nni_bright_seed")


# ---------------------------------------------------------------------------
# Algorithm helpers (pure-numpy, easy to unit-test)
# ---------------------------------------------------------------------------


def build_candidate_list(
    arr_kji,  # numpy (z, y, x) volume
    percentile: float,
    max_candidates: int,
    intensity_min: Optional[float] = None,
    intensity_max: Optional[float] = None,
    region_bbox_kji: Optional[dict] = None,
):
    """Return ``(candidates_kji, intensities, threshold)``.

    ``candidates_kji`` is an (N, 3) int32 array of (k, j, i) coords
    sorted by intensity descending and capped at ``max_candidates``.
    ``intensities`` is the parallel float32 array. ``threshold`` is
    the raw scalar value at ``percentile``.

    ``intensity_min`` / ``intensity_max`` (optional) clamp candidates
    to a specific intensity window AFTER the percentile threshold is
    applied. The local stapes smoke test showed why this matters:
    that chameleon CT is uint16 with edge artifacts at 20000+
    intensity while the actual stapes lives at ~13000, so the 99th
    percentile captured artifacts and missed bone. A per-specimen
    bone window is the right knob.

    ``region_bbox_kji`` (optional) is a dict ``{"k":[lo,hi], "j":[lo,hi],
    "i":[lo,hi]}`` that restricts candidates to a sub-volume. Use
    this when you know roughly where the target lives (e.g. from a
    coarse mesh/centroid hint) and want to filter out bright voxels
    that are clearly not the target anatomy.

    Returning a parallel int + float array (rather than a list of
    tuples) keeps the state compact enough to round-trip into JSON
    summaries even for skull-sized volumes (~200k candidates × 16
    bytes = 3 MB).
    """
    import numpy as np
    arr_f = arr_kji.astype(np.float32, copy=False)
    threshold = float(np.percentile(arr_f, percentile))
    mask = arr_f >= threshold

    if intensity_min is not None:
        mask &= (arr_f >= float(intensity_min))
    if intensity_max is not None:
        mask &= (arr_f <= float(intensity_max))

    if region_bbox_kji:
        region = np.zeros_like(mask)
        kr = region_bbox_kji.get("k") or [0, mask.shape[0]]
        jr = region_bbox_kji.get("j") or [0, mask.shape[1]]
        ir = region_bbox_kji.get("i") or [0, mask.shape[2]]
        region[int(kr[0]):int(kr[1]) + 1,
               int(jr[0]):int(jr[1]) + 1,
               int(ir[0]):int(ir[1]) + 1] = True
        mask &= region

    ks, js, is_ = np.where(mask)
    if len(ks) == 0:
        empty = np.zeros((0, 3), dtype=np.int32)
        return empty, np.zeros((0,), dtype=np.float32), threshold
    intensities = arr_f[ks, js, is_]
    order = np.argsort(-intensities)  # descending
    if max_candidates and max_candidates > 0 and len(order) > max_candidates:
        order = order[:max_candidates]
    cand_kji = np.stack([ks[order], js[order], is_[order]],
                        axis=1).astype(np.int32)
    cand_int = intensities[order].astype(np.float32)
    return cand_kji, cand_int, threshold


def next_unsegmented_candidate(
    cand_kji, mask_kji, start_idx: int,
):
    """Walk forward through the candidate list and return the first
    one that is NOT already inside ``mask_kji``.

    Returns ``(idx_picked, idx_after, skipped_inside)`` where
    ``idx_picked`` is None if the list is exhausted. ``idx_after`` is
    where the next call should resume from. This is the same skim-
    forward behaviour as the original Slicer recipe but lifted out so
    we can unit-test it without an nnInteractive backend.
    """
    idx = int(start_idx)
    skipped = 0
    n = int(cand_kji.shape[0])
    while idx < n:
        k = int(cand_kji[idx, 0])
        j = int(cand_kji[idx, 1])
        i = int(cand_kji[idx, 2])
        if bool(mask_kji[k, j, i]):
            idx += 1
            skipped += 1
            continue
        return idx, idx + 1, skipped
    return None, idx, skipped


def should_early_stop(
    deltas: list[int],
    *,
    min_delta: int,
    patience: int,
) -> bool:
    """Return True iff the trailing ``patience`` deltas are all below
    ``min_delta``. Mirrors the remote pipeline's saturation rule.
    """
    if patience <= 0:
        return False
    if len(deltas) < patience:
        return False
    return all(d < min_delta for d in deltas[-patience:])


# ---------------------------------------------------------------------------
# Bone-window for previews (CT only)
# ---------------------------------------------------------------------------


def _detect_intensity_window(arr_kji) -> Optional[tuple[float, float]]:
    """Heuristic: if the volume looks like a CT (signed-ish HU range
    that spans negatives to >500), apply a bone window so the preview
    actually shows bone vs cavity. uint8 data (mouse skull = 0..255)
    gets matplotlib auto-scaling instead.
    """
    import numpy as np
    arr_f = arr_kji.astype(np.float32, copy=False)
    lo = float(np.percentile(arr_f, 1.0))
    hi = float(np.percentile(arr_f, 99.0))
    if lo < -200 and hi > 500:
        return (-200.0, 2000.0)
    return None


# ---------------------------------------------------------------------------
# Main paint loop (uses the local Segmenter)
# ---------------------------------------------------------------------------


def run_bright_seed(
    *,
    input_path: str,
    output_dir: str,
    media_id: str,
    percentile: float = 99.0,
    max_candidates: int = 200_000,
    max_steps: int = 50,
    min_delta: int = 50,
    patience: int = 3,
    max_explosion_frac: float = 0.5,
    no_stop_rules: bool = False,
    save_previews: bool = True,
    intensity_window: Optional[tuple[float, float]] = None,
    intensity_min: Optional[float] = None,
    intensity_max: Optional[float] = None,
    region_bbox_kji: Optional[dict] = None,
    do_autozoom: bool = False,
    segmenter=None,
) -> dict:
    """Drive the bright-seed paint loop end to end.

    Returns a summary dict with the per-step trail. ``segmenter`` is
    injectable so the unit tests can run without nnInteractive.
    """
    import numpy as np

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if no_stop_rules:
        min_delta = 0
        patience = 10 ** 9
        max_explosion_frac = 1.0

    if segmenter is None:
        # AutoZoom defaults to ON in nnInteractive but its
        # implementation has a tensor-shape bug ("size of tensor a
        # (N) must match size of tensor b (N-1) at non-singleton
        # dim 4") that hit us on Felis v11 and again on the local
        # bright-seed smoke. AutoZoom also makes early clicks "land
        # outside the interaction map" when the zoom box hasn't been
        # established yet ("Point is outside the interaction map!
        # Ignoring") - which is exactly wrong for bright-seed
        # because our FIRST click is supposed to land on the
        # brightest voxel anywhere in the volume. So we ship with
        # AutoZoom OFF by default; pass do_autozoom=True if you
        # want to re-enable it for experimentation.
        cfg = SegmenterConfig(input_path=input_path,
                              output_dir=str(out_dir),
                              media_id=media_id,
                              do_autozoom=do_autozoom)
        try:
            seg = make_segmenter(cfg)
        except NNInteractiveUnavailable as exc:
            return {"success": False, "stage": "init",
                    "error": f"nnInteractive backend unavailable: {exc}"}
    else:
        seg = segmenter

    # Pull the CT array. The Segmenter has already loaded the volume
    # via SimpleITK, so we reuse it instead of re-reading from disk.
    sitk = seg._sitk
    arr_kji = sitk.GetArrayFromImage(seg.sitk_image)  # (z, y, x) = (k, j, i)

    if intensity_window is None:
        intensity_window = _detect_intensity_window(arr_kji)
        if intensity_window:
            log.info("Auto-detected CT bone window: vmin=%.0f vmax=%.0f HU",
                     *intensity_window)

    cand_kji, cand_int, threshold = build_candidate_list(
        arr_kji, percentile=percentile, max_candidates=max_candidates,
        intensity_min=intensity_min, intensity_max=intensity_max,
        region_bbox_kji=region_bbox_kji,
    )
    n_candidates = int(cand_kji.shape[0])
    total_voxels = int(arr_kji.size)
    log.info(
        "Bright-seed candidates: %d voxels above %.2fp threshold=%.3f "
        "(intensity range %s..%s, %.3f%% of volume)",
        n_candidates, percentile, threshold,
        f"{float(cand_int.min()):.2f}" if n_candidates else "n/a",
        f"{float(cand_int.max()):.2f}" if n_candidates else "n/a",
        100.0 * n_candidates / max(total_voxels, 1),
    )
    if n_candidates == 0:
        return {"success": False, "stage": "candidates",
                "error": "no bright voxels at the chosen percentile"}

    # Initial preview (no markers, empty mask).
    if save_previews:
        seg.save_orthogonal_previews(
            name_prefix=f"{media_id}_step00",
            intensity_window=intensity_window,
        )

    clicks_path = out_dir / f"{media_id}_bright_clicks.jsonl"
    clicks_fh = clicks_path.open("w", encoding="utf-8")

    history: list[dict] = []
    deltas: list[int] = []
    explosion_voxels = int(total_voxels * max_explosion_frac)
    next_idx = 0
    voxels_after = 0
    stop_reason: Optional[dict] = None

    t_start = time.time()
    for step in range(1, max_steps + 1):
        mask_before = (seg.mask_array > 0)
        voxels_before = int(mask_before.sum())

        idx_picked, next_idx, skipped = next_unsegmented_candidate(
            cand_kji, mask_before, next_idx,
        )
        if idx_picked is None:
            stop_reason = {"reason": "no_more_candidates",
                           "candidates_left": 0,
                           "voxels": voxels_before}
            log.info("Step %d: no candidates left (saturation).", step)
            break

        k = int(cand_kji[idx_picked, 0])
        j = int(cand_kji[idx_picked, 1])
        i = int(cand_kji[idx_picked, 2])
        intensity = float(cand_int[idx_picked])

        # nnInteractive's bounds check on prompts compares ``position[d]``
        # against ``interaction_map.shape[d]`` POSITIONALLY, while the
        # map shape is the numpy ``(z, y, x)`` order. So if we want
        # the bounds check to pass for any voxel in the volume we
        # must pass the prompt in the SAME order as ``arr.shape`` -
        # i.e. ``(k, j, i)`` not the documented ``(x, y, z)``. The
        # local smoke test on the cached Felis CT proved this: a
        # candidate at numpy ``(k=345, j=53, i=124)`` passed as
        # ``xyz=(124, 53, 345)`` was rejected with "Point is outside
        # the interaction map" because ``345 > shape[2] = i_max =
        # 211``. The same candidate passed as ``xyz=(345, 53, 124)``
        # falls inside ``shape=(384, 224, 211)`` and paints at the
        # intended voxel.
        x, y, z = k, j, i

        # BEFORE preview with the planned-click marker.
        marker = [{
            "xyz": (x, y, z),
            "positive": True,
            "label": f"s{step}",
        }]
        if save_previews:
            seg.save_orthogonal_previews(
                name_prefix=f"{media_id}_step{step:02d}_before",
                intensity_window=intensity_window,
                markers=marker,
            )

        log.info(
            "Step %d/%d: click (x=%d, y=%d, z=%d) intensity=%.2f "
            "(skipped %d already-inside; %d candidates left)",
            step, max_steps, x, y, z, intensity, skipped,
            n_candidates - next_idx,
        )
        t0 = time.time()
        try:
            seg.add_point(x, y, z, positive=True,
                          label=f"bright_step{step:02d}")
        except Exception as exc:
            log.error("Step %d: add_point raised %s", step, exc)
            stop_reason = {"reason": "add_point_error",
                           "error": repr(exc), "step": step}
            break
        click_seconds = round(time.time() - t0, 3)

        voxels_after = seg.voxel_count()
        delta = voxels_after - voxels_before
        deltas.append(delta)

        rec = {
            "step": step,
            "ijk_kji": [i, j, k],
            "xyz": [x, y, z],
            "intensity": intensity,
            "voxels_before": voxels_before,
            "voxels_after": voxels_after,
            "delta": delta,
            "skipped_inside": skipped,
            "candidates_left": int(n_candidates - next_idx),
            "click_seconds": click_seconds,
        }
        history.append(rec)
        clicks_fh.write(json.dumps(rec) + "\n")
        clicks_fh.flush()

        # AFTER preview with the same marker (now shows the resulting
        # mask change too).
        if save_previews:
            seg.save_orthogonal_previews(
                name_prefix=f"{media_id}_step{step:02d}_after",
                intensity_window=intensity_window,
                markers=marker,
            )

        log.info(
            "  -> voxels %d -> %d (delta %+d, click %.2fs)",
            voxels_before, voxels_after, delta, click_seconds,
        )

        # Runaway-explosion guard (one click adds > N% of the volume).
        if delta > explosion_voxels and explosion_voxels > 0:
            stop_reason = {
                "reason": "explosion",
                "delta": delta,
                "max_explosion_voxels": explosion_voxels,
                "step": step,
            }
            log.warning(
                "Step %d added %d voxels (> %d explosion guard); stopping.",
                step, delta, explosion_voxels,
            )
            break

        # Saturation: trailing ``patience`` deltas all below min_delta.
        if should_early_stop(deltas, min_delta=min_delta,
                             patience=patience):
            stop_reason = {
                "reason": "saturated",
                "trailing_deltas": deltas[-patience:],
                "min_delta": min_delta,
                "patience": patience,
                "step": step,
            }
            log.info("Step %d: trailing %d deltas all < %d; stopping.",
                     step, patience, min_delta)
            break
    else:
        stop_reason = {"reason": "max_steps", "max_steps": max_steps}
        log.info("Hit max_steps=%d; stopping.", max_steps)

    clicks_fh.close()

    labelmap_path = seg.save_labelmap()
    summary_path = seg.export_summary({
        "media_id": media_id,
        "mode": "bright_seed",
        "percentile": percentile,
        "intensity_threshold": threshold,
        "n_candidates": n_candidates,
        "max_candidates": max_candidates,
        "max_steps": max_steps,
        "no_stop_rules": no_stop_rules,
        "min_delta": min_delta,
        "patience": patience,
        "max_explosion_frac": max_explosion_frac,
        "stop_reason": stop_reason,
        "n_clicks": len(history),
        "history": history,
        "total_seconds": round(time.time() - t_start, 2),
    })

    log.info(
        "Bright-seed done: %d clicks, %d final voxels (%.2f mm^3), "
        "stop_reason=%s, labelmap=%s",
        len(history), seg.voxel_count(), seg.volume_mm3(),
        (stop_reason or {}).get("reason"), labelmap_path,
    )

    return {
        "success": True,
        "media_id": media_id,
        "mode": "bright_seed",
        "labelmap_path": labelmap_path,
        "summary_path": summary_path,
        "clicks_path": str(clicks_path),
        "n_clicks": len(history),
        "voxel_count": seg.voxel_count(),
        "volume_mm3": round(seg.volume_mm3(), 3),
        "stop_reason": stop_reason,
        "history": history,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input", required=True,
                   help="Path to the CT volume (NIfTI/NRRD/...).")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--media-id", required=True)
    p.add_argument("--intensity-percentile", type=float, default=99.0)
    p.add_argument("--max-candidates", type=int, default=200_000)
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--min-delta", type=int, default=50)
    p.add_argument("--patience", type=int, default=3)
    p.add_argument("--max-explosion-frac", type=float, default=0.5)
    p.add_argument("--no-stop-rules", action="store_true",
                   help="Run to saturation or max-steps (mouse-style).")
    p.add_argument("--no-previews", action="store_true",
                   help="Skip per-step BEFORE/AFTER PNG screenshots.")
    p.add_argument("--enable-autozoom", action="store_true",
                   help="Re-enable nnInteractive's AutoZoom (default: "
                        "OFF because it has a known tensor-shape bug "
                        "that aborts the first click and discards "
                        "out-of-zoom-box clicks).")
    p.add_argument("--intensity-min", type=float, default=None,
                   help="Reject candidate voxels with intensity below "
                        "this value (after the percentile filter). "
                        "Use for non-HU CTs (e.g. uint16 micro-CT) "
                        "where 'top N%%' picks scaling artifacts.")
    p.add_argument("--intensity-max", type=float, default=None,
                   help="Reject candidate voxels with intensity above "
                        "this value. Pair with --intensity-min to "
                        "set an explicit bone window.")
    p.add_argument("--region-bbox", type=str, default=None,
                   help="JSON dict like "
                        "'{\"k\":[lo,hi],\"j\":[lo,hi],\"i\":[lo,hi]}' "
                        "in numpy (z,y,x) coords. Restricts candidate "
                        "voxels to this sub-volume so bright "
                        "non-target structures (teeth, edge artifacts) "
                        "don't dominate.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    region_bbox = None
    if args.region_bbox:
        region_bbox = json.loads(args.region_bbox)
    result = run_bright_seed(
        input_path=args.input,
        output_dir=args.output_dir,
        media_id=args.media_id,
        percentile=args.intensity_percentile,
        max_candidates=args.max_candidates,
        max_steps=args.max_steps,
        min_delta=args.min_delta,
        patience=args.patience,
        max_explosion_frac=args.max_explosion_frac,
        no_stop_rules=args.no_stop_rules,
        save_previews=not args.no_previews,
        intensity_min=args.intensity_min,
        intensity_max=args.intensity_max,
        region_bbox_kji=region_bbox,
        do_autozoom=args.enable_autozoom,
    )
    print(json.dumps({k: v for k, v in result.items()
                      if k != "history"}, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
