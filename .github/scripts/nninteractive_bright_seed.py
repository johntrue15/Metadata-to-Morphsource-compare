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


def has_dense_bright_neighborhood(
    arr_kji,
    k: int, j: int, i: int,
    *,
    threshold: float,
    radius: int = 2,
    min_density: float = 0.4,
) -> bool:
    """Reject candidates whose local neighborhood isn't densely bright.

    The IMPC mouse-embryo 6-click smoke test showed why this matters:
    bright-seed picked candidate (x=202, y=106, z=150) at intensity 183
    which landed in the BACKGROUND outside the embryo body, where a
    handful of scan-noise voxels happened to be bright. nnInteractive
    grew that into a 41k-voxel runaway segment because there was no
    real organ boundary to constrain it. A real bright structure
    (heart, liver, bone) has many bright voxels around the candidate.
    A background artifact has just a few isolated bright voxels.

    Returns True iff at least ``min_density`` fraction of the voxels
    in the ``(2*radius+1)^3`` cube around ``(k, j, i)`` are above
    ``threshold``. Defaults (radius=2, min_density=0.4) correspond to
    "at least 50/125 voxels in a 5x5x5 cube must be bright".
    """
    import numpy as np
    z_max, y_max, x_max = arr_kji.shape
    kmin = max(0, k - radius)
    kmax = min(z_max, k + radius + 1)
    jmin = max(0, j - radius)
    jmax = min(y_max, j + radius + 1)
    imin = max(0, i - radius)
    imax = min(x_max, i + radius + 1)
    cube = arr_kji[kmin:kmax, jmin:jmax, imin:imax]
    if cube.size == 0:
        return False
    above = int(np.asarray(cube >= threshold).sum())
    return (above / cube.size) >= min_density


def intensity_below_obvious(
    intensity: float,
    *,
    threshold: float,
    peak_intensity: float,
    floor_frac: float,
) -> bool:
    """Return True when ``intensity`` is no longer "statistically obvious"
    relative to the volume's bright distribution.

    "Obvious" = above ``threshold + (peak - threshold) * floor_frac``.
    With the IMPC mouse defaults (threshold=102, peak=255, frac=0.5)
    the floor sits at 178; once bright-seed starts clicking voxels
    below that we declare saturation and exit.

    Returns False when ``floor_frac <= 0`` (rule disabled) or when
    ``peak <= threshold`` (degenerate volume).
    """
    if floor_frac <= 0:
        return False
    if peak_intensity <= threshold:
        return False
    floor = threshold + (peak_intensity - threshold) * floor_frac
    return intensity < floor


