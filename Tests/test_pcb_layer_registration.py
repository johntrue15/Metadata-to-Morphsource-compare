from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

import pcb_figure_gt as pfg  # noqa: E402


def _has_sitk() -> bool:
    try:
        import SimpleITK  # noqa: F401
        return True
    except Exception:
        return False


class FigureMaskTests(unittest.TestCase):
    def test_extract_copper_mask_finds_dominant_hue(self):
        rgb = np.zeros((120, 200, 3), dtype=np.uint8)
        rgb[:] = (200, 200, 200)  # gray background
        rgb[10:100, 20:180] = (20, 180, 40)  # dominant "copper" fill color
        rgb[30:40, 30:40] = (40, 40, 220)  # noise markers
        mask = pfg.extract_copper_mask(rgb)
        self.assertGreater(mask.sum(), 10_000)
        self.assertEqual(int(mask[35, 35]), 0)  # blue noise excluded


@unittest.skipUnless(_has_sitk(), "SimpleITK not installed")
class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pcb_reg_test_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_register_writes_registered_manifest_and_labelmap(self):
        import SimpleITK as sitk

        fig = np.zeros((140, 220, 3), dtype=np.uint8)
        fig[:] = (205, 205, 205)
        fig[10:120, 15:210] = (20, 180, 40)
        fig[50:80, 80:120] = (205, 205, 205)  # hole
        fig_path = self.tmp / "top.png"
        Image.fromarray(fig).save(fig_path)

        extract_dir = self.tmp / "extract"
        rc = pfg.main(
            [
                "extract",
                "--figure",
                f"top_copper={fig_path}",
                "--out-dir",
                str(extract_dir),
            ]
        )
        self.assertEqual(rc, 0)
        fig_manifest = extract_dir / "figure_gt_manifest.json"
        self.assertTrue(fig_manifest.is_file())

        ct_arr = np.zeros((41, 160, 260), dtype=np.float32)
        ct_arr[20, 20:140, 30:230] = 1.0
        ct = sitk.GetImageFromArray(ct_arr)
        ct.SetSpacing((0.05, 0.05, 0.05))
        ct_path = self.tmp / "ct.nii.gz"
        sitk.WriteImage(ct, str(ct_path))

        reg_dir = self.tmp / "registered"
        rc = pfg.main(
            [
                "register",
                "--manifest",
                str(fig_manifest),
                "--ct-volume",
                str(ct_path),
                "--out-dir",
                str(reg_dir),
            ]
        )
        self.assertEqual(rc, 0)
        reg_manifest_path = reg_dir / "registered_gt_manifest.json"
        self.assertTrue(reg_manifest_path.is_file())
        reg_manifest = json.loads(reg_manifest_path.read_text())
        self.assertEqual(reg_manifest["ct_shape_zyx"], [41, 160, 260])
        layer = reg_manifest["layers"][0]
        self.assertTrue(Path(layer["registered_labelmap"]).is_file())
        self.assertTrue(Path(layer["overlay_png"]).is_file())


if __name__ == "__main__":
    unittest.main()

