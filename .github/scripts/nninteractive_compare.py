"""
End-to-end test of the nnInteractive paint loop against a MorphoSource
ground-truth segmentation.

Pipeline
--------
1. Resolve a (CT, GT mesh) pair — either from explicit ``--ct-media-id`` /
   ``--gt-media-id`` arguments, or auto-discovered with
   :mod:`find_segmentation_pairs`.
2. Download both via :mod:`morphosource_api_download` (only "open" media
   succeed; restricted media fail fast).
3. Identify the CT volume file (NIfTI/NRRD/DICOM) and the GT mesh file
   (PLY/STL/OBJ) inside the downloaded archives.
4. Voxelize the GT mesh onto the CT's voxel grid via the headless
   ``voxelize_mesh_in_slicer.py`` (3D Slicer subprocess).
5. Run the LLM-driven nnInteractive paint loop on the CT volume in the
   nnInteractive venv (``$NNINTERACTIVE_HOME/bin/python``).
6. Compare the prediction labelmap to the voxelized GT labelmap with
   :mod:`segmentation_metrics` (Dice/IoU/Hausdorff/volume agreement),
   render an overlay panel, and write a Markdown report.

Outputs land in ``$OUTPUT_DIR/<ct_id>__vs__<gt_id>/`` and include:

    download/                     # raw MorphoSource downloads
    gt_voxelized.nii.gz           # GT mesh rasterized onto the CT grid
    nninteractive/                # paint-loop labelmap, screenshots, report
    metrics.json                  # full metrics payload
    overlay.png                   # 3×3 panel: volume / GT / prediction
    report.md                     # human-readable summary

Usage::

    python nninteractive_compare.py \\
        --ct-media-id 000656244 \\
        --gt-media-id 000656245 \\
        --goal "Segment the cranial bone" \\
        --output-dir /tmp/nni_compare

    # Or auto-discover the first viable pair:
    python nninteractive_compare.py --auto-discover \\
        --query "primate skull mesh" --goal "Segment the cranial bone"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _helpers import load_dotenv, AUTORESEARCHCLAW_HOME, SLICER_BIN  # noqa: E402

log = logging.getLogger("nni_compare")
load_dotenv()

NNI_HOME = Path(os.environ.get(
    "NNINTERACTIVE_HOME", str(AUTORESEARCHCLAW_HOME / "nninteractive")
))
NNI_PYTHON = NNI_HOME / "bin" / "python"

VOLUME_EXTS = {".nii", ".nii.gz", ".nrrd", ".nhdr", ".mha", ".mhd"}
MESH_EXTS = {".ply", ".stl", ".obj", ".off", ".gltf", ".glb"}
TIFF_EXTS = {".tif", ".tiff"}
MIN_TIFF_STACK_SIZE = 10  # treat a dir with >= N tiffs as a CT z-stack


@dataclass
class FilePick:
    path: Path
    size: int

    @property
    def display(self) -> str:
        return f"{self.path.name} ({self.size:,} bytes)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk_files(root: Path, extensions: set[str]) -> list[FilePick]:
    matches: list[FilePick] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Multi-extension support (".nii.gz")
        for ext in extensions:
            if p.name.lower().endswith(ext):
                matches.append(FilePick(path=p, size=p.stat().st_size))
                break
    matches.sort(key=lambda fp: fp.size, reverse=True)
    return matches


def _find_ct_volume(directory: Path) -> Optional[FilePick]:
    """Pick a CT input from a downloaded MorphoSource bundle.

    Recognized formats, in priority order:
        1. Pre-converted volumes: NIfTI / NRRD / MHA / NHDR.
        2. DICOM series directory (any subdir with .dcm files).
        3. TIFF z-stack directory (any subdir with >= 10 sequential .tif files).
           Common for paleontology micro-CT exports.
    """
    matches = _walk_files(directory, VOLUME_EXTS)
    if matches:
        return matches[0]

    best_dicom: Optional[FilePick] = None
    best_tiff: Optional[FilePick] = None

    for sub in directory.rglob("*"):
        if not sub.is_dir():
            continue
        try:
            entries = list(sub.iterdir())
        except (OSError, PermissionError):
            continue
        dcm_files = [e for e in entries if e.is_file() and (
            e.suffix.lower() == ".dcm" or e.name.upper() == "DICOMDIR"
        )]
        if dcm_files:
            total = sum(e.stat().st_size for e in dcm_files)
            if best_dicom is None or total > best_dicom.size:
                best_dicom = FilePick(path=sub, size=total)
            continue

        tif_files = [e for e in entries
                     if e.is_file() and e.suffix.lower() in TIFF_EXTS]
        if len(tif_files) >= MIN_TIFF_STACK_SIZE:
            total = sum(e.stat().st_size for e in tif_files)
            if best_tiff is None or total > best_tiff.size:
                best_tiff = FilePick(path=sub, size=total)

    if best_dicom is not None:
        return best_dicom
    return best_tiff


def _ct_input_kind(path: Path) -> str:
    """Return 'volume' (file), 'dicom', or 'tiff' for a CT input path."""
    if path.is_file():
        return "volume"
    try:
        entries = list(path.iterdir())
    except (OSError, PermissionError):
        return "volume"
    if any(e.is_file() and (e.suffix.lower() == ".dcm" or
                            e.name.upper() == "DICOMDIR")
           for e in entries):
        return "dicom"
    if sum(1 for e in entries
           if e.is_file() and e.suffix.lower() in TIFF_EXTS) \
            >= MIN_TIFF_STACK_SIZE:
        return "tiff"
    return "volume"


def _find_mesh(directory: Path) -> Optional[FilePick]:
    matches = _walk_files(directory, MESH_EXTS)
    return matches[0] if matches else None


def _download(media_id: str, dest: Path, *, max_retries: int = 3) -> dict:
    """Fetch a MorphoSource media bundle, skipping the network if already
    cached. A cache hit requires both the original .zip and at least one
    sibling extracted directory (the marker that ``extract_archives`` was
    able to unpack the bundle).

    Transient HTTP failures (``IncompleteRead``, ``ChunkedEncodingError``,
    timeouts, etc.) are common on MorphoSource for large CTs - we retry
    with exponential backoff and clean up partial downloads between
    attempts so the next try starts fresh.
    """
    dest.mkdir(parents=True, exist_ok=True)
    cached_zips = list(dest.glob("morphosource_media-id-*.zip"))
    cached_extracted = [p for p in cached_zips
                        if (p.parent / p.stem).is_dir()
                        and any((p.parent / p.stem).iterdir())]
    if cached_extracted:
        log.info("Cache hit for media %s — using existing %s",
                 media_id, dest)
        return {
            "success": True,
            "media_id": media_id,
            "visibility": "open",
            "downloaded_file": str(cached_extracted[0]),
            "file_size": cached_extracted[0].stat().st_size,
            "download_dir": str(dest),
            "from_cache": True,
        }

    from morphosource_api_download import download_media

    last_err: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        log.info("Downloading media %s -> %s (attempt %d/%d)",
                 media_id, dest, attempt, max_retries)
        result = download_media(media_id, str(dest))
        if result.get("success"):
            return result

        last_err = result.get("error") or "unknown download error"
        # Heuristically classify the failure. Retry on network-ish failures
        # only; ineligible media (auth required, 404) should fail fast.
        transient = any(needle in last_err.lower() for needle in (
            "incompleteread", "connection broken", "timed out", "timeout",
            "remotedisconnected", "chunkedencodingerror", "max retries",
            "temporary failure", "connection reset", "broken pipe",
            "read operation timed out", "503", "502", "504",
        ))
        if not transient:
            log.warning("Download failed non-transiently for %s: %s",
                        media_id, last_err)
            return result

        if attempt < max_retries:
            # Clean up partial zips so the next attempt starts fresh.
            for p in dest.glob("morphosource_media-id-*.zip*"):
                try:
                    p.unlink()
                except OSError:
                    pass
            backoff_s = 5 * (2 ** (attempt - 1))  # 5s, 10s, 20s
            log.warning("Transient download error for %s: %s. "
                        "Retrying in %d s (%d/%d).",
                        media_id, last_err, backoff_s,
                        attempt + 1, max_retries)
            time.sleep(backoff_s)

    log.error("Download for %s gave up after %d attempts. Last error: %s",
              media_id, max_retries, last_err)
    return {
        "success": False,
        "media_id": media_id,
        "error": f"Download error after {max_retries} attempts: {last_err}",
    }


def _tiff_stack_to_nifti(tiff_dir: Path, output: Path,
                         media_id: str = "",
                         center_origin: bool = True) -> dict:
    """Convert a TIFF z-stack directory into a single .nii.gz.

    By default uses ``--center-origin`` since MorphoSource TIFF stacks are
    typically generated by tools that center the volume on (0,0,0). If a
    specimen's GT mesh was exported in a different frame, set
    ``center_origin=False`` and pass an explicit origin.
    """
    if not NNI_PYTHON.exists():
        return {"error": f"nnInteractive venv missing at {NNI_PYTHON}"}
    cmd = [
        str(NNI_PYTHON),
        str(SCRIPT_DIR / "tiff_stack_to_nifti.py"),
        "--input-dir", str(tiff_dir),
        "--output", str(output),
    ]
    if center_origin:
        cmd.append("--center-origin")
    if media_id:
        cmd += ["--media-id", media_id]
    log.info("Converting TIFF stack to NIfTI: %s", tiff_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"error": "TIFF->NIfTI conversion timed out"}
    if proc.stdout:
        for line in proc.stdout.strip().split("\n")[-15:]:
            log.info("  tiffstack: %s", line)
    if proc.returncode != 0:
        return {"error": f"TIFF conversion exit {proc.returncode}",
                "stderr_tail": (proc.stderr or "")[-400:]}
    summary_path = output.with_suffix("").with_suffix(".tiffstack.json")
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"output_path": str(output)} if output.exists() \
        else {"error": "TIFF conversion produced no output"}


def _voxelize_slicer(reference_volume: Path, mesh: Path, output: Path) -> dict:
    """Invoke Slicer headlessly to voxelize *mesh* onto *reference_volume*'s grid."""
    if not Path(SLICER_BIN).exists():
        return {"error": f"3D Slicer not found at {SLICER_BIN}"}

    config = {
        "reference_volume": str(reference_volume),
        "mesh_path": str(mesh),
        "output_path": str(output),
        "fill_value": 1,
    }
    config_path = SCRIPT_DIR / "_voxelize_config.json"
    config_path.write_text(json.dumps(config))

    env = os.environ.copy()
    env["VOXELIZE_REFERENCE_VOLUME"] = str(reference_volume)
    env["VOXELIZE_MESH_PATH"] = str(mesh)
    env["VOXELIZE_OUTPUT_PATH"] = str(output)
    env["VOXELIZE_FILL_VALUE"] = "1"

    cmd = [
        SLICER_BIN, "--no-splash", "--no-main-window",
        "--python-script", str(SCRIPT_DIR / "voxelize_mesh_in_slicer.py"),
    ]
    log.info("Voxelizing GT mesh in Slicer (this may take 1-5 minutes)…")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "Slicer voxelization timed out (15 min)"}
    except Exception as exc:
        return {"error": f"Slicer subprocess failed: {exc}"}

    log.info("Slicer voxelize exit code: %d", proc.returncode)
    if proc.stdout:
        for line in proc.stdout.strip().split("\n")[-15:]:
            log.info("  voxelize: %s", line)
    if proc.returncode != 0 and proc.stderr:
        log.warning("voxelize stderr: %s", proc.stderr[-500:])

    if not output.exists():
        return {
            "error": "Voxelization produced no output",
            "stdout_tail": (proc.stdout or "")[-400:],
            "stderr_tail": (proc.stderr or "")[-400:],
        }

    summary_path = output.with_suffix("").with_suffix(".voxelize.json")
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"output_path": str(output), "size": output.stat().st_size}