def next_validated_candidate(
    arr_kji,
    cand_kji,
    cand_int,
    union_mask,
    *,
    start_idx: int,
    threshold: float,
    min_local_density: float = 0.0,
    neighborhood_radius: int = 2,
):
    """Combine the skim-past-already-in-mask rule with an optional
    local-density check.

    Returns ``(idx_picked, idx_after, skipped_inside, skipped_sparse)``.
    ``idx_picked`` is None when the candidate list is exhausted.
    ``skipped_inside`` counts voxels rejected because they fall inside
    a previous segment; ``skipped_sparse`` counts voxels rejected by
    the local-density filter. Both are useful for diagnosing why the
    loop ran out of candidates.

    The density filter is skipped when ``min_local_density <= 0`` so
    the function stays backward-compatible with the original behaviour.
    """
    idx = int(start_idx)
    n = int(cand_kji.shape[0])
    skipped_inside = 0
    skipped_sparse = 0
    while idx < n:
        k = int(cand_kji[idx, 0])
        j = int(cand_kji[idx, 1])
        i = int(cand_kji[idx, 2])
        if bool(union_mask[k, j, i]):
            idx += 1
            skipped_inside += 1
            continue
        if min_local_density > 0 and not has_dense_bright_neighborhood(
            arr_kji, k, j, i,
            threshold=threshold,
            radius=neighborhood_radius,
            min_density=min_local_density,
        ):
            idx += 1
            skipped_sparse += 1
            continue
        return idx, idx + 1, skipped_inside, skipped_sparse
    return None, idx, skipped_inside, skipped_sparse


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
    multi_segment: bool = True,
    min_segment_voxels: int = 200,
    max_segment_voxels: Optional[int] = None,
    min_local_density: float = 0.4,
    neighborhood_radius: int = 2,
    intensity_drop_floor_frac: float = 0.0,
    min_clicks_before_drop_stop: int = 5,
    segmenter=None,
) -> dict:
    """Drive the bright-seed paint loop end to end.

    Two paint modes:

    * ``multi_segment=True`` (default, matches ``slicer_remote_bright_seed.py``):
      every click starts a FRESH nnInteractive interaction session via
      ``Segmenter.reset_segment()``. nnInteractive grows one coherent
      structure per click. The resulting per-click masks are unioned
      into ``union_mask`` (used for the "already-inside" check on
      future candidates) and tracked individually so the composite
      labelmap is a multi-label image (1..N for the N kept segments).

      Each new segment is validated before being kept:
        - ``min_segment_voxels`` (reject artifact clicks)
        - ``max_segment_voxels`` (reject runaway segments)
      A rejected segment's prompt is rolled back via ``reset_segment``
      and the loop tries the next candidate instead of advancing the
      step counter.

    * ``multi_segment=False`` (legacy, the old behaviour):
      a single growing nnInteractive session shared across all clicks.
      Useful when you actually want one fused mask (the colors-of-skull
      cranial-bone use case), but produces the IMPC-mouse failure mode
      where click 6 lands just outside click 5's edge artifact.

    ``min_local_density`` (only used when > 0) requires the candidate's
    ``(2*neighborhood_radius+1)^3`` neighborhood to be at least that
    fraction bright. Defaults (0.4 = 50/125 voxels in a 5x5x5 cube)
    filter out isolated background-noise voxels, which is the IMPC
    mouse-embryo step-6 failure mode (click landed at intensity 183
    in the background outside the embryo body where a sparse cluster
    of bright noise voxels grew into a 41k-voxel runaway segment).

    ``intensity_drop_floor_frac`` (> 0 enables) declares saturation
    when the accepted click's intensity falls below
    ``threshold + (peak_intensity - threshold) * floor_frac``. This is
    the "statistically obvious bright voxel" rule: bright-seed walks
    through candidates in descending intensity order, so once we're
    down to "the bottom half of the bright tail" there's nothing left
    that's confidently brighter than background and we should exit.
    ``min_clicks_before_drop_stop`` guards against early exits on
    volumes where the very first candidates aren't representative.

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
    if max_segment_voxels is None:
        # Default: any single segment that swallows >50% of the volume
        # is almost certainly a runaway, not a real organ.
        max_segment_voxels = explosion_voxels

    # Multi-segment bookkeeping: the running union of all KEPT segment
    # masks (used for "already inside" candidate skipping) plus a list
    # of individual segment masks (so the composite labelmap can be
    # multi-label rather than a single binary union).
    union_mask = np.zeros(arr_kji.shape, dtype=bool)
    per_segment_masks: list = []  # entries: {"label", "click_xyz", "mask"}
    next_idx = 0
    rejections: list[dict] = []
    stop_reason: Optional[dict] = None

    t_start = time.time()
    step = 0
    while step < max_steps:
        step += 1
        # Pick the next candidate that is NOT inside any kept segment
        # AND has a dense enough bright neighborhood to be real tissue.
        idx_picked, next_idx, skipped_inside, skipped_sparse = (
            next_validated_candidate(
                arr_kji, cand_kji, cand_int, union_mask,
                start_idx=next_idx,
                threshold=threshold,
                min_local_density=min_local_density,
                neighborhood_radius=neighborhood_radius,
            )
        )
        if idx_picked is None:
            stop_reason = {
                "reason": "no_more_candidates",
                "candidates_left": 0,
                "voxels": int(union_mask.sum()),
                "skipped_inside_total": skipped_inside,
                "skipped_sparse_total": skipped_sparse,
            }
            log.info(
                "Step %d: no candidates left (skipped_inside=%d sparse=%d).",
                step, skipped_inside, skipped_sparse,
            )
            step -= 1  # this step didn't run
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
        # i.e. ``(k, j, i)`` not the documented ``(x, y, z)``.
        x, y, z = k, j, i

        # In multi-segment mode every click starts from a fresh nnInteractive
        # interaction session. The previous segment masks are preserved in
        # ``per_segment_masks`` / ``union_mask`` so the "already inside"
        # check still works.
        voxels_before_segment = int(union_mask.sum())
        if multi_segment:
            seg.reset_segment()

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
            "[skipped %d inside, %d sparse; %d candidates left] "
            "kept_segments=%d union_voxels=%d",
            step, max_steps, x, y, z, intensity,
            skipped_inside, skipped_sparse, n_candidates - next_idx,
            len(per_segment_masks), voxels_before_segment,
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

        # Snapshot THIS segment's mask before we decide whether to keep
        # or roll it back. In multi-segment mode seg.mask_array is the
        # current segment only; in single-segment mode it is the
        # cumulative mask, so we subtract the previous union to get
        # the per-click delta either way.
        current_seg_mask = (seg.mask_array > 0)
        if multi_segment:
            new_segment_mask = current_seg_mask & (~union_mask)
        else:
            new_segment_mask = current_seg_mask & (~union_mask)
        new_segment_voxels = int(new_segment_mask.sum())

        # Validate: reject tiny segments (single bright voxel that
        # nnInteractive couldn't grow into anything coherent) and
        # runaway segments (background blob that exploded).
        rejected_reason: Optional[str] = None
        if new_segment_voxels < min_segment_voxels:
            rejected_reason = "too_small"
        elif new_segment_voxels > max_segment_voxels:
            rejected_reason = "runaway"

        if rejected_reason is not None and multi_segment:
            # Roll back so the rejected segment doesn't contaminate
            # the union (and therefore doesn't block neighboring
            # candidates from being clicked next time).
            seg.reset_segment()
            rejections.append({
                "step": step,
                "xyz": [x, y, z],
                "intensity": intensity,
                "new_segment_voxels": new_segment_voxels,
                "reason": rejected_reason,
            })
            log.warning(
                "Step %d: rejected segment (%s, %d voxels) at (%d,%d,%d). "
                "Trying next candidate.",
                step, rejected_reason, new_segment_voxels, x, y, z,
            )
            # Don't advance step: try again with a new candidate.
            step -= 1
            continue

        # Accept: roll the new segment into the union and remember it.
        union_mask |= new_segment_mask
        per_segment_masks.append({
            "label": len(per_segment_masks) + 1,
            "click_xyz": [x, y, z],
            "click_kji": [k, j, i],
            "intensity": intensity,
            "voxels": new_segment_voxels,
            "mask": new_segment_mask,
        })

        voxels_after = int(union_mask.sum())
        delta = new_segment_voxels  # per-click delta = this segment's size
        deltas.append(delta)

        rec = {
            "step": step,
            "ijk_kji": [i, j, k],
            "xyz": [x, y, z],
            "intensity": intensity,
            "voxels_before": voxels_before_segment,
            "voxels_after": voxels_after,
            "delta": delta,
            "segment_voxels": new_segment_voxels,
            "segment_label": per_segment_masks[-1]["label"],
            "skipped_inside": skipped_inside,
            "skipped_sparse": skipped_sparse,
            "candidates_left": int(n_candidates - next_idx),
            "click_seconds": click_seconds,
            "n_segments_kept": len(per_segment_masks),
            "rejections_so_far": len(rejections),
        }
        history.append(rec)
        clicks_fh.write(json.dumps(rec) + "\n")
        clicks_fh.flush()

        if save_previews:
            seg.save_orthogonal_previews(
                name_prefix=f"{media_id}_step{step:02d}_after",
                intensity_window=intensity_window,
                markers=marker,
            )

        log.info(
            "  -> segment#%d at (%d,%d,%d) = %d voxels  "
            "(union %d -> %d, click %.2fs)",
            per_segment_masks[-1]["label"], x, y, z, new_segment_voxels,
            voxels_before_segment, voxels_after, click_seconds,
        )

        # Runaway-explosion guard against the GLOBAL mask (less likely
        # to trip now that single-segment runaways are caught above,
        # but kept for completeness in single-segment mode).
        if delta > explosion_voxels and explosion_voxels > 0 and not multi_segment:
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

        # "Statistically obvious bright voxel" saturation rule. Bright
        # voxels are clicked in descending intensity order, so once we
        # drop below threshold + (peak - threshold)*floor_frac there
        # is nothing left that's clearly brighter than background.
        if (intensity_drop_floor_frac > 0
                and len(history) >= min_clicks_before_drop_stop):
            peak_intensity = max(rec["intensity"] for rec in history)
            if intensity_below_obvious(
                intensity,
                threshold=threshold,
                peak_intensity=peak_intensity,
                floor_frac=intensity_drop_floor_frac,
            ):
                floor = (threshold
                         + (peak_intensity - threshold)
                         * intensity_drop_floor_frac)
                stop_reason = {
                    "reason": "intensity_below_obvious",
                    "intensity": intensity,
                    "intensity_floor": floor,
                    "peak_intensity": peak_intensity,
                    "threshold": threshold,
                    "floor_frac": intensity_drop_floor_frac,
                    "step": step,
                }
                log.info(
                    "Step %d: click intensity %.2f below 'obvious' floor "
                    "%.2f (peak=%.2f threshold=%.2f frac=%.2f); stopping.",
                    step, intensity, floor, peak_intensity,
                    threshold, intensity_drop_floor_frac,
                )
                break

    if stop_reason is None:
        stop_reason = {"reason": "max_steps", "max_steps": max_steps}
        log.info("Hit max_steps=%d; stopping.", max_steps)

    clicks_fh.close()

    # In multi-segment mode the segmenter's target buffer holds only
    # the LAST per-click segment (or nothing, if step N was rolled
    # back). Push the union into it so the final preview/labelmap
    # path reflects all 10 organs together — otherwise the
    # step10_after.png only shows segment #10 and the user can't see
    # the cumulative result.
    if multi_segment and per_segment_masks:
        try:
            import torch
            seg.target.zero_()
            seg.target.copy_(torch.from_numpy(
                union_mask.astype(np.uint8)
            ).to(seg.target.device))
        except Exception as exc:
            log.debug("Could not push union into target buffer: %s", exc)
        if save_previews:
            try:
                # Markers for every kept segment so the user can see
                # where each click landed on the final composite.
                final_markers = [
                    {"xyz": tuple(entry["click_xyz"]),
                     "positive": True,
                     "label": f"s{idx+1}"}
                    for idx, entry in enumerate(per_segment_masks)
                ]
                seg.save_orthogonal_previews(
                    name_prefix=f"{media_id}_final_union",
                    intensity_window=intensity_window,
                    markers=final_markers,
                )
            except Exception as exc:
                log.debug("Could not write final union preview: %s", exc)

    # Final mask export. In multi-segment mode we own the union and
    # write it ourselves (sitk directly) so we don't depend on the
    # segmenter's internal target buffer being current — between the
    # last click and now we may have rolled back rejected segments,
    # so seg.mask_array isn't necessarily the union.
    labelmap_path: str = ""
    multilabel_path: Optional[str] = None
    if multi_segment and per_segment_masks:
        try:
            union_image = seg._sitk.GetImageFromArray(
                union_mask.astype(np.uint8)
            )
            union_image.CopyInformation(seg.sitk_image)
            union_out = out_dir / f"{media_id}_nni_labelmap.nii.gz"
            seg._sitk.WriteImage(union_image, str(union_out),
                                 useCompression=True)
            labelmap_path = str(union_out)
            log.info("Wrote union labelmap: %s (%d voxels)",
                     union_out, int(union_mask.sum()))
        except Exception as exc:
            log.warning("Direct union labelmap write failed (%s); "
                        "falling back to seg.save_labelmap()", exc)
            labelmap_path = seg.save_labelmap()

        try:
            multilabel = np.zeros(arr_kji.shape, dtype=np.uint16)
            for entry in per_segment_masks:
                multilabel[entry["mask"]] = entry["label"]
            ml_image = seg._sitk.GetImageFromArray(multilabel)
            ml_image.CopyInformation(seg.sitk_image)
            ml_out = out_dir / f"{media_id}_nni_multilabel.nii.gz"
            seg._sitk.WriteImage(ml_image, str(ml_out), useCompression=True)
            multilabel_path = str(ml_out)
            log.info("Wrote multi-label labelmap: %s (%d segments)",
                     ml_out, len(per_segment_masks))
        except Exception as exc:
            log.warning("Failed to write multi-label labelmap: %s", exc)
    else:
        labelmap_path = seg.save_labelmap()

    summary_payload: dict = {
        "media_id": media_id,
        "mode": "bright_seed",
        "multi_segment": multi_segment,
        "percentile": percentile,
        "intensity_threshold": threshold,
        "n_candidates": n_candidates,
        "max_candidates": max_candidates,
        "max_steps": max_steps,
        "no_stop_rules": no_stop_rules,
        "min_delta": min_delta,
        "patience": patience,
        "max_explosion_frac": max_explosion_frac,
        "min_segment_voxels": min_segment_voxels,
        "max_segment_voxels": max_segment_voxels,
        "min_local_density": min_local_density,
        "neighborhood_radius": neighborhood_radius,
        "intensity_drop_floor_frac": intensity_drop_floor_frac,
        "min_clicks_before_drop_stop": min_clicks_before_drop_stop,
        "stop_reason": stop_reason,
        "n_clicks": len(history),
        "n_segments_kept": len(per_segment_masks),
        "n_rejections": len(rejections),
        "rejections": rejections,
        "per_segment": [
            {k: v for k, v in entry.items() if k != "mask"}
            for entry in per_segment_masks
        ],
        "history": history,
        "total_seconds": round(time.time() - t_start, 2),
    }
    summary_path = seg.export_summary(summary_payload)

    final_voxels = int(union_mask.sum()) if multi_segment else seg.voxel_count()
    final_mm3 = (
        float(final_voxels)
        * float(seg.sitk_image.GetSpacing()[0])
        * float(seg.sitk_image.GetSpacing()[1])
        * float(seg.sitk_image.GetSpacing()[2])
    )
    log.info(
        "Bright-seed done: %d clicks kept (%d rejected), %d segments, "
        "%d final voxels (%.2f mm^3), stop_reason=%s, labelmap=%s",
        len(history), len(rejections), len(per_segment_masks),
        final_voxels, final_mm3,
        (stop_reason or {}).get("reason"), labelmap_path,
    )

    return {
        "success": True,
        "media_id": media_id,
        "mode": "bright_seed",
        "multi_segment": multi_segment,
        "labelmap_path": labelmap_path,
        "multilabel_path": multilabel_path,
        "summary_path": summary_path,
        "clicks_path": str(clicks_path),
        "n_clicks": len(history),
        "n_segments_kept": len(per_segment_masks),
        "n_rejections": len(rejections),
        "rejections": rejections,
        "per_segment": [
            {k: v for k, v in entry.items() if k != "mask"}
            for entry in per_segment_masks
        ],
        "voxel_count": final_voxels,
        "volume_mm3": round(final_mm3, 3),
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
    p.add_argument("--no-multi-segment", action="store_true",
                   help="Disable per-click-new-segment mode (matches "
                        "the OLD bright-seed behaviour where every "
                        "click extends one shared mask). Default ON "
                        "matches slicer_remote_bright_seed.py: each "
                        "click starts a fresh nnInteractive session "
                        "via Segmenter.reset_segment().")
    p.add_argument("--min-segment-voxels", type=int, default=200,
                   help="Reject any click whose resulting NEW segment "
                        "is smaller than this (default 200 voxels). "
                        "Filters out single-voxel artifact clicks "
                        "that nnInteractive couldn't grow into "
                        "anything coherent.")
    p.add_argument("--max-segment-voxels", type=int, default=None,
                   help="Reject any click whose resulting NEW segment "
                        "is larger than this. Default = "
                        "max_explosion_frac * total_voxels (~50%% of "
                        "volume), which catches runaway background "
                        "blobs.")
    p.add_argument("--min-local-density", type=float, default=0.4,
                   help="Required fraction of bright neighbors in the "
                        "(2*radius+1)^3 cube around each candidate "
                        "(default 0.4 = 50/125 in a 5x5x5 cube). Set "
                        "to 0 to disable. Filters out isolated bright "
                        "background-noise voxels (the IMPC mouse "
                        "step-6 failure mode).")
    p.add_argument("--neighborhood-radius", type=int, default=2,
                   help="Radius (in voxels) of the local-density "
                        "neighborhood cube (default 2 -> 5x5x5).")
    p.add_argument("--intensity-drop-floor-frac", type=float, default=0.0,
                   help="Stop when a click's intensity falls below "
                        "threshold + (peak - threshold) * frac. "
                        "Default 0 = disabled. 0.5 is a good auto-"
                        "saturation default (stops once we're down to "
                        "the bottom half of the bright tail, i.e. "
                        "voxels no more statistically obvious than the "
                        "ambient bright background).")
    p.add_argument("--min-clicks-before-drop-stop", type=int, default=5,
                   help="Minimum number of accepted clicks before the "
                        "intensity-drop stop rule is allowed to fire. "
                        "Prevents premature exit on volumes where the "
                        "first few candidates happen to be on the "
                        "lower end of the bright tail (default 5).")
    p.add_argument("--auto-saturate", action="store_true",
                   help="Convenience: run until natural saturation. "
                        "Implies --max-steps 500, --no-stop-rules, "
                        "--intensity-drop-floor-frac 0.5 unless those "
                        "are overridden on the command line. The loop "
                        "then exits when either (a) the candidate "
                        "list is exhausted, (b) click intensity drops "
                        "into 'background-bright' territory, or (c) "
                        "the max-steps safety cap is reached.")
    p.add_argument("--autopilot", action="store_true",
                   help="Auto-derive every tunable knob from the CT "
                        "volume's intensity histogram (see "
                        "auto_params.py) and then run to natural "
                        "saturation. Picks --intensity-percentile, "
                        "--intensity-drop-floor-frac, "
                        "--min-segment-voxels, --max-segment-voxels, "
                        "--max-candidates, and turns on --auto-"
                        "saturate. Any flag explicitly passed on the "
                        "command line takes precedence (so you can "
                        "autopilot and override one knob).")
    return p.parse_args(argv)


def _user_passed(flag: str, argv) -> bool:
    """Return True if the user explicitly typed ``--flag`` (or
    ``--flag=...``) on the command line. ``argv`` may be None (use
    ``sys.argv``) or a pre-built list (unit tests).

    We use this to make ``--autopilot`` respect any flag the caller
    already overrode: argparse can't natively distinguish a default
    from "user typed the default value", so we sniff argv directly.
    Acceptable trade-off: it's a few lines, the alternative
    (custom Action subclasses for every flag) is much heavier.
    """
    src = list(argv) if argv is not None else sys.argv[1:]
    needles = (f"--{flag}", f"--{flag}=")
    return any(
        item == needles[0] or item.startswith(needles[1])
        for item in src
    )


def main(argv=None) -> int:
    args = _parse_args(argv)
    region_bbox = None
    if args.region_bbox:
        region_bbox = json.loads(args.region_bbox)

    # --autopilot: derive every tunable from the volume histogram via
    # auto_params, then fall through to the --auto-saturate path. Any
    # flag the user explicitly passed wins over the autopilot pick.
    if args.autopilot:
        try:
            import auto_params as _ap
            ap_result = _ap.derive_from_path(args.input)
        except Exception as exc:
            log.error("--autopilot failed to read %s: %s", args.input, exc)
            return 2
        log.info(
            "--autopilot derived: percentile=%.2f, "
            "intensity_drop_floor_frac=%.3f, min_segment_voxels=%d, "
            "max_segment_voxels=%d, max_candidates=%d "
            "(tail_ratio=%.3f, voxel_count=%d)",
            ap_result.percentile,
            ap_result.intensity_drop_floor_frac,
            ap_result.min_segment_voxels,
            ap_result.max_segment_voxels,
            ap_result.max_candidates,
            ap_result.meta.get("tail_ratio") or float("nan"),
            ap_result.meta.get("voxel_count", 0),
        )
        if not _user_passed("intensity-percentile", argv):
            args.intensity_percentile = ap_result.percentile
        if not _user_passed("intensity-drop-floor-frac", argv):
            args.intensity_drop_floor_frac = (
                ap_result.intensity_drop_floor_frac
            )
        if not _user_passed("min-segment-voxels", argv):
            args.min_segment_voxels = ap_result.min_segment_voxels
        if not _user_passed("max-segment-voxels", argv):
            args.max_segment_voxels = ap_result.max_segment_voxels
        if not _user_passed("max-candidates", argv):
            args.max_candidates = ap_result.max_candidates
        if not _user_passed("min-local-density", argv):
            args.min_local_density = ap_result.min_local_density
        if not _user_passed("neighborhood-radius", argv):
            args.neighborhood_radius = ap_result.neighborhood_radius
        if not _user_passed("min-clicks-before-drop-stop", argv):
            args.min_clicks_before_drop_stop = (
                ap_result.min_clicks_before_drop_stop
            )
        args.auto_saturate = True

    # --auto-saturate sets sensible "run-to-exhaustion" defaults but
    # never overrides values the caller passed explicitly. argparse
    # uses sentinels (the registered defaults) to detect "user did
    # not set this flag", so the check below compares against the
    # parser's defaults.
    if args.auto_saturate:
        if args.max_steps == 50:
            args.max_steps = 500
        args.no_stop_rules = True
        if args.intensity_drop_floor_frac == 0.0:
            args.intensity_drop_floor_frac = 0.5
        log.info(
            "--auto-saturate enabled: max_steps=%d, no_stop_rules=True, "
            "intensity_drop_floor_frac=%.2f",
            args.max_steps, args.intensity_drop_floor_frac,
        )

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
        multi_segment=not args.no_multi_segment,
        min_segment_voxels=args.min_segment_voxels,
        max_segment_voxels=args.max_segment_voxels,
        min_local_density=args.min_local_density,
        neighborhood_radius=args.neighborhood_radius,
        intensity_drop_floor_frac=args.intensity_drop_floor_frac,
        min_clicks_before_drop_stop=args.min_clicks_before_drop_stop,
    )
    print(json.dumps({k: v for k, v in result.items()
                      if k != "history"}, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
