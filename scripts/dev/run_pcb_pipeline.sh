#!/usr/bin/env bash
# PCB processing: try local Dell GPU first, else prepare Jetstream bundle.
#
#   bash scripts/dev/run_pcb_pipeline.sh              # auto route + run
#   bash scripts/dev/run_pcb_pipeline.sh --force-local
#   bash scripts/dev/run_pcb_pipeline.sh --jetstream-only
#   bash scripts/dev/run_pcb_pipeline.sh --max-steps 50 --max-axis 384
#
# Local outputs:  .local/pcb_brightseed/<run>/
# Jetstream pack: .local/pcb_jetstream/bundle/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
NNI_PY="$NNI_HOME/bin/python"
BRIGHT_SEED="$REPO_ROOT/.github/scripts/nninteractive_bright_seed.py"
ROUTE_PY="$REPO_ROOT/.github/scripts/route_to_runner.py"
PREP_PY="$REPO_ROOT/scripts/dev/prepare_pcb_volume.py"
PREP_BUNDLE_PY="$REPO_ROOT/scripts/dev/prepare_pcb_jetstream_bundle.py"

MEDIA_ID="pcb_ti"
INPUT="$REPO_ROOT/.local/pcb_data/pcb_ti.nii.gz"
MAX_AXIS=384
MAX_STEPS=50
FORCE_LOCAL=0
JETSTREAM_ONLY=0
SKIP_PREP=0
SPACING_ARGS=()

usage() {
  sed -n '2,12p' "$0"
  echo ""
  echo "  --force-local       Run on Dell even if routing says Jetstream"
  echo "  --jetstream-only    Skip local attempt; only build Jetstream bundle"
  echo "  --skip-prep         Use existing .local/pcb_data/pcb_ti.nii.gz"
  echo "  --max-axis N        Decimation cap (default 384)"
  echo "  --max-steps N       Bright-seed click cap (default 50)"
  echo "  --spacing-xyz Sx Sy Sz"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-local) FORCE_LOCAL=1; shift ;;
    --jetstream-only) JETSTREAM_ONLY=1; shift ;;
    --skip-prep) SKIP_PREP=1; shift ;;
    --max-axis) MAX_AXIS="$2"; shift 2 ;;
    --max-steps) MAX_STEPS="$2"; shift 2 ;;
    --spacing-xyz) SPACING_ARGS=(--spacing-xyz "$2" "$3" "$4"); shift 4 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ ! -x "$NNI_PY" ]]; then
  echo "nnInteractive venv missing at $NNI_PY" >&2
  exit 1
fi

if [[ $SKIP_PREP -eq 0 ]] || [[ ! -f "$INPUT" ]]; then
  echo "==> Preparing PCB volume (max-axis=$MAX_AXIS)"
  PREP_CMD=("$NNI_PY" "$PREP_PY" --max-axis "$MAX_AXIS")
  [[ ${#SPACING_ARGS[@]} -gt 0 ]] && PREP_CMD+=("${SPACING_ARGS[@]}")
  "${PREP_CMD[@]}"
fi

if [[ ! -f "$INPUT" ]]; then
  echo "Missing volume: $INPUT" >&2
  exit 1
fi

build_jetstream_bundle() {
  echo "==> Building Jetstream bundle"
  "$NNI_PY" "$PREP_BUNDLE_PY" \
    --input "$INPUT" \
    --max-steps "$MAX_STEPS" \
    --max-axis-jetstream 512
}

run_local_bright_seed() {
  local out="$REPO_ROOT/.local/pcb_brightseed/run_$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$out"
  echo "==> Local bright-seed -> $out"
  "$NNI_PY" "$BRIGHT_SEED" \
    --input "$INPUT" \
    --output-dir "$out" \
    --media-id "$MEDIA_ID" \
    --autopilot \
    --no-previews \
    --min-segment-voxels 80 \
    --max-steps "$MAX_STEPS"
  local summary="$out/${MEDIA_ID}_nni_summary.json"
  if [[ -f "$summary" ]]; then
    echo "==> Run summary"
    "$NNI_PY" -c "
import json
from pathlib import Path
s = json.loads(Path('$summary').read_text())
print('  success:', s.get('success'))
print('  n_clicks:', s.get('n_clicks'))
print('  union voxels:', s.get('voxel_count'))
print('  stop:', s.get('stop_reason'))
print('  labelmap:', s.get('labelmap_path'))
"
  fi
  echo "Artifacts: $out"
}

if [[ $JETSTREAM_ONLY -eq 1 ]]; then
  build_jetstream_bundle
  exit 0
fi

RUNNER="dell"
if [[ $FORCE_LOCAL -eq 0 ]]; then
  ROUTE_JSON="$("$NNI_PY" "$ROUTE_PY" --input "$INPUT" --json 2>/dev/null || true)"
  if [[ -n "$ROUTE_JSON" ]]; then
    RUNNER="$(echo "$ROUTE_JSON" | "$NNI_PY" -c "import json,sys; print(json.load(sys.stdin).get('runner','dell'))")"
    echo "==> route_to_runner: $RUNNER"
    echo "$ROUTE_JSON" | "$NNI_PY" -m json.tool 2>/dev/null | head -20 || true
  fi
fi

if [[ "$RUNNER" == "jetstream" ]] && [[ $FORCE_LOCAL -eq 0 ]]; then
  echo "==> Volume too large for comfortable Dell GPU run; preparing Jetstream"
  build_jetstream_bundle
  exit 0
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "WARNING: no nvidia-smi — falling back to Jetstream bundle" >&2
  build_jetstream_bundle
  exit 0
fi

set +e
run_local_bright_seed
LOCAL_EC=$?
set -e

if [[ $LOCAL_EC -ne 0 ]]; then
  echo "==> Local run failed (exit $LOCAL_EC); preparing Jetstream bundle" >&2
  build_jetstream_bundle
  exit "$LOCAL_EC"
fi

echo "==> Local processing complete"
exit 0
