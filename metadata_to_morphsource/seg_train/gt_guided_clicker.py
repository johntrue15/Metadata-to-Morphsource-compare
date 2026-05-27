"""Ground-truth-guided click selection for nnInteractive training loops.

During *training* we treat a voxelized MorphoSource mesh (.ply → labelmap
on the CT grid) as the reference. Each step compares the current
prediction mask to that reference and proposes the next positive click in
the largest false-negative region (GT foreground missed by the model).

This is the teacher signal for active learning: the agent learns which
regions to target so Dice improves toward the mesh-derived GT. At
deployment time the same policy can be replaced by uncertainty maps from
the student model (see :mod:`confidence_router`).

Coordinates returned are Slicer IJK ``(i, j, k)`` = ``(x, y, z)`` indices
into the volume array (SimpleITK array is ``(z, y, x)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

IJK = Tuple[int, int, int]


@dataclass
class ClickPlan:
    """One proposed prompt."""

    i: int
    j: int
    k: int
    positive: bool
    reason: str
    dice_before: float
    fn_voxels: int
    fp_voxels: int


def _dice(pred: np.ndarray, gt: np.ndarray) -> float:
    pred = pred.astype(bool)
    gt = gt.astype(bool)
    inter = np.logical_and(pred, gt).sum()
    denom = int(pred.sum()) + int(gt.sum())
    if denom == 0:
        return 1.0
    return float(2.0 * inter / denom)


def pick_next_click(
    pred_zyx: np.ndarray,
    gt_zyx: np.ndarray,
    *,
    allow_negative: bool = True,
    rng: Optional[np.random.Generator] = None,
) -> Optional[ClickPlan]:
    """Choose the next point prompt from pred vs GT binary masks.

    Priority:
      1. False negatives (missed GT) — positive click at the FN voxel
         farthest from the current prediction surface.
      2. False positives (spurious pred) — negative click at the largest
         connected FP blob centroid (optional).
      3. None if masks agree.
    """
    pred = pred_zyx.astype(bool)
    gt = gt_zyx.astype(bool)
    if pred.shape != gt.shape:
        raise ValueError(f"shape mismatch pred={pred.shape} gt={gt.shape}")

    dice = _dice(pred, gt)
    fn = np.logical_and(gt, np.logical_not(pred))
    fp = np.logical_and(pred, np.logical_not(gt))
    n_fn = int(fn.sum())
    n_fp = int(fp.sum())

    if n_fn > 0:
        try:
            from scipy import ndimage
        except ImportError:
            # Fallback: random FN voxel
            rng = rng or np.random.default_rng(0)
            flat = np.flatnonzero(fn)
            idx = int(rng.choice(flat))
            z, y, x = np.unravel_index(idx, fn.shape)
            return ClickPlan(
                i=int(x), j=int(y), k=int(z),
                positive=True,
                reason="random_false_negative",
                dice_before=dice,
                fn_voxels=n_fn,
                fp_voxels=n_fp,
            )
        # Distance to nearest predicted foreground (larger = deeper inside miss)
        dist_to_pred = ndimage.distance_transform_edt(~pred)
        scores = dist_to_pred * fn
        flat_idx = int(scores.argmax())
        z, y, x = np.unravel_index(flat_idx, fn.shape)
        return ClickPlan(
            i=int(x), j=int(y), k=int(z),
            positive=True,
            reason="boundary_false_negative",
            dice_before=dice,
            fn_voxels=n_fn,
            fp_voxels=n_fp,
        )

    if allow_negative and n_fp > 0:
        try:
            from scipy import ndimage
            labeled, n_cc = ndimage.label(fp)
            if n_cc > 0:
                sizes = ndimage.sum(fp, labeled, range(1, n_cc + 1))
                blob = int(1 + np.argmax(sizes))
                coords = np.argwhere(labeled == blob)
                z, y, x = coords.mean(axis=0).astype(int)
                return ClickPlan(
                    i=int(x), j=int(y), k=int(z),
                    positive=False,
                    reason="false_positive_centroid",
                    dice_before=dice,
                    fn_voxels=n_fn,
                    fp_voxels=n_fp,
                )
        except ImportError:
            pass
        flat = np.flatnonzero(fp)
        z, y, x = np.unravel_index(int(flat[0]), fp.shape)
        return ClickPlan(
            i=int(x), j=int(y), k=int(z),
            positive=False,
            reason="false_positive_first",
            dice_before=dice,
            fn_voxels=n_fn,
            fp_voxels=n_fp,
        )

    return None


def mask_from_sitk_array(arr: np.ndarray) -> np.ndarray:
    """Binarise a labelmap array (any positive = foreground)."""
    return (arr > 0).astype(bool)