def _voxelize_vtk(reference_volume: Path, mesh: Path, output: Path) -> dict:
    """Voxelize *mesh* onto *reference_volume*'s grid using pure-Python VTK.

    Runs inside the nnInteractive venv (which has SimpleITK + VTK installed
    by ``install_nninteractive.sh``). Avoids 3D Slicer entirely, so it works
    even when the runner is in a non-Aqua bootstrap.
    """
    if not NNI_PYTHON.exists():
        return {
            "error": (
                f"nnInteractive venv not found at {NNI_PYTHON}. "
                "Bootstrap with `.github/scripts/install_nninteractive.sh`."
            ),
        }

    cmd = [
        str(NNI_PYTHON),
        str(SCRIPT_DIR / "voxelize_mesh_vtk.py"),
        "--reference-volume", str(reference_volume),
        "--mesh", str(mesh),
        "--output", str(output),
        "--fill-value", "1",
    ]
    log.info("Voxelizing GT mesh with pure-Python VTK (no Slicer)…")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"error": "VTK voxelization timed out (15 min)"}
    except Exception as exc:
        return {"error": f"VTK voxelize subprocess failed: {exc}"}

    if proc.stdout:
        for line in proc.stdout.strip().split("\n")[-15:]:
            log.info("  voxelize-vtk: %s", line)
    if proc.returncode != 0:
        return {
            "error": f"VTK voxelization returned {proc.returncode}",
            "stderr_tail": (proc.stderr or "")[-400:],
            "stdout_tail": (proc.stdout or "")[-400:],
        }
    if not output.exists():
        return {"error": "VTK voxelization produced no output"}

    summary_path = output.with_suffix("").with_suffix(".voxelize.json")
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"output_path": str(output), "size": output.stat().st_size}


def _voxelize(reference_volume: Path, mesh: Path, output: Path,
              backend: str = "auto") -> dict:
    """Dispatch to either the Slicer-based or pure-Python voxelizer.

    backend: "auto" (try Slicer first, fall back to vtk), "slicer", or "vtk".
    """
    backend = (backend or "auto").lower()
    if backend == "slicer":
        return _voxelize_slicer(reference_volume, mesh, output)
    if backend == "vtk":
        return _voxelize_vtk(reference_volume, mesh, output)
    # auto: prefer Slicer if available, otherwise VTK
    if Path(SLICER_BIN).exists():
        log.info("Voxelize backend=auto → trying Slicer first")
        result = _voxelize_slicer(reference_volume, mesh, output)
        if "error" not in result and output.exists():
            return result
        log.warning("Slicer voxelization failed (%s) — falling back to VTK",
                    result.get("error", "unknown"))
    return _voxelize_vtk(reference_volume, mesh, output)


def _dicom_to_nifti(dicom_dir: Path, output: Path) -> dict:
    """Convert a DICOM series directory into a single .nii.gz."""
    if not NNI_PYTHON.exists():
        return {"error": f"nnInteractive venv missing at {NNI_PYTHON}"}
    cmd = [
        str(NNI_PYTHON),
        str(SCRIPT_DIR / "dicom_to_nifti.py"),
        "--input-dir", str(dicom_dir),
        "--output", str(output),
    ]
    log.info("Converting DICOM series to NIfTI: %s", dicom_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"error": "DICOM->NIfTI conversion timed out"}
    if proc.returncode != 0:
        return {"error": f"DICOM conversion exit {proc.returncode}",
                "stderr_tail": (proc.stderr or "")[-400:]}
    if not output.exists():
        return {"error": "DICOM conversion produced no output"}
    return {"output_path": str(output), "size": output.stat().st_size}


# ---------------------------------------------------------------------------
# Public, importable helpers (used by orchestrators that don't want to shell
# out to ``run_comparison``). Each tries the fast in-process path first and
# falls back to the subprocess path when SimpleITK/VTK aren't available in
# the calling interpreter.
# ---------------------------------------------------------------------------


def voxelize_mesh_to_labelmap(reference_volume: Path, mesh: Path,
                              output: Path,
                              fill_value: int = 1,
                              backend: str = "auto") -> dict:
    """Voxelize *mesh* onto *reference_volume*'s grid.

    Tries an in-process call to :func:`voxelize_mesh_vtk.voxelize` first
    (zero subprocess overhead — the caller is presumably already in the
    nnInteractive venv). If SimpleITK/VTK can't be imported, falls back
    to the existing subprocess paths via the venv's Python.
    """
    backend = (backend or "auto").lower()
    if backend in ("auto", "vtk"):
        try:
            from voxelize_mesh_vtk import voxelize as _vox_inproc
            log.info("Voxelizing in-process via voxelize_mesh_vtk.voxelize")
            return _vox_inproc(
                reference_volume=Path(reference_volume),
                mesh_path=Path(mesh),
                output_path=Path(output),
                fill_value=int(fill_value),
            )
        except ImportError as exc:
            log.info("In-process voxelize unavailable (%s) — falling back to subprocess",
                     exc)
    return _voxelize(reference_volume, mesh, output, backend=backend)


def crop_volume_around_mesh(reference_volume: Path, mesh: Path,
                            output: Path,
                            margin_mm: float = 5.0) -> dict:
    """Crop *reference_volume* to ``mesh.bbox + margin_mm`` (in mm).

    Tries an in-process call to :func:`crop_around_mesh.crop` first; falls
    back to the subprocess path when SimpleITK isn't importable here.
    """
    try:
        from crop_around_mesh import crop as _crop_inproc
        log.info("Cropping in-process via crop_around_mesh.crop")
        return _crop_inproc(
            reference_volume=Path(reference_volume),
            output_path=Path(output),
            margin_mm=float(margin_mm),
            mesh_path=Path(mesh),
        )
    except ImportError as exc:
        log.info("In-process crop unavailable (%s) — falling back to subprocess",
                 exc)
        return _crop_volume(reference_volume, mesh, output, margin_mm)


