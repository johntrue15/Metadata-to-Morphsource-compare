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

    def sample_intensity(self, x, y, z):
        # Real Segmenter samples the CT voxel value; the fake returns
        # a constant so tests can assert "intensity is recorded".
        return 1234.5

    # --- io ----------------------------------------------------------------
    def save_orthogonal_previews(self, name_prefix: str = "",
                                  intensity_window=None,
                                  markers=None) -> list[str]:
        # Accepts the markers kwarg the real Segmenter now takes so the
        # paint loop can pass click markers without crashing the fake.
        # We record the markers it was called with for assertion in
        # the BEFORE/AFTER trail tests.
        self._last_markers = list(markers) if markers is not None else None
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
    def _run(self, voxel_trace, llm_responses, max_steps=12, **kwargs):
        """Drive ``run_loop`` end-to-end with stubs.

        Extra ``kwargs`` are forwarded to ``run_loop`` so tests can pass
        ``expected_voxels=...`` to exercise the budget-aware best path.
        """
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
                **kwargs,
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


class BudgetAwareBestTests(unittest.TestCase):
    """When ``expected_voxels`` is set, "best" is the snapshot closest
    to the budget - not the largest mask. This replays the exact Felis v3
    trace (run 26374270004) which produced dice=0.087 under the old
    "biggest is best" rule because save-best restored a 459% explosion.
    With the new rule it should restore step 5 or 6 (451 k voxels = 64%
    of the 701 499 budget).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        import os
        os.environ["OPENAI_API_KEY"] = "sk-test-stub"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, voxel_trace, llm_responses, max_steps=12, **kwargs):
        import nninteractive_loop as mod
        fake_seg = FakeSegmenter(voxel_trace=voxel_trace,
                                 output_dir=self.tmp)
        with patch.object(mod, "make_segmenter", return_value=fake_seg), \
             patch.object(mod, "_call_vision_llm",
                          new=_scripted_llm(llm_responses)), \
             patch.dict(sys.modules, {"torch": FakeTorchModule}):
            return mod.run_loop(
                input_path=str(self.tmp / "ct.nii.gz"),
                goal="Segment cranial bone.",
                output_dir=str(self.tmp),
                media_id="000362550",
                max_steps=max_steps,
                vision_model="gpt-4o-stub",
                **kwargs,
            ), fake_seg

    def test_v3_trace_picks_near_budget_not_explosion(self):
        # Exact Felis v3 trace
        voxel_trace = [78284, 71549, 283240, 265059, 451462, 450870,
                       3218555, 0, 63, 4711, 106778, 126401]
        llm = [
            {"tool":"ADD_POINT","x":105,"y":112,"z":192,"positive":True},
            {"tool":"ADD_POINT","x":100,"y":110,"z":192,"positive":True},
            {"tool":"ADD_POINT","x":110,"y":120,"z":92, "positive":True},
            {"tool":"ADD_POINT","x":105,"y":112,"z":180,"positive":True},
            {"tool":"ADD_POINT","x":110,"y":115,"z":140,"positive":True},
            {"tool":"ADD_POINT","x":105,"y":112,"z":160,"positive":True},
            {"tool":"ADD_POINT","x":150,"y":112,"z":192,"positive":True},
            {"tool":"RESET","reason":"explosion"},
            {"tool":"ADD_POINT","x":110,"y":112,"z":192,"positive":True},
            {"tool":"ADD_POINT","x":110,"y":112,"z":180,"positive":True},
            {"tool":"ADD_POINT","x":120,"y":112,"z":140,"positive":True},
            {"tool":"ADD_POINT","x":110,"y":120,"z":150,"positive":True},
        ]
        res, _ = self._run(voxel_trace, llm, max_steps=12,
                           expected_voxels=701499,
                           expected_volume_mm3=32035.0)

        # Best should be the closest-to-budget snapshot, NOT the 3.2 M
        # explosion. Step 5 (451 462) and step 6 (450 870) are
        # essentially tied; either is acceptable as long as it's the
        # ~451 k range, not the explosion or the rebuild floor.
        self.assertIn(res["best_step"], (5, 6),
                      f"Best step should be 5 or 6 (near-budget), "
                      f"got step {res['best_step']} "
                      f"({res['best_voxel_count']} voxels)")
        self.assertGreaterEqual(res["best_voxel_count"], 400_000)
        self.assertLessEqual(res["best_voxel_count"], 500_000,
                             f"Best must be near budget (~451 k); the "
                             f"3.2 M explosion would be a regression. "
                             f"Got {res['best_voxel_count']}.")
        # Final post-RESET rebuild ended at 126 k = far from budget.
        # We should restore the near-budget snapshot.
        self.assertTrue(res["restored_from_best"])
        self.assertEqual(res["voxel_count"], res["best_voxel_count"])
        self.assertEqual(res["final_voxels_pre_restore"], 126401)

    def test_legacy_no_budget_still_picks_largest(self):
        """Without ``expected_voxels``, the legacy "biggest is best"
        behaviour must still apply (used by older callers).
        """
        voxel_trace = [100, 5000, 80, 0]
        llm = [
            {"tool":"ADD_POINT","x":1,"y":1,"z":1,"positive":True},
            {"tool":"ADD_POINT","x":1,"y":1,"z":1,"positive":True},
            {"tool":"ADD_POINT","x":1,"y":1,"z":1,"positive":False},
            {"tool":"DONE","reason":"x"},
        ]
        res, _ = self._run(voxel_trace, llm, max_steps=4)
        self.assertEqual(res["best_voxel_count"], 5000)
        self.assertTrue(res["restored_from_best"])
        self.assertEqual(res["voxel_count"], 5000)

    def test_budget_aware_keeps_under_budget_over_oversized(self):
        """At-budget should ALWAYS win against far-over even if the
        oversize is huge."""
        # 700 voxels (perfectly on a budget of 700) then 50 000 (71x).
        voxel_trace = [700, 50_000]
        llm = [
            {"tool":"ADD_POINT","x":1,"y":1,"z":1,"positive":True},
            {"tool":"ADD_POINT","x":1,"y":1,"z":1,"positive":True},
        ]
        res, _ = self._run(voxel_trace, llm, max_steps=2,
                           expected_voxels=700)
        self.assertEqual(res["best_voxel_count"], 700)
        # Final was 50 000 (way over). Restored from best (700).
        self.assertTrue(res["restored_from_best"])
        self.assertEqual(res["voxel_count"], 700)


class BudgetHintTests(unittest.TestCase):
    """The volume-budget hint added to ``_build_state_text`` should appear
    in the prompt and emit guidance that varies with current/expected
    ratio.
    """

    def _build(self, voxels, expected=701499):
        from nninteractive_loop import _build_state_text
        return _build_state_text(
            goal="Segment cranial bone.",
            step=2, max_steps=12,
            image_shape_xyz=[211, 224, 384],
            spacing_xyz=[0.357, 0.357, 0.357],
            voxel_count=voxels, volume_mm3=voxels * 0.045,
            history=[],
            expected_voxels=expected,
            expected_volume_mm3=32035.0,
        )

    def test_no_hint_when_no_budget(self):
        from nninteractive_loop import _build_state_text
        s = _build_state_text(
            goal="x", step=1, max_steps=4,
            image_shape_xyz=[10, 10, 10], spacing_xyz=[1, 1, 1],
            voxel_count=0, volume_mm3=0, history=[],
        )
        self.assertNotIn("BUDGET", s)

    def test_empty_mask_guidance(self):
        s = self._build(0)
        self.assertIn("expected_voxels: 701,499", s)
        self.assertIn("EMPTY", s)

    def test_under_size_guidance(self):
        s = self._build(200_000)  # 28% of budget
        self.assertIn("UNDER-sized", s)

    def test_approaching_budget_guidance(self):
        s = self._build(500_000)  # 71% of budget
        self.assertIn("APPROACHING", s)

    def test_target_size_guidance_prefers_done(self):
        s = self._build(700_000)  # 99% of budget
        self.assertIn("TARGET size", s)
        self.assertIn("DONE", s)

    def test_over_size_guidance_prefers_negatives(self):
        s = self._build(1_100_000)  # 157% of budget
        self.assertIn("OVER-sized", s)
        self.assertIn("negative", s)

    def test_far_over_size_guidance_mentions_reset(self):
        s = self._build(1_554_372)  # the Felis-v2 outcome: 221% of budget
        self.assertIn("OVER-sized", s)
        self.assertIn("RESET", s)


class BboxHintTests(unittest.TestCase):
    """The voxel-space bbox hint should appear in the prompt only when
    ``expected_bbox`` is provided. It must include the exact bbox the
    caller passed plus a concrete ADD_BBOX suggestion the LLM can copy.
    """

    def _build(self, *, bbox=None, seeds=None):
        from nninteractive_loop import _build_state_text
        return _build_state_text(
            goal="Segment cranial bone.",
            step=1, max_steps=12,
            image_shape_xyz=[211, 224, 384],
            spacing_xyz=[0.357, 0.357, 0.357],
            voxel_count=0, volume_mm3=0,
            history=[],
            expected_voxels=701499,
            expected_volume_mm3=32035.0,
            expected_bbox=bbox,
            expected_seed_points=seeds,
        )

    def test_no_hint_without_bbox(self):
        s = self._build(bbox=None)
        self.assertNotIn("LOCALISATION HINT", s)

    def test_bbox_hint_shows_full_extents(self):
        bbox = {"x": [42, 168], "y": [101, 197], "z": [87, 277]}
        s = self._build(bbox=bbox)
        self.assertIn("LOCALISATION HINT", s)
        self.assertIn("x: [42, 168]", s)
        self.assertIn("y: [101, 197]", s)
        self.assertIn("z: [87, 277]", s)

    def test_bbox_hint_does_NOT_include_add_bbox_seed(self):
        """Felis v10 (run 26385712938) regression: when the LOCALISATION
        HINT shipped an ADD_BBOX template, the LLM faithfully copied a
        96%-of-slice bbox and nnInteractive over-segmented to ~12 x
        budget. The hint must NOT seed bbox prompts."""
        bbox = {"x": [42, 168], "y": [101, 197], "z": [87, 277]}
        s = self._build(bbox=bbox)
        self.assertNotIn('"tool":"ADD_BBOX"', s)
        self.assertNotIn('"GT bbox seed on mid-z slice"', s)

    def test_bbox_hint_warns_to_stay_inside(self):
        bbox = {"x": [10, 20], "y": [10, 20], "z": [10, 20]}
        s = self._build(bbox=bbox)
        self.assertIn("do NOT click outside it", s)

    def test_partial_bbox_dict_is_ignored(self):
        # Missing 'z' -> hint omitted (defensive guard).
        s = self._build(bbox={"x": [0, 1], "y": [0, 1]})
        self.assertNotIn("LOCALISATION HINT", s)


class SeedPointHintTests(unittest.TestCase):
    """Felis v10 replaced the bbox seed (which over-segmented every
    time) with concrete ADD_POINT seeds sampled from voxels that are
    literally inside the GT mask. These tests pin down the shape of
    that hint."""

    def _build(self, *, bbox=None, seeds=None):
        from nninteractive_loop import _build_state_text
        return _build_state_text(
            goal="Segment cranial bone.",
            step=1, max_steps=12,
            image_shape_xyz=[211, 224, 384],
            spacing_xyz=[0.357, 0.357, 0.357],
            voxel_count=0, volume_mm3=0,
            history=[],
            expected_voxels=701499,
            expected_volume_mm3=32035.0,
            expected_bbox=bbox,
            expected_seed_points=seeds,
        )

    def test_no_hint_without_seeds(self):
        s = self._build(seeds=None)
        self.assertNotIn("POSITIVE SEED POINTS", s)

    def test_seed_points_section_appears(self):
        s = self._build(seeds=[[110, 80, 200], [120, 90, 210]])
        self.assertIn("POSITIVE SEED POINTS", s)
        self.assertIn("#1: x=110 y=80 z=200", s)
        self.assertIn("#2: x=120 y=90 z=210", s)

    def test_first_seed_is_concrete_add_point_template(self):
        """The very first seed must be wrapped in a ready-to-copy
        ADD_POINT JSON object so the LLM has zero spatial-reasoning
        work to do for step 1."""
        s = self._build(seeds=[[111, 82, 205]])
        self.assertIn('"tool":"ADD_POINT"', s)
        self.assertIn('"x":111', s)
        self.assertIn('"y":82', s)
        self.assertIn('"z":205', s)
        self.assertIn('"positive":true', s)

    def test_seed_hint_actively_discourages_bbox(self):
        """Without this, the LLM falls back on its prior of opening
        with ADD_BBOX (the cause of Felis v10's dice ~= 0)."""
        s = self._build(seeds=[[10, 10, 10]])
        self.assertIn("Do NOT open with ADD_BBOX", s)

    def test_only_first_five_seeds_are_listed(self):
        seeds = [[i, i, i] for i in range(20)]
        s = self._build(seeds=seeds)
        # First 5 listed, 6th not.
        self.assertIn("#5: x=4", s)
        self.assertNotIn("#6:", s)

    def test_seed_and_bbox_coexist(self):
        bbox = {"x": [0, 200], "y": [0, 200], "z": [0, 200]}
        s = self._build(bbox=bbox, seeds=[[50, 60, 70]])
        self.assertIn("LOCALISATION HINT", s)
        self.assertIn("POSITIVE SEED POINTS", s)
        # The bbox section should still warn to stay inside.
        self.assertIn("do NOT click outside it", s)


class LoopFileHandlerTests(unittest.TestCase):
    """``_attach_file_handler`` must mirror the loop's stderr log into a
    file inside ``output_dir`` so that we can recover the LLM's per-step
    actions after a SIGABRT clobbers the orchestrator's last-2-KB stderr
    tail (this is what happened on Felis v9, run 26384436616).
    """

    def setUp(self) -> None:
        import logging
        self.tmp = tempfile.mkdtemp(prefix="loop_filehandler_")
        # Snapshot existing root handlers so we can restore them and not
        # pollute later tests in the same process.
        self._original_handlers = list(logging.getLogger().handlers)

    def tearDown(self) -> None:
        import logging
        import shutil
        # Remove any handler we added that points at our tmpdir.
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self._original_handlers:
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_log_to_output_dir(self):
        import nninteractive_loop as mod
        log_path = mod._attach_file_handler(self.tmp, "TESTID")
        self.assertEqual(log_path,
                         str(Path(self.tmp) / "TESTID_loop.log"))
        self.assertTrue(Path(log_path).exists())
        mod.log.info("hello world")
        # Flush handlers so the assertion sees the write.
        import logging
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        body = Path(log_path).read_text(encoding="utf-8")
        self.assertIn("hello world", body)
        # We log "Mirroring loop log to ..." right after attaching - it
        # must end up in the file too (proves the handler is wired BEFORE
        # the message, not after).
        self.assertIn("Mirroring loop log to", body)

    def test_missing_output_dir_is_created(self):
        import nninteractive_loop as mod
        nested = Path(self.tmp) / "new" / "subdir"
        self.assertFalse(nested.exists())
        log_path = mod._attach_file_handler(str(nested), "X")
        self.assertTrue(nested.exists())
        self.assertTrue(Path(log_path).exists())


class BeforeAfterPreviewTests(unittest.TestCase):
    """Per the user request after Felis v11 / Crotalus failures:

        "Lets make sure we are screenshotting and saving what the
         data looks like before and after clicking to get nninteractive
         to run."

    These tests assert that the paint loop:
      - calls ``save_orthogonal_previews`` TWICE per interactive step
        (once with a ``_before`` prefix, once with ``_after``).
      - passes the planned click coords as a ``markers`` kwarg so the
        rendered PNG actually shows where the LLM is clicking.
      - records the CT-intensity sampled at the click location in the
        StepRecord (so post-mortems can spot "the LLM clicked an
        HU=-987 cavity voxel" without re-rendering).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["OPENAI_API_KEY"] = "sk-test-stub"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_one_point(self, click=(12, 34, 56)):
        """Drive the loop through ONE ADD_POINT then DONE.

        Returns (history, recorded_calls) where recorded_calls is the
        list of (name_prefix, markers) the loop passed to the fake
        segmenter's save_orthogonal_previews.
        """
        import nninteractive_loop as mod

        fake_seg = FakeSegmenter(voxel_trace=[5000, 5000],
                                 output_dir=self.tmp)
        recorded: list[tuple] = []
        real_save = fake_seg.save_orthogonal_previews

        def _capturing_save(name_prefix="", intensity_window=None,
                            markers=None):
            recorded.append((name_prefix,
                             [dict(m) for m in (markers or [])]))
            return real_save(name_prefix=name_prefix,
                             intensity_window=intensity_window,
                             markers=markers)
        fake_seg.save_orthogonal_previews = _capturing_save  # type: ignore[assignment]

        cx, cy, cz = click
        llm = [
            {"tool": "ADD_POINT", "x": cx, "y": cy, "z": cz,
             "positive": True, "label": "test seed",
             "reason": "stub"},
            {"tool": "DONE", "reason": "stop"},
        ]

        with patch.object(mod, "make_segmenter", return_value=fake_seg), \
             patch.object(mod, "_call_vision_llm",
                          new=_scripted_llm(llm)), \
             patch.dict(sys.modules, {"torch": FakeTorchModule}):
            mod.run_loop(
                input_path=str(self.tmp / "dummy_ct.nii.gz"),
                goal="Segment the cranial bone (skull) for testing.",
                output_dir=str(self.tmp),
                media_id="TESTID",
                max_steps=4,
                vision_model="gpt-4o-stub",
            )

        return recorded

    def test_step_emits_before_and_after_previews(self):
        recorded = self._run_one_point()
        prefixes = [p for p, _ in recorded]
        # step00 (initial) + step01_before + step01_after expected.
        self.assertIn("TESTID_step00", prefixes)
        self.assertTrue(any(p == "TESTID_step01_before"
                            for p in prefixes),
                        msg=f"missing _before preview, got {prefixes}")
        self.assertTrue(any(p == "TESTID_step01_after"
                            for p in prefixes),
                        msg=f"missing _after preview, got {prefixes}")

    def test_marker_xyz_matches_clicked_coordinate(self):
        click = (77, 88, 99)
        recorded = self._run_one_point(click=click)
        # Both before+after for step 1 must carry the exact xyz the
        # LLM picked.
        before = next((m for p, m in recorded
                       if p == "TESTID_step01_before"), None)
        after = next((m for p, m in recorded
                      if p == "TESTID_step01_after"), None)
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertEqual(len(before), 1)
        self.assertEqual(tuple(before[0]["xyz"]), click)
        self.assertEqual(tuple(after[0]["xyz"]), click)
        self.assertTrue(before[0]["positive"])
        self.assertEqual(after[0]["label"], "s1")

    def test_initial_step00_has_no_marker(self):
        recorded = self._run_one_point()
        markers00 = next((m for p, m in recorded
                          if p == "TESTID_step00"), None)
        self.assertEqual(markers00, [])

    def test_click_intensity_recorded_in_step_record(self):
        """The FakeSegmenter returns 1234.5 from sample_intensity, so
        the StepRecord for the ADD_POINT step must carry that value."""
        import nninteractive_loop as mod

        fake_seg = FakeSegmenter(voxel_trace=[5000, 5000],
                                 output_dir=self.tmp)
        captured: dict = {}
        original_run = mod.run_loop

        llm = [
            {"tool": "ADD_POINT", "x": 1, "y": 2, "z": 3,
             "positive": True, "reason": "stub"},
            {"tool": "DONE", "reason": "stop"},
        ]
        with patch.object(mod, "make_segmenter", return_value=fake_seg), \
             patch.object(mod, "_call_vision_llm",
                          new=_scripted_llm(llm)), \
             patch.dict(sys.modules, {"torch": FakeTorchModule}):
            result = original_run(
                input_path=str(self.tmp / "dummy_ct.nii.gz"),
                goal="Segment the cranial bone (skull) for testing.",
                output_dir=str(self.tmp),
                media_id="TESTID",
                max_steps=4,
                vision_model="gpt-4o-stub",
            )
        # run_loop returns {"steps": int, "history": [StepRecord-as-dict]}
        # so we read the per-step click_intensity off the history list.
        history = result.get("history") or []
        point_records = [r for r in history
                         if r.get("tool") == "ADD_POINT"]
        self.assertTrue(point_records, "no ADD_POINT step recorded")
        self.assertEqual(point_records[0].get("click_intensity"), 1234.5)


if __name__ == "__main__":
    unittest.main()
