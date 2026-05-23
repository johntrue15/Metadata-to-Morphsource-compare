"""Unit tests for the fixture / regression-gate helpers in
``.github/scripts/nninteractive_compare.py``.

These cover the pure-Python paths that don't need SimpleITK / nnInteractive
/ OpenAI to be installed, so they run on the standard `tests.yml` CI matrix.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))


class TestExportFixture(unittest.TestCase):
    """`_export_fixture` should copy only the requested files and write
    a fixture.json describing them. Files that don't exist are silently
    skipped (so we can call it from a `--skip-paint-loop` run that has
    no prediction labelmap).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_blob(self, name: str, body: bytes = b"x") -> Path:
        p = self.tmp / name
        p.write_bytes(body)
        return p

    def test_full_export_writes_all_files_and_metadata(self):
        from nninteractive_compare import _export_fixture

        ct = self._make_blob("ct_input.nii.gz", b"ct")
        gt = self._make_blob("gt_voxelized.nii.gz", b"gt")
        pred = self._make_blob("pred_input.nii.gz", b"pred")
        metrics = self._make_blob("metrics.json", b'{"dice": 0.42}')

        out = self.tmp / "fixture_out"
        result = _export_fixture(
            export_fixture_dir=out,
            ct_media_id="000408242",
            gt_media_id="000790324",
            goal="Segment the stapes",
            max_steps=6,
            voxelize_backend="vtk",
            crop_around_mesh_mm=1.5,
            ct_used=ct,
            gt_labelmap=gt,
            pred_labelmap=pred,
            metrics_path=metrics,
        )

        self.assertTrue((out / "ct.nii.gz").exists())
        self.assertTrue((out / "gt_voxelized.nii.gz").exists())
        self.assertTrue((out / "pred.nii.gz").exists())
        self.assertTrue((out / "baseline_metrics.json").exists())
        meta_path = out / "fixture.json"
        self.assertTrue(meta_path.exists())

        meta = json.loads(meta_path.read_text())
        self.assertEqual(meta["ct_media_id"], "000408242")
        self.assertEqual(meta["gt_media_id"], "000790324")
        self.assertEqual(meta["voxelize_backend"], "vtk")
        self.assertEqual(meta["max_steps"], 6)
        self.assertAlmostEqual(meta["crop_around_mesh_mm"], 1.5)
        self.assertIn("ct.nii.gz", meta["files"])
        self.assertIn("gt_voxelized.nii.gz", meta["files"])
        self.assertIn("pred.nii.gz", meta["files"])
        self.assertIn("baseline_metrics.json", meta["files"])
        self.assertEqual(result["dir"], str(out))

    def test_partial_export_skips_missing_paths(self):
        """If the paint loop didn't run, pred + metrics may be absent.
        The fixture export should still succeed and just record the
        files that *did* exist.
        """
        from nninteractive_compare import _export_fixture

        ct = self._make_blob("ct.nii.gz", b"ct")
        gt = self._make_blob("gt.nii.gz", b"gt")
        out = self.tmp / "fx"
        _export_fixture(
            export_fixture_dir=out,
            ct_media_id="ct1", gt_media_id="gt1",
            goal="", max_steps=0,
            voxelize_backend="vtk",
            crop_around_mesh_mm=0.0,
            ct_used=ct, gt_labelmap=gt,
            pred_labelmap=None, metrics_path=None,
        )
        self.assertTrue((out / "ct.nii.gz").exists())
        self.assertTrue((out / "gt_voxelized.nii.gz").exists())
        self.assertFalse((out / "pred.nii.gz").exists())
        self.assertFalse((out / "baseline_metrics.json").exists())
        meta = json.loads((out / "fixture.json").read_text())
        self.assertNotIn("pred.nii.gz", meta["files"])
        self.assertNotIn("baseline_metrics.json", meta["files"])


