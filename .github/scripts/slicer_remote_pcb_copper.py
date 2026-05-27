#!/usr/bin/env python3
"""
PCB two-phase segmentation on remote 3D Slicer (Jetstream).

Phase 1 — noise (bright-seed SMD/solder):
  Union small bright-seed segments into a reference noise mask, saved locally
  and on the Jetstream Desktop as ``pcb_noise_union.nii.gz``.

Phase 2 — copper (LLM + single-segment refinement):
  Reset the working segmentation (keep noise reference), start ONE copper
  segment, and drive it with the vision LLM. Positive clicks that land on
  known noise voxels are auto-flipped to negative.

Usage::

    set -a && source .env && set +a

    # Export noise from the current scene (~200 bright-seed segments)
    python3 .github/scripts/slicer_remote_pcb_copper.py \\
        --phase export-noise \\
        --volume pcb_ti_jetstream \\
        --exclude-segment Segment_232 \\
        --out-dir runs/pcb_noise_export

    # Fresh copper-layer test (uses exported noise manifest)
    python3 .github/scripts/slicer_remote_pcb_copper.py \\
        --phase copper \\
        --volume pcb_ti_jetstream \\
        --noise-manifest runs/pcb_noise_export/noise_manifest.json \\
        --max-steps 10 \\
        --out-dir runs/pcb_copper_llm_test
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import textwrap
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
sys.path.insert(0, str(_SCRIPT_DIR))

from run_telemetry import EXPORT_SEGMENTATION_SRC  # noqa: E402
from slicer_remote_bright_seed import post_python  # noqa: E402
from slicer_remote_loop import (  # noqa: E402
    CAPTURE_METADATA_SRC,
    RESET_SEGMENTATION_SRC,
    SET_ACTIVE_VOLUME_SRC_TEMPLATE,
    apply_action,
    call_llm,
    capture_views,
    compose_grid,
)

# ---------------------------------------------------------------------------
# PCB-specific Slicer recipes
# ---------------------------------------------------------------------------

EXPORT_PCB_NOISE_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, numpy as np, os, hashlib, traceback, tempfile, base64
    exclude = set({exclude_sids!r})
    noise_path = {noise_path!r}
    out = {{"status": "ok", "segments_included": [], "segments_excluded": []}}
    try:
        vol = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
        if vol is None:
            out["status"] = "no_volume"
        else:
            shape = slicer.util.arrayFromVolume(vol).shape
            union = np.zeros(shape, dtype=bool)
            for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                if "do not touch" in sn.GetName().lower():
                    continue
                if sn.GetName() == "PCB_Noise_Ref":
                    continue
                seg = sn.GetSegmentation()
                for ii in range(seg.GetNumberOfSegments()):
                    sid = seg.GetNthSegmentID(ii)
                    sname = seg.GetSegment(sid).GetName()
                    if sid in exclude or sname in exclude:
                        out["segments_excluded"].append({{"sid": sid, "name": sname}})
                        continue
                    try:
                        a = slicer.util.arrayFromSegmentBinaryLabelmap(sn, sid)
                        if a is not None and a.shape == shape:
                            union |= (a > 0)
                            out["segments_included"].append({{
                                "sid": sid, "name": sname,
                                "voxels": int((a > 0).sum()),
                            }})
                    except Exception as e:
                        out["segments_included"].append({{"sid": sid, "error": repr(e)}})
            out["noise_voxels"] = int(union.sum())
            out["shape_kji"] = list(shape)
            label = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLLabelMapVolumeNode", "_pcb_noise_export"
            )
            try:
                slicer.util.updateVolumeFromArray(label, union.astype(np.uint8))
                label.SetSpacing(vol.GetSpacing())
                label.SetOrigin(vol.GetOrigin())
                os.makedirs(os.path.dirname(noise_path) or ".", exist_ok=True)
                slicer.util.saveNode(label, noise_path)
                out["noise_path"] = noise_path
                out["noise_bytes"] = os.path.getsize(noise_path)
            finally:
                slicer.mrmlScene.RemoveNode(label)
            ref_old = slicer.mrmlScene.GetFirstNodeByName("PCB_Noise_Ref")
            if ref_old:
                slicer.mrmlScene.RemoveNode(ref_old)
            ref = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLSegmentationNode", "PCB_Noise_Ref"
            )
            ref.CreateDefaultDisplayNodes()
            d = ref.GetDisplayNode()
            if d:
                d.SetVisibility(True)
                d.SetOpacity(0.25)
                d.SetVisibility3D(False)
            ref.GetSegmentation().AddEmptySegment("noise_union")
            sid = ref.GetSegmentation().GetSegmentIdBySegmentName("noise_union")
            loaded = slicer.util.loadLabelVolume(noise_path)
            if loaded and sid:
                slicer.modules.segmentations.logic().ImportLabelmapToSegmentNode(
                    loaded, ref, sid
                )
                slicer.mrmlScene.RemoveNode(loaded)
            globals()["_PCB_NOISE_MASK"] = union
            out["noise_ref_node"] = ref.GetID()
    except Exception as e:
        out["status"] = "exception"
        out["error"] = repr(e)
        out["traceback"] = traceback.format_exc()
    __execResult.update(out)
""").strip()


