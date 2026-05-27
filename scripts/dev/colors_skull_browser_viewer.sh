#!/usr/bin/env bash
# Download Colors-of-Skull bright-seed composite from Jetstream, export GLB,
# and serve a browser viewer on this Mac.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

RUN_DIR="${RUN_DIR:-runs/colors_skull_bright_20260527T125014}"
REMOTE_COMPOSITE="${REMOTE_COMPOSITE:-/media/volume/MyData/MorphoClaw/${RUN_DIR}/artifacts/composite.nii.gz}"
VIEWER_DIR="${VIEWER_DIR:-runs/colors_skull_viewer}"
PORT="${PORT:-8765}"
DECIMATE="${DECIMATE:-0.88}"

mkdir -p "$VIEWER_DIR"

echo "==> Installing local deps (nibabel vtk trimesh) if needed…"
python3 -m pip install --user -q SimpleITK nibabel "vtk>=9.2" "trimesh>=4.0" numpy

echo "==> Downloading composite from Jetstream…"
python3 - <<PY
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(".github/scripts").resolve()))
from export_session import download_remote_file

base = os.environ.get("SLICER_WEBSERVER_URL", "").strip()
if not base:
    sys.exit("Set SLICER_WEBSERVER_URL in .env")
dest = Path("${VIEWER_DIR}") / "composite.nii.gz"
download_remote_file(base, "${REMOTE_COMPOSITE}", dest)
print(f"saved {dest} ({dest.stat().st_size:,} bytes)")
PY

GLB="${VIEWER_DIR}/crotalus_skull_bright.glb"
echo "==> Converting labelmap -> GLB (decimate=${DECIMATE})…"
python3 .github/scripts/labelmap_to_glb.py \
  --input "${VIEWER_DIR}/composite.nii.gz" \
  --output "$GLB" \
  --decimate "$DECIMATE" \
  --manifest "${VIEWER_DIR}/glb_manifest.json"

cat > "${VIEWER_DIR}/index.html" <<'HTML'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Crotalus Skull — Bright Seed GLB</title>
  <script type="module" src="https://ajax.googleapis.com/ajax/libs/model-viewer/3.5.0/model-viewer.min.js"></script>
  <style>
    html, body { margin: 0; height: 100%; background: #0f1117; color: #e6edf3; font-family: system-ui, sans-serif; }
    header { padding: 0.75rem 1rem; border-bottom: 1px solid #30363d; }
    model-viewer { width: 100%; height: calc(100% - 52px); background: radial-gradient(circle at 30% 20%, #1a2332, #0f1117); }
  </style>
</head>
<body>
  <header>
    <strong>Colors of Skull</strong> — Crotalus bright-seed segments (GLB)
  </header>
  <model-viewer
    src="crotalus_skull_bright.glb"
    camera-controls
    touch-action="pan-y"
    auto-rotate
    shadow-intensity="1"
    exposure="1.1"
    alt="Crotalus skull segmentation mesh">
  </model-viewer>
</body>
</html>
HTML

echo ""
echo "================================================================"
echo "Viewer ready"
echo "  GLB   : $GLB"
echo "  Open  : http://127.0.0.1:${PORT}/"
echo "================================================================"
echo "Starting http.server on port ${PORT} (Ctrl+C to stop)…"
exec python3 -m http.server "$PORT" --directory "$VIEWER_DIR"
