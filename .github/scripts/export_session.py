#!/usr/bin/env python3
"""
Bundle a paint session into a single, paper-ready, reproducible artifact.

A "session" here is one or more contiguous ``runs/<run_id>/`` directories
produced by ``slicer_remote_bright_seed.py`` (or the LLM loop) against the
*same* source volume. We identify membership by matching the
``sha256_voxels`` recorded in each run's ``inputs.json`` — runs against a
different volume are silently rejected.

The output bundle is the canonical thing you cite in a paper:

    <bundle>/
        manifest.json        — single source of truth (session + sources)
        volume.nii.gz        — the original input volume (bit-identical)
        composite.nii.gz     — final union segmentation
        segments/
            Segment_1.nii.gz
            …
        clicks.jsonl         — chronological list of every paint action,
                               with batch/run id, IJK, positive/negative,
                               intensity at click, segment id created,
                               voxel delta, and remote/local timing
        environment.json     — merged local+remote provenance per run
        README.md            — human-readable explanation
        replay.sh            — one-liner that re-runs replay_session.py
        replay.py            — copy of the standalone replay script

Usage
-----
  # Auto-discover all runs/<>/ whose inputs.json sha256_voxels match
  # the currently active volume on the remote Slicer:
  python3 .github/scripts/export_session.py \\
      --auto-discover \\
      --out paper_artifacts/mouse_skull_session_001

  # Or list runs explicitly:
  python3 .github/scripts/export_session.py \\
      --runs runs/impc_bright_10click_20260522_074044 \\
             runs/impc_bright_10more_20260522_074300 \\
             runs/impc_bright_sat_b1_20260522_075107 \\
      --out paper_artifacts/mouse_skull_session_001

  # Skip the live Slicer round-trip (use what's already on disk):
  python3 .github/scripts/export_session.py --runs … --offline …
"""
from __future__ import annotations

import argparse
import base64
import datetime
import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _read_url() -> str:
    url = (
        os.environ.get("SLICER_WEBSERVER_URL", "").strip()
        or os.environ.get("NNI_REMOTE_URL", "").strip()
    )
    if not url:
        sys.exit(
            "ERROR: SLICER_WEBSERVER_URL (or NNI_REMOTE_URL) not set. "
            "Source your .env first:\n"
            "    set -a && source .env && set +a"
        )
    if url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    return url.rstrip("/")


def post_python(base_url: str, source: str, timeout: float = 240,
                retries: int = 3, retry_sleep: float = 4.0) -> dict:
    body = source.encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                base_url + "/slicer/exec", data=body, method="POST",
                headers={"Content-Type": "text/plain"},
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                status = resp.status
            if status != 200:
                raise RuntimeError(f"/slicer/exec -> HTTP {status}: {content[:300]!r}")
            result = json.loads(content)
            result["_dt_s"] = round(time.time() - t0, 3)
            return result
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last_err = e
            print(f"  [exec retry {attempt+1}/{retries}] {e!r}; sleeping {retry_sleep}s")
            time.sleep(retry_sleep)
    raise RuntimeError(f"/slicer/exec exhausted retries: {last_err!r}")


# ---------------------------------------------------------------------------
# Chunked-download primitives
# ---------------------------------------------------------------------------
# The Exosphere proxy in front of MorphoCloud kills any single /slicer/exec
# call that takes longer than ~60s (504 Gateway Timeout). To export a 10 MB
# volume + 110 per-segment NIfTIs reliably we split the work into many small
# /exec calls:
#
#   1. SAVE_VOLUME_TO_DISK_SRC               — write volume.nii.gz to a
#       tempdir on Jetstream; returns the
#       remote path + size + sha256.
#   2. SAVE_SEGMENTS_BATCH_SRC_TEMPLATE      — write a contiguous range of
#       per-segment + composite NIfTIs to a
#       tempdir on Jetstream; idempotent —
#       call N times to cover all segments.
#   3. READ_FILE_CHUNK_SRC_TEMPLATE          — read N bytes at offset M from
#       a remote path, return base64.
#   4. LIST_REMOTE_DIR_SRC_TEMPLATE          — directory listing helper.
#
# All four are tiny — each typically returns in well under 60s.