def _crop_volume(reference_volume: Path, mesh: Path,
                 output: Path, margin_mm: float) -> dict:
    """Crop a volume to mesh bbox + margin (mm) using SimpleITK."""
    if not NNI_PYTHON.exists():
        return {"error": f"nnInteractive venv missing at {NNI_PYTHON}"}
    cmd = [
        str(NNI_PYTHON),
        str(SCRIPT_DIR / "crop_around_mesh.py"),
        "--reference-volume", str(reference_volume),
        "--mesh", str(mesh),
        "--output", str(output),
        "--margin-mm", str(margin_mm),
    ]
    log.info("Cropping volume around mesh bbox + %.1fmm margin", margin_mm)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "Crop timed out"}
    if proc.returncode != 0:
        return {"error": f"Crop exit {proc.returncode}",
                "stderr_tail": (proc.stderr or "")[-400:]}
    summary_path = output.with_suffix("").with_suffix(".crop.json")
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"output_path": str(output)} if output.exists() \
        else {"error": "Crop produced no output"}


def _count_nonzero_voxels(labelmap: Path) -> Optional[int]:
    """Best-effort voxel-count read of a labelmap via the nnInteractive
    venv (which has SimpleITK installed). Returns ``None`` if the read
    fails - the paint loop just won't get a size budget that run, no
    crash."""
    if not NNI_PYTHON.exists() or not labelmap.exists():
        return None
    script = (
        "import sys, SimpleITK as sitk, numpy as np\n"
        "img = sitk.ReadImage(sys.argv[1])\n"
        "arr = sitk.GetArrayFromImage(img)\n"
        "print(int((arr > 0).sum()))\n"
    )
    try:
        proc = subprocess.run(
            [str(NNI_PYTHON), "-c", script, str(labelmap)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip().isdigit():
            return int(proc.stdout.strip())
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("_count_nonzero_voxels failed for %s: %s", labelmap, exc)
    return None


def _volume_mm3_of(labelmap: Path) -> Optional[float]:
    """Best-effort foreground volume of a labelmap via the nnInteractive
    venv. Returns None on any failure."""
    if not NNI_PYTHON.exists() or not labelmap.exists():
        return None
    script = (
        "import sys, SimpleITK as sitk, numpy as np\n"
        "img = sitk.ReadImage(sys.argv[1])\n"
        "sx, sy, sz = img.GetSpacing()\n"
        "arr = sitk.GetArrayFromImage(img)\n"
        "n = int((arr > 0).sum())\n"
        "print(n * sx * sy * sz)\n"
    )
    try:
        proc = subprocess.run(
            [str(NNI_PYTHON), "-c", script, str(labelmap)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            return float(proc.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log.debug("_volume_mm3_of failed for %s: %s", labelmap, exc)
    return None


def _run_paint_loop(input_volume: Path, goal: str, output_dir: Path,
                     media_id: str, max_steps: int,
                     expected_voxels: Optional[int] = None,
                     expected_volume_mm3: Optional[float] = None) -> dict:
    """Run nninteractive_loop.py.

    Backend selection:
      * If ``NNI_REMOTE_WS`` is set, the loop talks to a remote
        nnInteractive WebSocket server (see ``nni_ws_server.py``). In
        that case we run the loop with the *current* Python interpreter
        because it doesn't need a local nnInteractive/torch install —
        it only needs websocket-client + SimpleITK + matplotlib + openai.
      * Otherwise, the loop runs in the dedicated nnInteractive venv
        (``$NNINTERACTIVE_HOME/bin/python``) which has the full backend.
    """
    remote_url = os.environ.get("NNI_REMOTE_WS", "").strip()
    if remote_url:
        # Remote backend: use the parent Python (the same one that's
        # invoking this script). All the deps the loop needs in remote
        # mode (websocket-client, SimpleITK, matplotlib, openai) are part
        # of the AutoResearchClaw env.
        loop_python = sys.executable
        log.info("Using remote nnInteractive backend at %s "
                 "(loop python: %s)", remote_url, loop_python)
    else:
        if not NNI_PYTHON.exists():
            return {
                "error": (
                    f"nnInteractive venv not found at {NNI_PYTHON}. "
                    "Bootstrap it once with "
                    "`.github/scripts/install_nninteractive.sh`, or set "
                    "NNI_REMOTE_WS=ws://… to use a remote server."
                ),
            }
        loop_python = str(NNI_PYTHON)

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        loop_python,
        str(SCRIPT_DIR / "nninteractive_loop.py"),
        "--input", str(input_volume),
        "--goal", goal,
        "--media-id", media_id,
        "--output-dir", str(output_dir),
        "--max-steps", str(max_steps),
    ]
    # Provide the LLM with a target-size budget when we know it. Without
    # this hint the model has no anchor for "how big should the mask be"
    # and tends to over-segment (Felis run 26373115227 saw the mask grow
    # to 2.2x the GT size while precision dropped to 9.8%).
    if expected_voxels is not None and expected_voxels > 0:
        cmd += ["--expected-voxels", str(int(expected_voxels))]
    if expected_volume_mm3 is not None and expected_volume_mm3 > 0:
        cmd += ["--expected-volume-mm3", f"{float(expected_volume_mm3):.3f}"]
    env = os.environ.copy()
    env.setdefault("NNINTERACTIVE_HOME", str(NNI_HOME))
    # nnInteractive's nnU-Net backbone hits ops that aren't implemented
    # on Apple Silicon's MPS yet (e.g. aten::avg_pool3d.out). Without this
    # fallback every prompt step errors and the prediction stays empty.
    # CPU is slower but correct; CUDA / Linux ignore this var.
    env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    # On a 16 GB Mac mini, full-thread CPU inference blows past RAM and the
    # OS kills the worker (SIGKILL / exit -9). Even 4 threads triggers OOM
    # on a 119^3 volume. Default to 1 thread; users with more RAM can
    # override any of these env vars before invoking the comparison.
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                "NUMEXPR_NUM_THREADS", "NNINTERACTIVE_TORCH_THREADS"):
        env.setdefault(var, "1")

    log.info("Running nnInteractive paint loop (goal=%s, max_steps=%d)",
             goal, max_steps)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=3600, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "Paint loop timed out (1h)"}
    except Exception as exc:
        return {"error": f"Paint-loop subprocess failed: {exc}"}

    log.info("Paint loop exit code: %d", proc.returncode)
    if proc.stdout:
        for line in proc.stdout.strip().split("\n")[-10:]:
            log.info("  loop: %s", line)
    if proc.returncode != 0 and proc.stderr:
        log.warning("loop stderr: %s", proc.stderr[-500:])

    summary_file = output_dir / f"{media_id}_nni_summary.json"
    if summary_file.exists():
        try:
            return json.loads(summary_file.read_text())
        except json.JSONDecodeError:
            pass
    return {"error": "No nnInteractive summary produced",
            "stdout_tail": (proc.stdout or "")[-400:]}


def _compute_metrics(prediction: Path, ground_truth: Path,
                     volume: Path, overlay_path: Path,
                     metrics_path: Path) -> dict:
    """Run segmentation_metrics.

    Uses the nnInteractive venv when available (it always has SimpleITK),
    otherwise falls back to the current Python — which works in remote
    backend mode where the local box doesn't need nnInteractive/torch but
    does need SimpleITK and matplotlib (already installed in the parent
    AutoResearchClaw env).
    """
    metrics_python = str(NNI_PYTHON) if NNI_PYTHON.exists() else sys.executable
    cmd = [
        metrics_python,
        str(SCRIPT_DIR / "segmentation_metrics.py"),
        "--pred", str(prediction),
        "--gt", str(ground_truth),
        "--volume", str(volume),
        "--output", str(metrics_path),
        "--overlay", str(overlay_path),
    ]
    log.info("Computing comparison metrics…")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=900)
    except Exception as exc:
        return {"error": f"metrics subprocess failed: {exc}"}

    if proc.stdout:
        for line in proc.stdout.strip().split("\n"):
            log.info("  metrics: %s", line)
    if proc.returncode != 0:
        return {"error": f"metrics returned {proc.returncode}",
                "stderr_tail": (proc.stderr or "")[-400:]}
    if metrics_path.exists():
        try:
            return json.loads(metrics_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"error": "metrics file missing"}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _align_mesh_to_volume(mesh_path: Path, volume_path: Path,
                          out_path: Path, method: str = "centroid") -> dict:
    """Translate ``mesh_path`` into ``volume_path``'s world frame.

    Many MorphoSource derivative-mesh projects (e.g. 358382 "Colors of Skull
    Anatomy") ship .ply / .stl files in the modeller's local frame — the
    mesh is centered at its own bbox instead of the CT scanner's world
    coordinates. Bare voxelization then fails because the mesh sits
    entirely outside the CT volume.

    Methods:
      - ``"centroid"`` : translate so bbox centroids coincide.
      - ``"auto"``     : skip when bboxes already overlap by >50% of the
                          smaller bbox; otherwise apply ``centroid``.

    Runs as a subprocess into the nnInteractive venv (needs vtk + SimpleITK
    + numpy, which the system python on the runner does not have).
    """
    if not NNI_PYTHON.exists():
        return {"error": f"nnInteractive venv missing at {NNI_PYTHON}"}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out_path.with_suffix("").with_suffix(".align.json")
    cmd = [
        str(NNI_PYTHON),
        str(SCRIPT_DIR / "align_mesh_to_volume.py"),
        "--reference-volume", str(volume_path),
        "--mesh", str(mesh_path),
        "--output", str(out_path),
        "--method", method,
        "--summary", str(summary_path),
    ]
    log.info("Aligning mesh to CT frame (method=%s)", method)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"error": "mesh alignment timed out"}
    if proc.stdout:
        for line in proc.stdout.strip().split("\n")[-10:]:
            log.info("  align: %s", line)
    if proc.returncode != 0:
        return {"error": f"alignment exit {proc.returncode}",
                "stderr_tail": (proc.stderr or "")[-400:]}
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"output_path": str(out_path)} if out_path.exists() \
        else {"error": "alignment produced no output"}