RESET_COPPER_WORKSPACE_SRC = textwrap.dedent("""
    import slicer, io, gzip, json, traceback
    import numpy as np
    import requests
    out = {{"status": "ok"}}
    try:
        mod = slicer.modules.slicernninteractive
        plugin = mod.widgetRepresentation().self()
        vol = plugin.get_volume_node()
        if vol is None:
            out["status"] = "no_active_volume"
        else:
            arr = slicer.util.arrayFromVolume(vol)
            empty = np.zeros(arr.shape, dtype=np.uint8)
            buf = io.BytesIO()
            np.save(buf, empty, allow_pickle=False)
            compressed = gzip.compress(buf.getvalue())
            r = requests.post(
                f"{{plugin.server}}/upload_segment",
                files={{"file": ("seg.npy.gz", io.BytesIO(compressed),
                                "application/octet-stream")}},
                timeout=120,
            )
            out["upload_segment_status"] = r.status_code
            cleared = []
            for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                nm = sn.GetName()
                if "do not touch" in nm.lower() or nm == "PCB_Noise_Ref":
                    continue
                seg = sn.GetSegmentation()
                seg.RemoveAllSegments()
                cleared.append(nm)
            out["cleared_nodes"] = cleared
            # Working segmentation for copper (reuse cleared node or create)
            work = None
            for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                if sn.GetName() == "Segmentation":
                    work = sn
                    break
            if work is None:
                work = slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLSegmentationNode", "Segmentation"
                )
            work.GetSegmentation().RemoveAllSegments()
            work.GetSegmentation().AddEmptySegment("Copper_Layer")
            work.CreateDefaultDisplayNodes()
            d = work.GetDisplayNode()
            if d:
                d.SetVisibility(True)
                d.SetOpacity(0.55)
                d.SetColor([1.0, 0.55, 0.2])
            try:
                plugin.setup_prompts()
            except Exception as e:
                out["setup_prompts"] = repr(e)
            out["copper_segment"] = "Copper_Layer"
            out["work_node"] = work.GetName()
    except Exception as e:
        out["status"] = "exception"
        out["error"] = repr(e)
        out["traceback"] = traceback.format_exc()
    __execResult.update(out)
""").strip()


CHECK_NOISE_IJK_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, numpy as np
    i, j, k = {i}, {j}, {k}
    in_noise = False
    noise_voxels = 0
    mask = globals().get("_PCB_NOISE_MASK")
    if mask is not None:
        try:
            in_noise = bool(mask[int(k), int(j), int(i)])
            noise_voxels = int(mask.sum())
        except Exception:
            in_noise = False
    else:
        ref = slicer.mrmlScene.GetFirstNodeByName("PCB_Noise_Ref")
        if ref is not None:
            sid = ref.GetSegmentation().GetNthSegmentID(0)
            try:
                a = slicer.util.arrayFromSegmentBinaryLabelmap(ref, sid)
                if a is not None:
                    in_noise = bool(a[int(k), int(j), int(i)] > 0)
                    noise_voxels = int((a > 0).sum())
            except Exception:
                pass
    __execResult["in_noise"] = in_noise
    __execResult["noise_voxels_total"] = noise_voxels
    __execResult["ijk"] = [int(i), int(j), int(k)]
