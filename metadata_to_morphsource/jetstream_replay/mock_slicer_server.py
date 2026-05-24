"""Minimal in-process mock of Slicer's ``/slicer/exec`` Web Server API."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Tuple


class _MockState:
    shape_kji = (48, 48, 48)
    spacing_mm = (1.0, 1.0, 1.0)
    volume_name = "mock_volume"
    volume_id = "vtkMRMLScalarVolumeNodeMock1"
    step = 0
    voxels = 0
    n_candidates = 5000
    n_segments = 0

    @classmethod
    def reset(cls) -> None:
        cls.step = 0
        cls.voxels = 0
        cls.n_candidates = 5000
        cls.n_segments = 0


def _tiny_labelmap_b64() -> str:
    """Return base64 of a minimal valid NIfTI labelmap (48³ uint8)."""
    if not hasattr(_tiny_labelmap_b64, "_cache"):
        import tempfile
        import numpy as np
        import SimpleITK as sitk

        arr = np.zeros((48, 48, 48), dtype=np.uint8)
        arr[20:28, 20:28, 20:28] = 1
        with tempfile.NamedTemporaryFile(suffix=".nii.gz", delete=False) as tf:
            path = tf.name
        try:
            sitk.WriteImage(sitk.GetImageFromArray(arr), path)
            _tiny_labelmap_b64._cache = base64.b64encode(  # type: ignore[attr-defined]
                Path(path).read_bytes()
            ).decode("ascii")
        finally:
            Path(path).unlink(missing_ok=True)
    return _tiny_labelmap_b64._cache  # type: ignore[attr-defined]


def _respond(body: str) -> dict:
    text = body or ""
    s = _MockState

    if "slicer_version" in text or "nninteractive_version" in text:
        return {
            "status": "ok",
            "slicer_version": "mock-5.0",
            "torch_version": "2.0.0",
            "torch_cuda_available": False,
            "torch_mps_available": False,
            "nninteractive_version": "mock",
        }

    if "SetActiveVolumeID" in text or "SetActiveVolume" in text:
        return {
            "status": "ok",
            "volume_id": s.volume_id,
            "name": s.volume_name,
            "dimensions_ijk": list(s.shape_kji),
            "spacing_mm": list(s.spacing_mm),
        }

    if "arrayFromVolume" in text and "sha256_voxels" in text:
        raw = b"mock_voxels"
        return {
            "status": "ok",
            "volume_id": s.volume_id,
            "volume_name": s.volume_name,
            "shape_kji": list(s.shape_kji),
            "dtype": "int16",
            "nbytes": len(raw),
            "spacing_mm": list(s.spacing_mm),
            "origin": [0.0, 0.0, 0.0],
            "scalar_min": 0.0,
            "scalar_max": 255.0,
            "scalar_mean": 100.0,
            "sha256_voxels": hashlib.sha256(raw).hexdigest(),
        }

    if "sha256" in text and "GetArrayFromImage" in text:
        raw = b"mock_voxels"
        return {
            "status": "ok",
            "sha256_voxels": hashlib.sha256(raw).hexdigest(),
            "shape_kji": list(s.shape_kji),
            "dtype": "int16",
            "spacing_mm": list(s.spacing_mm),
        }

    if "ExportSegmentsToLabelmapNode" in text or "bs_export_" in text:
        segments = []
        for i in range(1, max(1, s.n_segments) + 1):
            fname = f"Segment_{i}.nii.gz"
            b64 = _tiny_labelmap_b64()
            segments.append({
                "sid": f"Segment_{i}",
                "name": f"Segment_{i}",
                "filename": fname,
                "data_b64": b64,
                "sha256": hashlib.sha256(base64.b64decode(b64)).hexdigest(),
            })
        comp_b64 = _tiny_labelmap_b64()
        return {
            "status": "ok",
            "per_segment": segments,
            "composite": {
                "filename": "composite.nii.gz",
                "data_b64": comp_b64,
                "sha256": hashlib.sha256(base64.b64decode(comp_b64)).hexdigest(),
            },
        }

    if "RemoveNode" in text or "ClearDefaultStorageNode" in text:
        return {"status": "ok", "cleared_nodes": 0}

    if "SetSegmentationVisibility" in text or "PropagateVolumeSelection" in text:
        return {"status": "ok", "nodes": []}

    if "percentile" in text and "candidates" in text.lower():
        return {
            "status": "ok",
            "volume_name": s.volume_name,
            "shape_kji": list(s.shape_kji),
            "threshold": 100.0,
            "scalar_type": "int16",
            "n_candidates": s.n_candidates,
            "intensity_min": 50.0,
            "intensity_max": 200.0,
        }

    if "picked_ijk" in text or ("click_positive" in text and "nnInteractive" in text):
        if s.n_candidates <= 0:
            return {"status": "no_more_candidates", "voxels": s.voxels, "candidates_left": 0}
        s.step += 1
        s.n_segments += 1
        before = s.voxels
        delta = 800 + s.step * 50
        s.voxels = before + delta
        s.n_candidates = max(0, s.n_candidates - 100)
        return {
            "status": "ok",
            "picked_ijk": [24, 24, 24],
            "intensity": 150.0,
            "voxels_before": before,
            "voxels_after": s.voxels,
            "delta": delta,
            "skipped_inside": 0,
            "candidates_left": s.n_candidates,
            "click_seconds": 0.05,
            "segment_id": f"Segment_{s.n_segments}",
            "segment_voxels": delta,
            "n_segments_after": s.n_segments,
            "made_new_segment": True,
        }

    if "ms_remote_volumes" in text and ("open(path" in text or "truncate" in text):
        path = "/tmp/ms_remote_volumes/mock_upload.nii.gz"
        return {
            "status": "ok",
            "remote_path": path,
            "jetstream_dir": "/tmp/ms_remote_volumes",
        }

    if "_DATA_B64" in text or "_CHUNK_INDEX" in text:
        return {
            "status": "ok",
            "chunk_size": 128,
            "chunk_sha256": hashlib.sha256(b"mock").hexdigest(),
            "file_size_after": 79382,
            "chunk_index": 0,
        }

    if "loadVolume" in text and "hashlib" in text:
        return {
            "status": "ok",
            "remote_path": "/tmp/ms_remote_volumes/mock_upload.nii.gz",
            "sha256": hashlib.sha256(b"mock").hexdigest(),
            "size_bytes": 79382,
            "volume_id": s.volume_id,
            "volume_name": s.volume_name,
            "shape_kji": list(s.shape_kji),
            "dtype": "int16",
            "spacing_mm": list(s.spacing_mm),
            "origin": [0.0, 0.0, 0.0],
        }

    if "loadVolume" in text:
        return {
            "status": "ok",
            "id": s.volume_id,
            "name": s.volume_name,
            "shape": list(s.shape_kji),
        }

    if "GetNodesByClass" in text and "ScalarVolume" in text:
        return {"status": "ok", "volumes": [{"id": s.volume_id, "name": s.volume_name}]}

    if "grab" in text.lower() and "png" in text.lower():
        return {"status": "ok", "views": {}}

    return {"status": "ok"}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if "screenshot" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                )
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        result = _respond(body)
        payload = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve_in_background(host: str = "127.0.0.1", port: int = 0) -> Tuple[str, HTTPServer, threading.Thread]:
    _MockState.reset()
    httpd = HTTPServer((host, port), _Handler)
    host, port = httpd.server_address
    base = f"http://{host}:{port}/"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return base, httpd, thread
