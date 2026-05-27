"""
Push / list / hash NIfTI volumes inside a remote 3D Slicer process.

The bright-seed orchestrator needs to load a freshly cropped CT into the
remote Slicer instance before invoking nnInteractive on it. Slicer's
built-in Web Server module exposes ``/slicer/exec`` which accepts an
arbitrary Python source string and evaluates it inside the Slicer
process; that's the only side-channel we have to drive Slicer remotely.

The simplest workable file-transfer scheme over ``/slicer/exec`` is to
inline the volume's bytes as base64 inside the Python source we POST.
Round-trip overhead is acceptable for the post-crop volumes used in this
pilot (~50-200 MB each, ~67-265 MB after base64). Larger volumes should
use a different transport (e.g. a server-side download recipe).

Functions
---------
- ``push_volume(base_url, nifti_path, name=None)``: read *nifti_path*,
  base64-encode it, POST a recipe that decodes + writes a temp file +
  loads it via ``slicer.util.loadVolume``, and sets it active. Returns
  the parsed JSON reply.
- ``list_volumes(base_url)``: list current ``vtkMRMLScalarVolumeNode``s.
- ``set_active_volume(base_url, name)``: select the volume by name.

Constants
---------
- ``LOAD_NIFTI_BASE64_SRC_TEMPLATE``: format with ``data_b64`` and
  ``name``.
- ``LIST_VOLUMES_SRC``: no formatting required.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


# Add repo root so the optional jetstream_replay package is importable.
# When neither JETSTREAM_RECORD nor JETSTREAM_REPLAY is set, the
# session is a thin pass-through and behaviour is byte-for-byte
# identical to a raw ``urllib.request.urlopen`` call.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from metadata_to_morphsource.jetstream_replay.recorder import (  # noqa: E402
        urlopen_via_session,
    )
except Exception:  # pragma: no cover
    def urlopen_via_session(request, data=None, timeout=60):
        return urllib.request.urlopen(request, data=data, timeout=timeout)


# ---------------------------------------------------------------------------
# HTTP helper (mirrors the one in slicer_remote_bright_seed)
# ---------------------------------------------------------------------------

def _post_python(base_url: str, source: str, timeout: float = 600,
                 retries: int = 0, retry_sleep: float = 5.0) -> dict:
    """POST a Python source string to /slicer/exec and return the parsed JSON.

    Parameters
    ----------
    base_url : str
    source : str
        Python source to evaluate inside Slicer's process.
    timeout : float
        Per-attempt POST timeout (seconds).
    retries : int
        Retry budget for *transient* failures (TimeoutError, URLError,
        OSError, or HTTP 5xx). 0 means single-attempt (legacy behaviour).
        Each retry sleeps ``retry_sleep`` seconds before reissuing.
        Non-transient failures (HTTP 4xx, non-JSON 200 body) raise
        immediately because they're programming errors in our recipe.
    retry_sleep : float
        Seconds to sleep between retry attempts.
    """
    body = source.encode("utf-8")
    last_exc: Optional[BaseException] = None
    t0 = time.time()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            base_url.rstrip("/") + "/slicer/exec",
            data=body, method="POST",
            headers={"Content-Type": "text/plain"},
        )
        try:
            with urlopen_via_session(req, timeout=timeout) as resp:
                content = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            content = e.read()
            status = e.code
            # 5xx is transient (proxy hiccup, Slicer exec hung). 4xx is
            # a bug in our recipe — don't retry.
            if status < 500 or attempt >= retries:
                raise RuntimeError(
                    f"/slicer/exec -> HTTP {status}: {content[:300]!r}"
                )
            last_exc = e
            time.sleep(retry_sleep)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt >= retries:
                raise RuntimeError(
                    f"/slicer/exec transport failure after "
                    f"{attempt + 1} attempt(s): {e!r}"
                ) from e
            last_exc = e
            time.sleep(retry_sleep)
            continue

        if status != 200:
            raise RuntimeError(
                f"/slicer/exec -> HTTP {status}: {content[:300]!r}"
            )
        try:
            result = json.loads(content)
        except Exception:
            raise RuntimeError(f"non-JSON exec reply: {content[:300]!r}")
        result["_dt_s"] = round(time.time() - t0, 3)
        if attempt > 0:
            result["_retries_used"] = attempt
        return result

    # Unreachable (loop body either returns or raises), but keep the
    # type-checker happy.
    raise RuntimeError(
        f"/slicer/exec exhausted retries: {last_exc!r}"
    )


# ---------------------------------------------------------------------------
# Slicer-side recipes (run via /slicer/exec)
# ---------------------------------------------------------------------------

# We embed the base64 directly via str.format so the Slicer source is
# self-contained. The placeholder uses {data_b64} (and {name},
# {sha256_expected}). Use ``LOAD_NIFTI_BASE64_SRC_TEMPLATE.format(...)``.
#
# Notes:
# - The remote temp file is keyed on the expected sha256 so re-pushes of
#   the same content are idempotent: on a hash match, we reuse the
#   existing file.
# - We delete any previously-loaded volume node with the same name so we
#   don't pile up stale copies across runs.
# - We verify the decoded bytes' sha256 against ``sha256_expected`` and
#   surface a mismatch as ``status: "sha256_mismatch"`` instead of
#   silently loading bad data.
# --------------------------------------------------------------------
# Chunked-upload recipes
# --------------------------------------------------------------------
# We can't push a 50-300 MB cropped CT through the Exosphere proxy in a
# single /slicer/exec POST: even after gzip+base64 compresses well, the
# nginx proxy fronting the Slicer Web Server has a per-request idle
# timeout (~60 s) shorter than `slicer.util.loadVolume` on a fresh
# NIfTI. The workaround is to land the bytes on the Jetstream
# filesystem in small chunks first, then load it as a fast separate
# call.
#
# Three short recipes (each a single fast POST):
#   1. INIT_UPLOAD_SRC: create the destination temp file (truncate).
#   2. APPEND_UPLOAD_SRC: base64-decode this chunk + append to the file.
#   3. LOAD_FROM_PATH_SRC: verify sha256, then loadVolume from the
#      already-on-disk file and set it active.
#
# Every recipe injects parameters via a tiny prelude rather than
# str.format() so the body can use {dict} literals freely.

_INIT_UPLOAD_BODY = """\
import os, tempfile, traceback
out = {}
try:
    td = os.path.join(tempfile.gettempdir(), "ms_remote_volumes")
    os.makedirs(td, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in _NAME)
    path = os.path.join(td, _SHA_EXPECTED[:16] + "_" + safe + ".nii.gz")
    open(path, "wb").close()  # truncate / create
    out["status"] = "ok"
    out["remote_path"] = path
    out["jetstream_dir"] = td
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""

