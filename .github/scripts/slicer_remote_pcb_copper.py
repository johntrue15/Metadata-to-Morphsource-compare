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
import csv
import json
import os
import sys
import textwrap
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
sys.path.insert(0, str(_SCRIPT_DIR))

from remote_volume_io import load_volume_from_remote_path  # noqa: E402
from run_telemetry import EXPORT_SEGMENTATION_SRC  # noqa: E402
from slicer_remote_bright_seed import post_python  # noqa: E402
from slicer_remote_loop import (  # noqa: E402
    APPLY_BBOX_SRC_TEMPLATE,
    APPLY_POINT_SRC_TEMPLATE,
    CAPTURE_METADATA_SRC,
    http_get,
)

DEFAULT_REMOTE_VOLUME_PATH = "/home/exouser/Desktop/pcb_ti_jetstream.nii.gz"

# Load PCB, remove skull volumes from scene, bind nnInteractive segment-editor source.
ENSURE_PCB_VOLUME_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, os, traceback
    target_name = {target_name!r}
    load_path = {load_path!r}
    remove_skull = {remove_skull!r}
    out = {{}}
    try:
        def _find(name):
            for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
                if v.GetName() == name:
                    return v
            return None

        removed = []
        if remove_skull:
            for v in list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")):
                nm = v.GetName()
                if nm == target_name:
                    continue
                low = nm.lower()
                if any(x in low for x in ("crotalus", "skull", "tuatara", "colors_of")):
                    removed.append(nm)
                    slicer.mrmlScene.RemoveNode(v)
        out["removed_volumes"] = removed

        vol = _find(target_name)
        loaded = False
        if vol is None and load_path and os.path.isfile(load_path):
            vol = slicer.util.loadVolume(load_path, properties={{"name": target_name}})
            loaded = bool(vol)
        elif vol is not None and load_path and os.path.isfile(load_path):
            # Replace stale node so nnInteractive cannot keep an old binding.
            slicer.mrmlScene.RemoveNode(vol)
            vol = slicer.util.loadVolume(load_path, properties={{"name": target_name}})
            loaded = True
        if vol is None:
            out["status"] = "not_found"
            out["available"] = [
                v.GetName() for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
            ]
        else:
            sel = slicer.app.applicationLogic().GetSelectionNode()
            sel.SetActiveVolumeID(vol.GetID())
            slicer.app.applicationLogic().PropagateVolumeSelection(0)
            slicer.util.setSliceViewerLayers(background=vol, fit=True)

            mod = slicer.modules.slicernninteractive
            plugin = mod.widgetRepresentation().self()
            prev = plugin.get_volume_node()
            out["plugin_volume_before"] = prev.GetName() if prev else None
            ew = getattr(plugin, "editor_widget", None)
            if ew is not None and hasattr(ew, "setSourceVolumeNode"):
                ew.setSourceVolumeNode(vol)
                out["sync_method"] = "editor_widget.setSourceVolumeNode"
            else:
                out["sync_method"] = "missing_editor_widget"
            pv = plugin.get_volume_node()
            out["plugin_volume_after"] = pv.GetName() if pv else None
            out["plugin_volume_ok"] = bool(pv and pv.GetID() == vol.GetID())
            try:
                plugin.setup_prompts()
                out["setup_prompts"] = "ok"
            except Exception as e:
                out["setup_prompts"] = repr(e)

            img = vol.GetImageData()
            out["status"] = "ok"
            out["loaded_from_path"] = loaded
            out["volume_id"] = vol.GetID()
            out["volume_name"] = vol.GetName()
            out["dimensions_ijk"] = list(img.GetDimensions())
            out["spacing_mm"] = [round(s, 4) for s in vol.GetSpacing()]
            out["available"] = [
                v.GetName() for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
            ]
    except Exception as e:
        out["status"] = "exception"
        out["error"] = repr(e)
        out["traceback"] = traceback.format_exc()
    __execResult.update(out)