SAVE_VOLUME_TO_DISK_SRC = textwrap.dedent("""
    import slicer, os, tempfile, hashlib, traceback
    out = {}
    try:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
        if vol is None:
            __execResult["status"] = "no_active_volume"
        else:
            td = os.path.join(tempfile.gettempdir(), "ms_remote_export")
            os.makedirs(td, exist_ok=True)
            fp = os.path.join(td, "volume.nii.gz")
            ok = slicer.util.saveNode(vol, fp)
            if not ok or not os.path.exists(fp):
                __execResult["status"] = "save_failed"
            else:
                size = os.path.getsize(fp)
                h = hashlib.sha256()
                with open(fp, "rb") as f:
                    while True:
                        b = f.read(1 << 20)
                        if not b: break
                        h.update(b)
                arr = slicer.util.arrayFromVolume(vol)
                __execResult.update({
                    "status": "ok",
                    "remote_path":   fp,
                    "size_bytes":    size,
                    "sha256_file":   h.hexdigest(),
                    "sha256_voxels": hashlib.sha256(arr.tobytes()).hexdigest(),
                    "shape_kji":     list(arr.shape),
                    "dtype":         str(arr.dtype),
                    "spacing_mm":    [round(s, 6) for s in vol.GetSpacing()],
                    "origin":        [round(o, 6) for o in vol.GetOrigin()],
                    "volume_name":   vol.GetName(),
                })
    except Exception as e:
        __execResult["status"] = "exception"
        __execResult["error"] = repr(e)
        __execResult["traceback"] = traceback.format_exc()
""").strip()


SAVE_SEGMENTS_BATCH_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, os, tempfile, hashlib, traceback, vtk
    start_idx, count = {start_idx}, {count}
    write_composite = {write_composite}
    out = {{"per_segment": [], "composite": None}}
    def _hash_file(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b: break
                h.update(b)
        return h.hexdigest()
    try:
        seg_nodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
        seg_node = None
        for n in seg_nodes:
            if "do not touch" in n.GetName().lower():
                continue
            seg_node = n
            break
        if seg_node is None:
            __execResult["status"] = "no_segmentation"
        else:
            sel = slicer.app.applicationLogic().GetSelectionNode()
            ref_vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
            td = os.path.join(tempfile.gettempdir(), "ms_remote_export", "segments")
            os.makedirs(td, exist_ok=True)
            seg = seg_node.GetSegmentation()
            n_total = seg.GetNumberOfSegments()
            stop_idx = min(start_idx + count, n_total)
            for ii in range(start_idx, stop_idx):
                sid = seg.GetNthSegmentID(ii)
                s = seg.GetSegment(sid)
                sname = s.GetName()
                safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in sid)
                fp = os.path.join(td, f"{{safe}}.nii.gz")
                if os.path.exists(fp) and os.path.getsize(fp) > 0:
                    out["per_segment"].append({{
                        "sid": sid, "name": sname,
                        "filename": f"{{safe}}.nii.gz",
                        "remote_path": fp,
                        "size_bytes": os.path.getsize(fp),
                        "sha256": _hash_file(fp),
                        "color": [round(c, 4) for c in s.GetColor()],
                        "cached": True,
                    }})
                    continue
                label_node = slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLLabelMapVolumeNode", f"_export_{{sid}}"
                )
                try:
                    ids = vtk.vtkStringArray()
                    ids.InsertNextValue(sid)
                    ok = slicer.modules.segmentations.logic().ExportSegmentsToLabelmapNode(
                        seg_node, ids, label_node, ref_vol
                    )
                    if not ok:
                        out["per_segment"].append({{
                            "sid": sid, "name": sname,
                            "error": "ExportSegmentsToLabelmapNode returned False",
                        }})
                    else:
                        saved = slicer.util.saveNode(label_node, fp)
                        if saved and os.path.exists(fp):
                            out["per_segment"].append({{
                                "sid": sid, "name": sname,
                                "filename": f"{{safe}}.nii.gz",
                                "remote_path": fp,
                                "size_bytes": os.path.getsize(fp),
                                "sha256": _hash_file(fp),
                                "color": [round(c, 4) for c in s.GetColor()],
                                "cached": False,
                            }})
                        else:
                            out["per_segment"].append({{
                                "sid": sid, "name": sname,
                                "error": "saveNode failed",
                            }})
                finally:
                    slicer.mrmlScene.RemoveNode(label_node)
            out["start_idx"] = start_idx
            out["stop_idx"] = stop_idx
            out["n_total"] = n_total
            out["batch_done"] = (stop_idx >= n_total)
            if write_composite and stop_idx >= n_total:
                comp_node = slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLLabelMapVolumeNode", "_export_composite"
                )
                try:
                    ok = slicer.modules.segmentations.logic().ExportAllSegmentsToLabelmapNode(
                        seg_node, comp_node
                    )
                    if ok:
                        fp = os.path.join(td, "..", "composite.nii.gz")
                        fp = os.path.abspath(fp)
                        saved = slicer.util.saveNode(comp_node, fp)
                        if saved and os.path.exists(fp):
                            out["composite"] = {{
                                "filename": "composite.nii.gz",
                                "remote_path": fp,
                                "size_bytes": os.path.getsize(fp),
                                "sha256": _hash_file(fp),
                            }}
                finally:
                    slicer.mrmlScene.RemoveNode(comp_node)
            out["status"] = "ok"
            out["segmentation_node_name"] = seg_node.GetName()
            out["segment_count"] = n_total
            __execResult.update(out)
    except Exception as e:
        __execResult["status"] = "exception"
        __execResult["error"] = repr(e)
        __execResult["traceback"] = traceback.format_exc()
