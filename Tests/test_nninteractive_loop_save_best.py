"""Regression tests for the save-best + RESET-veto policy in
``.github/scripts/nninteractive_loop.py``.

These tests replay the exact failure mode that produced ``dice=0.0`` on
the Felis catus run (000362550) of MorphoSource project 358382: the LLM
built up a real cranial-bone mask of ~430 k voxels across the first
3 steps, trimmed it down to ~176 k by step 11, then called RESET on the
final (12th) step, leaving the saved labelmap empty.

We do not exercise nnInteractive itself — only the loop's policy code.
A fake ``Segmenter`` is injected via ``make_segmenter`` and the vision
LLM is monkey-patched to return scripted tool calls.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Iterable
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))


class FakeSitkImage:
    def GetSpacing(self) -> tuple[float, float, float]:
        return (1.0, 1.0, 1.0)


class FakeTensor:
    """Minimal stand-in for ``seg.target``.

    The buffer is sized so each scripted voxel count fits exactly: setting
    voxel_count=N sets the first N entries of the flat buffer to 1.
    Production code restoring the buffer via ``target[:] = ...`` is
    visible to ``voxel_count()`` and ``mask_array``.
    """

    def __init__(self, shape: tuple[int, int, int]) -> None:
        import numpy as np
        self._buf = np.zeros(shape, dtype="uint8")
        self.device = "cpu"
        self.dtype = "uint8"

    def __setitem__(self, idx, value) -> None:
        import numpy as np
        if hasattr(value, "numpy"):
            value = value.numpy()
        self._buf[idx] = np.asarray(value, dtype=self._buf.dtype)

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self._buf

    def set_voxel_count(self, n: int) -> None:
        """Helper used by FakeSegmenter to encode a scripted voxel count."""
        import numpy as np
        self._buf[...] = 0
        flat = self._buf.reshape(-1)
        flat[: min(int(n), flat.size)] = 1


class FakeFromNumpyTensor:
    """Replacement for ``torch.from_numpy(arr).to(device, dtype=...)``."""
    def __init__(self, arr):
        self._arr = arr

    def to(self, device, dtype=None):
        return self

    def numpy(self):
        return self._arr


class FakeTorchModule:
    @staticmethod
    def from_numpy(arr):
        return FakeFromNumpyTensor(arr)


class FakeSegmenter:
    """Tiny stand-in for ``nninteractive_segment.Segmenter``.

    Each ``add_point`` / ``add_bbox`` updates the mask voxel count
    according to a scripted ``voxel_trace`` so we can replay exact
    paint-loop trajectories.
    """

    def __init__(self, voxel_trace: Iterable[int], output_dir: Path) -> None:
        import numpy as np
        self._trace = list(voxel_trace)
        self._idx = 0
        # 1M voxels in the buffer — fits every scripted voxel count in the
        # tests below (the real Felis trace peaks at 430 071).
        self.image_shape_zyx = (100, 100, 100)
        self.sitk_image = FakeSitkImage()
        self.target = FakeTensor(self.image_shape_zyx)
        self.output_dir = output_dir
        self.device = "cpu"  # used by the markdown report writer
        self._save_labelmap_called_with: list[int] = []
        self._np = np

    # --- counters / accessors ---------------------------------------------
    def voxel_count(self) -> int:
        # Read directly from the buffer so restore-from-best (which writes
        # `target[:] = ...`) is observable.
        return int((self.target.numpy() > 0).sum())

    def volume_mm3(self) -> float:
        return float(self.voxel_count())

    @property
    def mask_array(self):
        return self.target.numpy().copy()

    # --- prompt API --------------------------------------------------------
    def _advance(self):
        self._idx = min(self._idx + 1, len(self._trace))
        self.target.set_voxel_count(self._trace[self._idx - 1])

    def add_point(self, x, y, z, *, positive=True, label="") -> None:
        self._advance()

    def add_bbox(self, x_range, y_range, z_range, *,
                 positive=True, label="") -> None:
        self._advance()

    def reset_segment(self) -> None:
        # The scripted trace already encodes a 0 entry at the reset step,
        # so just advance.
        self._advance()

    # --- io ----------------------------------------------------------------
    def save_orthogonal_previews(self, name_prefix: str = "") -> list[str]:
        p = self.output_dir / f"{name_prefix}_preview.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal stub
        return [str(p)]

    def save_labelmap(self, name: str = "") -> str:
        p = self.output_dir / (name or "fake_labelmap.nii.gz")
        # Record the voxel count at save time so tests can assert the
        # save-best restore actually happened *before* save_labelmap ran.
        self._save_labelmap_called_with.append(self.voxel_count())
        p.write_bytes(b"NIFTI-STUB")
        return str(p)

    def export_summary(self, payload: dict) -> str:
        p = self.output_dir / "fake_summary.json"
        p.write_text(json.dumps(payload, default=str))
        return str(p)


def _scripted_llm(responses: Iterable[dict]):
    """Return an iterator of canned tool-call JSON strings, one per loop step."""
    queue = list(responses)
    def _fn(*args, **kwargs):
        if not queue:
            return '{"tool":"DONE","reason":"end of script"}'
        return json.dumps(queue.pop(0))
    return _fn


class SaveBestPolicyTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["OPENAI_API_KEY"] = "sk-test-stub"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -----------------------------------------------------------------
    def _run(self, voxel_trace, llm_responses, max_steps=12):
        """Drive ``run_loop`` end-to-end with stubs."""
        import nninteractive_loop as mod

        fake_seg = FakeSegmenter(voxel_trace=voxel_trace,
                                 output_dir=self.tmp)
        with patch.object(mod, "make_segmenter", return_value=fake_seg), \
             patch.object(mod, "_call_vision_llm",
                          new=_scripted_llm(llm_responses)), \
             patch.dict(sys.modules, {"torch": FakeTorchModule}):
            res = mod.run_loop(
                input_path=str(self.tmp / "dummy_ct.nii.gz"),
                goal="Segment the cranial bone (skull) of this Felis specimen.",
                output_dir=str(self.tmp),
                media_id="000362550",
                max_steps=max_steps,
                vision_model="gpt-4o-stub",
            )
        return res, fake_seg

    # -----------------------------------------------------------------
    # Scenario 1: exact Felis trace ----------------------------------
    def test_felis_reset_on_last_step_is_vetoed_and_best_restored(self):
        # voxel counts after each step (1-based), matching the Felis trace
        # in 000362550_nni_report.md:
        # step  1: 49035   ADD_POINT
        # step  2: 209619  ADD_POINT
        # step  3: 430071  ADD_POINT    <- best
        # step  4: 197884  ADD_POINT (neg)
        # step  5: 181711  ADD_POINT
        # step  6: 171439  ADD_BBOX (neg)
        # step  7: 167064  ADD_POINT
        # step  8: 164372  ADD_POINT
        # step  9: 175290  ADD_POINT
        # step 10: 179556  ADD_BBOX (neg)
        # step 11: 176558  ADD_POINT (neg)
        # step 12: 0       RESET           <- catastrophic
        voxel_trace = [49035, 209619, 430071, 197884, 181711, 171439,
                       167064, 164372, 175290, 179556, 176558, 0]
        # LLM script — last one is RESET, matching the real failure.
        llm = [
            {"tool":"ADD_POINT","x":106,"y":112,"z":192,"positive":True,
             "label":"cranial bone"},
            {"tool":"ADD_POINT","x":106,"y":112,"z":91,"positive":True,
             "label":"cranial bone"},
            {"tool":"ADD_POINT","x":106,"y":112,"z":130,"positive":True,
             "label":"cranial bone"},
            {"tool":"ADD_POINT","x":106,"y":112,"z":105,"positive":False,
             "label":"non-cranial area"},
            {"tool":"ADD_POINT","x":106,"y":112,"z":110,"positive":True,
             "label":"cranial bone"},
            {"tool":"ADD_BBOX","x":[105,107],"y":[111,113],"z":[108,109],
             "positive":False,"label":"non-cranial area"},
            {"tool":"ADD_POINT","x":106,"y":112,"z":110,"positive":True,
             "label":"cranial bone"},
            {"tool":"ADD_POINT","x":120,"y":110,"z":108,"positive":True,
             "label":"cranial bone"},
            {"tool":"ADD_POINT","x":100,"y":120,"z":115,"positive":True,
             "label":"cranial bone"},
            {"tool":"ADD_BBOX","x":[150,151],"y":[110,130],"z":[105,130],
             "positive":False,"label":"non-cranial"},
            {"tool":"ADD_POINT","x":105,"y":110,"z":110,"positive":False,
             "label":"over-segmented"},
            {"tool":"RESET","reason":"mask looks wrong, start over"},
        ]
        res, fake_seg = self._run(voxel_trace, llm, max_steps=12)

        # --- The veto should have fired on step 12 -----------------
        history = res["history"]
        last = history[-1]
        self.assertEqual(last["tool"], "RESET_VETOED",
                         f"Last step should have been vetoed, got: {last}")

        # --- The veto prevents RESET from collapsing to 0 voxels ---
        # After the veto the mask is still 176 558 voxels (step 11).
        # But 176 558 is < 50% of the best (430 071 at step 3) so the
        # save-best restore *does* fire — bringing the final mask back
        # to 430 071. This is exactly what we want for the Felis case.
        self.assertEqual(res["best_voxel_count"], 430071,
                         f"Best should be step 3's 430 071, "
                         f"got {res['best_voxel_count']}")
        self.assertEqual(res["best_step"], 3)
        self.assertEqual(res["final_voxels_pre_restore"], 176558,
                         "After RESET veto, mask should remain at step 11")
        self.assertTrue(res["restored_from_best"],
                        f"176 558 < 50% of 430 071, restore must fire; "
                        f"got restored_from_best={res['restored_from_best']}")
        self.assertEqual(res["voxel_count"], 430071,
                         f"After restore, voxel_count should be the peak; "
                         f"got {res['voxel_count']}")

        # --- save_labelmap was called *after* the restore -----------
        self.assertEqual(fake_seg._save_labelmap_called_with[-1], 430071,
                         "Saved labelmap should be the restored best mask")

    # -----------------------------------------------------------------
    # Scenario 2: model trims badly without RESET --------------------
    def test_save_best_restores_when_final_collapses_below_threshold(self):
        """Model finishes via DONE with only 1% of its peak mask
        (e.g. a chain of over-aggressive negatives). The pipeline should
        restore the peak mask snapshot.
        """
        # Peak 500000 at step 3, then negatives trim to 4000 by step 6,
        # then DONE.
        voxel_trace = [100000, 250000, 500000, 100000, 30000, 4000]
        llm = [
            {"tool":"ADD_POINT","x":50,"y":50,"z":50,"positive":True},
            {"tool":"ADD_POINT","x":60,"y":50,"z":50,"positive":True},
            {"tool":"ADD_POINT","x":70,"y":50,"z":50,"positive":True},
            {"tool":"ADD_POINT","x":50,"y":50,"z":50,"positive":False},
            {"tool":"ADD_POINT","x":60,"y":50,"z":50,"positive":False},
            {"tool":"ADD_POINT","x":70,"y":50,"z":50,"positive":False},
            {"tool":"DONE","reason":"giving up"},
        ]
        res, fake_seg = self._run(voxel_trace, llm, max_steps=12)

        # Note: snapshot is taken after each step but `mask_array` returns a
        # buffer whose nonzero count equals voxel_count(), so the restored
        # mask should give back 500000 voxels.
        self.assertTrue(res["restored_from_best"])
        self.assertEqual(res["best_voxel_count"], 500000)
        self.assertEqual(res["voxel_count"], 500000,
                         "Restoring best should bring voxel_count back to peak")

    # -----------------------------------------------------------------
    # Scenario 3: healthy run --- no restore --------------------------
    def test_healthy_run_keeps_final_mask_unchanged(self):
        """If the final mask is >= 50% of best, no restore.
        This is the normal trim-down behaviour for an oversized peak.
        """
        # Peak 700000 at step 3, trimmed to 600000 (86% of best) by DONE.
        voxel_trace = [200000, 500000, 700000, 650000, 600000]
        llm = [
            {"tool":"ADD_POINT","x":50,"y":50,"z":50,"positive":True},
            {"tool":"ADD_POINT","x":60,"y":50,"z":50,"positive":True},
            {"tool":"ADD_POINT","x":70,"y":50,"z":50,"positive":True},
            {"tool":"ADD_POINT","x":50,"y":50,"z":50,"positive":False},
            {"tool":"ADD_POINT","x":60,"y":50,"z":50,"positive":False},
            {"tool":"DONE","reason":"good enough"},
        ]
        res, _ = self._run(voxel_trace, llm, max_steps=12)

        self.assertFalse(res["restored_from_best"])
        self.assertEqual(res["voxel_count"], 600000)
        self.assertEqual(res["best_voxel_count"], 700000)

    # -----------------------------------------------------------------
    # Scenario 4: early RESET is allowed (still budget) --------------
    def test_early_reset_not_vetoed(self):
        """If the model RESETs early in the budget, the veto should NOT
        fire — the model has room to rebuild.
        """
        # Step 1: small ADD; step 2: RESET (10 remaining → allowed); steps
        # 3-12: rebuild to a healthy mask.
        voxel_trace = [50000, 0, 100000, 200000, 300000, 400000, 500000,
                       550000, 580000, 600000, 620000, 640000]
        llm = [
            {"tool":"ADD_POINT","x":50,"y":50,"z":50,"positive":True},
            {"tool":"RESET","reason":"wrong location"},
            *[{"tool":"ADD_POINT","x":60,"y":50,"z":50,"positive":True}
              for _ in range(9)],
            {"tool":"DONE","reason":"good enough"},
        ]
        res, _ = self._run(voxel_trace, llm, max_steps=12)

        tools = [h["tool"] for h in res["history"]]
        self.assertEqual(tools[1], "RESET",
                         f"Early RESET should NOT be vetoed; got tools={tools}")
        # 12 steps total; the loop breaks on the DONE response so the
        # final ADD_POINT we executed was step 11, which advanced the
        # trace to its 11th entry (index 10) = 620 000.
        self.assertEqual(res["voxel_count"], 620000)


if __name__ == "__main__":
    unittest.main()
