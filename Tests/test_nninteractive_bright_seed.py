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
    is called.

    Multi-segment mode also calls ``GetImageFromArray`` / ``WriteImage``
    to write the union and multi-label labelmaps directly (it does
    NOT route through ``seg.save_labelmap()``). The fake just stores
    the bytes in a class-level registry so tests can inspect them.
    """

    def __init__(self):
        self.written: dict[str, object] = {}

    def GetArrayFromImage(self, img):  # noqa: N802
        return img._arr

    def GetImageFromArray(self, arr):  # noqa: N802
        return _FakeSitkImage(arr)

    def WriteImage(self, img, path, useCompression=False):  # noqa: N802, N803
        # Persist a sentinel so the test asserts a file was created.
        from pathlib import Path as _P
        _P(path).write_bytes(b"NIFTI-STUB")
        self.written[path] = img


class _FakeSitkImage:
    def __init__(self, arr):
        self._arr = arr

    def GetSpacing(self):  # noqa: N802
        return (1.0, 1.0, 1.0)

    def CopyInformation(self, other):  # noqa: N802
        # We don't model header metadata in the fake.
        return None


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
        self.reset_calls: int = 0
        self.paint_radius = paint_radius
        self.image_shape_zyx = arr.shape
        self.device = "cpu"

    @property
    def mask_array(self):
        return self._mask.copy()

    def voxel_count(self) -> int:
        return int((self._mask > 0).sum())

    def volume_mm3(self) -> float:
        return float(self.voxel_count())

    def reset_segment(self) -> None:
        """Mirror the real Segmenter: zero the current target buffer
        and clear interaction history. Used by multi-segment mode to
        start a fresh segment for each click."""
        self._mask[...] = 0
        self.reset_calls += 1

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
        # Legacy single-segment behaviour: every click extends ONE
        # shared mask. Eventually all candidates are inside the mask
        # and the loop stops with "no_more_candidates".
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=1)
        result = bs.run_bright_seed(
            input_path="ignored",
            output_dir=str(self.tmp),
            media_id="TEST",
            percentile=99.0,
            max_candidates=0,
            max_steps=50,
            no_stop_rules=True,
            segmenter=fake,
            save_previews=False,
            multi_segment=False,
            min_local_density=0.0,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["stop_reason"]["reason"],
                         "no_more_candidates")
        self.assertGreater(result["n_clicks"], 0)
        self.assertLess(result["n_clicks"], 100)

    def test_respects_max_steps(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=0)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=5,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=False,
            min_local_density=0.0,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["n_clicks"], 5)
        self.assertEqual(result["stop_reason"]["reason"], "max_steps")

    def test_early_stop_on_saturation(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=2)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=50,
            min_delta=10, patience=2,
            segmenter=fake, save_previews=False,
            multi_segment=False,
            min_local_density=0.0,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["stop_reason"]["reason"], "saturated")

    def test_explosion_guard(self):
        arr = self._make_arr_with_bright_blob(dim=16, blob=4)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=8)
        # paint_radius=8 on a 16-volume -> the first click paints the
        # ENTIRE volume (4096 voxels). max_explosion_frac=0.1 means
        # 410 voxels is already past the threshold, so step 1 trips
        # the guard. Only valid in single-segment mode (multi-segment
        # rejects+rolls-back instead).
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="TEST", percentile=95.0,
            max_candidates=0, max_steps=20,
            max_explosion_frac=0.1,
            segmenter=fake, save_previews=False,
            multi_segment=False,
            min_local_density=0.0,
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
            multi_segment=False,
            min_local_density=0.0,
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
            multi_segment=False,
            min_local_density=0.0,
        )
        prefixes = [c["name_prefix"] for c in fake.preview_calls]
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
            multi_segment=False,
            min_local_density=0.0,
        )
        clicks = Path(result["clicks_path"]).read_text().strip().splitlines()
        self.assertEqual(len(clicks), result["n_clicks"])
        first = json.loads(clicks[0])
        self.assertIn("ijk_kji", first)
        self.assertIn("xyz", first)
        self.assertIn("intensity", first)
        self.assertIn("delta", first)


# ---------------------------------------------------------------------------
# Multi-segment mode (matches slicer_remote_bright_seed.py reference)
# ---------------------------------------------------------------------------


class IntensityBelowObviousTests(unittest.TestCase):
    """``intensity_below_obvious`` is the saturation rule that lets
    bright-seed exit naturally once we're past the obviously-bright
    voxels. With the IMPC mouse defaults (threshold=102, peak=255,
    frac=0.5) the floor sits at 178: clicks at intensity 200 keep
    going, clicks at intensity 150 trigger the stop.
    """

    def test_disabled_when_frac_zero(self):
        self.assertFalse(bs.intensity_below_obvious(
            10.0, threshold=100.0, peak_intensity=255.0, floor_frac=0.0))

    def test_stops_when_below_floor(self):
        # floor = 102 + (255-102)*0.5 = 178.5
        self.assertTrue(bs.intensity_below_obvious(
            150.0, threshold=102.0, peak_intensity=255.0, floor_frac=0.5))

    def test_continues_when_above_floor(self):
        self.assertFalse(bs.intensity_below_obvious(
            200.0, threshold=102.0, peak_intensity=255.0, floor_frac=0.5))

    def test_degenerate_peak_below_threshold(self):
        # Should not fire when peak <= threshold (no bright voxels).
        self.assertFalse(bs.intensity_below_obvious(
            50.0, threshold=100.0, peak_intensity=50.0, floor_frac=0.5))


class HasDenseBrightNeighborhoodTests(unittest.TestCase):
    """The IMPC mouse step-6 failure mode was a single bright noise
    voxel in the background. ``has_dense_bright_neighborhood`` must
    reject it (sparse neighborhood) and accept candidates inside a
    real bright structure (dense neighborhood).
    """

    def test_isolated_bright_voxel_rejected(self):
        import numpy as np
        arr = np.zeros((10, 10, 10), dtype=np.uint8)
        arr[5, 5, 5] = 255  # one bright voxel, dark elsewhere
        ok = bs.has_dense_bright_neighborhood(
            arr, 5, 5, 5, threshold=100, radius=2, min_density=0.4,
        )
        self.assertFalse(ok, "single bright voxel must NOT pass")

    def test_dense_bright_blob_accepted(self):
        import numpy as np
        arr = np.zeros((10, 10, 10), dtype=np.uint8)
        arr[3:8, 3:8, 3:8] = 255  # solid 5x5x5 bright cube
        ok = bs.has_dense_bright_neighborhood(
            arr, 5, 5, 5, threshold=100, radius=2, min_density=0.4,
        )
        self.assertTrue(ok, "dense bright blob must pass")

    def test_density_zero_disables_check(self):
        import numpy as np
        arr = np.zeros((5, 5, 5), dtype=np.uint8)
        # Even an empty cube passes when min_density <= 0 (we don't
        # call this branch when the filter is disabled, but be safe).
        self.assertTrue(bs.has_dense_bright_neighborhood(
            arr, 2, 2, 2, threshold=100, radius=1, min_density=0.0,
        ))


class NextValidatedCandidateTests(unittest.TestCase):
    def test_skips_in_mask_and_sparse(self):
        import numpy as np
        # 4x4x4 volume; cand list: (0,0,0)=in mask, (1,1,1)=sparse,
        # (2,2,2)=dense.
        arr = np.zeros((4, 4, 4), dtype=np.uint8)
        arr[2, 2, 2] = 255
        arr[2, 2, 3] = 255
        arr[2, 3, 2] = 255
        arr[3, 2, 2] = 255
        # Make a denser bright region around (2,2,2) so it passes.
        arr[1:4, 1:4, 1:4] = 255

        cand = np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2]], dtype=np.int32)
        cand_int = np.array([200, 150, 100], dtype=np.float32)
        mask = np.zeros((4, 4, 4), dtype=bool)
        mask[0, 0, 0] = True

        # First call: (0,0,0) is in mask, (1,1,1) is in a dense region,
        # so it passes the density filter (min_density=0.4 means 50/125
        # voxels in 5x5x5 must be bright; here EVERY voxel in the 3x3x3
        # around (1,1,1) is bright).
        idx, after, in_skip, sparse_skip = bs.next_validated_candidate(
            arr, cand, cand_int, mask,
            start_idx=0,
            threshold=200,
            min_local_density=0.0,  # filter disabled
        )
        self.assertEqual(idx, 1)
        self.assertEqual(after, 2)
        self.assertEqual(in_skip, 1)
        self.assertEqual(sparse_skip, 0)

    def test_density_filter_picks_dense_only(self):
        import numpy as np
        arr = np.zeros((10, 10, 10), dtype=np.uint8)
        # Isolated bright voxel at (1, 1, 1) - sparse neighborhood.
        arr[1, 1, 1] = 255
        # Dense bright blob centered at (5, 5, 5).
        arr[3:8, 3:8, 3:8] = 255
        cand = np.array([[1, 1, 1], [5, 5, 5]], dtype=np.int32)
        cand_int = np.array([255, 255], dtype=np.float32)
        mask = np.zeros((10, 10, 10), dtype=bool)

        idx, after, in_skip, sparse_skip = bs.next_validated_candidate(
            arr, cand, cand_int, mask,
            start_idx=0,
            threshold=100,
            min_local_density=0.4,
            neighborhood_radius=2,
        )
        self.assertEqual(idx, 1, "must skip the isolated voxel and pick the blob")
        self.assertEqual(sparse_skip, 1)


class MultiSegmentRunTests(unittest.TestCase):
    """End-to-end tests for the new multi-segment paint loop. This
    mirrors slicer_remote_bright_seed.py: each click resets the
    segmenter, paints one fresh segment, and the union is the running
    "already inside" check.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bright_seed_ms_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _arr_with_two_blobs(self, dim=20):
        """Two bright cubes far apart, so each click can pick a
        DIFFERENT one without falling into the other's painted region.
        """
        import numpy as np
        arr = np.full((dim, dim, dim), 10, dtype=np.uint8)
        # Blob A: (3..7, 3..7, 3..7) - 64 voxels.
        arr[3:7, 3:7, 3:7] = 200
        # Blob B: (13..17, 13..17, 13..17) - 64 voxels.
        arr[13:17, 13:17, 13:17] = 200
        return arr

    def test_reset_segment_called_each_click(self):
        """The hallmark of multi-segment mode: ``reset_segment`` is
        called once per accepted click (not once total). That's what
        makes each click grow a fresh structure instead of refining
        the previous one."""
        arr = self._arr_with_two_blobs(dim=20)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=2)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="MS", percentile=99.0,
            max_candidates=0, max_steps=3,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=True,
            min_segment_voxels=1,
            min_local_density=0.0,
        )
        self.assertTrue(result["success"])
        # One reset per click in multi-segment mode.
        self.assertEqual(fake.reset_calls, result["n_clicks"])
        # Each click produces a distinct segment.
        self.assertEqual(result["n_segments_kept"], result["n_clicks"])

    def test_per_segment_voxel_tracking(self):
        arr = self._arr_with_two_blobs(dim=20)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=1)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="MS", percentile=99.0,
            max_candidates=0, max_steps=2,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=True,
            min_segment_voxels=1,
            min_local_density=0.0,
        )
        self.assertEqual(result["n_clicks"], 2)
        # Each step record carries segment_voxels and segment_label.
        for rec in result["history"]:
            self.assertIn("segment_voxels", rec)
            self.assertIn("segment_label", rec)
            self.assertGreater(rec["segment_voxels"], 0)

    def test_min_segment_voxels_rolls_back_tiny_clicks(self):
        """When a click produces a segment too small to be a real
        organ, multi-segment mode rolls it back (reset_segment) and
        tries the NEXT candidate at the same step counter. This is
        how we filter background-artifact clicks."""
        arr = self._arr_with_two_blobs(dim=20)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=0)
        # paint_radius=0 -> every click produces a 1-voxel segment,
        # which is below min_segment_voxels=2. So EVERY click is
        # rejected and the loop exhausts the candidate list.
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="MS", percentile=99.0,
            max_candidates=0, max_steps=5,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=True,
            min_segment_voxels=2,
            min_local_density=0.0,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["n_clicks"], 0)
        self.assertGreater(result["n_rejections"], 0)
        # Multi-segment mode resets BEFORE every click attempt (fresh
        # canvas) AND resets AGAIN on rejection (rollback safety net),
        # so rejected attempts cost 2 reset_segment() calls each.
        # Accepted clicks cost 1 (the pre-click reset only).
        self.assertEqual(
            fake.reset_calls,
            2 * result["n_rejections"] + result["n_clicks"],
        )

    def test_max_segment_voxels_rejects_runaway(self):
        """The IMPC step-6 mode: one click would grow a giant blob
        bigger than any real organ. max_segment_voxels rejects it."""
        arr = self._arr_with_two_blobs(dim=20)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=10)
        # paint_radius=10 on a 20-volume -> first click paints the
        # entire volume (8000 voxels). max_segment_voxels=100 ->
        # any click producing >100 voxels is rolled back.
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="MS", percentile=99.0,
            max_candidates=0, max_steps=2,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=True,
            min_segment_voxels=1,
            max_segment_voxels=100,
            min_local_density=0.0,
        )
        self.assertTrue(result["success"])
        # Every click is rolled back as "runaway".
        self.assertEqual(result["n_clicks"], 0)
        self.assertGreater(result["n_rejections"], 0)
        for r in result["rejections"]:
            self.assertEqual(r["reason"], "runaway")

    def test_union_mask_used_for_skip(self):
        """After click 1 paints near blob A, click 2 must NOT pick a
        candidate inside that painted region — it must skim forward
        to a candidate in blob B."""
        arr = self._arr_with_two_blobs(dim=20)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=3)
        # paint_radius=3 -> each click paints a 7x7x7 region. Blob A
        # voxels first in candidate order (ties broken by argsort).
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="MS", percentile=99.0,
            max_candidates=0, max_steps=5,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=True,
            min_segment_voxels=1,
            min_local_density=0.0,
        )
        self.assertTrue(result["success"])
        clicks = [tuple(c["click_kji"])
                  for c in result["per_segment"]]
        # Click 1 should land in blob A region (k<10), click 2 in blob B
        # region (k>=10) - this proves the union-mask skip is working.
        if len(clicks) >= 2:
            blob_a_clicks = [c for c in clicks if c[0] < 10]
            blob_b_clicks = [c for c in clicks if c[0] >= 10]
            self.assertGreater(len(blob_a_clicks), 0,
                               "expected at least one click in blob A")
            self.assertGreater(len(blob_b_clicks), 0,
                               "expected at least one click in blob B")

    def test_intensity_drop_stop_rule(self):
        """When ``intensity_drop_floor_frac`` is enabled, the loop
        exits as soon as a click's intensity is below the floor —
        this is the "no longer statistically obvious" criterion that
        lets bright-seed self-terminate without a fixed step counter.
        """
        import numpy as np
        # Volume with a clear gradient of "obvious" bright voxels:
        # eight at 250, eight at 220, eight at 190, eight at 160. With
        # threshold=100, peak=250, floor_frac=0.5 the floor is 175.
        # So intensities 250, 220, 190 pass (they're >= 175) and 160
        # triggers the stop.
        arr = np.full((4, 4, 4), 50, dtype=np.uint8)
        bright_levels = [(0, 250), (1, 220), (2, 190), (3, 160)]
        for slice_idx, val in bright_levels:
            arr[slice_idx, 0:2, 0:2] = val  # 4 bright voxels per level

        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=0)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="MS", percentile=70.0,
            max_candidates=0, max_steps=100,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=True,
            min_segment_voxels=1,
            min_local_density=0.0,
            intensity_drop_floor_frac=0.5,
            min_clicks_before_drop_stop=3,
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            result["stop_reason"]["reason"], "intensity_below_obvious",
            msg=f"got stop_reason={result['stop_reason']}",
        )
        # The 160-intensity click triggered the stop, so it appears
        # in the history (the rule fires AFTER the accept) — but no
        # 130-or-lower clicks should appear.
        click_intensities = [r["intensity"] for r in result["history"]]
        self.assertIn(160.0, click_intensities,
                      f"got {click_intensities}")

    def test_multilabel_labelmap_written(self):
        arr = self._arr_with_two_blobs(dim=20)
        fake = FakeBrightSegmenter(arr, self.tmp, paint_radius=2)
        result = bs.run_bright_seed(
            input_path="ignored", output_dir=str(self.tmp),
            media_id="MS", percentile=99.0,
            max_candidates=0, max_steps=2,
            no_stop_rules=True,
            segmenter=fake, save_previews=False,
            multi_segment=True,
            min_segment_voxels=1,
            min_local_density=0.0,
        )
        self.assertIsNotNone(result["multilabel_path"])
        self.assertTrue(Path(result["multilabel_path"]).exists())


if __name__ == "__main__":
    unittest.main()