""").strip()


READ_FILE_CHUNK_SRC_TEMPLATE = textwrap.dedent("""
    import os, base64, hashlib
    out = {{}}
    path = {path!r}
    offset = {offset}
    length = {length}
    try:
        if not os.path.exists(path):
            out["status"] = "missing"
        else:
            sz = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(length)
            out["status"] = "ok"
            out["offset"] = offset
            out["length"] = len(data)
            out["total_size"] = sz
            out["data_b64"] = base64.b64encode(data).decode("ascii")
            out["chunk_sha256"] = hashlib.sha256(data).hexdigest()
    except Exception as e:
        out["status"] = "exception"
        out["error"] = repr(e)
    __execResult.update(out)
""").strip()


def download_remote_file(base_url: str, remote_path: str, dest: Path,
                         expected_sha: str | None = None,
                         chunk_bytes: int = 4 * 1024 * 1024) -> dict:
    """Pull ``remote_path`` from the Jetstream filesystem in 4 MB chunks
    via /slicer/exec. Verifies the on-disk sha256 against ``expected_sha``
    if given.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    offset = 0
    total = None
    h = hashlib.sha256()
    chunk_idx = 0
    while True:
        src = READ_FILE_CHUNK_SRC_TEMPLATE.format(
            path=remote_path, offset=offset, length=chunk_bytes,
        )
        r = post_python(base_url, src, timeout=120, retries=4, retry_sleep=6.0)
        if r.get("status") != "ok":
            raise RuntimeError(f"read_chunk failed at offset={offset}: {r}")
        data = base64.b64decode(r["data_b64"])
        if not data:
            break
        with open(dest, "ab") as f:
            f.write(data)
        h.update(data)
        offset += len(data)
        total = r["total_size"]
        chunk_idx += 1
        print(f"     chunk {chunk_idx:>3}  +{len(data):>9,} bytes  "
              f"({offset:>9,}/{total:>9,}  {100*offset/max(1,total):5.1f}%)")
        if offset >= total:
            break
    sha = h.hexdigest()
    if expected_sha and sha != expected_sha:
        raise RuntimeError(
            f"sha256 mismatch on {dest}: expected {expected_sha}, got {sha}"
        )
    return {"size_bytes": offset, "sha256": sha, "n_chunks": chunk_idx}


def chunked_dump_volume(base_url: str, dest_dir: Path) -> dict:
    """Server-side saveNode → chunked read."""
    print("  -> step 1/2: server-side saveNode for volume…")
    r = post_python(base_url, SAVE_VOLUME_TO_DISK_SRC, timeout=120,
                    retries=4, retry_sleep=6.0)
    if r.get("status") != "ok":
        raise RuntimeError(f"server-side volume save failed: {r}")
    print(f"     wrote remote {r['remote_path']}  "
          f"({r['size_bytes']:,} bytes  sha256={r['sha256_file'][:16]}…)")
    print("  -> step 2/2: chunked download…")
    res = download_remote_file(
        base_url, r["remote_path"], dest_dir / "volume.nii.gz",
        expected_sha=r["sha256_file"], chunk_bytes=4 * 1024 * 1024,
    )
    print(f"     done. {res['n_chunks']} chunks, total {res['size_bytes']:,} bytes")
    return {
        "path": "volume.nii.gz",
        "size_bytes": r["size_bytes"],
        "sha256_file": r["sha256_file"],
        "sha256_voxels": r["sha256_voxels"],
        "shape_kji": r["shape_kji"],
        "dtype": r["dtype"],
        "spacing_mm": r["spacing_mm"],
        "origin": r["origin"],
        "volume_name": r["volume_name"],
    }