def _resample_cap_axis(ct_path: Path, gt_path: Path,
                       max_axis: int) -> Optional[dict]:
    """Resample ``ct_path`` and ``gt_path`` in place so no axis exceeds ``max_axis``.

    Keeps fixture bundles manageable on whole-skull specimens. CT goes
    through linear interpolation; GT labelmap goes nearest-neighbour.

    Runs as a subprocess into the nnInteractive venv (needs SimpleITK +
    numpy, which the system python on the runner does not have).
    """
    if not max_axis or max_axis <= 0:
        return None
    if not NNI_PYTHON.exists():
        return {"error": f"nnInteractive venv missing at {NNI_PYTHON}"}
    summary_path = ct_path.with_name(ct_path.name + ".resample.json")
    cmd = [
        str(NNI_PYTHON),
        str(SCRIPT_DIR / "resample_volume_pair.py"),
        "--ct", str(ct_path),
        "--gt", str(gt_path),
        "--max-axis", str(int(max_axis)),
        "--summary", str(summary_path),
    ]
    log.info("Resampling (CT, GT) pair to max_axis=%d", max_axis)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "resample timed out"}
    if proc.stdout:
        for line in proc.stdout.strip().split("\n")[-10:]:
            log.info("  resample: %s", line)
    if proc.returncode != 0:
        return {"error": f"resample exit {proc.returncode}",
                "stderr_tail": (proc.stderr or "")[-400:]}
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"resampled": True, "max_axis_cap": max_axis,
            "ct_path": str(ct_path), "gt_path": str(gt_path)}