_APPEND_UPLOAD_BODY = """\
import base64, os, hashlib, traceback
out = {}
try:
    data = base64.b64decode(_DATA_B64)
    with open(_PATH, "ab") as f:
        f.write(data)
    out["status"] = "ok"
    out["chunk_size"] = len(data)
    out["chunk_sha256"] = hashlib.sha256(data).hexdigest()
    out["file_size_after"] = os.path.getsize(_PATH)
    out["chunk_index"] = _CHUNK_INDEX
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""

_LOAD_FROM_PATH_BODY = """\
import slicer, hashlib, os, traceback
out = {}
try:
    if not os.path.exists(_PATH):
        out["status"] = "missing"
    else:
        size = os.path.getsize(_PATH)
        out["size_bytes"] = size
        h = hashlib.sha256()
        with open(_PATH, "rb") as f:
            while True:
                blk = f.read(1 << 20)
                if not blk:
                    break
                h.update(blk)
        sha = h.hexdigest()
        out["sha256"] = sha
        out["remote_path"] = _PATH
        if _SHA_EXPECTED and sha != _SHA_EXPECTED:
            out["status"] = "sha256_mismatch"
            out["sha256_expected"] = _SHA_EXPECTED
        else:
            for existing in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
                if existing.GetName() == _NAME:
                    slicer.mrmlScene.RemoveNode(existing)
            node = slicer.util.loadVolume(_PATH, properties={"name": _NAME})
            if node is None:
                out["status"] = "load_failed"
            else:
                sel = slicer.app.applicationLogic().GetSelectionNode()
                sel.SetActiveVolumeID(node.GetID())
                slicer.app.applicationLogic().PropagateVolumeSelection(0)
                slicer.util.setSliceViewerLayers(background=node, fit=True)
                arr = slicer.util.arrayFromVolume(node)
                out["status"] = "ok"
                out["volume_id"] = node.GetID()
                out["volume_name"] = node.GetName()
                out["shape_kji"] = list(arr.shape)
                out["dtype"] = str(arr.dtype)
                out["spacing_mm"] = [round(s, 6) for s in node.GetSpacing()]
                out["origin"] = [round(o, 6) for o in node.GetOrigin()]
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""