""").strip()

# ---------------------------------------------------------------------------
# PCB-specific Slicer recipes
# ---------------------------------------------------------------------------

EXPORT_PCB_NOISE_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, numpy as np, os, hashlib, traceback, tempfile, base64
    exclude = set({exclude_sids!r})
    noise_path = {noise_path!r}
    out = {{"status": "ok", "segments_included": [], "segments_excluded": []}}
    try:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
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
    import slicer, io, gzip, traceback
    import numpy as np
    import requests
    out = {}
    try:
        mod = slicer.modules.slicernninteractive
        plugin = mod.widgetRepresentation().self()
        sel = slicer.app.applicationLogic().GetSelectionNode()
        vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
        if vol is None:
            vol = plugin.get_volume_node()
        pv = plugin.get_volume_node()
        out["plugin_volume"] = pv.GetName() if pv else None
        out["selection_volume"] = vol.GetName() if vol else None
        if vol is None:
            out["status"] = "no_active_volume"
        else:
            arr = slicer.util.arrayFromVolume(vol)
            empty = np.zeros(arr.shape, dtype=np.uint8)
            buf = io.BytesIO()
            np.save(buf, empty, allow_pickle=False)
            compressed = gzip.compress(buf.getvalue())
            server = plugin.server
            if isinstance(server, dict):
                server = server.get("url") or server.get("base_url") or str(server)
            server = str(server).rstrip("/")
            r = requests.post(
                server + "/upload_segment",
                files={"file": ("seg.npy.gz", io.BytesIO(compressed),
                                "application/octet-stream")},
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
                d.SetColor(1.0, 0.55, 0.2)
            try:
                plugin.setup_prompts()
            except Exception as e:
                out["setup_prompts"] = repr(e)
            out["copper_segment"] = "Copper_Layer"
            out["work_node"] = work.GetName()
            out["status"] = "ok"
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


# PCB layer view = axial (Red) slice through the thin board-thickness axis.
# Side views (sagittal/coronal) are NOT sent to the LLM and are not clicked.
SETUP_PCB_LAYER_VIEW_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, vtk
    i, j, k = {i}, {j}, {k}
    out = {{}}
    try:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID())
        if vol is None:
            out["status"] = "no_volume"
        else:
            m = vtk.vtkMatrix4x4()
            vol.GetIJKToRASMatrix(m)
            ras4 = [0.0] * 4
            m.MultiplyPoint([float(i), float(j), float(k), 1.0], ras4)
            ras = ras4[:3]
            sw = slicer.app.layoutManager().sliceWidget("Red")
            if sw:
                sw.sliceLogic().GetSliceNode().JumpSliceByCentering(*ras)
            out["status"] = "ok"
            out["layer_ijk"] = [int(i), int(j), int(k)]
            out["ras"] = [round(r, 2) for r in ras]
    except Exception as e:
        out["status"] = "exception"
        out["error"] = repr(e)
    __execResult.update(out)
""").strip()


RECENTER_LAYER_VIEW_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, vtk
    i, j, k = {i}, {j}, {k}
    sel = slicer.app.applicationLogic().GetSelectionNode()
    vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID())
    if vol:
        m = vtk.vtkMatrix4x4()
        vol.GetIJKToRASMatrix(m)
        ras4 = [0.0] * 4
        m.MultiplyPoint([float(i), float(j), float(k), 1.0], ras4)
        ras = ras4[:3]
        sw = slicer.app.layoutManager().sliceWidget("Red")
        if sw:
            sw.sliceLogic().GetSliceNode().JumpSliceByCentering(*ras)
        __execResult["ras"] = [round(r, 2) for r in ras]
        __execResult["status"] = "ok"
    else:
        __execResult["status"] = "no_volume"
""").strip()


PCB_COPPER_SYSTEM_PROMPT = textwrap.dedent("""
    You guide nnInteractive on industrial PCB micro-CT to segment ONE large
    flat copper pour. This is electronics QA, not medical imaging.

    You receive a SINGLE top-down LAYER VIEW image (board seen from above,
  through the copper plane). You do NOT see edge-on side views.

    Rules:
    - Segment the broad flat copper plane visible in this layer view.
    - Use 1–2 BBOX prompts spanning the full copper pour in the i–j plane.
    - Refine with a few point clicks on the same plane.
    - POSITIVE on mid-to-bright copper; NEGATIVE on SMD pads, solder, vias.
    - Ignore tiny bright dots (prior bright-seed noise).
    - All coordinates lie on ONE fixed k index (the copper layer slice).

    Output strict JSON (one action per step):

        {"action": "point", "i": int, "j": int, "k": int,
         "positive": true|false, "rationale": "<=120 chars"}

        {"action": "bbox", "i0": int, "j0": int, "k0": int,
         "i1": int, "j1": int, "k1": int, "positive": true,
         "rationale": "<=120 chars"}

        {"action": "done", "rationale": "<=120 chars"}

    For bbox, k0 MUST equal k1 (flat box in the layer plane). For point,
    k is fixed to the layer index provided in the user message.