def run_comparison(ct_media_id: str, gt_media_id: str, goal: str,
                   output_dir: Path, max_steps: int = 12,
                   voxelize_backend: str = "auto",
                   crop_around_mesh_mm: float = 0.0,
                   max_voxel_axis: int = 0,
                   align_mesh_to_ct: str = "",
                   skip_paint_loop: bool = False,
                   export_fixture_dir: Optional[Path] = None) -> dict:
    """End-to-end comparison.

    voxelize_backend  "auto" | "slicer" | "vtk"   (auto = Slicer first, VTK fallback)
    crop_around_mesh_mm  > 0 to crop CT to the GT mesh bbox + margin (mm)
                         before running the paint loop. 0 disables cropping.
    max_voxel_axis      > 0 to resample the (cropped) CT + voxelized GT so no
                         axis exceeds this many voxels. Keeps fixture bundles
                         under control on whole-skull specimens. 0 disables.
    align_mesh_to_ct    "" | "centroid" | "auto" — apply a translation to
                         the mesh BEFORE crop/voxelize so its bbox overlaps
                         the CT. Necessary for derivative-mesh projects
                         (e.g. 358382 "Colors of Skull Anatomy") where the
                         .ply/.stl is in the modeller's local frame.
    skip_paint_loop     stop after voxelization + alignment report. Useful as
                        a dry run to verify coordinate alignment of the GT mesh
                        against the CT volume *before* spending OpenAI quota
                        on the iterative paint loop.
    export_fixture_dir  If set, copy the small inputs/outputs needed to re-run
                        ``run_comparison_from_fixture`` (CT + GT labelmap, and
                        the prediction labelmap + metrics if the paint loop
                        ran) into this directory. This is the "cached file"
                        path that lets PR-level CI smoke-test the comparison
                        pipeline in <2 min on a hosted runner.
    """
    t0 = time.time()
    pair_dir = output_dir / f"{ct_media_id}__vs__{gt_media_id}"
    pair_dir.mkdir(parents=True, exist_ok=True)
    download_root = pair_dir / "download"

    # ---- 1. Download CT + GT mesh ----
    ct_dl = _download(ct_media_id, download_root / f"ct_{ct_media_id}")
    if not ct_dl.get("success"):
        return {"success": False, "stage": "download_ct", "result": ct_dl}

    gt_dl = _download(gt_media_id, download_root / f"gt_{gt_media_id}")
    if not gt_dl.get("success"):
        return {"success": False, "stage": "download_gt", "result": gt_dl}

    # ---- 2. Locate input files ----
    ct_dir = Path(ct_dl["download_dir"])
    gt_dir = Path(gt_dl["download_dir"])

    ct_pick = _find_ct_volume(ct_dir)
    if ct_pick is None:
        return {
            "success": False, "stage": "locate_ct",
            "error": f"No CT volume (NIfTI/NRRD/DICOM) under {ct_dir}",
        }
    log.info("Selected CT volume: %s", ct_pick.display)

    mesh_pick = _find_mesh(gt_dir)
    if mesh_pick is None:
        return {
            "success": False, "stage": "locate_mesh",
            "error": f"No mesh (PLY/STL/OBJ) under {gt_dir}",
        }
    log.info("Selected GT mesh: %s", mesh_pick.display)
    # ``mesh_path_used`` may be replaced below by an aligned copy. It is the
    # file passed to crop + voxelize. ``mesh_pick.path`` always points at the
    # original download for provenance.
    mesh_path_used = mesh_pick.path

    # ---- 2b. Normalize the CT input to a single NIfTI on disk ----
    ct_path = ct_pick.path
    kind = _ct_input_kind(ct_path)
    if kind == "dicom":
        log.info("CT is a DICOM series — converting to NIfTI")
        nifti_path = pair_dir / f"ct_{ct_media_id}.nii.gz"
        dicom_result = _dicom_to_nifti(ct_path, nifti_path)
        if "error" in dicom_result:
            return {"success": False, "stage": "dicom_to_nifti",
                    "result": dicom_result}
        ct_path = nifti_path
    elif kind == "tiff":
        log.info("CT is a TIFF z-stack — converting to NIfTI "
                 "(spacing from MorphoSource API)")
        nifti_path = pair_dir / f"ct_{ct_media_id}.nii.gz"
        tiff_result = _tiff_stack_to_nifti(ct_path, nifti_path,
                                           media_id=ct_media_id)
        if "error" in tiff_result:
            return {"success": False, "stage": "tiff_to_nifti",
                    "result": tiff_result}
        ct_path = nifti_path
        log.info("TIFF→NIfTI: size=%s spacing=%s source=%s",
                 tiff_result.get("size"),
                 tiff_result.get("spacing"),
                 tiff_result.get("spacing_source"))

    # ---- 2c1. Optional: align mesh to CT frame (translation only) ----
    # Derivative-mesh projects (e.g. 358382 "Colors of Skull Anatomy") ship
    # meshes in the modeller's local frame. Without alignment the mesh sits
    # entirely outside the CT in world space, so both cropping and voxelization
    # silently emit nothing useful.
    alignment_summary = None
    if align_mesh_to_ct in {"centroid", "auto"}:
        aligned_mesh = pair_dir / f"mesh_aligned{mesh_pick.path.suffix}"
        try:
            alignment_summary = _align_mesh_to_volume(
                mesh_path=mesh_pick.path,
                volume_path=ct_path,
                out_path=aligned_mesh,
                method=align_mesh_to_ct,
            )
        except Exception as exc:
            log.exception("Mesh alignment failed")
            return {"success": False, "stage": "align_mesh",
                    "error": repr(exc)}
        if "error" in alignment_summary:
            return {"success": False, "stage": "align_mesh",
                    "result": alignment_summary}
        if alignment_summary.get("applied") or aligned_mesh.exists():
            mesh_path_used = aligned_mesh

    # ---- 2c. Optional: crop to GT mesh bbox + margin (faster + tractable) ----
    cropped_ct = ct_path
    cropped_summary = None
    if crop_around_mesh_mm and crop_around_mesh_mm > 0:
        cropped_ct = pair_dir / f"ct_{ct_media_id}_cropped.nii.gz"
        cropped_summary = _crop_volume(
            reference_volume=ct_path, mesh=mesh_path_used,
            output=cropped_ct, margin_mm=crop_around_mesh_mm,
        )
        if "error" in cropped_summary:
            return {"success": False, "stage": "crop", "result": cropped_summary}
        log.info("Cropped CT: %s -> size %s",
                 cropped_ct, cropped_summary.get("crop_size"))

    # ---- 3. Voxelize GT mesh onto (cropped) CT grid ----
    gt_labelmap = pair_dir / "gt_voxelized.nii.gz"
    voxelize_result = _voxelize(cropped_ct, mesh_path_used, gt_labelmap,
                                backend=voxelize_backend)
    if "error" in voxelize_result:
        return {"success": False, "stage": "voxelize", "result": voxelize_result}

    # ---- 3a. Optional: resample so no axis exceeds max_voxel_axis ----
    # This keeps the fixture bundle (and runtime memory) manageable when running
    # whole-skull specimens. The CT goes linear; the GT labelmap goes nearest
    # neighbour so its binary value space stays exact. Both end on the same grid.
    resample_summary = None
    if max_voxel_axis and max_voxel_axis > 0 and cropped_ct != ct_path:
        # Only resample when we actually cropped; resampling the full untouched
        # CT volume would be both surprising and unhelpful.
        resample_summary = _resample_cap_axis(cropped_ct, gt_labelmap,
                                              max_voxel_axis)
    elif max_voxel_axis and max_voxel_axis > 0:
        # User asked for the cap but didn't crop. Still apply it so they get
        # the size guarantee they requested.
        resample_summary = _resample_cap_axis(cropped_ct, gt_labelmap,
                                              max_voxel_axis)

    # ---- 3b. Optional dry-run: stop here, write alignment report ----
    if skip_paint_loop:
        align_report_path = pair_dir / "alignment_report.md"
        align_report_path.write_text(_render_alignment_report(
            ct_media_id, gt_media_id, ct_pick, mesh_pick,
            cropped_summary, voxelize_result, ct_path, cropped_ct,
            gt_labelmap, pair_dir,
        ))
        return {
            "success": True,
            "stage": "voxelize_only",
            "ct_media_id": ct_media_id,
            "gt_media_id": gt_media_id,
            "ct_path_used": str(cropped_ct),
            "mesh_path": str(mesh_pick.path),
            "gt_labelmap": str(gt_labelmap),
            "voxelize_backend": voxelize_result.get("backend",
                                                    voxelize_backend),
            "foreground_voxels": voxelize_result.get("foreground_voxels"),
            "foreground_volume_mm3": voxelize_result.get("foreground_volume_mm3"),
            "crop_summary": cropped_summary,
            "voxelize_summary": voxelize_result,
            "alignment_report": str(align_report_path),
            "duration_s": round(time.time() - t0, 1),
            "skipped_paint_loop": True,
        }

    # ---- 4. Run paint loop on the (cropped) CT volume ----
    # After cropping + voxelization we know exactly how big the target is.
    # Pass that as a budget to the paint loop so the LLM stops adding
    # positives once the mask is close to the right size.
    gt_voxels = _count_nonzero_voxels(gt_labelmap)
    gt_volume_mm3 = _volume_mm3_of(gt_labelmap)
    nni_dir = pair_dir / "nninteractive"
    nni_result = _run_paint_loop(
        cropped_ct, goal, nni_dir, ct_media_id, max_steps,
        expected_voxels=gt_voxels,
        expected_volume_mm3=gt_volume_mm3,
    )
    if "error" in nni_result:
        return {"success": False, "stage": "paint_loop", "result": nni_result}
    pred_labelmap = Path(nni_result.get("labelmap_path", ""))
    if not pred_labelmap.exists():
        return {"success": False, "stage": "paint_loop",
                "error": f"prediction labelmap missing: {pred_labelmap}"}

    # ---- 5. Compute metrics + render overlay ----
    metrics = _compute_metrics(
        prediction=pred_labelmap,
        ground_truth=gt_labelmap,
        volume=cropped_ct,
        overlay_path=pair_dir / "overlay.png",
        metrics_path=pair_dir / "metrics.json",
    )
    if "error" in metrics:
        return {"success": False, "stage": "metrics", "result": metrics}

    # ---- 6. Markdown report ----
    report_path = pair_dir / "report.md"
    report_path.write_text(_render_report(
        ct_media_id, gt_media_id, goal, ct_pick, mesh_pick, voxelize_result,
        nni_result, metrics, pair_dir, max_steps,
    ))

    # ---- 7. Optional: export a compact fixture bundle for cached re-runs ----
    fixture_summary = None
    if export_fixture_dir is not None:
        fixture_summary = _export_fixture(
            export_fixture_dir=Path(export_fixture_dir),
            ct_media_id=ct_media_id,
            gt_media_id=gt_media_id,
            goal=goal,
            max_steps=max_steps,
            voxelize_backend=voxelize_result.get("backend", voxelize_backend),
            crop_around_mesh_mm=crop_around_mesh_mm,
            max_voxel_axis=max_voxel_axis,
            ct_used=cropped_ct,
            gt_labelmap=gt_labelmap,
            pred_labelmap=pred_labelmap,
            metrics_path=pair_dir / "metrics.json",
        )

    return {
        "success": True,
        "ct_media_id": ct_media_id,
        "gt_media_id": gt_media_id,
        "goal": goal,
        "ct_path": str(ct_path),
        "ct_path_used": str(cropped_ct),
        "mesh_path": str(mesh_pick.path),
        "gt_labelmap": str(gt_labelmap),
        "prediction_labelmap": str(pred_labelmap),
        "metrics_path": str(pair_dir / "metrics.json"),
        "overlay_path": str(pair_dir / "overlay.png"),
        "report_path": str(report_path),
        "voxelize_backend": voxelize_result.get("backend",
                                                voxelize_backend),
        "alignment_summary": alignment_summary,
        "crop_summary": cropped_summary,
        "resample_summary": resample_summary,
        "metrics": metrics,
        "duration_s": round(time.time() - t0, 1),
        "fixture_export": fixture_summary,
    }


# ---------------------------------------------------------------------------
# Cached / fixture-based runs (PR CI smoke test path)
# ---------------------------------------------------------------------------


def _export_fixture(export_fixture_dir: Path,
                    ct_media_id: str, gt_media_id: str,
                    goal: str, max_steps: int,
                    voxelize_backend: str,
                    crop_around_mesh_mm: float,
                    ct_used: Path, gt_labelmap: Path,
                    pred_labelmap: Optional[Path],
                    metrics_path: Optional[Path],
                    max_voxel_axis: int = 0) -> dict:
    """Copy the small subset of files needed for ``--from-fixture`` re-runs.

    Layout written:
        <dir>/ct.nii.gz                  # cropped (or full) CT volume
        <dir>/gt_voxelized.nii.gz        # GT mesh rasterized onto CT grid
        <dir>/pred.nii.gz                # nnInteractive prediction (optional)
        <dir>/baseline_metrics.json      # baseline metrics for regression gate
        <dir>/fixture.json               # metadata: media ids, goal, steps, backend
    """
    import shutil
    export_fixture_dir.mkdir(parents=True, exist_ok=True)

    copied: dict[str, str] = {}

    def _copy(src: Path, dest_name: str) -> None:
        if src and Path(src).exists():
            dest = export_fixture_dir / dest_name
            shutil.copy2(src, dest)
            copied[dest_name] = str(dest)

    _copy(ct_used, "ct.nii.gz")
    _copy(gt_labelmap, "gt_voxelized.nii.gz")
    if pred_labelmap is not None:
        _copy(pred_labelmap, "pred.nii.gz")
    if metrics_path is not None:
        _copy(metrics_path, "baseline_metrics.json")

    meta = {
        "ct_media_id": ct_media_id,
        "gt_media_id": gt_media_id,
        "goal": goal,
        "max_steps": max_steps,
        "voxelize_backend": voxelize_backend,
        "crop_around_mesh_mm": crop_around_mesh_mm,
        "max_voxel_axis": max_voxel_axis,
        "files": copied,
    }
    (export_fixture_dir / "fixture.json").write_text(
        json.dumps(meta, indent=2)
    )
    log.info("Exported fixture bundle to %s (files: %s)",
             export_fixture_dir, sorted(copied.keys()))
    return {"dir": str(export_fixture_dir), "files": copied, "meta": meta}


