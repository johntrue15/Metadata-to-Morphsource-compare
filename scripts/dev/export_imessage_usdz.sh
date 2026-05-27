#!/usr/bin/env bash
# Build a native iMessage / AR Quick Look USDZ from a GLB (or labelmap NIfTI).
#
# Prerequisites (once):
#   bash scripts/macos/install_usdz_tools.sh
#
# From existing GLB:
#   bash scripts/dev/export_imessage_usdz.sh \\
#     --glb runs/colors_skull_viewer/crotalus_skull_bright.glb
#
# From labelmap (runs labelmap_to_glb.py first):
#   bash scripts/dev/export_imessage_usdz.sh \\
#     --labelmap runs/colors_skull_viewer/composite.nii.gz \\
#     --name crotalus_skull_bright

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

TOOLS_DIR="${USDZ_TOOLS_DIR:-$REPO_ROOT/.local/usdzconvert}"
CONVERTER="$TOOLS_DIR/usdzconvert"
OUT_DIR="${OUT_DIR:-runs/colors_skull_viewer}"
NAME="${NAME:-crotalus_skull_bright}"
GLB=""
LABELMAP=""
DECIMATE="${DECIMATE:-0.88}"

usage() {
  sed -n '2,20p' "$0"
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --glb) GLB="$2"; shift 2 ;;
    --labelmap) LABELMAP="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --decimate) DECIMATE="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1"; usage ;;
  esac
done

mkdir -p "$OUT_DIR"

if [[ ! -x "$CONVERTER" ]]; then
  echo "USDZ tools missing — run: bash scripts/macos/install_usdz_tools.sh"
  exit 1
fi

python3 -c "from pxr import Usd" 2>/dev/null || {
  echo "usd-core missing — run: bash scripts/macos/install_usdz_tools.sh"
  exit 1
}

if [[ -n "$LABELMAP" ]]; then
  GLB="${OUT_DIR}/${NAME}.glb"
  echo "==> Labelmap → GLB ($GLB)…"
  python3 .github/scripts/labelmap_to_glb.py \
    --input "$LABELMAP" \
    --output "$GLB" \
    --decimate "$DECIMATE" \
    --manifest "${OUT_DIR}/glb_manifest.json"
fi

if [[ -z "$GLB" ]]; then
  GLB="${OUT_DIR}/${NAME}.glb"
fi
[[ -f "$GLB" ]] || { echo "GLB not found: $GLB (pass --glb or --labelmap)"; exit 1; }

USDZ="${OUT_DIR}/${NAME}.usdz"
echo "==> GLB → USDZ ($USDZ)…"
python3 "$CONVERTER" "$GLB" "$USDZ"

echo ""
echo "================================================================"
echo "iMessage-ready USDZ"
echo "  File    : $USDZ ($(du -h "$USDZ" | awk '{print $1}'))"
echo "  Preview : open -a Preview \"$USDZ\"   # or press Space in Finder"
echo "  Share   : drag $USDZ into Messages"
echo "================================================================"

# Reveal in Finder for easy drag-to-Messages
open -R "$USDZ"