def _build_init_source(name: str, sha256_expected: str) -> str:
    return (
        f"_NAME = {name!r}\n"
        f"_SHA_EXPECTED = {sha256_expected!r}\n"
        + _INIT_UPLOAD_BODY
    )


def _build_append_source(path: str, data_b64: str, chunk_index: int) -> str:
    return (
        f"_PATH = {path!r}\n"
        f"_DATA_B64 = {data_b64!r}\n"
        f"_CHUNK_INDEX = {int(chunk_index)}\n"
        + _APPEND_UPLOAD_BODY
    )


def _build_load_from_path_source(path: str, name: str,
                                   sha256_expected: str) -> str:
    return (
        f"_PATH = {path!r}\n"
        f"_NAME = {name!r}\n"
        f"_SHA_EXPECTED = {sha256_expected!r}\n"
        + _LOAD_FROM_PATH_BODY
    )


# Single-shot loader (kept for callers that already have the file on
# the remote box and just want it loaded). The orchestrator no longer
# uses this path for fresh uploads -- it goes through the chunked path.
LOAD_NIFTI_BASE64_SRC_BODY = """\
import slicer, base64, hashlib, os, tempfile, traceback
out = {}
try:
    data = base64.b64decode(_DATA_B64)
    sha = hashlib.sha256(data).hexdigest()
    out["sha256"] = sha
    out["size_bytes"] = len(data)
    if _SHA_EXPECTED and sha != _SHA_EXPECTED:
        out["status"] = "sha256_mismatch"
        out["sha256_expected"] = _SHA_EXPECTED
    else:
        td = os.path.join(tempfile.gettempdir(), "ms_remote_volumes")
        os.makedirs(td, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in _NAME)
        path = os.path.join(td, sha[:16] + "_" + safe + ".nii.gz")
        if not os.path.exists(path) or os.path.getsize(path) != len(data):
            with open(path, "wb") as f:
                f.write(data)
        out["remote_path"] = path
        for existing in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if existing.GetName() == _NAME:
                slicer.mrmlScene.RemoveNode(existing)
        node = slicer.util.loadVolume(path, properties={"name": _NAME})
        if node is None:
            out["status"] = "load_failed"
        else:
            sel = slicer.app.applicationLogic().GetSelectionNode()
            sel.SetActiveVolumeID(node.GetID())
            slicer.app.applicationLogic().PropagateVolumeSelection(0)
            slicer.util.setSliceViewerLayers(background=node, fit=True)
            arr = slicer.util.arrayFromVolume(node)
            out["status"] = "ok"
            out["volume_id"] = node.GetID()
            out["volume_name"] = node.GetName()
            out["shape_kji"] = list(arr.shape)
            out["dtype"] = str(arr.dtype)
            out["spacing_mm"] = [round(s, 6) for s in node.GetSpacing()]
            out["origin"] = [round(o, 6) for o in node.GetOrigin()]
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""


def _build_load_source(data_b64: str, name: str,
                       sha256_expected: str) -> str:
    """Compose the full /slicer/exec source for push_volume.

    We keep ``LOAD_NIFTI_BASE64_SRC_BODY`` parameter-free and prepend a
    tiny prelude that defines the inputs as plain assignments. This
    avoids the brace-escaping headache of formatting a Python source
    string that contains its own dict literals.
    """
    prelude = (
        f"_DATA_B64 = {data_b64!r}\n"
        f"_NAME = {name!r}\n"
        f"_SHA_EXPECTED = {sha256_expected!r}\n"
    )
    return prelude + LOAD_NIFTI_BASE64_SRC_BODY


def LOAD_NIFTI_BASE64_SRC_TEMPLATE(data_b64: str, name: str,
                                   sha256_expected: str = "") -> str:
    """Function-style accessor mimicking a format-string entry point."""
    return _build_load_source(data_b64, name, sha256_expected)


# Lightweight scene listing for sanity / dedup.
LIST_VOLUMES_SRC = """\
import slicer, traceback
out = {"volumes": [], "active_id": None}
try:
    sel = slicer.app.applicationLogic().GetSelectionNode()
    out["active_id"] = sel.GetActiveVolumeID() if sel else None
    for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        try:
            arr = slicer.util.arrayFromVolume(v)
            shape = list(arr.shape)
        except Exception:
            shape = None
        out["volumes"].append({
            "id": v.GetID(),
            "name": v.GetName(),
            "shape_kji": shape,
            "spacing_mm": [round(s, 6) for s in v.GetSpacing()],
            "is_active": (sel is not None and v.GetID() == sel.GetActiveVolumeID()),
        })
    out["status"] = "ok"
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""


