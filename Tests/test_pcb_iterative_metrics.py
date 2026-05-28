from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

import slicer_remote_pcb_copper as copper  # noqa: E402


def _has_sitk() -> bool:
    try:
        import SimpleITK  # noqa: F401
        return True
    except Exception:
        return False


class LayerConstraintTests(unittest.TestCase):
    def test_snap_point_to_fixed_layer(self):
        action = {"action": "point", "i": 10, "j": 11, "k": 12, "positive": True}
        out = copper._snap_action_to_layer(
            action,
            layer_axis=2,
            layer_index=20,
            dims_ijk=[100, 100, 41],
        )
        self.assertEqual(out["k"], 20)

    def test_snap_bbox_to_fixed_layer(self):
        action = {
            "action": "bbox",
            "i0": 30,
            "j0": 40,
            "k0": 1,
            "i1": 10,
            "j1": 60,
            "k1": 35,
            "positive": True,
        }
        out = copper._snap_action_to_layer(
            action,
            layer_axis=2,
            layer_index=19,
            dims_ijk=[80, 120, 41],
        )
        self.assertEqual(out["k0"], 19)
        self.assertEqual(out["k1"], 19)
        self.assertLessEqual(out["i0"], out["i1"])
        self.assertLessEqual(out["j0"], out["j1"])


@unittest.skipUnless(_has_sitk(), "SimpleITK not installed")
class MetricWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pcb_metric_test_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_score_labelmaps_and_csv(self):
        import SimpleITK as sitk

        arr_pred = np.zeros((16, 32, 32), dtype=np.uint8)
        arr_gt = np.zeros((16, 32, 32), dtype=np.uint8)
        arr_pred[8, 10:20, 10:20] = 1
        arr_gt[8, 12:22, 12:22] = 1

        pred = sitk.GetImageFromArray(arr_pred)
        gt = sitk.GetImageFromArray(arr_gt)
        pred.CopyInformation(gt)
        pred_path = self.tmp / "pred.nii.gz"
        gt_path = self.tmp / "gt.nii.gz"
        sitk.WriteImage(pred, str(pred_path))
        sitk.WriteImage(gt, str(gt_path))

        metrics = copper._score_labelmaps(pred_path, gt_path, no_surface=True)
        self.assertEqual(metrics.get("status"), "ok")
        self.assertGreater(metrics.get("dice", 0), 0.0)
        self.assertLess(metrics.get("dice", 1), 1.0)

        rows = [
            {"step": 0, "phase": "step", "dice": metrics["dice"]},
            {"step": 1, "phase": "final", "dice": metrics["dice"]},
        ]
        csv_path = self.tmp / "results.csv"
        copper._write_results_csv(rows, csv_path)
        self.assertTrue(csv_path.is_file())
        with csv_path.open(newline="", encoding="utf-8") as f:
            got = list(csv.DictReader(f))
        self.assertEqual(len(got), 2)


if __name__ == "__main__":
    unittest.main()