def chunked_dump_segmentation(base_url: str, dest_dir: Path,
                              batch_size: int = 5) -> dict:
    """Server-side: write all segments to disk in batches of ``batch_size``;
    then chunked-download each one.
    """
    seg_dir = dest_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    all_segments: list[dict] = []
    composite = None
    start = 0
    print(f"  -> step 1/2: server-side saveNode for segments (batch {batch_size})…")
    while True:
        # Only ask Slicer to write the composite on the FINAL batch.
        src = SAVE_SEGMENTS_BATCH_SRC_TEMPLATE.format(
            start_idx=start, count=batch_size, write_composite=True,
        )
        r = post_python(base_url, src, timeout=180, retries=4, retry_sleep=6.0)
        if r.get("status") != "ok":
            raise RuntimeError(f"server-side seg-save failed at start={start}: {r}")
        for ps in r.get("per_segment", []):
            all_segments.append(ps)
        if r.get("composite"):
            composite = r["composite"]
        n_total = r.get("n_total", 0)
        stop = r.get("stop_idx", start)
        cached = sum(1 for ps in r.get("per_segment", []) if ps.get("cached"))
        new = len(r.get("per_segment", [])) - cached
        print(f"     batch [{start:>3}:{stop:>3}]/{n_total:>3}  "
              f"new={new}  cached={cached}  comp={'yes' if composite else 'no'}")
        if r.get("batch_done") or stop >= n_total:
            break
        start = stop
    print(f"  -> step 2/2: chunked download of {len(all_segments)} segments + composite…")
    per_segment_records: list[dict] = []
    for ps in all_segments:
        if not ps.get("remote_path"):
            print(f"     SKIP {ps.get('sid')}: {ps.get('error')}")
            continue
        dst = seg_dir / ps["filename"]
        res = download_remote_file(
            base_url, ps["remote_path"], dst,
            expected_sha=ps["sha256"], chunk_bytes=4 * 1024 * 1024,
        )
        per_segment_records.append({
            "sid": ps["sid"],
            "name": ps.get("name"),
            "filename": f"segments/{ps['filename']}",
            "size_bytes": res["size_bytes"],
            "sha256": res["sha256"],
            "color": ps.get("color"),
        })
    composite_record = None
    if composite and composite.get("remote_path"):
        dst = dest_dir / "composite.nii.gz"
        res = download_remote_file(
            base_url, composite["remote_path"], dst,
            expected_sha=composite["sha256"], chunk_bytes=4 * 1024 * 1024,
        )
        composite_record = {
            "path": "composite.nii.gz",
            "size_bytes": res["size_bytes"],
            "sha256": res["sha256"],
        }
    return {
        "composite": composite_record,
        "per_segment": per_segment_records,
        "segment_count": len(per_segment_records),
    }


# Snippet: dump the active volume as a NIfTI on the box and base64 it back.
DUMP_VOLUME_SRC = textwrap.dedent("""
    import slicer, base64, os, tempfile, hashlib, traceback
    out = {}
    try:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
        if vol is None:
            __execResult["status"] = "no_active_volume"
        else:
            td = tempfile.mkdtemp(prefix="bs_volexport_")
            fp = os.path.join(td, "volume.nii.gz")
            ok = slicer.util.saveNode(vol, fp)
            if not ok or not os.path.exists(fp):
                __execResult["status"] = "save_failed"
            else:
                with open(fp, "rb") as f:
                    data = f.read()
                h = hashlib.sha256(data).hexdigest()
                out["status"] = "ok"
                out["filename"] = "volume.nii.gz"
                out["size_bytes"] = len(data)
                out["sha256_file"] = h
                out["data_b64"] = base64.b64encode(data).decode("ascii")
                # Per-voxel hash (matches HASH_ACTIVE_VOLUME_SRC):
                arr = slicer.util.arrayFromVolume(vol)
                out["sha256_voxels"] = hashlib.sha256(arr.tobytes()).hexdigest()
                out["shape_kji"] = list(arr.shape)
                out["dtype"] = str(arr.dtype)
                out["spacing_mm"] = [round(s, 6) for s in vol.GetSpacing()]
                out["origin"]     = [round(o, 6) for o in vol.GetOrigin()]
                out["volume_name"] = vol.GetName()
                try:
                    os.unlink(fp); os.rmdir(td)
                except Exception:
                    pass
                __execResult.update(out)
    except Exception as e:
        __execResult["status"] = "exception"
        __execResult["error"] = repr(e)
        __execResult["traceback"] = traceback.format_exc()
""").strip()


# Import EXPORT_SEGMENTATION_SRC from the sibling module to keep behavior
# consistent with what the bright-seed script writes per-run.
sys.path.insert(0, str(SCRIPTS_DIR))
from run_telemetry import EXPORT_SEGMENTATION_SRC  # noqa: E402


# ---------------------------------------------------------------------------
# Run discovery / loading
# ---------------------------------------------------------------------------

def load_run(run_dir: Path) -> dict:
    """Read the persistent artifacts from a single run directory."""
    inputs = {}
    if (run_dir / "inputs.json").exists():
        inputs = json.loads((run_dir / "inputs.json").read_text())
    manifest = {}
    if (run_dir / "manifest.json").exists():
        manifest = json.loads((run_dir / "manifest.json").read_text())
    environment = {}
    if (run_dir / "environment.json").exists():
        environment = json.loads((run_dir / "environment.json").read_text())
    stop_reason = {}
    if (run_dir / "stop_reason.json").exists():
        stop_reason = json.loads((run_dir / "stop_reason.json").read_text())
    # Parse events.jsonl into a flat list of dicts.
    events: list[dict] = []
    ep = run_dir / "events.jsonl"
    if ep.exists():
        for line in ep.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return {
        "dir": str(run_dir),
        "inputs": inputs,
        "manifest": manifest,
        "environment": environment,
        "stop_reason": stop_reason,
        "events": events,
    }