def run_comparison_from_fixture(fixture_dir: Path, output_dir: Path,
                                pred_labelmap: Optional[Path] = None,
                                goal: Optional[str] = None,
                                max_steps: Optional[int] = None,
                                skip_paint_loop: bool = False) -> dict:
    """Run the comparison using a pre-computed fixture.

    Skips download, TIFF→NIfTI, crop, and voxelize. The fixture directory
    must contain ``ct.nii.gz`` and ``gt_voxelized.nii.gz``; ``fixture.json``
    provides the metadata. If ``pred_labelmap`` is supplied (or
    ``pred.nii.gz`` exists in the fixture dir), the paint loop is also
    skipped and only metrics + overlay + report are produced — the cheap
    no-GPU no-OpenAI smoke path.
    """
    t0 = time.time()
    fixture_dir = Path(fixture_dir)
    fixture_meta_path = fixture_dir / "fixture.json"
    if not fixture_meta_path.exists():
        return {"success": False, "stage": "fixture_load",
                "error": f"Missing {fixture_meta_path}"}
    try:
        meta = json.loads(fixture_meta_path.read_text())
    except json.JSONDecodeError as exc:
        return {"success": False, "stage": "fixture_load",
                "error": f"Could not parse fixture.json: {exc}"}

    ct_path = fixture_dir / "ct.nii.gz"
    gt_labelmap = fixture_dir / "gt_voxelized.nii.gz"
    if not ct_path.exists() or not gt_labelmap.exists():
        return {"success": False, "stage": "fixture_load",
                "error": "fixture is missing ct.nii.gz or gt_voxelized.nii.gz"}

    ct_media_id = meta.get("ct_media_id", "ct")
    gt_media_id = meta.get("gt_media_id", "gt")
    fixture_goal = goal or meta.get("goal", "")
    fixture_max_steps = (
        max_steps if max_steps is not None
        else int(meta.get("max_steps", 12))
    )

    pair_dir = output_dir / f"{ct_media_id}__vs__{gt_media_id}"
    pair_dir.mkdir(parents=True, exist_ok=True)

    pred_candidate = pred_labelmap or (fixture_dir / "pred.nii.gz")
    have_cached_pred = pred_candidate.exists() if pred_candidate else False

    # ---- 1. Render an alignment report so callers can sanity-check inputs ----
    ct_pick = FilePick(path=ct_path, size=ct_path.stat().st_size)
    mesh_pick = FilePick(path=Path("(fixture, mesh not included)"), size=0)

    # ---- 2. Optionally short-circuit on alignment only ----
    if skip_paint_loop and not have_cached_pred:
        align_report_path = pair_dir / "alignment_report.md"
        align_report_path.write_text(_render_alignment_report(
            ct_media_id, gt_media_id, ct_pick, mesh_pick,
            crop_summary=None,
            voxelize_result={"backend": meta.get("voxelize_backend", "fixture")},
            ct_full=ct_path, ct_used=ct_path,
            gt_labelmap=gt_labelmap, pair_dir=pair_dir,
        ))
        return {
            "success": True, "stage": "fixture_alignment_only",
            "ct_path_used": str(ct_path),
            "gt_labelmap": str(gt_labelmap),
            "alignment_report": str(align_report_path),
            "duration_s": round(time.time() - t0, 1),
            "from_fixture": True,
        }

    # ---- 3. Either run the paint loop (slow GPU path) or use cached pred ----
    if have_cached_pred:
        log.info("Using cached prediction labelmap %s — skipping paint loop",
                 pred_candidate)
        pred_path = pred_candidate
        nni_result = {
            "labelmap_path": str(pred_path),
            "from_cache": True,
            "n_prompts": 0,
        }
    else:
        nni_dir = pair_dir / "nninteractive"
        nni_result = _run_paint_loop(ct_path, fixture_goal, nni_dir,
                                     ct_media_id, fixture_max_steps)
        if "error" in nni_result:
            return {"success": False, "stage": "paint_loop",
                    "result": nni_result, "from_fixture": True}
        pred_path = Path(nni_result.get("labelmap_path", ""))
        if not pred_path.exists():
            return {"success": False, "stage": "paint_loop",
                    "error": f"prediction labelmap missing: {pred_path}",
                    "from_fixture": True}

    # ---- 4. Metrics + overlay ----
    metrics = _compute_metrics(
        prediction=pred_path,
        ground_truth=gt_labelmap,
        volume=ct_path,
        overlay_path=pair_dir / "overlay.png",
        metrics_path=pair_dir / "metrics.json",
    )
    if "error" in metrics:
        return {"success": False, "stage": "metrics", "result": metrics,
                "from_fixture": True}

    # ---- 5. Report ----
    report_path = pair_dir / "report.md"
    report_path.write_text(_render_report(
        ct_media_id, gt_media_id, fixture_goal, ct_pick, mesh_pick,
        voxelize_result={
            "backend": meta.get("voxelize_backend", "fixture"),
            "reference_dims": None,
            "reference_spacing_xyz": None,
            "foreground_voxels": metrics.get("voxel_count_gt"),
            "foreground_volume_mm3": metrics.get("volume_mm3_gt"),
        },
        nni_result=nni_result,
        metrics=metrics,
        pair_dir=pair_dir,
        max_steps=fixture_max_steps,
    ))

    return {
        "success": True,
        "ct_media_id": ct_media_id,
        "gt_media_id": gt_media_id,
        "goal": fixture_goal,
        "ct_path_used": str(ct_path),
        "gt_labelmap": str(gt_labelmap),
        "prediction_labelmap": str(pred_path),
        "metrics_path": str(pair_dir / "metrics.json"),
        "overlay_path": str(pair_dir / "overlay.png"),
        "report_path": str(report_path),
        "metrics": metrics,
        "from_fixture": True,
        "used_cached_pred": have_cached_pred,
        "duration_s": round(time.time() - t0, 1),
    }


def evaluate_regression(metrics: dict, baseline_metrics_path: Optional[Path],
                        assert_dice: Optional[float],
                        regression_tol: float = 0.01) -> tuple[bool, list[str]]:
    """Return ``(passed, messages)``.

    Fails (returns ``passed=False``) if either:
      * ``assert_dice`` is set and the run's dice falls below it, OR
      * ``baseline_metrics_path`` is set and dice or IoU regresses by more
        than ``regression_tol`` (absolute) vs the baseline.
    """
    msgs: list[str] = []
    passed = True
    dice = metrics.get("dice")
    iou = metrics.get("iou")

    if assert_dice is not None:
        if dice is None:
            msgs.append(f"[FAIL] dice is null; expected >= {assert_dice}")
            passed = False
        elif dice + 1e-9 < assert_dice:
            msgs.append(
                f"[FAIL] dice {dice:.4f} < floor {assert_dice:.4f}"
            )
            passed = False
        else:
            msgs.append(f"[OK]   dice {dice:.4f} >= floor {assert_dice:.4f}")

    if baseline_metrics_path is not None:
        bp = Path(baseline_metrics_path)
        if not bp.exists():
            msgs.append(f"[FAIL] baseline metrics not found: {bp}")
            passed = False
        else:
            try:
                baseline = json.loads(bp.read_text())
            except json.JSONDecodeError as exc:
                msgs.append(f"[FAIL] baseline parse error: {exc}")
                return False, msgs
            b_dice = baseline.get("dice")
            b_iou = baseline.get("iou")
            for label, current, expected in (("dice", dice, b_dice),
                                             ("iou", iou, b_iou)):
                if expected is None or current is None:
                    msgs.append(
                        f"[SKIP] {label}: missing (cur={current} base={expected})"
                    )
                    continue
                drop = expected - current
                if drop > regression_tol + 1e-9:
                    msgs.append(
                        f"[FAIL] {label} regressed {drop:.4f} "
                        f"(cur {current:.4f} vs baseline {expected:.4f}, "
                        f"tol {regression_tol})"
                    )
                    passed = False
                else:
                    msgs.append(
                        f"[OK]   {label} {current:.4f} vs baseline "
                        f"{expected:.4f} (delta {-drop:+.4f}, tol {regression_tol})"
                    )
    return passed, msgs


# ---------------------------------------------------------------------------
# Presets — quick way to run a known-good test pair
# ---------------------------------------------------------------------------

PRESETS = {
    # Veiled chameleon (Chamaeleo calyptratus), uf:herp:191369
    # — all 7 media items are open-download.
    # The Right Stapes is one of the smallest bones in the body
    # (~3 mm), so this is the fastest possible end-to-end test.
    "chameleon_stapes": {
        "ct_media_id": "000408242",   # Head CT (smallest CT, 1.35 GB)
        "gt_media_id": "000790324",   # Right Stapes (587 KB PLY)
        "goal": "Segment the right stapes (small middle-ear bone). "
                "It is a tiny irregular bone in the inner ear region.",
        "max_steps": 6,
        # The stapes is roughly 3 mm wide; a 1.5 mm margin keeps the cropped
        # CT around 120^3 voxels (~1.7M voxels) which fits in ~16 GB RAM
        # during nnInteractive CPU inference on a Mac mini. Larger margins
        # (e.g. 4.0) blow past 32 GB peak and trigger OS OOM-kills.
        "crop_around_mesh_mm": 1.5,
        "voxelize_backend": "vtk",
    },
}