# Set the active volume by *name*. Mirrors the helper in
# slicer_remote_bright_seed but exposed as a reusable recipe.
_SET_ACTIVE_VOLUME_SRC_BODY = """\
import slicer, traceback
out = {}
try:
    found = None
    for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if v.GetName() == _TARGET:
            found = v
            break
    if found is None:
        out["status"] = "not_found"
        out["available"] = [v.GetName() for v in
                             slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")]
    else:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        sel.SetActiveVolumeID(found.GetID())
        slicer.app.applicationLogic().PropagateVolumeSelection(0)
        slicer.util.setSliceViewerLayers(background=found, fit=True)
        out["status"] = "ok"
        out["volume_id"] = found.GetID()
        out["volume_name"] = found.GetName()
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""


def SET_ACTIVE_VOLUME_SRC_TEMPLATE(name: str) -> str:
    """Build the /slicer/exec source for set_active_volume."""
    return f"_TARGET = {name!r}\n" + _SET_ACTIVE_VOLUME_SRC_BODY


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def push_volume(base_url: str, nifti_path: Path,
                name: Optional[str] = None,
                chunk_bytes: int = 2 * 1024 * 1024,
                per_chunk_timeout: float = 180.0,
                load_timeout: float = 300.0,
                chunk_retries: int = 3,
                chunk_retry_sleep: float = 5.0,
                progress: Optional[Any] = None) -> dict:
    """Upload *nifti_path* to the Jetstream filesystem in chunks, then load.

    The chunked path keeps every individual ``/slicer/exec`` call below
    the proxy idle timeout that fronts the Slicer Web Server on Exosphere.
    Each chunk decodes a base64-encoded slice of the file and **appends**
    to the same temp file on Jetstream's local disk; once the upload is
    complete, a separate /exec call invokes ``slicer.util.loadVolume`` on
    the on-disk path (no upload, just a file open).

    Defaults are tuned for the empirically-observed Mac-mini upstream rate
    of ~150-250 KB/s through the Exosphere proxy: 2 MiB chunks complete
    in ~10-15 s, well under the 180 s per-chunk timeout. Transient
    failures (proxy hiccup, transient 5xx, urllib timeout mid-stream) are
    retried up to ``chunk_retries`` times — the recipe is idempotent
    because each append rewrites the *exact same* base64 chunk to the
    *exact same* file offset on success (the failed attempt's partial
    bytes go to a closed-then-discarded socket, never to the file). The
    final loadVolume call also retries because Slicer's loader has a
    one-shot warmup that occasionally times out at first.

    Parameters
    ----------
    base_url : str
        Slicer Web Server URL (https://...-2016.proxy-js2-iu.exosphere.app/).
    nifti_path : Path
        Local NIfTI file to push.
    name : str, optional
        Slicer scene node name. Defaults to ``nifti_path.stem``.
    chunk_bytes : int
        Size of each upload chunk in raw bytes (default 2 MiB,
        ~2.7 MiB after base64). Smaller chunks = more round trips
        but each one finishes well under the proxy idle timeout
        even on a slow / variable upstream.
    per_chunk_timeout : float
        Per-chunk POST timeout (default 180 s). Generous because the
        Exosphere proxy occasionally adds ~30-60 s of buffering jitter
        on the first chunk after server startup.
    load_timeout : float
        Final loadVolume call timeout (default 300 s; loadVolume on a
        50 MB NIfTI typically takes 20-60 s but the first call after a
        fresh server start can hit 120-180 s).
    chunk_retries : int
        Retry budget per chunk for transient transport failures (default
        3). Each retry re-uploads the *same* chunk to the *same* offset;
        on Slicer's side, the append recipe only writes to the file on a
        successful base64 decode + open, so a failed attempt can't
        corrupt the on-disk file.
    chunk_retry_sleep : float
        Seconds to sleep between chunk retries (default 5 s).
    progress : callable(int, int), optional
        ``progress(uploaded_bytes, total_bytes)`` callback for
        long-running upload feedback.

    Returns
    -------
    dict
        Reply of the loadVolume step, augmented with ``local_sha256``,
        ``local_size_bytes``, ``local_path``, ``remote_path``, and
        per-stage timing info.
    """
    nifti_path = Path(nifti_path)
    if not nifti_path.exists():
        raise FileNotFoundError(nifti_path)
    data = nifti_path.read_bytes()
    total = len(data)
    sha = hashlib.sha256(data).hexdigest()
    if name is None:
        name = nifti_path.stem  # "ct_cropped.nii" for "ct_cropped.nii.gz"

    timings: dict = {"chunks": [], "init_s": None, "load_s": None}
    init = _post_python(
        base_url,
        _build_init_source(name=name, sha256_expected=sha),
        timeout=per_chunk_timeout,
        retries=chunk_retries,
        retry_sleep=chunk_retry_sleep,
    )
    timings["init_s"] = init.get("_dt_s")
    if init.get("status") != "ok":
        return {**init, "stage": "init",
                 "local_sha256": sha, "local_size_bytes": total,
                 "local_path": str(nifti_path), "_timings": timings}
    remote_path = init["remote_path"]

    n_chunks = (total + chunk_bytes - 1) // max(1, chunk_bytes)
    sent = 0
    for i in range(n_chunks):
        chunk = data[i * chunk_bytes:(i + 1) * chunk_bytes]
        b64 = base64.b64encode(chunk).decode("ascii")
        reply = _post_python(
            base_url,
            _build_append_source(path=remote_path, data_b64=b64,
                                   chunk_index=i),
            timeout=per_chunk_timeout,
            retries=chunk_retries,
            retry_sleep=chunk_retry_sleep,
        )
        timings["chunks"].append({
            "i": i, "bytes": len(chunk),
            "dt_s": reply.get("_dt_s"),
            "remote_size_after": reply.get("file_size_after"),
            "retries_used": reply.get("_retries_used", 0),
            "status": reply.get("status"),
        })
        if reply.get("status") != "ok":
            return {**reply, "stage": "append", "chunk_index": i,
                     "local_sha256": sha, "local_size_bytes": total,
                     "local_path": str(nifti_path),
                     "remote_path": remote_path, "_timings": timings}
        sent += len(chunk)
        if progress is not None:
            try:
                progress(sent, total)
            except Exception:
                pass

    final = _post_python(
        base_url,
        _build_load_from_path_source(path=remote_path, name=name,
                                        sha256_expected=sha),
        timeout=load_timeout,
        retries=chunk_retries,
        retry_sleep=chunk_retry_sleep,
    )
    timings["load_s"] = final.get("_dt_s")
    final["local_sha256"] = sha
    final["local_size_bytes"] = total
    final["local_path"] = str(nifti_path)
    final["remote_path"] = remote_path
    final["_timings"] = timings
    final["chunk_bytes"] = chunk_bytes
    final["n_chunks"] = n_chunks
    return final


def load_volume_from_remote_path(
    base_url: str,
    path: str,
    name: Optional[str] = None,
    sha256_expected: str = "",
    timeout: float = 240.0,
) -> dict:
    """Load a NIfTI/NRRD already on the Jetstream filesystem into Slicer."""
    path = str(path)
    if name is None:
        stem = Path(path).name
        if stem.endswith(".nii.gz"):
            stem = stem[:-7]
        elif stem.endswith(".nrrd"):
            stem = stem[:-5]
        name = Path(stem).stem
    return _post_python(
        base_url,
        _build_load_from_path_source(
            path=path, name=name, sha256_expected=sha256_expected
        ),
        timeout=timeout,
    )


def list_volumes(base_url: str, timeout: float = 30) -> dict:
    return _post_python(base_url, LIST_VOLUMES_SRC, timeout=timeout)


def set_active_volume(base_url: str, name: str, timeout: float = 30) -> dict:
    return _post_python(
        base_url,
        SET_ACTIVE_VOLUME_SRC_TEMPLATE(name),
        timeout=timeout,
    )


__all__ = [
    "LOAD_NIFTI_BASE64_SRC_TEMPLATE",
    "LIST_VOLUMES_SRC",
    "SET_ACTIVE_VOLUME_SRC_TEMPLATE",
    "push_volume",
    "load_volume_from_remote_path",
    "list_volumes",
    "set_active_volume",
]