def discover_runs(roots: list[Path], match_sha256: str | None,
                  min_started: float | None = None) -> list[Path]:
    """Find runs/<id>/ dirs under `roots` whose sha256_voxels match."""
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            ij = child / "inputs.json"
            if not ij.exists():
                continue
            try:
                inp = json.loads(ij.read_text())
            except Exception:
                continue
            sha = inp.get("sha256_voxels")
            if match_sha256 and sha != match_sha256:
                continue
            mf = child / "manifest.json"
            try:
                mft = json.loads(mf.read_text()) if mf.exists() else {}
            except Exception:
                mft = {}
            t = mft.get("started_unix") or 0
            if min_started and t and t < min_started:
                continue
            out.append(child)
    # Sort by run start time when available, else by dir name
    def _key(p: Path) -> tuple:
        try:
            m = json.loads((p / "manifest.json").read_text())
            return (m.get("started_unix") or 0.0, p.name)
        except Exception:
            return (0.0, p.name)
    out.sort(key=_key)
    return out


# ---------------------------------------------------------------------------
# Click extraction
# ---------------------------------------------------------------------------

def extract_clicks_from_events(events: list[dict]) -> list[dict]:
    """Pull every successful nnInteractive paint click out of an event log.

    The bright-seed script emits one ``step_end`` event per successful
    click. The fields we care about for replay are stable across versions:
    ``picked_ijk``, ``click_positive``, ``made_new_segment``, ``segment_id``,
    ``voxels_before``, ``voxels_after``, ``delta``, ``intensity``,
    ``click_seconds``, ``candidates_left``.
    """
    clicks: list[dict] = []
    for ev in events:
        if ev.get("event") != "step_end":
            continue
        if ev.get("status") != "ok":
            continue
        if not ev.get("picked_ijk"):
            continue
        clicks.append({
            "ts_utc": ev.get("t"),
            "elapsed_s": ev.get("elapsed_s"),
            "step": ev.get("step"),
            "ijk": list(ev["picked_ijk"]),
            "click_positive": bool(ev.get("click_positive", True)),
            "made_new_segment": bool(ev.get("made_new_segment", False)),
            "segment_id": ev.get("segment_id"),
            "intensity": ev.get("intensity"),
            "voxels_before": ev.get("voxels_before"),
            "voxels_after": ev.get("voxels_after"),
            "delta": ev.get("delta"),
            "segment_voxels": ev.get("segment_voxels"),
            "candidates_left": ev.get("candidates_left"),
            "click_seconds": ev.get("click_seconds"),
        })
    return clicks


# ---------------------------------------------------------------------------
# Live-side dumps (volume + final segmentation)
# ---------------------------------------------------------------------------

def dump_active_volume(base_url: str, dest_dir: Path) -> dict:
    print("  -> dumping active volume from Slicer (this is the big one)…")
    r = post_python(base_url, DUMP_VOLUME_SRC, timeout=600, retries=3,
                    retry_sleep=8.0)
    if r.get("status") != "ok":
        raise RuntimeError(f"dump volume failed: {r}")
    data = base64.b64decode(r["data_b64"])
    dest = dest_dir / "volume.nii.gz"
    dest.write_bytes(data)
    sha_disk = hashlib.sha256(data).hexdigest()
    assert sha_disk == r["sha256_file"], "volume sha256 mismatch on disk!"
    print(f"     wrote {dest}  ({len(data):,} bytes  sha256={sha_disk[:16]}…)")
    return {
        "path": "volume.nii.gz",
        "size_bytes": len(data),
        "sha256_file": sha_disk,
        "sha256_voxels": r.get("sha256_voxels"),
        "shape_kji": r.get("shape_kji"),
        "dtype": r.get("dtype"),
        "spacing_mm": r.get("spacing_mm"),
        "origin": r.get("origin"),
        "volume_name": r.get("volume_name"),
    }


def dump_final_segmentation(base_url: str, dest_dir: Path) -> dict:
    print("  -> dumping final segmentation (composite + per-segment NIfTIs)…")
    r = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=900,
                    retries=3, retry_sleep=8.0)
    if r.get("status") != "ok":
        raise RuntimeError(f"dump segmentation failed: {r}")
    seg_dir = dest_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    per_segment_records: list[dict] = []
    for ps in r.get("per_segment", []):
        b64 = ps.get("data_b64")
        if not b64:
            print(f"     SKIP segment {ps.get('sid')}: {ps.get('error')}")
            continue
        data = base64.b64decode(b64)
        fn = ps["filename"]
        (seg_dir / fn).write_bytes(data)
        per_segment_records.append({
            "sid": ps["sid"],
            "name": ps.get("name"),
            "filename": f"segments/{fn}",
            "size_bytes": ps["size_bytes"],
            "sha256": ps["sha256"],
            "color": ps.get("color"),
        })
    composite_record = None
    comp = r.get("composite") or {}
    if comp.get("data_b64"):
        data = base64.b64decode(comp["data_b64"])
        (dest_dir / "composite.nii.gz").write_bytes(data)
        composite_record = {
            "path": "composite.nii.gz",
            "size_bytes": comp["size_bytes"],
            "sha256": comp["sha256"],
        }
    print(f"     wrote {len(per_segment_records)} segments + composite "
          f"({composite_record['size_bytes'] if composite_record else 0:,} bytes)")
    return {
        "composite": composite_record,
        "per_segment": per_segment_records,
        "segmentation_node_name": r.get("segmentation_node_name"),
        "segment_count": r.get("segment_count"),
    }


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------

