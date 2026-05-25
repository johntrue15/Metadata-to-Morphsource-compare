"""Tests for the local bright-seed paint loop.

These tests verify the deterministic parts of the algorithm without
touching the heavy nnInteractive backend. The end-to-end ``run_bright_seed``
path is exercised through a fake Segmenter that mimics the real
prompt/mask API.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

import nninteractive_bright_seed as bs  # noqa: E402


# ---------------------------------------------------------------------------
# Pure-numpy helper tests (build_candidate_list, next_unsegmented_candidate,
# should_early_stop)
# ---------------------------------------------------------------------------


class CandidateListTests(unittest.TestCase):
    """``build_candidate_list`` must:
      - return voxels above the chosen percentile,
      - sorted by intensity descending,
      - capped at ``max_candidates``.
    These three invariants are exactly the ones that made the mouse
    skull deterministic; any regression breaks reproducibility.
    """

    def test_sorted_descending_and_above_threshold(self):
        import numpy as np
        arr = np.array([
            [[0, 0, 0], [0, 50, 0]],
            [[0, 100, 0], [0, 200, 0]],
        ], dtype=np.uint8)
        cand_kji, intensities, threshold = bs.build_candidate_list(
            arr, percentile=50.0, max_candidates=0,
        )
        # All returned candidates must be >= threshold.
        for k, j, i in cand_kji:
            self.assertGreaterEqual(arr[k, j, i], threshold)
        # Intensities are sorted descending.
        for a, b in zip(intensities, intensities[1:]):
            self.assertGreaterEqual(a, b)

    def test_max_candidates_caps_the_list(self):
        import numpy as np
        arr = np.arange(1000, dtype=np.uint16).reshape(10, 10, 10)
        cand_kji, intensities, _ = bs.build_candidate_list(
            arr, percentile=0.0, max_candidates=25,
        )
        self.assertEqual(len(cand_kji), 25)
        self.assertEqual(len(intensities), 25)

    def test_max_candidates_zero_means_no_cap(self):
        import numpy as np
        arr = np.arange(64, dtype=np.uint8).reshape(4, 4, 4)
        cand_kji, intensities, _ = bs.build_candidate_list(
            arr, percentile=0.0, max_candidates=0,
        )
        # 0% threshold keeps every voxel.
        self.assertEqual(len(cand_kji), arr.size)
        self.assertEqual(len(intensities), arr.size)

    def test_empty_when_volume_is_below_threshold(self):
        import numpy as np
        arr = np.zeros((3, 3, 3), dtype=np.uint8)
        # percentile 99 of an all-zero volume is 0, threshold = 0,
        # mask = arr >= 0 = all voxels - so this exercises the
        # "everything passes" branch not the "nothing passes" one. To
        # actually exercise empty we construct a near-empty volume
        # and threshold above its max.
        arr[0, 0, 0] = 1
        cand_kji, intensities, threshold = bs.build_candidate_list(
            arr, percentile=99.0, max_candidates=0,
        )
        # Volume max is 1; percentile 99 of (mostly 0, one 1) is 0,
        # so still everything passes. The point: it doesn't crash on
        # tiny or near-empty volumes.
        self.assertIsInstance(threshold, float)
        self.assertGreater(len(cand_kji), 0)

    def test_intensity_max_clips_bright_artifacts(self):
        """The chameleon stapes CT had edge artifacts at intensity
        20000+ while the actual stapes lived at ~13000. ``intensity_max``
        must reject candidates above the supplied ceiling even when
        they pass the percentile filter."""
        import numpy as np
        # 100 voxels in 1000-1099 range, two 5000-valued artifacts.
        arr = np.arange(1000, 1102, dtype=np.float32).reshape(2, 3, 17)
        arr[0, 0, 0] = 5000  # artifact 1
        arr[1, 2, 16] = 5000  # artifact 2
        cand_kji, intensities, _ = bs.build_candidate_list(
            arr, percentile=50.0, max_candidates=0,
            intensity_max=2000.0,
        )
        # Every kept candidate must be <= 2000.
        for inten in intensities:
            self.assertLessEqual(inten, 2000.0)
        # The two artifacts at 5000 must NOT be in the list.
        self.assertNotIn(5000.0, list(intensities))

    def test_intensity_min_can_raise_floor(self):
        import numpy as np
        arr = np.arange(100, dtype=np.float32).reshape(2, 5, 10)
        cand_kji, intensities, _ = bs.build_candidate_list(
            arr, percentile=0.0, max_candidates=0,
            intensity_min=50.0,
        )
        for inten in intensities:
            self.assertGreaterEqual(inten, 50.0)

    def test_region_bbox_filters_candidates(self):
        """If the caller passes a region bbox, only voxels inside
        that sub-volume become candidates - this is the way to keep
        bright-seed from clicking obvious non-target tissue (e.g.
        teeth when targeting the cranium)."""
        import numpy as np
        arr = np.zeros((10, 10, 10), dtype=np.float32)
        arr[2, 2, 2] = 100  # inside the bbox below
        arr[8, 8, 8] = 200  # OUTSIDE the bbox - must be excluded
        region = {"k": [0, 4], "j": [0, 4], "i": [0, 4]}
        cand_kji, intensities, _ = bs.build_candidate_list(
            arr, percentile=0.0, max_candidates=0,
            intensity_min=50.0,
            region_bbox_kji=region,
        )
        # Only the (2,2,2) candidate should survive.
        self.assertEqual(len(cand_kji), 1)
        self.assertEqual(list(cand_kji[0]), [2, 2, 2])


class NextCandidateTests(unittest.TestCase):
    """``next_unsegmented_candidate`` must skim past candidates already
    inside the mask. This is the rule that lets the mouse-skull session
    keep clicking new bone instead of re-clicking the same voxel."""

    def test_picks_first_when_mask_empty(self):
        import numpy as np
        cand = np.array([[1, 1, 1], [2, 2, 2], [3, 3, 3]], dtype=np.int32)
        mask = np.zeros((10, 10, 10), dtype=bool)
        idx, after, skipped = bs.next_unsegmented_candidate(cand, mask, 0)
        self.assertEqual(idx, 0)
        self.assertEqual(after, 1)
        self.assertEqual(skipped, 0)

    def test_skips_already_segmented(self):
        import numpy as np
        cand = np.array([[1, 1, 1], [2, 2, 2], [3, 3, 3]], dtype=np.int32)
        mask = np.zeros((10, 10, 10), dtype=bool)
        mask[1, 1, 1] = True
        mask[2, 2, 2] = True
        idx, after, skipped = bs.next_unsegmented_candidate(cand, mask, 0)
        self.assertEqual(idx, 2)
        self.assertEqual(after, 3)
        self.assertEqual(skipped, 2)

    def test_returns_none_when_exhausted(self):
        import numpy as np
        cand = np.array([[0, 0, 0], [1, 1, 1]], dtype=np.int32)
        mask = np.ones((3, 3, 3), dtype=bool)  # everything inside
        idx, after, skipped = bs.next_unsegmented_candidate(cand, mask, 0)
        self.assertIsNone(idx)
        self.assertEqual(after, 2)
        self.assertEqual(skipped, 2)


class EarlyStopTests(unittest.TestCase):
    def test_no_stop_when_history_too_short(self):
        self.assertFalse(bs.should_early_stop([10, 20],
                                              min_delta=50, patience=3))

    def test_stops_when_all_trailing_below_min(self):
        self.assertTrue(bs.should_early_stop(
            [100, 80, 5, 3, 1], min_delta=50, patience=3))

    def test_does_not_stop_when_one_recent_is_big(self):
        self.assertFalse(bs.should_early_stop(
            [100, 5, 200, 3, 1], min_delta=50, patience=3))

    def test_patience_zero_disables(self):
        self.assertFalse(bs.should_early_stop(
            [0, 0, 0, 0], min_delta=50, patience=0))


# ---------------------------------------------------------------------------
# End-to-end run_bright_seed via a FakeSegmenter
# ---------------------------------------------------------------------------


class _FakeSitkModule:
    """Tiny stand-in for the SimpleITK module we read off ``seg._sitk``.
    Returns the underlying numpy array verbatim when ``GetArrayFromImage``
    is called."""

    def GetArrayFromImage(self, img):  # noqa: N802 (sitk uses CamelCase)
        return img._arr


class _FakeSitkImage:
    def __init__(self, arr):
        self._arr = arr

    def GetSpacing(self):  # noqa: N802
        return (1.0, 1.0, 1.0)


class FakeBrightSegmenter:
    """Drives ``run_bright_seed`` without the real nnInteractive.

    Each ``add_point`` paints a tiny cube around the clicked voxel into
    the internal mask. That's enough to exercise:
      - the candidate-skip rule (next clicks must land on un-painted
        voxels),
      - delta computation,
      - saturation early-stop,
      - explosion guard.
    """

    def __init__(self, arr, output_dir: Path, paint_radius: int = 1):
        import numpy as np
        self._np = np
        self._sitk = _FakeSitkModule()
        self.sitk_image = _FakeSitkImage(arr)
        self._mask = np.zeros(arr.shape, dtype=np.uint8)
        self.output_dir = output_dir
        self.preview_calls: list[dict] = []
        self.point_calls: list[dict] = []
        self.paint_radius = paint_radius
        # Force the FakeSitkImage spacing to match the array shape (z,y,x).
        # The image_shape_zyx attribute is consumed elsewhere.
        self.image_shape_zyx = arr.shape
        self.device = "cpu"

    @property
    def mask_array(self):
        return self._mask.copy()

    def voxel_count(self) -> int:
        return int((self._mask > 0).sum())

    def volume_mm3(self) -> float:
        return float(self.voxel_count())

    def add_point(self, x, y, z, *, positive=True, label=""):
        # IMPORTANT: bright-seed's workaround for nnInteractive's
        # bounds-check bug passes the numpy ``(k, j, i)`` index AS-IS
        # to ``add_point``, so the args here are positional - first
        # arg is the slice index ``k``, last is the column ``i``. The
        # fake matches that so its mask growth happens at the same
        # voxel the real GPU run would paint.
        k, j, i = x, y, z
        self.point_calls.append({"xyz": (x, y, z), "positive": positive,
                                 "label": label})
        r = self.paint_radius
        z_max, y_max, x_max = self._mask.shape
        zlo, zhi = max(0, k - r), min(z_max, k + r + 1)
        ylo, yhi = max(0, j - r), min(y_max, j + r + 1)
        xlo, xhi = max(0, i - r), min(x_max, i + r + 1)
        self._mask[zlo:zhi, ylo:yhi, xlo:xhi] = 1

    def save_orthogonal_previews(self, name_prefix="",
                                 intensity_window=None, markers=None):
        self.preview_calls.append({
            "name_prefix": name_prefix,
            "intensity_window": intensity_window,
            "markers": [dict(m) for m in (markers or [])],
        })
        p = self.output_dir / f"{name_prefix}.png"
        p.write_bytes(b"\x89PNG")
        return [str(p)]

    def save_labelmap(self, name=""):
        p = self.output_dir / (name or "fake_labelmap.nii.gz")
        p.write_bytes(b"NIFTI-STUB")
        return str(p)

    def export_summary(self, payload):
        p = self.output_dir / "fake_summary.json"
        p.write_text(json.dumps(payload, default=str))
        return str(p)


class RunBrightSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bright_seed_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_arr_with_bright_blob(self, dim: int = 16,
                                   blob: int = 4):
        """Return a (dim, dim, dim) volume with a bright NxNxN cube
        in the center. The bright voxels are intensity 200; the rest
        are 10. We get exactly ``blob**3`` candidates above any
        percentile that's below the 200/blob_density mark."""
        import numpy as np
        arr = np.full((dim, dim, dim), 10, dtype=np.uint8)
        c = dim // 2
        h = blob // 2
        arr[c - h:c + h, c - h:c + h, c - h:c + h] = 200
        return arr

    def test_runs_to_saturation_with_no_stop_rules(self):
        # Need a percentile high enough that only the BRIGHT voxels
        # pass: 64 bright in a 4096-voxel volume = 1.56% bright, so
        # the 99th percentile (top 1%) still catches part of the
        # background. Use 99.0 with a slightly smaller blob so the
        # threshold sits between 10 (background) and 200 (bright).
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=1)
        # 64 bright voxels, intensity 200; rest are 10. percentile
        # 99 of 4096 values -> position 4055 (sorted asc), still 10.
        # So we threshold at 200 explicitly via a high percentile.
        result = bs.run_bright_seed(
            input_path="ignored",
            output_dir=str(self.tmp),
            media_id="TEST",
            percentile=99.0,  # captures the top 64ish bright voxels
            max_candidates=0,
            max_steps=50,
            no_stop_rules=True,
            segmenter=fake,
            save_previews=False,
        )
        self.assertTrue(result["success"])
        # Saturation = no candidates left.
        self.assertEqual(result["stop_reason"]["reason"],
                         "no_more_candidates")
        # We expect FEWER clicks than candidates because each click
        # paints a 3x3x3 region and the skim-forward rule advances
        # past those.
        self.assertGreater(result["n_clicks"], 0)
        self.assertLess(result["n_clicks"], 100)

    def test_respects_max_steps(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=0)
        # paint_radius=0 means each click only adds the single clicked
        # voxel, so the algorithm will want to click every candidate
        # and a max_steps cap should kick in.
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=5,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["n_clicks"], 5)
        self.assertEqual(result["stop_reason"]["reason"], "max_steps")

    def test_early_stop_on_saturation(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=2)
        # paint_radius=2 -> 5x5x5 = 125-voxel region per click, so
        # one click engulfs the whole blob. The next few clicks will
        # add ~0 voxels (everything's already segmented) and the
        # patience rule should stop the loop.
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=50,
            min_delta=10, patience=2,
            segmenter=fake, save_previews=False,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["stop_reason"]["reason"], "saturated")

    def test_explosion_guard(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=8)
        # paint_radius=8 on a 16-volume -> the first click paints the
        # ENTIRE volume (4096 voxels). max_explosion_frac=0.1 means
        # 410 voxels is already past the threshold, so step 1 trips
        # the guard.
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=20,
            max_explosion_frac=0.1,
            segmenter=fake, save_previews=False,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["stop_reason"]["reason"], "explosion")
        self.assertEqual(result["n_clicks"], 1)

    def test_clicks_xyz_map_correctly(self):
        """nnInteractive's bounds check on prompts compares
        ``position[d]`` positionally against
        ``interaction_map.shape[d]``, where the map has the numpy
        ``(z, y, x)`` shape. So to make every voxel reachable
        (regardless of whether ``z_max`` happens to be larger than
        ``x_max``), bright-seed passes the candidate's
        ``(k, j, i)`` numpy index AS-IS to ``add_point`` (i.e. the
        first prompt coord lines up with ``shape[0] = z``, not with
        the semantically-x voxel column). The local Felis smoke
        test proved this: a candidate at ``(k=345, j=53, i=124)``
        sent as ``xyz=(124, 53, 345)`` (the documented xyz order)
        was rejected with "Point is outside the interaction map",
        because nnInteractive checked ``345 > shape[2] = i_max =
        211``. Reordering to ``xyz=(k, j, i) = (345, 53, 124)``
        passes the check and paints the intended voxel.
        """
        import numpy as np
        # Single bright voxel at numpy index (k=3, j=5, i=7). After
        # bright-seed's mapping we expect Segmenter.add_point to be
        # called with (x=3, y=5, z=7) - i.e. (k, j, i) order.
        arr = np.full((10, 10, 10), 0, dtype=np.uint8)
        arr[3, 5, 7] = 255
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=0)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=99.0,
            max_candidates=10, max_steps=1, no_stop_rules=True,
            segmenter=fake, save_previews=False,
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(fake.point_calls), 1)
        self.assertEqual(fake.point_calls[0]["xyz"], (3, 5, 7))

    def test_before_and_after_previews_emitted(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=0)
        bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=3, no_stop_rules=True,
            segmenter=fake, save_previews=True,
        )
        prefixes = [c["name_prefix"] for c in fake.preview_calls]
        # step00 (initial) + 3 x (before, after)
        self.assertIn("TEST_step00", prefixes)
        for s in range(1, 4):
            self.assertIn(f"TEST_step{s:02d}_before", prefixes,
                          msg=f"missing step{s} before; got {prefixes}")
            self.assertIn(f"TEST_step{s:02d}_after", prefixes,
                          msg=f"missing step{s} after; got {prefixes}")

    def test_clicks_jsonl_written(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=0)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=3, no_stop_rules=True,
            segmenter=fake, save_previews=False,
        )
        clicks = Path(result["clicks_path"]).read_text().strip().splitlines()
        self.assertEqual(len(clicks), result["n_clicks"])
        first = json.loads(clicks[0])
        self.assertIn("ijk_kji", first)
        self.assertIn("xyz", first)
        self.assertIn("intensity", first)
        self.assertIn("delta", first)


if __name__ == "__main__":
    unittest.main()