# ---------------------------------------------------------------------------


def _render_alignment_report(ct_id: str, gt_id: str,
                             ct_pick: FilePick, mesh_pick: FilePick,
                             crop_summary: dict | None,
                             voxelize_result: dict,
                             ct_full: Path, ct_used: Path,
                             gt_labelmap: Path, pair_dir: Path) -> str:
    """Coordinate-alignment dry-run summary (no paint loop run yet)."""
    fg = voxelize_result.get("foreground_voxels", 0)
    fg_mm3 = voxelize_result.get("foreground_volume_mm3", 0.0)
    mesh_world = voxelize_result.get("mesh_world_bounds")
    mesh_idx = voxelize_result.get("mesh_index_bounds")
    ref_dims = voxelize_result.get("reference_dims")
    ref_spacing = voxelize_result.get("reference_spacing_xyz")

    aligned = bool(fg and fg > 0)
    diag = "OK — GT mesh overlaps the CT grid" if aligned else (
        "FAILED — GT mesh produced 0 foreground voxels. Likely a "
        "coordinate-frame mismatch (e.g. LPS vs RAS, mm vs μm, or the "
        "mesh was exported in a transformed space)."
    )

    lines = [
        f"# Alignment dry run — `{ct_id}` vs `{gt_id}`",
        "",
        f"**Status:** {'**ALIGNED**' if aligned else '**NOT ALIGNED**'}  ",
        f"**Diagnosis:** {diag}",
        "",
        "## Inputs",
        "",
        f"- **CT volume:** [`{ct_id}`](https://www.morphosource.org/concern/media/{ct_id})  ",
        f"  source file: `{ct_pick.path.name}` ({ct_pick.size:,} bytes)",
        f"- **GT mesh:**  [`{gt_id}`](https://www.morphosource.org/concern/media/{gt_id})  ",
        f"  source file: `{mesh_pick.path.name}` ({mesh_pick.size:,} bytes)",
        "",
        "## CT preprocessing",
        "",
        f"- Working CT: `{ct_used.name}`",
    ]
    if crop_summary:
        lines.extend([
            f"- Crop margin: {crop_summary.get('margin_mm', 'n/a')} mm",
            f"- Original size: {crop_summary.get('original_size')}",
            f"- Cropped size: {crop_summary.get('crop_size')}",
            f"- Crop bounds (world, mm): {crop_summary.get('world_bounds_xyz')}",
        ])
    else:
        lines.append("- Cropping: disabled (full CT used)")

    lines.extend([
        "",
        "## GT voxelization",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Backend | `{voxelize_result.get('backend','?')}` |",
        f"| Reference dims (x,y,z) | {ref_dims} |",
        f"| Reference spacing (mm) | {ref_spacing} |",
        f"| Mesh #points / #cells | {voxelize_result.get('mesh_n_points')} / {voxelize_result.get('mesh_n_cells')} |",
        f"| Mesh world bounds (mm) | {mesh_world} |",
        f"| Mesh in voxel-index space | {mesh_idx} |",
        f"| GT foreground voxels | **{fg:,}** |",
        f"| GT foreground volume | **{fg_mm3:,.4f} mm³** |",
        "",
        "## Files",
        "",
        f"- [`gt_voxelized.nii.gz`](gt_voxelized.nii.gz) — rasterized GT labelmap",
        f"- [`gt_voxelized.voxelize.json`](gt_voxelized.voxelize.json) — voxelization summary",
    ])
    if crop_summary:
        lines.append(
            f"- [`{Path(crop_summary['output_path']).name}`]"
            f"({Path(crop_summary['output_path']).name}) — cropped CT"
        )
    if not aligned:
        lines.extend([
            "",
            "## Suggested fix when alignment fails",
            "",
            "1. Inspect `gt_voxelized.voxelize.json`: compare `mesh_world_bounds` "
            "to `reference_origin + reference_dims * reference_spacing`.",
            "2. If the mesh bbox is in millimetres but the CT origin/spacing is "
            "in micrometres (or vice versa), the mesh may need a 1000× scale.",
            "3. If only the *sign* of one axis differs, the mesh may be exported "
            "in **LPS** while the CT is in **RAS** (or vice versa); flip "
            "X and Y signs of the mesh.",
            "4. As a last resort, re-export the PLY from MorphoSource using the "
            "CT's coordinate system, or run the legacy Slicer voxelizer "
            "(`--voxelize-backend slicer`) which handles some transforms "
            "automatically.",
        ])
    return "\n".join(lines) + "\n"


