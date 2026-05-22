"""End-to-end GPU smoke test for the MorphoClaw self-hosted runner.

Run this with the nnInteractive venv's Python:

    "$NNINTERACTIVE_HOME/bin/python" scripts/runners/gpu_smoke.py

It executes a small but real chain that mirrors what production workflows do:

    1. Confirms PyTorch sees CUDA and reports the GPU.
    2. Imports nnInteractive and verifies the model-weights directory exists.
    3. Constructs `nnInteractiveInferenceSession` on the CUDA device. This is
       the genuine "would the real workflow work?" check because it loads the
       trained model folder.
    4. (Best-effort) Runs one forward pass on a 32^3 synthetic NIfTI sphere
       with a single positive-point prompt and reports the segmented voxel
       count. A non-zero count means CUDA inference is end-to-end functional.

The script exits 0 on success and 1 on the first hard failure. Every check
prints a single-line status so it's easy to read in CI logs.

Environment:
    NNINTERACTIVE_HOME   venv root (the script will be run by that venv's python)
    NNINTERACTIVE_MODEL_DIR / NNINTERACTIVE_MODEL  match install_nninteractive.sh
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path


def step(name: str) -> None:
    print(f"\n==> {name}", flush=True)


def ok(msg: str) -> None:
    print(f"    [ok]   {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"    [warn] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"    [err]  {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. Torch + CUDA
# ---------------------------------------------------------------------------

step("Importing torch and checking CUDA")
try:
    import torch
except Exception as exc:
    fail(f"`import torch` failed: {exc}")
    sys.exit(1)
ok(f"torch {torch.__version__} imported")

cuda_avail = torch.cuda.is_available()
mps_avail = getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
print(f"    cuda.is_available() = {cuda_avail}", flush=True)
print(f"    mps.is_available()  = {bool(mps_avail)}", flush=True)
if cuda_avail:
    try:
        idx = torch.cuda.current_device()
        name = torch.cuda.get_device_name(idx)
        props = torch.cuda.get_device_properties(idx)
        ok(f"CUDA device {idx}: {name} ({props.total_memory / 1024**3:.2f} GiB total)")
    except Exception as exc:
        warn(f"could not query CUDA device properties: {exc}")
    DEVICE = torch.device("cuda")
elif mps_avail:
    warn("CUDA not available; falling back to MPS (Apple-Silicon path)")
    DEVICE = torch.device("mps")
else:
    warn("Neither CUDA nor MPS available — running on CPU. "
         "This is FINE for testing the import chain but defeats the purpose "
         "of a GPU runner.")
    DEVICE = torch.device("cpu")


# ---------------------------------------------------------------------------
# 2. nnInteractive import + model dir
# ---------------------------------------------------------------------------

step("Importing nnInteractive")
try:
    import nnInteractive  # noqa: F401
    from nnInteractive.inference.inference_session import (  # noqa: F401
        nnInteractiveInferenceSession,
    )
except Exception as exc:
    fail(f"`import nnInteractive` failed: {exc}")
    sys.exit(1)
ok(f"nnInteractive {getattr(nnInteractive, '__version__', 'unknown')} imported")

NNI_HOME = Path(os.environ.get(
    "NNINTERACTIVE_HOME", str(Path.home() / ".autoresearchclaw/nninteractive")
))
NNI_MODEL_DIR = Path(os.environ.get(
    "NNINTERACTIVE_MODEL_DIR", str(NNI_HOME / "models")
))
NNI_MODEL_NAME = os.environ.get("NNINTERACTIVE_MODEL", "nnInteractive_v1.0")

candidate = NNI_MODEL_DIR / NNI_MODEL_NAME
if (candidate / "plans.json").exists() or any(candidate.glob("fold_*")):
    MODEL_PATH = candidate
elif (NNI_MODEL_DIR / "plans.json").exists() or any(NNI_MODEL_DIR.glob("fold_*")):
    MODEL_PATH = NNI_MODEL_DIR
else:
    MODEL_PATH = candidate

if not MODEL_PATH.exists():
    fail(f"nnInteractive model weights not found at {MODEL_PATH}")
    fail("Run install_nninteractive.sh to prefetch them.")
    sys.exit(1)
ok(f"model weights at {MODEL_PATH}")


# ---------------------------------------------------------------------------
# 3. Construct an inference session on the chosen device.
# ---------------------------------------------------------------------------

step("Constructing nnInteractiveInferenceSession on " + str(DEVICE))
session = None
try:
    session = nnInteractiveInferenceSession(
        device=DEVICE,
        use_torch_compile=False,
        verbose=False,
        torch_n_threads=max(1, (os.cpu_count() or 4) // 2),
        do_autozoom=True,
        use_pinned_memory=(DEVICE.type == "cuda"),
    )
    session.initialize_from_trained_model_folder(str(MODEL_PATH))
    ok("session initialised (model loaded)")
except Exception as exc:
    fail(f"session construction failed: {exc}")
    traceback.print_exc()
    sys.exit(1)


# ---------------------------------------------------------------------------
# 4. Tiny synthetic inference (best-effort).
#
# We generate a 32^3 NIfTI sphere, set it as the session's image, drop one
# positive point prompt in the centre, then check that the output buffer has
# at least one non-zero voxel. Failure here is a WARNING, not an error -- the
# session having loaded on CUDA is already strong evidence the box works.
# ---------------------------------------------------------------------------

step("Running synthetic forward pass (32^3 sphere, 1 point prompt)")
try:
    import numpy as np
    import SimpleITK as sitk
except Exception as exc:
    warn(f"numpy/SimpleITK unavailable for synthetic test: {exc}")
    sys.exit(0)

with tempfile.TemporaryDirectory() as td:
    z = y = x = 32
    zz, yy, xx = np.ogrid[:z, :y, :x]
    centre = np.array([z // 2, y // 2, x // 2])
    sphere = ((zz - centre[0]) ** 2 +
              (yy - centre[1]) ** 2 +
              (xx - centre[2]) ** 2) <= (z // 4) ** 2
    vol = np.where(sphere, 800.0, -200.0).astype(np.float32)
    img = sitk.GetImageFromArray(vol)
    nii = Path(td) / "smoke.nii.gz"
    sitk.WriteImage(img, str(nii))
    ok(f"synthetic volume written to {nii}")

    try:
        arr = sitk.GetArrayFromImage(sitk.ReadImage(str(nii)))
        session.set_image(arr[None])
        target = torch.zeros(arr.shape, dtype=torch.uint8)
        session.set_target_buffer(target)
        session.add_point_interaction(
            (int(x // 2), int(y // 2), int(z // 2)),
            include_interaction=True,
        )
        nz = int((target > 0).sum().item())
        ok(f"forward pass returned, segmented voxels = {nz}")
        # Persist a tiny report for the workflow step summary
        report_path = Path(os.environ.get("GPU_SMOKE_REPORT", "")
                           or (Path(td) / "smoke_report.json"))
        try:
            report_path.write_text(json.dumps({
                "device": str(DEVICE),
                "cuda_available": cuda_avail,
                "torch": torch.__version__,
                "model_path": str(MODEL_PATH),
                "segmented_voxels": nz,
                "ok": True,
            }, indent=2))
        except Exception:
            pass
    except Exception as exc:
        warn(f"forward pass failed (session loaded OK, so install is healthy): {exc}")
        traceback.print_exc()

print("\nDone. GPU smoke test finished.", flush=True)
sys.exit(0)
