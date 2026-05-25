"""
LLM-in-the-loop iterative segmentation with nnInteractive.

This is AutoResearchClaw's "paint" loop — the agent's eyes-and-hands wrapper
around `nnInteractive_segment.Segmenter`. Each step:

    1. Render orthogonal previews of the current mask over the volume
    2. Send the screenshots + state to the LLM (vision model)
    3. Parse a tool call: ADD_POINT / ADD_BBOX / RESET / DONE
    4. Apply it via nnInteractive — segmentation refines incrementally
    5. Repeat until DONE or `max_steps` reached

Outputs a final NIfTI labelmap, the prompt history, and a JSON summary
suitable for posting to a GitHub issue or feeding back into research_agent.

Usage:

    python3 nninteractive_loop.py \
        --input /path/to/volume.nii.gz \
        --goal  "Segment the cranial cavity" \
        --media-id 000769445 \
        --output-dir /tmp/nni_loop \
        --max-steps 12
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Allow this file to be run with the nnInteractive venv's Python (where
# `_helpers` is not normally on sys.path because it lives in the parent
# autoresearchclaw env). We tolerate either layout.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from nninteractive_segment import (  # noqa: E402
    NNInteractiveUnavailable, Segmenter, SegmenterConfig, make_segmenter,
)

try:
    from _helpers import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    # _helpers is part of the larger AutoResearchClaw env; running standalone
    # in the nnInteractive venv just means we read OPENAI_API_KEY from the
    # ambient env directly.
    pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("nni_loop")


SYSTEM_PROMPT = """You are a senior morphometrics researcher controlling
nnInteractive, a 3D promptable segmentation model, to extract a structure
from a 3D medical/CT volume.

You are shown three orthogonal screenshots (axial / coronal / sagittal)
of the volume with the *current* segmentation mask overlaid in red.
Each screenshot has axis tick marks every 50 voxels and a title like
"axial (x-y plane, z=192)" telling you which slice you are looking at
and which voxel coords map to the horizontal/vertical pixels.

You must decide ONE next action. Respond with EXACTLY one JSON object,
no other text:

{"tool":"ADD_POINT","x":INT,"y":INT,"z":INT,"positive":TRUE_OR_FALSE,"label":"...","reason":"..."}
{"tool":"ADD_BBOX","x":[X1,X2],"y":[Y1,Y2],"z":[Z1,Z2],"positive":TRUE_OR_FALSE,"label":"...","reason":"..."}
{"tool":"RESET","reason":"start over because the mask is wrong"}
{"tool":"DONE","reason":"mask now matches the goal","summary":"<2-3 sentence summary>"}

================ HOW TO READ THE PREVIEWS ================
- The first preview is "axial (x-y plane, z=K)": horizontal pixels are
  the x-voxel coordinate, vertical pixels are the y-voxel coordinate,
  and the slice was taken at depth z=K.
- The second is "coronal (x-z plane, y=K)": horizontal = x, vertical = z.
- The third is "sagittal (y-z plane, x=K)": horizontal = y, vertical = z.
- Read the tick labels to convert a pixel position into a voxel index.
  DO NOT guess "middle of the image"; estimate the actual voxel index
  the structure is centred on using the tick marks.

================ HOW TO PICK A POINT ON A CT IMAGE ================
- In a CT preview the brightness encodes Hounsfield Units. When the
  goal mentions a *bone-like* structure (bone, skull, cranial, cortical,
  tooth, vertebra, mandible, stapes, ...), the previews are rendered
  with a CT bone window: DENSE BONE is brilliant WHITE/very bright;
  soft tissue (brain, muscle, fat) is mid-gray; air is black.
- For bone targets: ONLY click on bright white pixels. Pick a pixel that
  is clearly on the target structure, not adjacent to it. If the goal
  says "skull", remember the skull is a HOLLOW shell of bone surrounding
  the brain cavity - the bone you want is the bright ring/walls, not
  the dark gray interior. Avoid the geometric centre of the skull; it
  lies inside the brain and is mostly soft tissue.
- For soft-tissue targets (heart, liver, kidney, brain, tumour, ...):
  click in the homogeneous interior of the target organ where the
  greyscale value is characteristic of that tissue.
- Cross-check your point across views: the same (x, y, z) voxel should
  look like the target tissue in all three orthogonal slices.