README_TEMPLATE = """# Paint session bundle: `{run_label}`

This is a self-contained reproduction artifact for an nnInteractive paint
session on a 3D medical volume, driven by `slicer_remote_bright_seed.py`.

## Layout

| File / dir | What |
|---|---|
| `manifest.json` | Single source of truth for the session. Lists every constituent run (by id, start time, args), the source volume hash, plugin + Slicer versions. |
| `volume.nii.gz` | The original input volume — bit-identical to what the model saw on the remote Slicer. SHA256 below. |
| `composite.nii.gz` | Final union segmentation (one multi-label NIfTI, one label per segment). |
| `segments/Segment_*.nii.gz` | Per-segment binary labelmaps. The `sid` and order match `manifest.json -> final_segmentation.per_segment`. |
| `clicks.jsonl` | Chronological list of every paint click. Each row is one JSON dict with `step, ijk, click_positive, made_new_segment, segment_id, intensity, voxels_before, voxels_after, delta, click_seconds, run_id, batch_index`. |
| `environment.json` | Per-run local + remote environment (git commit, Slicer version, plugin version, torch/cuda, model bytes hash). |
| `replay.sh` / `replay.py` | Reproduces this exact set of clicks against any remote Slicer that has a volume with the same `sha256_voxels`. |

## Source volume

- `sha256_voxels` = `{sha256_voxels}`
- shape (k, j, i) = `{shape_kji}`
- spacing (mm)    = `{spacing_mm}`
- dtype           = `{dtype}`

## How to reproduce

1. Start a remote 3D Slicer + SlicerNNInteractive instance (e.g. on
   Jetstream2 / MorphoCloud). Load a volume whose voxel SHA256 matches
   the value above.
2. Export the proxy URLs:
   ```bash
   export SLICER_WEBSERVER_URL=https://<your-slicer-proxy>/
   export NNI_REMOTE_URL=https://<your-nninteractive-proxy>/
   ```
3. Run the replay:
   ```bash
   bash ./replay.sh
   ```

The replay verifies the target volume's `sha256_voxels` before issuing any
clicks. If the hash matches, every click is re-issued in order; the final
segmentation is exported and its `composite.nii.gz` SHA256 is compared
against this bundle's. A "bit-identical" reproduction is expected when
the same nnInteractive model version is loaded; minor differences in
voxel counts indicate a non-determinism source (driver / CUDA version /
nnInteractive model update).

Generated by `export_session.py` on `{built_at}` (host `{host}`).
"""


