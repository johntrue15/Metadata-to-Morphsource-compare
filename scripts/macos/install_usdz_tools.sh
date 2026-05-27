#!/usr/bin/env bash
# Install Apple-compatible USDZ CLI tools on macOS (for iMessage / AR Quick Look).
#
# Uses:
#   - pip: usd-core (Pixar OpenUSD Python bindings)
#   - git: niw/usdzconvert (patched Apple usdzconvert for modern Python + GLB)
#
# Reality Converter (GUI) is optional — same engine family. This script gives
# you a fully scriptable GLB/OBJ → USDZ path without the App Store download.
#
#   bash scripts/macos/install_usdz_tools.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOLS_DIR="${USDZ_TOOLS_DIR:-$REPO_ROOT/.local/usdzconvert}"
USDZ_REPO="${USDZ_CONVERT_REPO:-https://github.com/niw/usdzconvert.git}"

log() { printf '[install-usdz] %s\n' "$*"; }
die() { printf '[install-usdz] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "macOS only"

command -v python3 >/dev/null 2>&1 || die "python3 required"
command -v git >/dev/null 2>&1 || die "git required"

log "Installing usd-core (OpenUSD Python)…"
python3 -m pip install --user -q 'usd-core>=24.0'

if [[ -d "$TOOLS_DIR/.git" ]]; then
  log "Updating usdzconvert at $TOOLS_DIR…"
  git -C "$TOOLS_DIR" pull --ff-only || true
else
  log "Cloning usdzconvert → $TOOLS_DIR…"
  mkdir -p "$(dirname "$TOOLS_DIR")"
  git clone --depth 1 "$USDZ_REPO" "$TOOLS_DIR"
fi

CONVERTER="$TOOLS_DIR/usdzconvert"
[[ -x "$CONVERTER" ]] || chmod +x "$CONVERTER"

log "Smoke test…"
python3 "$CONVERTER" --help >/dev/null 2>&1 || python3 "$CONVERTER" -h >/dev/null

cat <<EOF

USDZ tools ready.

  Converter : $CONVERTER
  Export    : bash scripts/dev/export_imessage_usdz.sh

Convert manually:
  python3 $CONVERTER input.glb output.usdz

Optional GUI (drag-and-drop): install Apple's Reality Converter from
  https://developer.apple.com/augmented-reality/resources/
  (same USDZ output; not required for the CLI path above)

Share in Messages: drag the .usdz into a chat — recipients get AR Quick Look.
Preview on Mac: select the file and press Space (Quick Look).
EOF