def _render_report(ct_id: str, gt_id: str, goal: str,
                   ct_pick: FilePick, mesh_pick: FilePick,
                   voxelize_result: dict, nni_result: dict,
                   metrics: dict, pair_dir: Path,
                   max_steps: int) -> str:
    dice = metrics.get("dice")
    iou = metrics.get("iou")
    voxel_pred = metrics.get("voxel_count_pred", 0)
    voxel_gt = metrics.get("voxel_count_gt", 0)
    n_steps = nni_result.get("n_prompts", nni_result.get("steps", 0))

    lines = [
        f"# nnInteractive vs MorphoSource GT — `{ct_id}` vs `{gt_id}`",
        "",
        f"**Goal:** {goal}  ",
        f"**Max LLM steps:** {max_steps}  ",
        f"**Steps used:** {n_steps}  ",
        "",
        "## Inputs",
        "",
        f"- **CT volume:** [`{ct_id}`](https://www.morphosource.org/concern/media/{ct_id})  ",
        f"  file: `{ct_pick.path.name}` ({ct_pick.size:,} bytes)",
        f"- **GT mesh:**  [`{gt_id}`](https://www.morphosource.org/concern/media/{gt_id})  ",
        f"  file: `{mesh_pick.path.name}` ({mesh_pick.size:,} bytes)",
        "",
        "## Voxelization of GT mesh",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Reference dims | {voxelize_result.get('reference_dims', 'N/A')} |",
        f"| Reference spacing (mm) | {voxelize_result.get('reference_spacing_xyz', 'N/A')} |",
        f"| GT foreground voxels | {voxelize_result.get('foreground_voxels', 'N/A'):,} |",
        f"| GT volume (mm³) | {voxelize_result.get('foreground_volume_mm3', 'N/A')} |",
        "",
        "## Comparison metrics",
        "",
    ]

    # Re-render the metrics table from segmentation_metrics — we can't easily
    # rebuild SegMetrics here (different env), so build it inline.
    metric_rows = [
        ("Dice", f"**{dice:.4f}**" if dice is not None else "N/A"),
        ("IoU (Jaccard)", f"{iou:.4f}" if iou is not None else "N/A"),
        ("Precision", f"{metrics.get('precision', 0):.4f}"),
        ("Recall (sensitivity)", f"{metrics.get('recall', 0):.4f}"),
        ("Volume diff", f"{metrics.get('volume_difference_pct', 0):.2f} %"),
        ("Voxels pred / GT", f"{voxel_pred:,} / {voxel_gt:,}"),
        ("Hausdorff (max, mm)", f"{metrics.get('hausdorff_mm', 'N/A')}"),
        ("Hausdorff (95-pct, mm)", f"{metrics.get('hausdorff_95_mm', 'N/A')}"),
        ("Mean surface distance (mm)",
            f"{metrics.get('average_surface_dist_mm', 'N/A')}"),
        ("Centroid distance (mm)", f"{metrics.get('centroid_distance_mm', 'N/A')}"),
    ]
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for k, v in metric_rows:
        lines.append(f"| {k} | {v} |")

    lines.extend([
        "",
        "## Visual comparison",
        "",
        "Volume only / GT (blue) / Prediction (orange):",
        "",
        "![overlay](overlay.png)",
        "",
        "## Files",
        "",
        f"- [`gt_voxelized.nii.gz`](gt_voxelized.nii.gz) — GT mesh rasterized onto the CT grid",
        f"- [`nninteractive/{ct_id}_nni_labelmap.nii.gz`](nninteractive/{ct_id}_nni_labelmap.nii.gz) — nnInteractive prediction",
        f"- [`metrics.json`](metrics.json) — full metrics payload",
        f"- [`nninteractive/{ct_id}_nni_report.md`](nninteractive/{ct_id}_nni_report.md) — paint-loop step trace",
    ])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args():
    p = argparse.ArgumentParser(
        description="Compare nnInteractive against a MorphoSource GT segmentation"
    )
    p.add_argument("--ct-media-id", default="",
                   help="MorphoSource media ID of the unsegmented CT volume")
    p.add_argument("--gt-media-id", default="",
                   help="MorphoSource media ID of the segmented derivative mesh")
    p.add_argument("--goal", default="",
                   help="Plain-English target for the paint loop, "
                        "e.g. 'Segment the cranial bone'")
    p.add_argument("--output-dir", default="/tmp/nni_compare",
                   help="Where to write outputs (default: /tmp/nni_compare)")
    p.add_argument("--max-steps", type=int, default=12,
                   help="Max LLM iterations for the paint loop")
    p.add_argument("--auto-discover", action="store_true",
                   help="Auto-pick the first viable CT↔mesh pair via "
                        "find_segmentation_pairs (--query controls the search)")
    p.add_argument("--query", default="skull mesh",
                   help="Search query when --auto-discover is set")
    p.add_argument("--require-taxonomy", default="",
                   help="Optional taxonomy filter for --auto-discover")
    p.add_argument("--voxelize-backend", default="auto",
                   choices=["auto", "slicer", "vtk"],
                   help="GT-mesh voxelization backend. 'vtk' is pure-Python "
                        "and works without a display server. 'auto' tries "
                        "Slicer first and falls back to VTK on failure.")
    p.add_argument("--crop-around-mesh-mm", type=float, default=0.0,
                   help="If >0, crop the CT to the GT mesh bbox + margin "
                        "(in mm) before running the paint loop. Useful for "
                        "small parts (e.g. a stapes inside a whole-head CT).")
    p.add_argument("--max-voxel-axis", type=int, default=0,
                   help="If >0, resample the (cropped) CT + voxelized GT so "
                        "no axis exceeds this many voxels. CT uses linear "
                        "interpolation; GT labelmap uses nearest-neighbour. "
                        "Keeps fixture sizes manageable on whole-skull "
                        "specimens. Default 0 (disabled).")
    p.add_argument("--align-mesh-to-ct", default="",
                   choices=["", "centroid", "auto"],
                   help="Apply a translation to the mesh so it overlaps the "
                        "CT in world coordinates before crop/voxelize. "
                        "'centroid' always aligns bbox centroids; 'auto' "
                        "skips when bboxes already overlap. Necessary for "
                        "derivative-mesh projects (358382, etc) whose .ply "
                        "files are in the modeller's local frame.")
    p.add_argument("--preset", default="", choices=[""] + list(PRESETS.keys()),
                   help="Pre-canned test pair. Overrides individual fields "
                        "but per-flag arguments still win if explicitly set.")
    p.add_argument("--skip-paint-loop", action="store_true",
                   help="Stop after voxelizing the GT mesh and emit an "
                        "alignment report (alignment_report.md). Useful as a "
                        "dry run before spending OpenAI quota on the loop.")
    p.add_argument("--export-fixture-dir", default="",
                   help="After a successful end-to-end run, copy the small "
                        "subset of files needed for cached fixture-based "
                        "re-runs (ct.nii.gz, gt_voxelized.nii.gz, pred.nii.gz, "
                        "baseline_metrics.json, fixture.json) into this dir. "
                        "Used by the CI pipeline to publish a small "
                        "`nninteractive-fixtures` artifact alongside the "
                        "full run output.")
    p.add_argument("--from-fixture", default="",
                   help="Run the comparison using a cached fixture directory "
                        "(skips download, TIFF, crop, voxelize). The dir must "
                        "contain ct.nii.gz, gt_voxelized.nii.gz, and "
                        "fixture.json. Add --pred-from-fixture to also skip "
                        "the paint loop.")
    p.add_argument("--pred-from-fixture", default="",
                   help="When using --from-fixture, load this prediction "
                        "labelmap instead of running the paint loop. Defaults "
                        "to <fixture_dir>/pred.nii.gz if present. Enables a "
                        "no-GPU, no-OpenAI smoke test of the comparison "
                        "pipeline (metrics + overlay + report).")
    p.add_argument("--assert-dice", type=float, default=None,
                   help="Fail (exit 3) if the final dice < this floor.")
    p.add_argument("--baseline-metrics", default="",
                   help="Compare the run's metrics against this baseline "
                        "metrics.json and fail (exit 3) if dice or IoU drop "
                        "by more than --regression-tol vs the baseline.")
    p.add_argument("--regression-tol", type=float, default=0.01,
                   help="Allowed absolute dice/iou regression vs "
                        "--baseline-metrics (default: 0.01)")
    return p.parse_args()


def _apply_regression_gates(result: dict, args, log_) -> int:
    """Inspect ``result`` and apply ``--assert-dice`` / ``--baseline-metrics``.

    Returns the final exit code. 0 = success, 1 = run failed, 3 = run
    succeeded but a regression gate was tripped.
    """
    if not result.get("success"):
        return 1
    metrics = result.get("metrics") or {}
    baseline_path = Path(args.baseline_metrics) if args.baseline_metrics else None
    passed, msgs = evaluate_regression(
        metrics=metrics,
        baseline_metrics_path=baseline_path,
        assert_dice=args.assert_dice,
        regression_tol=args.regression_tol,
    )
    for m in msgs:
        log_.info("regression-gate: %s", m)
    return 0 if passed else 3


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    args = _parse_args()

    # ---- Fast path: cached fixture run (PR smoke test) ----
    if args.from_fixture:
        fixture_dir = Path(args.from_fixture)
        pred_override = Path(args.pred_from_fixture) if args.pred_from_fixture \
            else None
        log.info("Running comparison from fixture dir %s (pred override: %s)",
                 fixture_dir, pred_override)
        result = run_comparison_from_fixture(
            fixture_dir=fixture_dir,
            output_dir=Path(args.output_dir),
            pred_labelmap=pred_override,
            goal=args.goal.strip() or None,
            max_steps=args.max_steps if args.max_steps != 12 else None,
            skip_paint_loop=args.skip_paint_loop,
        )
        print(json.dumps({k: v for k, v in result.items() if k != "metrics"},
                         indent=2, default=str))
        return _apply_regression_gates(result, args, log)

    ct_id = args.ct_media_id.strip()
    gt_id = args.gt_media_id.strip()
    goal = args.goal.strip()
    max_steps = args.max_steps
    voxelize_backend = args.voxelize_backend
    crop_mm = args.crop_around_mesh_mm

    if args.preset:
        preset = PRESETS[args.preset]
        log.info("Applying preset %r: %s", args.preset, preset)
        ct_id = ct_id or preset["ct_media_id"]
        gt_id = gt_id or preset["gt_media_id"]
        if not goal:
            goal = preset["goal"]
        if max_steps == 12:  # parser default
            max_steps = preset["max_steps"]
        if voxelize_backend == "auto":
            voxelize_backend = preset["voxelize_backend"]
        if crop_mm == 0.0:
            crop_mm = preset["crop_around_mesh_mm"]

    if args.auto_discover and (not ct_id or not gt_id):
        from find_segmentation_pairs import find_pairs
        log.info("Auto-discovery enabled — searching MorphoSource…")
        pairs = find_pairs(query=args.query, max_pairs=1,
                           require_taxonomy=args.require_taxonomy)
        if not pairs:
            log.error("No viable open-download CT↔mesh pair found for query=%r",
                      args.query)
            return 2
        pair = pairs[0]
        ct_id = pair.ct["media_id"]
        gt_id = pair.mesh["media_id"]
        log.info("Auto-picked pair: CT=%s GT=%s (specimen %s — %s)",
                 ct_id, gt_id, pair.physical_object_id, pair.taxonomy)

    if not ct_id or not gt_id:
        log.error("Both --ct-media-id and --gt-media-id are required "
                  "(or use --auto-discover, or --preset).")
        return 2

    if not goal and not args.skip_paint_loop:
        log.error("--goal is required (e.g. \"Segment the cranial bone\"). "
                  "Use --preset for a default goal on a known pair, or "
                  "--skip-paint-loop for an alignment-only dry run.")
        return 2

    log.info(
        "Running comparison: CT=%s GT=%s goal=%r max_steps=%d "
        "backend=%s crop_mm=%.1f skip_paint=%s",
        ct_id, gt_id, goal or "(none — skip-paint-loop)",
        max_steps, voxelize_backend, crop_mm, args.skip_paint_loop,
    )

    result = run_comparison(
        ct_media_id=ct_id,
        gt_media_id=gt_id,
        goal=goal,
        output_dir=Path(args.output_dir),
        max_steps=max_steps,
        voxelize_backend=voxelize_backend,
        crop_around_mesh_mm=crop_mm,
        max_voxel_axis=args.max_voxel_axis,
        align_mesh_to_ct=args.align_mesh_to_ct,
        skip_paint_loop=args.skip_paint_loop,
        export_fixture_dir=(Path(args.export_fixture_dir)
                            if args.export_fixture_dir else None),
    )
    print(json.dumps({k: v for k, v in result.items() if k != "metrics"},
                     indent=2, default=str))
    return _apply_regression_gates(result, args, log)


if __name__ == "__main__":
    sys.exit(main())