def write_bundle(out_dir: Path, source_runs: list[dict],
                 clicks: list[dict],
                 volume_record: dict | None,
                 seg_record: dict | None,
                 args: argparse.Namespace) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    # clicks.jsonl
    (out_dir / "clicks.jsonl").write_text("\n".join(
        json.dumps(c, default=str) for c in clicks
    ) + ("\n" if clicks else ""))
    # environment.json (merged per-run)
    (out_dir / "environment.json").write_text(json.dumps({
        "runs": {
            r["manifest"].get("run_id", Path(r["dir"]).name): r["environment"]
            for r in source_runs
        }
    }, indent=2, default=str))
    # manifest.json
    manifest = {
        "schema_version": 1,
        "bundle_label": args.label or out_dir.name,
        "built_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "built_by_host": socket.gethostname(),
        "source_volume": volume_record,
        "final_segmentation": seg_record,
        "click_count": len(clicks),
        "source_runs": [
            {
                "dir": r["dir"],
                "run_id": r["manifest"].get("run_id"),
                "label": r["manifest"].get("label"),
                "started_utc": r["manifest"].get("started_utc"),
                "started_unix": r["manifest"].get("started_unix"),
                "args": r["manifest"].get("args"),
                "stop_reason": r.get("stop_reason"),
                "inputs": r.get("inputs"),
                "n_events": len(r.get("events", [])),
            }
            for r in source_runs
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    # README
    src_vol = volume_record or {}
    (out_dir / "README.md").write_text(README_TEMPLATE.format(
        run_label=args.label or out_dir.name,
        sha256_voxels=src_vol.get("sha256_voxels"),
        shape_kji=src_vol.get("shape_kji"),
        spacing_mm=src_vol.get("spacing_mm"),
        dtype=src_vol.get("dtype"),
        built_at=manifest["built_at_utc"],
        host=manifest["built_by_host"],
    ))
    # replay.py (verbatim copy of replay_session.py)
    replayer = SCRIPTS_DIR / "replay_session.py"
    if replayer.exists():
        shutil.copy2(replayer, out_dir / "replay.py")
        try:
            os.chmod(out_dir / "replay.py", 0o755)
        except Exception:
            pass
    # replay.sh
    sh = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        cd "$(dirname "$0")"
        : "${{SLICER_WEBSERVER_URL:?set SLICER_WEBSERVER_URL or source .env first}}"
        python3 ./replay.py \\
            --bundle . \\
            --target-volume "{args.target_volume_for_replay or src_vol.get('volume_name') or ''}" \\
            "$@"
    """)
    (out_dir / "replay.sh").write_text(sh)
    try:
        os.chmod(out_dir / "replay.sh", 0o755)
    except Exception:
        pass
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--runs", nargs="+", type=Path,
                     help="Explicit list of run directories to bundle.")
    src.add_argument("--auto-discover", action="store_true",
                     help="Scan --runs-root for all runs sharing the "
                          "currently active volume's sha256_voxels.")
    p.add_argument("--runs-root", type=Path, default=Path("runs"),
                   help="Root directory to search when --auto-discover "
                        "(default: ./runs)")
    p.add_argument("--out", type=Path, required=True,
                   help="Bundle output directory (created)")
    p.add_argument("--label", type=str, default=None,
                   help="Human-readable label embedded in the bundle "
                        "manifest + README")
    p.add_argument("--offline", action="store_true",
                   help="Do not contact the remote Slicer. Skip the "
                        "live volume + segmentation dump; reuse the "
                        "newest run's per-segment artifacts on disk.")
    p.add_argument("--inline", action="store_true",
                   help="Use legacy single-shot base64 /exec dumps "
                        "(often hit the ~60 s proxy idle timeout). "
                        "Default is chunked server-side save + 4 MB reads.")
    p.add_argument("--seg-batch-size", type=int, default=5,
                   help="Segments per server-side save batch when using "
                        "chunked export (default: 5).")
    p.add_argument("--inline", action="store_true",
                   help="Use legacy single-shot base64 dumps (often hit "
                        "the ~60 s Exosphere proxy idle timeout). "
                        "Default is chunked server-side save + 4 MB reads.")
    p.add_argument("--seg-batch-size", type=int, default=5,
                   help="Segments per server-side save batch when using "
                        "chunked export (default: 5).")
    p.add_argument("--target-volume-for-replay", type=str, default=None,
                   help="Default --target-volume passed to the generated "
                        "replay.sh. Defaults to the source volume name "
                        "(works when replaying on the same Slicer box).")
    p.add_argument("--match-sha256", type=str, default=None,
                   help="Override sha256_voxels filter for auto-discovery "
                        "(defaults to the currently active volume's hash).")
    args = p.parse_args(argv)

    base_url = None if args.offline else _read_url()

    # ---- determine the canonical source volume ----------------------
    target_sha = args.match_sha256
    if args.auto_discover and not args.offline and not target_sha:
        # Ask the live Slicer
        print("• Probing remote Slicer for active volume…")
        from run_telemetry import HASH_ACTIVE_VOLUME_SRC  # local import
        meta = post_python(base_url, HASH_ACTIVE_VOLUME_SRC, timeout=120)
        if meta.get("status") != "ok":
            sys.exit(f"could not hash active volume: {meta}")
        target_sha = meta["sha256_voxels"]
        print(f"  active = {meta['volume_name']!r}  "
              f"shape={meta['shape_kji']}  sha256_voxels={target_sha[:16]}…")

    # ---- resolve run set --------------------------------------------
    if args.runs:
        run_dirs = [d.resolve() for d in args.runs if d.exists()]
        if not run_dirs:
            sys.exit("none of --runs exist")
        # Filter by sha256 anyway, to refuse mixed bundles
        valid = []
        sha_seen: set[str] = set()
        for d in run_dirs:
            r = load_run(d)
            sha = r["inputs"].get("sha256_voxels")
            if sha:
                sha_seen.add(sha)
            valid.append(d)
        if len(sha_seen) > 1:
            sys.exit(f"refusing to bundle runs across multiple volumes: {sha_seen}")
        if not target_sha and sha_seen:
            target_sha = next(iter(sha_seen))
    else:
        run_dirs = discover_runs([args.runs_root], match_sha256=target_sha)
        if not run_dirs:
            sys.exit(f"no runs found in {args.runs_root} with sha256_voxels={target_sha}")
        print(f"• Discovered {len(run_dirs)} runs sharing the volume hash:")
        for d in run_dirs:
            print(f"    {d}")

    runs = [load_run(d) for d in run_dirs]

    # ---- merge clicks across runs (chronological) -------------------
    print("\n• Merging clicks across runs…")
    all_clicks: list[dict] = []
    for idx, r in enumerate(runs):
        sub = extract_clicks_from_events(r["events"])
        for c in sub:
            c["run_id"] = r["manifest"].get("run_id") or Path(r["dir"]).name
            c["run_dir"] = r["dir"]
            c["batch_index"] = idx
            c["global_step"] = len(all_clicks)
            all_clicks.append(c)
        print(f"    run #{idx:02d} {Path(r['dir']).name}  +{len(sub)} clicks")
    print(f"  TOTAL clicks = {len(all_clicks)}")

    # ---- live exports ------------------------------------------------
    volume_record = None
    seg_record = None
    if not args.offline:
        print("\n• Live exports from Slicer "
              f"({'inline base64' if args.inline else 'chunked'}):")
        args.out.mkdir(parents=True, exist_ok=True)
        try:
            if args.inline:
                volume_record = dump_active_volume(base_url, args.out)
            else:
                volume_record = chunked_dump_volume(base_url, args.out)
        except Exception as e:
            print(f"  ! volume dump failed: {e!r}")
        try:
            if args.inline:
                seg_record = dump_final_segmentation(base_url, args.out)
            else:
                seg_record = chunked_dump_segmentation(
                    base_url, args.out, batch_size=args.seg_batch_size,
                )
        except Exception as e:
            print(f"  ! segmentation dump failed: {e!r}")

    # If we got a live volume hash, double-check it matches the runs.
    if volume_record and target_sha and \
       volume_record.get("sha256_voxels") != target_sha:
        print("\n⚠ WARNING: the currently active volume's sha256_voxels "
              f"({volume_record.get('sha256_voxels')[:16]}…) does NOT "
              f"match the runs' input volume ({target_sha[:16]}…). "
              "The bundle will record the runs' hash as authoritative.")

    # If we ran offline, hydrate volume_record / seg_record from the
    # newest run's artifacts when available.
    if args.offline:
        if runs and runs[-1]["inputs"]:
            inp = runs[-1]["inputs"]
            volume_record = {
                "path": None,
                "size_bytes": None,
                "sha256_file": None,
                "sha256_voxels": inp.get("sha256_voxels"),
                "shape_kji": inp.get("shape_kji"),
                "dtype": inp.get("dtype"),
                "spacing_mm": inp.get("spacing_mm"),
                "origin": inp.get("origin"),
                "volume_name": inp.get("volume_name"),
            }
        # Try copying the newest run's artifacts/ into the bundle as a
        # fallback so the bundle is self-contained even without live
        # access. We pick the run with the LARGEST per-segment file count
        # (later runs sometimes fail their export when the proxy times
        # out, so "newest" isn't necessarily "most complete").
        candidate = None
        n_seg = -1
        for r in reversed(runs):
            ai = Path(r["dir"]) / "artifacts" / "index.json"
            if not ai.exists():
                continue
            try:
                idx = json.loads(ai.read_text())
            except Exception:
                continue
            seg_count = sum(
                1 for x in idx
                if x.get("kind") in ("per_segment_nifti", "per_segment_labelmap")
            )
            if seg_count > n_seg:
                n_seg = seg_count
                candidate = (Path(r["dir"]), idx)
        if candidate:
            run_dir, idx = candidate
            print(f"\n• Offline mode: copying segmentation artifacts from "
                  f"{run_dir.name} ({n_seg} per-segment files)")
            seg_dir = args.out / "segments"
            seg_dir.mkdir(parents=True, exist_ok=True)
            per_segment_records = []
            for x in idx:
                src = run_dir / x["path"]
                if not src.exists():
                    continue
                if x.get("kind") in ("per_segment_nifti", "per_segment_labelmap"):
                    dst = seg_dir / Path(x["path"]).name
                    shutil.copy2(src, dst)
                    per_segment_records.append({
                        "sid": (x.get("extra") or {}).get("sid"),
                        "name": (x.get("extra") or {}).get("name"),
                        "filename": f"segments/{dst.name}",
                        "size_bytes": x.get("size_bytes"),
                        "sha256": x.get("sha256"),
                    })
                elif x.get("kind") in ("composite_nifti", "composite_labelmap"):
                    dst = args.out / "composite.nii.gz"
                    shutil.copy2(src, dst)
                    composite_record_local = {
                        "path": "composite.nii.gz",
                        "size_bytes": x.get("size_bytes"),
                        "sha256": x.get("sha256"),
                    }
                    seg_record = (seg_record or {})
                    seg_record["composite"] = composite_record_local
            seg_record = (seg_record or {})
            seg_record["per_segment"] = per_segment_records

    # ---- write the bundle -------------------------------------------
    print("\n• Writing bundle…")
    manifest = write_bundle(args.out, runs, all_clicks,
                            volume_record, seg_record, args)

    # ---- summary ----------------------------------------------------
    print()
    print(f"DONE.  bundle -> {args.out}")
    print(f"   clicks         : {manifest['click_count']}")
    print(f"   runs           : {len(manifest['source_runs'])}")
    print(f"   source volume  : {(volume_record or {}).get('volume_name')!r}  "
          f"sha256={(volume_record or {}).get('sha256_voxels')}")
    print(f"   segments       : {len((seg_record or {}).get('per_segment', []))}")
    if seg_record and seg_record.get("composite"):
        print(f"   composite sha256 = {seg_record['composite']['sha256']}")
    print()
    print("Reproduce with:")
    print(f"   set -a && source .env && set +a")
    print(f"   bash {args.out / 'replay.sh'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