""").strip()


LOAD_NOISE_MASK_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, numpy as np, os, traceback
    path = {noise_path!r}
    out = {{"status": "ok"}}
    try:
        if not os.path.isfile(path):
            out["status"] = "missing"
        else:
            node = slicer.util.loadLabelVolume(path)
            arr = slicer.util.arrayFromVolume(node)
            mask = arr > 0
            globals()["_PCB_NOISE_MASK"] = mask
            out["noise_voxels"] = int(mask.sum())
            out["shape_kji"] = list(mask.shape)
            ref = slicer.mrmlScene.GetFirstNodeByName("PCB_Noise_Ref")
            if ref is None:
                ref = slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLSegmentationNode", "PCB_Noise_Ref"
                )
            ref.GetSegmentation().RemoveAllSegments()
            ref.GetSegmentation().AddEmptySegment("noise_union")
            sid = ref.GetSegmentation().GetSegmentIdBySegmentName("noise_union")
            if sid:
                slicer.modules.segmentations.logic().ImportLabelmapToSegmentNode(
                    node, ref, sid
                )
            slicer.mrmlScene.RemoveNode(node)
            out["loaded_from"] = path
    except Exception as e:
        out["status"] = "exception"
        out["error"] = repr(e)
        out["traceback"] = traceback.format_exc()
    __execResult.update(out)
""").strip()


PCB_COPPER_SYSTEM_PROMPT = textwrap.dedent("""
    You guide nnInteractive in 3D Slicer to segment LARGE FLAT COPPER PLANES
    on a PCB CT volume. This is NOT skull/bone segmentation.

    Context:
    - A prior bright-seed pass already mapped SMD pads, solder bumps, and
      other small bright "noise" features (shown as many tiny colored islands
      in reference screenshots if present).
    - Your job is ONE continuous copper layer segment — like a manual orange
      overlay covering a broad copper pour — NOT hundreds of tiny segments.
    - Use POSITIVE clicks on mid-to-bright flat copper regions inside large
      planes. Use NEGATIVE clicks to exclude SMD/solder/vias and holes.
    - Prefer 1–2 well-placed BBOX prompts that span a full copper plane on
      the best-visible slice, then refine with a few points.
    - Avoid clicking on tiny bright dots (those are noise/SMD).

    Views (2×2 grid): Red=axial, Yellow=sagittal, Green=coronal, 3D.

    Output strict JSON (one action per step):

        {"action": "point", "i": int, "j": int, "k": int,
         "positive": true|false, "rationale": "<=120 chars"}

        {"action": "bbox", "i0": int, "j0": int, "k0": int,
         "i1": int, "j1": int, "k1": int, "positive": true,
         "rationale": "<=120 chars"}

        {"action": "done", "rationale": "<=120 chars"}

    Coordinates are IJK voxel indices. All clicks refine the SAME copper
    segment — do not ask for multiple separate structures unless clearly
    separate copper pours are visible.
""").strip()


DEFAULT_COPPER_GOAL = (
    "Segment the largest visible flat copper pour on this PCB slice stack. "
    "Cover the broad orange/copper plane; exclude SMD pads, solder, vias, "
    "and drill holes. One unified copper segment."
)


def _read_url() -> str:
    url = (
        os.environ.get("SLICER_WEBSERVER_URL", "").strip()
        or os.environ.get("NNI_REMOTE_URL", "").strip()
    )
    if not url:
        sys.exit("ERROR: set SLICER_WEBSERVER_URL")
    if url.startswith("ws://"):
        url = "http://" + url[len("ws://") :]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://") :]
    return url.rstrip("/")


def _write_b64_artifact(entry: dict, dest: Path) -> None:
    if not entry or not entry.get("data_b64"):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(entry["data_b64"]))


def _set_volume(base_url: str, name: str) -> dict:
    return post_python(
        base_url,
        SET_ACTIVE_VOLUME_SRC_TEMPLATE.format(target_name=name),
        timeout=180,
    )