================ HOW TO USE BBOX ================
- ADD_BBOX with a single-voxel-thick plane is a great FIRST PROMPT.
  Drag a 2D box around the target on the slice that shows it best
  (e.g. axial bbox at z=K covering the bone's full x and y extent),
  then refine with positive/negative points. nnInteractive responds
  much better to a confining bbox than to a single naked point near
  the centre of a large volume.
- Bounding boxes must be 2D for nnInteractive: one of x/y/z must span
  a single voxel, e.g. z:[42, 43].

================ GENERAL RULES ================
- Coordinates are voxel indices in (x, y, z) order. The volume's full
  shape is given to you as image_shape_xyz.
- A positive point adds tissue similar to that voxel; negative removes.
- Once the mask reasonably covers the target, call DONE. Avoid loops.
- RESET wipes the ENTIRE mask back to 0 voxels. Only call RESET when
  the mask is essentially wrong (covers <5% of the target) AND you
  still have at least 4 steps remaining to rebuild. Never call RESET
  on the last 3 steps - small corrective negative points are safer.
- Prefer keeping a slightly imperfect mask over RESET; the pipeline
  saves the best mask you reach during the loop, so trim conservatively
  near the end of the step budget.
- The goal you must achieve is shown in the user message."""


@dataclass
class StepRecord:
    step: int
    tool: str
    args: dict
    result: str
    voxel_count: int
    screenshots: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Vision LLM call (uses the openai package if available, else raw HTTP).
# We keep the dependency surface minimal because this script is run inside
# the nnInteractive venv, which only has torch + nnInteractive by default.
# ---------------------------------------------------------------------------


def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("utf-8")


def _call_vision_llm(api_key: str, model: str, system: str,
                     state_text: str, image_paths: list[str],
                     max_tokens: int = 800) -> Optional[str]:
    """Vision model call. Returns the LLM text or None on failure."""
    user_content: list[dict] = [{"type": "text", "text": state_text}]
    for p in image_paths:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_b64(p)}"},
        })

    # Prefer the openai SDK if available
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except ImportError:
        pass
    except Exception as exc:
        log.error("openai SDK call failed: %s", exc)
        return None

    # Fallback: raw urllib HTTP
    import urllib.request
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8")
            status = r.status
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error("Raw HTTP LLM returned non-JSON (status=%s): %s",
                      status, raw[:300])
            return None
        choices = data.get("choices") or []
        if not choices:
            log.error("Raw HTTP LLM response had no choices "
                      "(status=%s, body head=%s)", status, raw[:300])
            return None
        content = (choices[0].get("message") or {}).get("content") or ""
        if not content:
            log.error("Raw HTTP LLM returned empty content "
                      "(finish_reason=%s, body head=%s)",
                      choices[0].get("finish_reason"), raw[:300])
            return None
        return content.strip()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        log.error("Raw HTTP LLM call HTTP %s: %s", exc.code, body)
        return None
    except Exception as exc:
        log.error("Raw HTTP LLM call failed: %s", exc)
        return None


def _parse_action(text: str) -> dict:
    """Extract a single JSON action from the LLM's response."""
    if not text:
        return {"tool": "DONE", "reason": "empty LLM response"}
    if "```" in text:
        for chunk in text.split("```")[1::2]:
            body = chunk[4:].strip() if chunk.startswith("json") else chunk.strip()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    s, e = text.find("{"), text.rfind("}")
    if 0 <= s < e:
        try:
            return json.loads(text[s : e + 1])
        except json.JSONDecodeError:
            pass
    log.warning("Could not parse LLM response as JSON; treating as DONE")
    return {"tool": "DONE", "reason": "unparseable LLM response", "raw": text[:300]}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_loop(input_path: str, goal: str, output_dir: str,
             media_id: str = "unknown", max_steps: int = 12,
             vision_model: str = "",
             expected_voxels: Optional[int] = None,
             expected_volume_mm3: Optional[float] = None) -> dict:
    """Drive the LLM-in-the-loop paint loop.

    Parameters
    ----------
    expected_voxels, expected_volume_mm3 : optional
        Approximate size of the target structure (typically the GT
        labelmap voxel count + foreground volume). When set, the LLM is
        told its budget every step so it stops growing the mask once it
        is roughly the right size. Without this, models tend to over-
        segment - the cat-skull Felis run (3b6d7fa) reached 2.2x GT.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "success": False,
            "error": "OPENAI_API_KEY not set; the nnInteractive paint loop "
                     "needs a vision model to choose prompts.",
        }
    vision_model = vision_model or os.environ.get(
        "NNINTERACTIVE_VISION_MODEL", "gpt-4o"
    )

    cfg = SegmenterConfig(input_path=input_path, output_dir=str(output),
                          media_id=media_id)
    try:
        # Returns either a local Segmenter or a RemoteSegmenter depending
        # on NNI_REMOTE_WS — see nninteractive_segment.make_segmenter.
        seg = make_segmenter(cfg)
    except NNInteractiveUnavailable as exc:
        log.error("%s", exc)
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        log.exception("Failed to construct segmenter")
        return {"success": False, "error": f"segmenter init failed: {exc}"}

    log.info("=" * 60)
    log.info("nnInteractive paint loop")
    log.info("Goal:     %s", goal)
    log.info("Volume:   %s", input_path)
    log.info("Shape:    %s (z, y, x)", seg.image_shape_zyx)
    log.info("Spacing:  %s mm (x, y, z)", seg.sitk_image.GetSpacing())
    log.info("Max steps:%d", max_steps)
    log.info("Vision model: %s", vision_model)
    log.info("=" * 60)

    # Pick an intensity window for the previews so the target tissue is
    # visually unambiguous. For bone-targeting goals we use a CT bone
    # window (HU range -200..2000) which renders dense bone as brilliant
    # white and soft tissue as muted gray. Felis v4 (run 26375110522)
    # showed that under matplotlib's default auto-scaling the LLM cannot
    # reliably distinguish bone from brain matter and ends up clicking
    # inside the dark brain cavity instead of on the bright bone shell.
    goal_lc = (goal or "").lower()
    bone_keywords = ("bone", "skull", "cranial", "cortical",
                     "calcified", "calcium", "osteo", "vertebra",
                     "mandible", "stapes")
    intensity_window: Optional[tuple[float, float]] = None
    if any(kw in goal_lc for kw in bone_keywords):
        intensity_window = (-200.0, 2000.0)
        log.info("Goal mentions bone-like keyword - rendering previews "
                 "with bone window vmin=-200 vmax=2000 HU.")

    initial = seg.save_orthogonal_previews(
        name_prefix=f"{media_id}_step00",
        intensity_window=intensity_window,
    )

    history: list[StepRecord] = []
    z, y, x = seg.image_shape_zyx
    image_shape_xyz = [x, y, z]

    # Save-best tracking: keep a snapshot of the *best-so-far* mask so we
    # can restore it at the end if the LLM torches its own work (RESET on
    # the last step, over-aggressive negatives, or a single positive that
    # causes nnInteractive to re-segment everything globally).
    #
    # "Best" depends on whether we have a target-size budget:
    #   * No budget   -> best = largest non-empty mask (legacy behaviour)
    #   * With budget -> best = closest to expected_voxels (lower is better).
    #     This matters because nnInteractive sometimes 7x-explodes the mask
    #     on a single positive point (Felis v3 run 26374270004 step 7:
    #     451k -> 3.2M voxels). The post-RESET rebuild often lands far
    #     under budget; the legacy "biggest is best" would restore the
    #     3.2M leak instead of the 451k near-budget snapshot.
    best_mask_np: Optional[object] = None  # numpy array (kept dep-free here)
    best_voxel_count: int = 0
    best_step: int = 0
    # Score is lower-is-better. Initialised to +inf so any real snapshot
    # replaces it.
    best_score: float = float("inf")

    def _score_for(voxel_count: int) -> float:
        if expected_voxels and expected_voxels > 0:
            return float(abs(voxel_count - expected_voxels))
        return -float(voxel_count)  # bigger is better without a budget

    def _snapshot_if_best() -> None:
        nonlocal best_mask_np, best_voxel_count, best_step, best_score
        vc = seg.voxel_count()
        if vc == 0:
            return  # never preserve an empty mask as best
        score = _score_for(vc)
        if score < best_score:
            best_mask_np = seg.mask_array.copy()
            best_voxel_count = vc
            best_score = score
            best_step = step
            log.debug("step %d: new best mask voxel count = %d "
                      "(score=%.0f, budget=%s)",
                      step, vc, score,
                      expected_voxels if expected_voxels else "n/a")

    for step in range(1, max_steps + 1):
        last_screens = (
            history[-1].screenshots if history else initial
        )
        state_text = _build_state_text(
            goal=goal,
            step=step,
            max_steps=max_steps,
            image_shape_xyz=image_shape_xyz,
            spacing_xyz=list(seg.sitk_image.GetSpacing()),
            voxel_count=seg.voxel_count(),
            volume_mm3=seg.volume_mm3(),
            history=history,
            expected_voxels=expected_voxels,
            expected_volume_mm3=expected_volume_mm3,
        )

        log.info("Step %d/%d — asking LLM (%d voxels currently in mask)",
                 step, max_steps, seg.voxel_count())
        text = _call_vision_llm(api_key, vision_model, SYSTEM_PROMPT,
                                state_text, last_screens, max_tokens=800)
        action = _parse_action(text or "")
        tool = action.get("tool", "DONE").upper()
        log.info("  Tool: %s — %s", tool, action.get("reason", "")[:120])

        # Execute the tool
        if tool == "DONE":
            history.append(StepRecord(
                step=step, tool="DONE", args=action,
                result=action.get("reason", "done"),
                voxel_count=seg.voxel_count(),
            ))
            break

        if tool == "RESET":
            # Veto destructive RESETs when there is no budget left to rebuild.
            # The LLM occasionally picks RESET on the last step expecting more
            # turns; without this guard the saved labelmap ends up empty.
            steps_remaining = max_steps - step
            current_voxels = seg.voxel_count()
            if steps_remaining < 3 and current_voxels > 0:
                log.warning(
                    "Vetoing RESET at step %d/%d: only %d step(s) left and "
                    "current mask has %d voxels (cannot be rebuilt in time)",
                    step, max_steps, steps_remaining, current_voxels,
                )
                history.append(StepRecord(
                    step=step, tool="RESET_VETOED", args=action,
                    result=(f"reset vetoed: {steps_remaining} steps left, "
                            f"would lose {current_voxels} voxels"),
                    voxel_count=current_voxels,
                ))
            else:
                seg.reset_segment()
                history.append(StepRecord(
                    step=step, tool="RESET", args=action,
                    result="segment reset",
                    voxel_count=seg.voxel_count(),
                ))
        elif tool == "ADD_POINT":
            try:
                seg.add_point(
                    int(action["x"]), int(action["y"]), int(action["z"]),
                    positive=bool(action.get("positive", True)),
                    label=str(action.get("label", "")),
                )
                result = (
                    f"point ({action['x']},{action['y']},{action['z']}) "
                    f"{'pos' if action.get('positive', True) else 'neg'}"
                )
            except Exception as exc:
                log.error("ADD_POINT failed: %s", exc)
                result = f"error: {exc}"
            history.append(StepRecord(
                step=step, tool="ADD_POINT", args=action,
                result=result,
                voxel_count=seg.voxel_count(),
            ))
        elif tool == "ADD_BBOX":
            try:
                seg.add_bbox(
                    action["x"], action["y"], action["z"],
                    positive=bool(action.get("positive", True)),
                    label=str(action.get("label", "")),
                )
                result = f"bbox x={action['x']} y={action['y']} z={action['z']}"
            except Exception as exc:
                log.error("ADD_BBOX failed: %s", exc)
                result = f"error: {exc}"
            history.append(StepRecord(
                step=step, tool="ADD_BBOX", args=action,
                result=result,
                voxel_count=seg.voxel_count(),
            ))
        else:
            log.warning("Unknown tool '%s' — treating as DONE", tool)
            history.append(StepRecord(
                step=step, tool="DONE", args=action,
                result=f"unknown tool '{tool}'",
                voxel_count=seg.voxel_count(),
            ))
            break

        # Snapshot the best mask seen so far (cheap numpy copy).
        _snapshot_if_best()

        # Render the new state for the next iteration
        screens = seg.save_orthogonal_previews(
            name_prefix=f"{media_id}_step{step:02d}",
            intensity_window=intensity_window,
        )
        history[-1].screenshots = screens

    # Restore best mask if the final state is worse than a snapshot we
    # took earlier. "Worse" is defined by the same score the snapshotter
    # used: distance to expected_voxels when we have a budget, else
    # negative voxel count (so "more is better").
    final_voxels = seg.voxel_count()
    final_score = _score_for(final_voxels)
    restored_from_best = False
    has_budget = bool(expected_voxels and expected_voxels > 0)
    # Budget mode: any score regression triggers restore (cheap, safe,
    # and Felis-v3 showed even ~10% drift from the budget hurts dice).
    # Legacy mode: keep the original 50%-of-best collapse threshold so
    # we don't undo small intentional trims.
    if has_budget:
        should_restore = final_score > best_score
    else:
        should_restore = (best_voxel_count > 0
                          and final_voxels < 0.5 * best_voxel_count)
    if (best_mask_np is not None
            and best_voxel_count > 0
            and should_restore):
        log.warning(
            "Final mask voxel_count=%d (score=%.0f) is worse than the "
            "best snapshot of %d voxels at step %d (score=%.0f) - "
            "restoring best.",
            final_voxels, final_score, best_voxel_count, best_step,
            best_score,
        )
        try:
            import torch  # local import keeps the module importable without torch
            seg.target[:] = torch.from_numpy(
                best_mask_np.astype("uint8")
            ).to(seg.target.device, dtype=seg.target.dtype)
            restored_from_best = True
        except Exception as exc:
            log.error("Failed to restore best mask: %s", exc)

    # Finalise — dump labelmap, summary, and a markdown report.
    labelmap_path = seg.save_labelmap()
    summary_path = seg.export_summary({
        "goal": goal,
        "vision_model": vision_model,
        "labelmap_path": labelmap_path,
        "history": [_record_to_dict(r) for r in history],
        "best_mask": {
            "voxel_count": best_voxel_count,
            "step": best_step,
        },
        "restored_from_best": restored_from_best,
        "final_voxels_pre_restore": final_voxels,
    })
    report_path = _write_report(output, media_id, goal, history,
                                seg, labelmap_path)

    log.info("Done. %d steps, final mask: %d voxels (%.2f mm^3)"
             "%s",
             len(history), seg.voxel_count(), seg.volume_mm3(),
             (f" [restored from best={best_voxel_count} @ step "
              f"{best_step}]" if restored_from_best else ""))

    return {
        "success": True,
        "media_id": media_id,
        "goal": goal,
        "steps": len(history),
        "voxel_count": seg.voxel_count(),
        "volume_mm3": round(seg.volume_mm3(), 3),
        "labelmap_path": labelmap_path,
        "summary_path": summary_path,
        "report_path": report_path,
        "history": [_record_to_dict(r) for r in history],
        "best_voxel_count": best_voxel_count,
        "best_step": best_step,
        "restored_from_best": restored_from_best,
        "final_voxels_pre_restore": final_voxels,
    }


def _build_state_text(*, goal: str, step: int, max_steps: int,
                      image_shape_xyz: list, spacing_xyz: list,
                      voxel_count: int, volume_mm3: float,
                      history: list[StepRecord],
                      expected_voxels: Optional[int] = None,
                      expected_volume_mm3: Optional[float] = None) -> str:
    lines = [
        f"GOAL: {goal}",
        "",
        f"Step {step}/{max_steps}",
        f"image_shape_xyz: {image_shape_xyz}",
        f"voxel_spacing_mm_xyz: {[round(s, 4) for s in spacing_xyz]}",
        f"current_mask_voxels: {voxel_count}",
        f"current_mask_volume_mm3: {round(volume_mm3, 2)}",
    ]

    # Inject the size budget when we have one. This is the strongest
    # signal we can give the model about correctness - it has no other
    # way to know what fraction of the target it has covered.
    if expected_voxels is not None and expected_voxels > 0:
        ratio = (voxel_count / expected_voxels) if expected_voxels else 0.0
        # Heuristic action guide tied to the budget
        if voxel_count == 0:
            guidance = "Mask is EMPTY. Add positive points inside the target."
        elif ratio < 0.5:
            guidance = ("Mask is UNDER-sized (<50% of budget). Prefer "
                        "ADD_POINT positive at unsegmented parts of the "
                        "target.")
        elif ratio < 0.9:
            guidance = ("Mask is APPROACHING budget. Add at most one or "
                        "two more positives near unsegmented regions, "
                        "then consider DONE.")
        elif ratio <= 1.3:
            guidance = ("Mask is at TARGET size (90-130% of budget). "
                        "Prefer DONE unless there is a clear leak; small "
                        "negatives only.")
        elif ratio <= 1.8:
            guidance = ("Mask is OVER-sized (130-180% of budget). Trim "
                        "with negative points at obviously non-target "
                        "regions; do NOT add positives.")
        else:
            guidance = ("Mask is far OVER-sized (>180% of budget). "
                        "Heavy over-segmentation - prefer negatives or "
                        "RESET if you still have >=4 steps left, then "
                        "rebuild with sparse positives.")
        lines += [
            "",
            "BUDGET (from rasterized GT mesh):",
            f"  expected_voxels: {expected_voxels:,}",
            (f"  expected_volume_mm3: {round(expected_volume_mm3, 2)}"
             if expected_volume_mm3 else
             "  expected_volume_mm3: (unknown)"),
            f"  current_vs_expected_ratio: {ratio:.2f}",
            f"  guidance: {guidance}",
        ]
    lines += [
        "",
        "Previous actions (most recent last):",
    ]
    for r in history[-6:]:
        args_short = {k: v for k, v in r.args.items()
                      if k not in ("reason", "summary", "raw")}
        lines.append(
            f"  step {r.step}: {r.tool} {json.dumps(args_short, default=str)} "
            f"-> {r.result} (mask={r.voxel_count} voxels)"
        )
    if not history:
        lines.append("  (none)")
    lines.append("")
    lines.append("Choose ONE next action as a JSON object. No prose.")
    return "\n".join(lines)


def _record_to_dict(r: StepRecord) -> dict:
    return {
        "step": r.step,
        "tool": r.tool,
        "args": r.args,
        "result": r.result,
        "voxel_count": r.voxel_count,
        "screenshots": r.screenshots,
    }


def _write_report(output: Path, media_id: str, goal: str,
                  history: list[StepRecord], seg: Segmenter,
                  labelmap_path: str) -> str:
    lines = [
        f"# nnInteractive paint loop — {media_id}",
        "",
        f"**Goal:** {goal}",
        f"**Steps:** {len(history)}",
        f"**Final voxel count:** {seg.voxel_count():,}",
        f"**Final volume:** {seg.volume_mm3():.2f} mm³",
        f"**Labelmap:** `{labelmap_path}`",
        f"**Device:** `{seg.device}`",
        "",
        "## Prompt history",
        "",
        "| Step | Tool | Args | Mask voxels | Result |",
        "|-----:|------|------|-----------:|--------|",
    ]
    for r in history:
        args_str = json.dumps({k: v for k, v in r.args.items()
                               if k not in ("reason", "summary", "raw")},
                              default=str)
        lines.append(
            f"| {r.step} | `{r.tool}` | `{args_str}` | "
            f"{r.voxel_count:,} | {r.result} |"
        )
    lines.append("")
    lines.append("## Final preview")
    lines.append("")
    for view in ("axial", "coronal", "sagittal"):
        lines.append(f"![{view}]({media_id}_nni_{view}.png)")
    out_path = output / f"{media_id}_nni_report.md"
    out_path.write_text("\n".join(lines))
    return str(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args():
    p = argparse.ArgumentParser(description="Iterative LLM-driven nnInteractive segmentation")
    p.add_argument("--input", required=True, help="Path to volume (NIfTI/NRRD)")
    p.add_argument("--goal", required=True, help="What to segment")
    p.add_argument("--media-id", default="unknown")
    p.add_argument("--output-dir", default="/tmp/nni_loop")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--vision-model", default="",
                   help="OpenAI vision model (default: $NNINTERACTIVE_VISION_MODEL or gpt-4o)")
    p.add_argument("--expected-voxels", type=int, default=0,
                   help="Approximate voxel count of the target structure "
                        "(typically the voxelized GT mesh). When set, the "
                        "LLM is told its size budget each step. Strongly "
                        "recommended for whole-organ goals; without it "
                        "models tend to over-segment by ~2x.")
    p.add_argument("--expected-volume-mm3", type=float, default=0.0,
                   help="Approximate target volume in mm^3 (companion to "
                        "--expected-voxels; informational only).")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    t0 = time.time()
    result = run_loop(
        input_path=args.input,
        goal=args.goal,
        output_dir=args.output_dir,
        media_id=args.media_id,
        max_steps=args.max_steps,
        vision_model=args.vision_model,
        expected_voxels=(args.expected_voxels or None),
        expected_volume_mm3=(args.expected_volume_mm3 or None),
    )
    result["duration_s"] = round(time.time() - t0, 1)
    print(json.dumps({k: v for k, v in result.items() if k != "history"},
                     indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