""").strip()


DEFAULT_COPPER_GOAL = (
    "On the top-down PCB layer view, segment the largest flat copper pour. "
    "Exclude SMD, solder, and vias. One unified segment on a single k slice."
)


def _pick_layer_plane(dims_ijk: list[int]) -> tuple[int, int]:
    """Return (layer_axis_index, layer_voxel_index) using the thinnest IJK dim."""
    dims = [max(1, int(d)) for d in dims_ijk[:3]]
    axis = min(range(3), key=lambda a: dims[a])
    return axis, dims[axis] // 2


def _layer_axis_name(axis: int) -> str:
    return ("i", "j", "k")[axis]


def _snap_action_to_layer(
    action: dict,
    *,
    layer_axis: int,
    layer_index: int,
    dims_ijk: list[int],
) -> dict:
    """Force prompts onto the copper layer plane (no side-view depth clicks)."""
    out = dict(action)
    kind = out.get("action", "")
    ax = _layer_axis_name(layer_axis)
    dims = [max(1, int(d)) for d in dims_ijk[:3]]
    if kind == "point":
        out["i"] = max(0, min(dims[0] - 1, int(out.get("i", 0))))
        out["j"] = max(0, min(dims[1] - 1, int(out.get("j", 0))))
        out["k"] = max(0, min(dims[2] - 1, int(out.get("k", 0))))
        out[ax] = layer_index
    elif kind == "bbox":
        for prefix in ("i", "j", "k"):
            a0 = f"{prefix}0"
            a1 = f"{prefix}1"
            lo = max(0, min(dims[{"i": 0, "j": 1, "k": 2}[prefix]] - 1,
                       int(out.get(a0, 0))))
            hi = max(0, min(dims[{"i": 0, "j": 1, "k": 2}[prefix]] - 1,
                       int(out.get(a1, lo))))
            if lo > hi:
                lo, hi = hi, lo
            out[a0], out[a1] = lo, hi
        out[f"{ax}0"] = layer_index
        out[f"{ax}1"] = layer_index
    return out


def _layer_ijk_center(
    dims_ijk: list[int],
    layer_axis: int,
    layer_index: int,
) -> tuple[int, int, int]:
    dims = [max(1, int(d)) for d in dims_ijk[:3]]
    ijk = [dims[0] // 2, dims[1] // 2, dims[2] // 2]
    ijk[layer_axis] = max(0, min(dims[layer_axis] - 1, int(layer_index)))
    return ijk[0], ijk[1], ijk[2]


def _setup_pcb_layer_view(
    base_url: str,
    dims_ijk: list[int],
    *,
    layer_axis: int,
    layer_index: int,
) -> dict:
    """Center the axial (top-down) slice on the PCB copper plane."""
    i, j, k = _layer_ijk_center(dims_ijk, layer_axis, layer_index)
    return post_python(
        base_url,
        SETUP_PCB_LAYER_VIEW_SRC_TEMPLATE.format(i=i, j=j, k=k),
        timeout=60,
    )


def capture_pcb_layer_view(base_url: str, step_dir: Path) -> Path:
    """Axial slice only — top-down copper layer view (not side views)."""
    step_dir.mkdir(parents=True, exist_ok=True)
    out = step_dir / "layer_view.png"
    url = f"{base_url}/slicer/slice?orientation=axial"
    out.write_bytes(http_get(url, timeout=30))
    return out


def compose_layer_view(
    layer_path: Path,
    out_path: Path,
    *,
    layer_k: int,
    step_idx: int,
) -> Path:
    """Single-panel image for the vision LLM (layer view only)."""
    from slicer_remote_loop import _load_pillow

    Image, ImageDraw, ImageFont = _load_pillow()
    im = Image.open(layer_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = ImageFont.load_default()
    label = f"PCB layer view (axial, k={layer_k})  step {step_idx}"
    draw.rectangle([4, 4, 520, 20], fill=(0, 0, 0))
    draw.text((8, 6), label, fill=(255, 255, 255), font=font)
    im.save(out_path, format="PNG", optimize=True)
    return out_path


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


def _score_labelmaps(
    pred_path: Path,
    gt_path: Path,
    *,
    no_surface: bool,
) -> dict:
    """Compute segmentation metrics; returns serializable dict with status."""
    try:
        from segmentation_metrics import compare_labelmaps
    except Exception as exc:
        return {"status": "error", "error": f"metrics_import_failed: {exc!r}"}
    try:
        metrics = compare_labelmaps(
            str(pred_path),
            str(gt_path),
            compute_surface_distances=not no_surface,
        )
        data = metrics.to_dict()
        data["status"] = "ok"
        return data
    except Exception as exc:
        return {"status": "error", "error": repr(exc)}


def _write_results_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _ensure_pcb_volume(
    base_url: str,
    name: str,
    *,
    load_path: str,
    remove_skull: bool = True,
) -> dict:
    """Load PCB volume, set Slicer selection, sync nnInteractive source volume."""
    r = post_python(
        base_url,
        ENSURE_PCB_VOLUME_SRC_TEMPLATE.format(
            target_name=name,
            load_path=load_path,
            remove_skull=remove_skull,
        ),
        timeout=360,
    )
    if r.get("status") == "not_found" and load_path:
        print(f"-> Volume {name!r} missing; loading from {load_path!r}…")
        lr = load_volume_from_remote_path(base_url, load_path, name=name, timeout=360)
        print(f"   load: {lr.get('status')}  dims={lr.get('shape_kji')}")
        if lr.get("status") != "ok":
            return lr
        r = post_python(
            base_url,
            ENSURE_PCB_VOLUME_SRC_TEMPLATE.format(
                target_name=name,
                load_path=load_path,
                remove_skull=remove_skull,
            ),
            timeout=360,
        )
    return r


def export_noise(
    base_url: str,
    out_dir: Path,
    *,
    volume: str,
    remote_volume_path: str,
    exclude_segments: list[str],
    remote_noise_path: str,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    if volume:
        r = _ensure_pcb_volume(
            base_url, volume, load_path=remote_volume_path,
        )
        if r.get("status") != "ok":
            raise RuntimeError(f"PCB volume setup failed: {r}")
        print(f"   nnInteractive source: {r.get('plugin_volume_after')!r}  "
              f"(sync={r.get('sync_method')})")

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


def _apply_pcb_layer_action(
    base_url: str,
    action: dict,
    dims_ijk: list[int],
    *,
    layer_axis: int,
    layer_index: int,
    auto_negative_on_noise: bool,
) -> dict:
    """Apply nnInteractive prompt on the fixed copper layer plane only."""
    from slicer_remote_bright_seed import post_python as post_python_robust

    action = _snap_action_to_layer(
        action,
        layer_axis=layer_axis,
        layer_index=layer_index,
        dims_ijk=dims_ijk,
    )
    kind = action.get("action", "")
    if kind == "point" and auto_negative_on_noise and action.get("positive", True):
        i = int(action["i"])
        j = int(action["j"])
        k = int(action["k"])
        if _is_in_noise(base_url, i, j, k):
            action = {**action, "positive": False,
                      "rationale": (action.get("rationale", "") +
                                    " [auto-negative: noise voxel]")[:120]}
            print("  noise filter  : flipped to NEGATIVE (SMD/solder mask)")
    dims = [max(1, int(d)) for d in dims_ijk[:3]]
    if kind == "point":
        i = int(action["i"])
        j = int(action["j"])
        k = int(action["k"])
        src = APPLY_POINT_SRC_TEMPLATE.format(
            i=i, j=j, k=k, positive=bool(action.get("positive", True)),
        )
        result = post_python_robust(base_url, src, timeout=600)
        result["clamped_ijk"] = [i, j, k]
        try:
            post_python_robust(
                base_url,
                RECENTER_LAYER_VIEW_SRC_TEMPLATE.format(i=i, j=j, k=k),
                timeout=30,
            )
        except Exception as e:
            result["recenter_error"] = repr(e)
        return result
    if kind == "bbox":
        i0, j0, k0 = int(action["i0"]), int(action["j0"]), int(action["k0"])
        i1, j1, k1 = int(action["i1"]), int(action["j1"]), int(action["k1"])
        src = APPLY_BBOX_SRC_TEMPLATE.format(
            i0=i0, j0=j0, k0=k0, i1=i1, j1=j1, k1=k1,
            positive=bool(action.get("positive", True)),
        )
        result = post_python_robust(base_url, src, timeout=600)
        ic, jc, kc = (i0 + i1) // 2, (j0 + j1) // 2, k0
        result["clamped_ijk"] = [ic, jc, kc]
        try:
            post_python_robust(
                base_url,
                RECENTER_LAYER_VIEW_SRC_TEMPLATE.format(i=ic, j=jc, k=kc),
                timeout=30,
            )
        except Exception as e:
            result["recenter_error"] = repr(e)
        return result
    return {"status": "skipped", "reason": f"unknown action {kind!r}"}


def run_copper_llm(
    base_url: str,
    out_dir: Path,
    *,
    volume: str,
    remote_volume_path: str,
    noise_manifest: Path | None,
    remote_noise_path: str,
    goal: str,
    max_steps: int,
    model: str,
    dry_run: bool,
    layer_k: int | None = None,
    gt_labelmap: Path | None = None,
    score_each_step: bool = False,
    score_no_surface: bool = False,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    if volume:
        print(f"-> Ensuring PCB workspace volume {volume!r} (not crotalus/skull)…")
        r = _ensure_pcb_volume(
            base_url, volume, load_path=remote_volume_path,
        )
        if r.get("status") != "ok":
            print(f"FAILED volume setup: {r}")
            return 2
        if r.get("removed_volumes"):
            print(f"   removed skull volumes: {r.get('removed_volumes')}")
        print(f"   selection={r.get('volume_name')!r}  "
              f"nnInteractive={r.get('plugin_volume_after')!r}  "
              f"dims={r.get('dimensions_ijk')}  sync={r.get('sync_method')}")
        if r.get("plugin_volume_after") != volume:
            print(f"WARN: nnInteractive source is {r.get('plugin_volume_after')!r}, "
                  f"expected {volume!r}")
            if not r.get("plugin_volume_ok"):
                print("FAILED: could not bind nnInteractive to PCB volume")
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

    # Layer-plane setup (top-down view only; no side-view LLM prompts).
    meta0 = post_python(base_url, CAPTURE_METADATA_SRC, timeout=60)
    dims0 = meta0.get("dimensions_ijk", [266, 481, 41])
    layer_axis, layer_index_auto = _pick_layer_plane(dims0)
    layer_index = layer_index_auto
    if layer_k is not None:
        layer_index = max(0, min(int(dims0[layer_axis]) - 1, int(layer_k)))
    ax_name = _layer_axis_name(layer_axis)
    print(f"-> PCB layer view: axial (top-down), {ax_name}={layer_index} "
          f"(dims={dims0})")
    lvr = _setup_pcb_layer_view(
        base_url, dims0, layer_axis=layer_axis, layer_index=layer_index,
    )
    print(f"   layer view setup: {lvr.get('status')}  plane={ax_name}={layer_index}")

    client = None
    if not dry_run:
        from slicer_remote_loop import _load_openai

        OpenAI = _load_openai()
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None)

    history: list[dict] = []
    metric_rows: list[dict] = []
    # Patch call_llm system prompt via wrapper
    def _call_pcb_llm(grid_path, meta):
        from openai import OpenAI as _O  # noqa: F401 — already have client

        ax_name = _layer_axis_name(layer_axis)
        grid_b64 = base64.b64encode(grid_path.read_bytes()).decode("ascii")
        user_text = textwrap.dedent(f"""
            Goal: {goal}

            PCB copper-layer mode: segment ONE large flat copper pour.
            Known noise (SMD/solder) voxels in reference mask: {lr.get('noise_voxels', '?')}.
            Do NOT click positive on tiny bright pad-like dots.

            Volume: {meta.get('volume_name')!r}
            IJK dims (i, j, k): {meta.get('dimensions_ijk')}
            Spacing mm: {meta.get('spacing_mm')}
            Copper layer plane: {ax_name}={layer_index} ONLY (top-down layer view).
            All point/bbox prompts MUST use {ax_name}={layer_index};
            for bbox, {ax_name}0={ax_name}1={layer_index}.
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
            create_kwargs["max_completion_tokens"] = 4000
        else:
            create_kwargs["max_tokens"] = 800
            create_kwargs["temperature"] = 0.2
        raw = None
        refusal = None
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(**create_kwargs)
            except Exception as e:
                msg = str(e)
                if "max_tokens" in msg and "max_completion_tokens" in msg:
                    create_kwargs.pop("max_tokens", None)
                    create_kwargs.pop("temperature", None)
                    create_kwargs["max_completion_tokens"] = 4000
                    resp = client.chat.completions.create(**create_kwargs)
                elif "temperature" in msg:
                    create_kwargs.pop("temperature", None)
                    resp = client.chat.completions.create(**create_kwargs)
                else:
                    raise
            raw = resp.choices[0].message.content
            refusal = getattr(resp.choices[0].message, "refusal", None)
            if raw:
                break
            if attempt == 0 and is_reasoning:
                create_kwargs["max_completion_tokens"] = 6000
                print("  LLM retry     : empty content — bumping token budget")
                continue
            break
        if not raw:
            return {"action": "error",
                    "error": "empty LLM content",
                    "refusal": refusal}
        try:
            return json.loads(raw)
        except Exception as e:
            return {"action": "error", "raw": raw, "error": repr(e)}

    for step in range(max_steps):
        step_dir = out_dir / f"step_{step:02d}"
        step_dir.mkdir(parents=True, exist_ok=True)
        print(f"--- Step {step:02d} -----------------------------------------")

        meta = post_python(base_url, CAPTURE_METADATA_SRC, timeout=120)
        print(f"  vox_count={meta.get('segmentation_voxel_count')}  "
              f"dims={meta.get('dimensions_ijk')}  layer_k={layer_index}")

        layer_img = capture_pcb_layer_view(base_url, step_dir)
        grid = compose_layer_view(
            layer_img, step_dir / "grid.png",
            layer_k=layer_index, step_idx=step,
        )
        print(f"  saved layer view: {grid}")

        if dry_run:
            print("  (dry-run)")
            break

        action = _call_pcb_llm(grid, meta)
        print(f"  LLM action    : {json.dumps(action)[:200]}")

        state = {
            "step": step,
            "metadata": meta,
            "llm_action": action,
            "layer_k": layer_index,
            "view_mode": "layer_only",
        }

        if action.get("action") == "done":
            history.append({"step": step, "action": action})
            (step_dir / "state.json").write_text(json.dumps(state, indent=2, default=str))
            break
        if action.get("action") == "error":
            print(f"  LLM error      : {action.get('error')} — stopping")
            history.append({"step": step, "action": action})
            (step_dir / "state.json").write_text(json.dumps(state, indent=2, default=str))
            break

        try:
            result = _apply_pcb_layer_action(
                base_url, action,
                dims_ijk=meta.get("dimensions_ijk", [10**6] * 3),
                layer_axis=layer_axis,
                layer_index=layer_index,
                auto_negative_on_noise=True,
            )
        except (RuntimeError, TimeoutError, OSError) as exc:
            print(f"  apply failed    : {exc!r} — continuing")
            result = {"status": "apply_failed", "error": repr(exc)}
            history.append({"step": step, "action": action, "result": result})
            continue
        print(f"  applied       : {result.get('seconds')}s  "
              f"ijk={result.get('clamped_ijk')}")
        step_rec: dict = {"step": step, "action": action, "result": result}

        if gt_labelmap and score_each_step and gt_labelmap.is_file():
            step_export = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=900)
            comp = step_export.get("composite")
            if comp and comp.get("data_b64"):
                pred_path = step_dir / "pred_step.nii.gz"
                _write_b64_artifact(comp, pred_path)
                metrics = _score_labelmaps(
                    pred_path,
                    gt_labelmap,
                    no_surface=score_no_surface,
                )
                step_rec["metrics"] = metrics
                state["metrics"] = metrics
                if metrics.get("status") == "ok":
                    metric_rows.append(
                        {
                            "step": step,
                            "phase": "step",
                            "dice": metrics.get("dice"),
                            "iou": metrics.get("iou"),
                            "precision": metrics.get("precision"),
                            "recall": metrics.get("recall"),
                            "hausdorff_mm": metrics.get("hausdorff_mm"),
                            "hausdorff_95_mm": metrics.get("hausdorff_95_mm"),
                            "average_surface_dist_mm": metrics.get("average_surface_dist_mm"),
                            "centroid_distance_mm": metrics.get("centroid_distance_mm"),
                        }
                    )
                print(f"  step score    : {metrics.get('status')} "
                      f"dice={metrics.get('dice')}")

        history.append(step_rec)
        (step_dir / "state.json").write_text(json.dumps(state, indent=2, default=str))
        time.sleep(1.0)

    summary = {
        "phase": "copper",
        "goal": goal,
        "steps": len(history),
        "history": history,
        "noise_path": noise_path,
        "layer_k": layer_index,
        "view_mode": "layer_only",
        "gt_labelmap": str(gt_labelmap) if gt_labelmap else None,
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

    final_metrics = None
    if gt_labelmap and gt_labelmap.is_file() and comp and comp.get("data_b64"):
        pred_final = art / "copper_composite.nii.gz"
        final_metrics = _score_labelmaps(
            pred_final,
            gt_labelmap,
            no_surface=score_no_surface,
        )
        summary["final_metrics"] = final_metrics
        (out_dir / "final_metrics.json").write_text(
            json.dumps(final_metrics, indent=2, default=str)
        )
        if final_metrics.get("status") == "ok":
            metric_rows.append(
                {
                    "step": len(history),
                    "phase": "final",
                    "dice": final_metrics.get("dice"),
                    "iou": final_metrics.get("iou"),
                    "precision": final_metrics.get("precision"),
                    "recall": final_metrics.get("recall"),
                    "hausdorff_mm": final_metrics.get("hausdorff_mm"),
                    "hausdorff_95_mm": final_metrics.get("hausdorff_95_mm"),
                    "average_surface_dist_mm": final_metrics.get("average_surface_dist_mm"),
                    "centroid_distance_mm": final_metrics.get("centroid_distance_mm"),
                }
            )
        print(f"-> Final score  : {final_metrics.get('status')} "
              f"dice={final_metrics.get('dice')}")
    if metric_rows:
        _write_results_csv(metric_rows, out_dir / "results.csv")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(f"DONE. artifacts -> {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--phase", choices=("export-noise", "copper", "full"),
                   default="full")
    p.add_argument("--volume", default="pcb_ti_jetstream")
    p.add_argument(
        "--remote-volume-path",
        default=os.environ.get("PCB_REMOTE_VOLUME_PATH", DEFAULT_REMOTE_VOLUME_PATH),
        help="NIfTI on Jetstream to load when --volume is not in the Slicer scene",
    )
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
    p.add_argument("--model",
                   default=os.environ.get("OPENAI_MODEL")
                   or os.environ.get("NNINTERACTIVE_VISION_MODEL")
                   or os.environ.get("OPENAI_VISION_MODEL")
                   or "gpt-4o",
                   help="Vision model for copper LLM steps (default: OPENAI_MODEL "
                        "or NNINTERACTIVE_VISION_MODEL or gpt-4o)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--layer-k",
        type=int,
        default=None,
        help="Fixed k index for copper layer plane (default: mid of thinnest IJK dim)",
    )
    p.add_argument(
        "--gt-labelmap",
        type=Path,
        default=None,
        help="Optional GT labelmap on same CT grid for iterative/final scoring",
    )
    p.add_argument(
        "--score-each-step",
        action="store_true",
        help="Export current composite and score each step against --gt-labelmap",
    )
    p.add_argument(
        "--score-no-surface",
        action="store_true",
        help="Skip expensive Hausdorff/surface-distance computation in scoring",
    )
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
            remote_volume_path=args.remote_volume_path,
            exclude_segments=exclude,
            remote_noise_path=args.remote_noise_path,
        )
        args.noise_manifest = noise_dir / "noise_manifest.json"

    if args.phase in ("copper", "full"):
        copper_dir = args.out_dir / "copper_llm" if args.phase == "full" else args.out_dir
        return run_copper_llm(
            base_url, copper_dir,
            volume=args.volume,
            remote_volume_path=args.remote_volume_path,
            noise_manifest=args.noise_manifest,
            remote_noise_path=args.remote_noise_path,
            goal=args.goal,
            max_steps=args.max_steps,
            model=args.model,
            dry_run=args.dry_run,
            layer_k=args.layer_k,
            gt_labelmap=args.gt_labelmap,
            score_each_step=args.score_each_step,
            score_no_surface=args.score_no_surface,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