def export_noise(
    base_url: str,
    out_dir: Path,
    *,
    volume: str,
    exclude_segments: list[str],
    remote_noise_path: str,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    if volume:
        r = _set_volume(base_url, volume)
        if r.get("status") != "ok":
            raise RuntimeError(f"volume not found: {r}")

    print("-> Exporting PCB noise union (bright-seed segments)…")
    r = post_python(
        base_url,
        EXPORT_PCB_NOISE_SRC_TEMPLATE.format(
            exclude_sids=exclude_segments,
            noise_path=remote_noise_path,
        ),
        timeout=600,
    )
    if r.get("status") != "ok":
        # File may still have been written before ref-node setup failed
        if r.get("noise_path") and r.get("noise_voxels"):
            print(f"   WARN: ref node setup failed but noise file exists: {r.get('error')}")
        else:
            raise RuntimeError(f"noise export failed: {r!r}")

    manifest = {
        "phase": "export-noise",
        "volume": volume,
        "exclude_segments": exclude_segments,
        "remote_noise_path": remote_noise_path,
        "noise_voxels": r.get("noise_voxels"),
        "shape_kji": r.get("shape_kji"),
        "segments_included": r.get("segments_included"),
        "segments_excluded": r.get("segments_excluded"),
        "n_noise_segments": len(r.get("segments_included") or []),
    }
    (out_dir / "noise_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"   noise segments : {manifest['n_noise_segments']}")
    print(f"   noise voxels   : {manifest['noise_voxels']:,}")
    print(f"   remote file    : {remote_noise_path}")

    print("-> Pulling per-segment NIfTIs for archive…")
    export = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=900)
    art = out_dir / "artifacts"
    art.mkdir(exist_ok=True)
    for seg in export.get("per_segment") or []:
        fn = seg.get("filename")
        if fn:
            _write_b64_artifact(seg, art / fn)
    comp = export.get("composite")
    if comp:
        _write_b64_artifact(comp, art / "composite_pre_copper.nii.gz")
    manifest["export_status"] = export.get("status")
    (out_dir / "noise_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def _is_in_noise(base_url: str, i: int, j: int, k: int) -> bool:
    r = post_python(
        base_url,
        CHECK_NOISE_IJK_SRC_TEMPLATE.format(i=i, j=j, k=k),
        timeout=60,
    )
    return bool(r.get("in_noise"))


def _apply_pcb_action(
    base_url: str,
    action: dict,
    dims_ijk: list[int],
    *,
    auto_negative_on_noise: bool,
) -> dict:
    kind = action.get("action", "")
    if kind == "point" and auto_negative_on_noise and action.get("positive", True):
        i = int(action.get("i", 0))
        j = int(action.get("j", 0))
        k = int(action.get("k", 0))
        if _is_in_noise(base_url, i, j, k):
            action = {**action, "positive": False,
                      "rationale": (action.get("rationale", "") +
                                    " [auto-negative: noise voxel]")[:120]}
            print("  noise filter  : flipped to NEGATIVE (SMD/solder mask)")
    return apply_action(base_url, action, dims_ijk)


def run_copper_llm(
    base_url: str,
    out_dir: Path,
    *,
    volume: str,
    noise_manifest: Path | None,
    remote_noise_path: str,
    goal: str,
    max_steps: int,
    model: str,
    dry_run: bool,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    if volume:
        r = _set_volume(base_url, volume)
        if r.get("status") != "ok":
            print(f"FAILED volume: {r}")
            return 2

    noise_path = remote_noise_path
    if noise_manifest and noise_manifest.is_file():
        manifest = json.loads(noise_manifest.read_text())
        noise_path = manifest.get("remote_noise_path", noise_path)
        print(f"-> Loaded noise manifest ({manifest.get('n_noise_segments')} segments)")

    print(f"-> Loading noise reference from {noise_path!r}…")
    lr = post_python(
        base_url,
        LOAD_NOISE_MASK_SRC_TEMPLATE.format(noise_path=noise_path),
        timeout=300,
    )
    if lr.get("status") != "ok":
        print(f"   WARN: noise load: {lr!r}")

    print("-> Resetting copper workspace (keeping PCB_Noise_Ref)…")
    rr = post_python(base_url, RESET_COPPER_WORKSPACE_SRC, timeout=240)
    print(f"   {rr.get('status')}  copper={rr.get('copper_segment')}")

    client = None
    if not dry_run:
        from slicer_remote_loop import _load_openai

        OpenAI = _load_openai()
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None)

    history: list[dict] = []
    # Patch call_llm system prompt via wrapper
    def _call_pcb_llm(grid_path, meta):
        from openai import OpenAI as _O  # noqa: F401 — already have client

        grid_b64 = base64.b64encode(grid_path.read_bytes()).decode("ascii")
        user_text = textwrap.dedent(f"""
            Goal: {goal}

            PCB copper-layer mode: segment ONE large flat copper pour.
            Known noise (SMD/solder) voxels in reference mask: {lr.get('noise_voxels', '?')}.
            Do NOT click positive on tiny bright pad-like dots.

            Volume: {meta.get('volume_name')!r}
            IJK dims (i, j, k): {meta.get('dimensions_ijk')}
            Spacing mm: {meta.get('spacing_mm')}
            Current copper segment voxel count: {meta.get('segmentation_voxel_count')}

            Action history:
            {json.dumps(history[-6:], indent=2)}

            Reply with strict JSON only.
        """).strip()
        msgs = [
            {"role": "system", "content": PCB_COPPER_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{grid_b64}",
                               "detail": "high"}},
            ]},
        ]
        create_kwargs = dict(model=model, messages=msgs,
                             response_format={"type": "json_object"})
        is_reasoning = not (model.startswith("gpt-4") or model.startswith("gpt-3"))
        if is_reasoning:
            create_kwargs["max_completion_tokens"] = 1200
        else:
            create_kwargs["max_tokens"] = 400
            create_kwargs["temperature"] = 0.2
        resp = client.chat.completions.create(**create_kwargs)
        return json.loads(resp.choices[0].message.content)

    for step in range(max_steps):
        step_dir = out_dir / f"step_{step:02d}"
        step_dir.mkdir(parents=True, exist_ok=True)
        print(f"--- Step {step:02d} -----------------------------------------")

        meta = post_python(base_url, CAPTURE_METADATA_SRC, timeout=120)
        print(f"  vox_count={meta.get('segmentation_voxel_count')}  "
              f"dims={meta.get('dimensions_ijk')}")

        views = capture_views(base_url, step_dir)
        grid = compose_grid(views, step_dir / "grid.png", meta, step)
        print(f"  saved grid    : {grid}")

        if dry_run:
            print("  (dry-run)")
            break

        action = _call_pcb_llm(grid, meta)
        print(f"  LLM action    : {json.dumps(action)[:200]}")

        (step_dir / "state.json").write_text(json.dumps({
            "step": step, "metadata": meta, "llm_action": action,
        }, indent=2, default=str))

        if action.get("action") == "done":
            history.append({"step": step, "action": action})
            break
        if action.get("action") == "error":
            break

        result = _apply_pcb_action(
            base_url, action,
            dims_ijk=meta.get("dimensions_ijk", [10**6] * 3),
            auto_negative_on_noise=True,
        )
        print(f"  applied       : {result.get('seconds')}s  "
              f"ijk={result.get('clamped_ijk')}")
        history.append({"step": step, "action": action, "result": result})
        time.sleep(1.0)

    summary = {
        "phase": "copper",
        "goal": goal,
        "steps": len(history),
        "history": history,
        "noise_path": noise_path,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print("-> Exporting final copper segmentation…")
    export = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=900)
    art = out_dir / "artifacts"
    art.mkdir(exist_ok=True)
    for seg in export.get("per_segment") or []:
        fn = seg.get("filename")
        if fn:
            _write_b64_artifact(seg, art / fn)
    comp = export.get("composite")
    if comp:
        _write_b64_artifact(comp, art / "copper_composite.nii.gz")

    print(f"DONE. artifacts -> {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--phase", choices=("export-noise", "copper", "full"),
                   default="full")
    p.add_argument("--volume", default="pcb_ti_jetstream")
    p.add_argument("--exclude-segment", action="append", default=[],
                   help="Segment ID/name to omit from noise union "
                        "(repeatable; default Segment_232 manual copper)")
    p.add_argument("--remote-noise-path",
                   default="/home/exouser/Desktop/pcb_noise_union.nii.gz")
    p.add_argument("--noise-manifest", type=Path, default=None)
    p.add_argument("--goal", default=DEFAULT_COPPER_GOAL)
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--out-dir", type=Path,
                   default=Path("runs") / f"pcb_copper_{time.strftime('%Y%m%dT%H%M%S')}")
    p.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    base_url = _read_url()
    exclude = args.exclude_segment or ["Segment_232"]

    print(f"=== PCB copper pipeline ({args.phase}) ===")
    print(f"server : {base_url}")
    print(f"out    : {args.out_dir}")

    if args.phase in ("export-noise", "full"):
        noise_dir = args.out_dir / "noise_export" if args.phase == "full" else args.out_dir
        export_noise(
            base_url, noise_dir,
            volume=args.volume,
            exclude_segments=exclude,
            remote_noise_path=args.remote_noise_path,
        )
        args.noise_manifest = noise_dir / "noise_manifest.json"

    if args.phase in ("copper", "full"):
        copper_dir = args.out_dir / "copper_llm" if args.phase == "full" else args.out_dir
        return run_copper_llm(
            base_url, copper_dir,
            volume=args.volume,
            noise_manifest=args.noise_manifest,
            remote_noise_path=args.remote_noise_path,
            goal=args.goal,
            max_steps=args.max_steps,
            model=args.model,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