class TestEvaluateRegression(unittest.TestCase):
    """The dice-floor and baseline-comparison logic must:

    * pass when dice >= floor
    * fail when dice < floor (with a clear message)
    * pass when dice/iou are within ``regression_tol`` of the baseline
    * fail when dice/iou drop more than ``regression_tol`` below baseline
    * tolerate missing baseline fields without crashing
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _baseline(self, **kwargs) -> Path:
        p = self.tmp / "baseline.json"
        p.write_text(json.dumps(kwargs))
        return p

    def test_no_gates_passes_trivially(self):
        from nninteractive_compare import evaluate_regression
        passed, msgs = evaluate_regression(
            metrics={"dice": 0.5, "iou": 0.4},
            baseline_metrics_path=None,
            assert_dice=None,
        )
        self.assertTrue(passed)
        self.assertEqual(msgs, [])

    def test_assert_dice_floor_passes(self):
        from nninteractive_compare import evaluate_regression
        passed, msgs = evaluate_regression(
            metrics={"dice": 0.75},
            baseline_metrics_path=None,
            assert_dice=0.50,
        )
        self.assertTrue(passed)
        self.assertTrue(any("OK" in m for m in msgs))

    def test_assert_dice_floor_fails(self):
        from nninteractive_compare import evaluate_regression
        passed, msgs = evaluate_regression(
            metrics={"dice": 0.10},
            baseline_metrics_path=None,
            assert_dice=0.50,
        )
        self.assertFalse(passed)
        self.assertTrue(any("FAIL" in m and "dice" in m for m in msgs))

    def test_assert_dice_floor_fails_when_dice_missing(self):
        from nninteractive_compare import evaluate_regression
        passed, _ = evaluate_regression(
            metrics={},
            baseline_metrics_path=None,
            assert_dice=0.10,
        )
        self.assertFalse(passed)

    def test_baseline_within_tolerance_passes(self):
        from nninteractive_compare import evaluate_regression
        baseline = self._baseline(dice=0.80, iou=0.65)
        passed, msgs = evaluate_regression(
            metrics={"dice": 0.795, "iou": 0.645},
            baseline_metrics_path=baseline,
            assert_dice=None,
            regression_tol=0.01,
        )
        self.assertTrue(passed, msgs)

    def test_baseline_regression_fails(self):
        from nninteractive_compare import evaluate_regression
        baseline = self._baseline(dice=0.80, iou=0.65)
        passed, msgs = evaluate_regression(
            metrics={"dice": 0.50, "iou": 0.40},
            baseline_metrics_path=baseline,
            assert_dice=None,
            regression_tol=0.01,
        )
        self.assertFalse(passed)
        failures = [m for m in msgs if m.startswith("[FAIL]")]
        self.assertEqual(len(failures), 2,
                         f"expected dice+iou failures, got {msgs!r}")

    def test_baseline_missing_field_is_skipped_not_failed(self):
        from nninteractive_compare import evaluate_regression
        baseline = self._baseline(dice=0.80)
        passed, msgs = evaluate_regression(
            metrics={"dice": 0.81},
            baseline_metrics_path=baseline,
            assert_dice=None,
            regression_tol=0.01,
        )
        self.assertTrue(passed, msgs)
        self.assertTrue(any("SKIP" in m or "iou" not in m for m in msgs))

    def test_missing_baseline_path_fails_loudly(self):
        from nninteractive_compare import evaluate_regression
        passed, msgs = evaluate_regression(
            metrics={"dice": 0.9},
            baseline_metrics_path=self.tmp / "does_not_exist.json",
            assert_dice=None,
        )
        self.assertFalse(passed)
        self.assertTrue(any("not found" in m for m in msgs))


class TestFromFixtureDispatcher(unittest.TestCase):
    """``run_comparison_from_fixture`` should refuse to run if the
    fixture directory is incomplete, and should short-circuit when a
    cached prediction labelmap is supplied (no paint-loop subprocess).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_fixture_json(self):
        from nninteractive_compare import run_comparison_from_fixture
        result = run_comparison_from_fixture(
            fixture_dir=self.tmp / "nope",
            output_dir=self.tmp / "out",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "fixture_load")

    def test_missing_required_files(self):
        from nninteractive_compare import run_comparison_from_fixture
        (self.tmp / "fixture.json").write_text(json.dumps(
            {"ct_media_id": "x", "gt_media_id": "y", "goal": "g",
             "max_steps": 1, "voxelize_backend": "vtk",
             "crop_around_mesh_mm": 0.0, "files": {}}
        ))
        result = run_comparison_from_fixture(
            fixture_dir=self.tmp,
            output_dir=self.tmp / "out",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "fixture_load")

    def test_with_cached_pred_invokes_metrics_only(self):
        """With both `gt_voxelized.nii.gz` and `pred.nii.gz` present, the
        dispatcher must skip the paint loop entirely and only call
        `_compute_metrics`. We assert that by mocking out
        ``_compute_metrics`` and ``_run_paint_loop`` and checking which
        was invoked.
        """
        import nninteractive_compare as mod

        for fname in ("ct.nii.gz", "gt_voxelized.nii.gz", "pred.nii.gz"):
            (self.tmp / fname).write_bytes(b"stub")
        (self.tmp / "fixture.json").write_text(json.dumps({
            "ct_media_id": "ct1", "gt_media_id": "gt1",
            "goal": "test goal", "max_steps": 3,
            "voxelize_backend": "vtk", "crop_around_mesh_mm": 0.0,
            "files": {},
        }))

        fake_metrics = {
            "dice": 0.77, "iou": 0.63,
            "voxel_count_pred": 100, "voxel_count_gt": 110,
            "volume_mm3_pred": 1.0, "volume_mm3_gt": 1.1,
            "precision": 0.75, "recall": 0.78,
            "hausdorff_mm": 0.5, "hausdorff_95_mm": 0.4,
        }

        with patch.object(mod, "_compute_metrics",
                          return_value=fake_metrics) as m_metrics, \
             patch.object(mod, "_run_paint_loop") as m_paint:
            result = mod.run_comparison_from_fixture(
                fixture_dir=self.tmp,
                output_dir=self.tmp / "out",
            )

        self.assertTrue(result["success"], result)
        self.assertTrue(result["from_fixture"])
        self.assertTrue(result["used_cached_pred"])
        m_metrics.assert_called_once()
        m_paint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
