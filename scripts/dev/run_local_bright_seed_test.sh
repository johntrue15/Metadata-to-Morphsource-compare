#!/usr/bin/env bash
# Local GPU test: deterministic bright-seed clicks via nnInteractive.
#
# Presets:
#   impc  — small cached IMPC mouse NRRD (sanity check, ~minutes)
#   pcb   — stack PCB "TI tiff stack" -> NIfTI, downsample, then bright-seed
#
# Run from WSL (Dell GPU runner):
#   bash scripts/dev/run_local_bright_seed_test.sh
#   bash scripts/dev/run_local_bright_seed_test.sh --preset pcb --max-steps 20
#
# Env overrides:
#   NNINTERACTIVE_HOME  (default: ~/.autoresearchclaw/nninteractive)
#   PCB_SPACING_XYZ     e.g. "0.05 0.05 0.05" when known

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
NNI_PY="$NNI_HOME/bin/python"
BRIGHT_SEED="$REPO_ROOT/.github/scripts/nninteractive_bright_seed.py"

PRESET="impc"
MAX_STEPS=""
MAX_AXIS="384"
SPACING_ARGS=()
DRY_RUN=0
SKIP_PREP=0

usage() {
  sed -n '2,18p' "$0"
  echo ""
  echo "Options:"
  echo "  --preset impc|pcb     Volume source (default: impc)"
  echo "  --max-steps N         Cap clicks (default: 8 impc / 25 pcb)"
  echo "  --max-axis N          PCB downsample cap (default: 384)"
  echo "  --spacing-xyz Sx Sy Sz  PCB voxel spacing in mm"
  echo "  --dry-run             Print commands only"
  echo "  --skip-prep           Skip PCB TIFF stacking (volume already built)"
  echo "  -h, --help"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --preset) PRESET="$2"; shift 2 ;;
    --max-steps) MAX_STEPS="$2"; shift 2 ;;
    --max-axis) MAX_AXIS="$2"; shift 2 ;;
    --spacing-xyz) SPACING_ARGS=(--spacing-xyz "$2" "$3" "$4"); shift 4 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-prep) SKIP_PREP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ ! -x "$NNI_PY" ]]; then
  echo "nnInteractive python not found at $NNI_PY" >&2
  echo "Bootstrap: bash .github/scripts/install_nninteractive.sh" >&2
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "WARNING: nvidia-smi not found — GPU may be unavailable" >&2
else
  nvidia-smi -L || true
fi

case "$PRESET" in
  impc)
    INPUT="$REPO_ROOT/.local/impc_data/IMPC_sample_data.nrrd"
    MEDIA_ID="impc_mouse_local_test"
    OUT_DIR="$REPO_ROOT/.local/impc_brightseed/local_test_$(date +%Y%m%d_%H%M%S)"
    [[ -n "$MAX_STEPS" ]] || MAX_STEPS=8
    ;;
  pcb)
    INPUT="$REPO_ROOT/.local/pcb_data/pcb_ti.nii.gz"
    MEDIA_ID="pcb_ti_local_test"
    OUT_DIR="$REPO_ROOT/.local/pcb_brightseed/local_test_$(date +%Y%m%d_%H%M%S)"
    [[ -n "$MAX_STEPS" ]] || MAX_STEPS=25
    PREP_ARGS=(--max-axis "$MAX_AXIS")
    if [[ -n "${PCB_SPACING_XYZ:-}" ]]; then
      # shellcheck disable=SC2206
      read -r -a _sp <<< "$PCB_SPACING_XYZ"
      PREP_ARGS+=(--spacing-xyz "${_sp[@]}")
    fi
    if [[ ${#SPACING_ARGS[@]} -gt 0 ]]; then
      PREP_ARGS+=("${SPACING_ARGS[@]}")
    fi
    if [[ $SKIP_PREP -eq 0 ]]; then
      PREP_CMD=("$NNI_PY" "$REPO_ROOT/scripts/dev/prepare_pcb_volume.py" "${PREP_ARGS[@]}")
      echo "==> Preparing PCB volume"
      if [[ $DRY_RUN -eq 1 ]]; then
        printf '  '; printf '%q ' "${PREP_CMD[@]}"; echo
      else
        "${PREP_CMD[@]}"
      fi
    else
      echo "==> Skipping PCB prep (--skip-prep)"
    fi
    ;;
  *)
    echo "Unknown preset: $PRESET (use impc or pcb)" >&2
    exit 2
    ;;
esac

if [[ ! -f "$INPUT" ]]; then
  echo "Input volume missing: $INPUT" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

RUN_CMD=(
  "$NNI_PY" "$BRIGHT_SEED"
  --input "$INPUT"
  --output-dir "$OUT_DIR"
  --media-id "$MEDIA_ID"
  --autopilot
  --max-steps "$MAX_STEPS"
)

echo "==> Running bright-seed (preset=$PRESET, max_steps=$MAX_STEPS)"
echo "    input:  $INPUT"
echo "    output: $OUT_DIR"

if [[ $DRY_RUN -eq 1 ]]; then
  printf '  '; printf '%q ' "${RUN_CMD[@]}"; echo
  exit 0
fi

"${RUN_CMD[@]}"
EC=$?

SUMMARY="$OUT_DIR/${MEDIA_ID}_nni_summary.json"
if [[ ! -f "$SUMMARY" ]]; then
  SUMMARY="$OUT_DIR/${MEDIA_ID}_bright_summary.json"
fi
if [[ -f "$SUMMARY" ]]; then
  echo "==> Summary"
  "$NNI_PY" -c "
import json
from pathlib import Path
s = json.loads(Path('$SUMMARY').read_text())
print('  n_clicks:', s.get('n_clicks'))
print('  stop_reason:', s.get('stop_reason'))
print('  union_voxels:', s.get('union_voxels'))
print('  duration_s:', s.get('duration_s'))
"
fi
echo "Done (exit $EC). Artifacts in $OUT_DIR"
exit $EC
