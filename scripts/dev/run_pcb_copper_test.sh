#!/usr/bin/env bash
# PCB two-phase test on Jetstream:
#   1) export ~200 bright-seed segments as noise reference
#   2) reset + LLM copper-layer segmentation (single large segment)
#
# Run from Mac (needs OPENAI_API_KEY in .env) or on-box via localhost:
#   SLICER_WEBSERVER_URL=http://127.0.0.1:2016/ bash scripts/dev/run_pcb_copper_test.sh
#
# Excludes Segment_232 (manual orange copper, 353k voxels) from noise union.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

OUT_DIR="${OUT_DIR:-runs/pcb_copper_test_$(date +%Y%m%dT%H%M%S)}"
MAX_STEPS="${MAX_STEPS:-10}"
PHASE="${PHASE:-full}"

echo "==> PCB copper test  phase=$PHASE  out=$OUT_DIR"
echo "==> Slicer: ${SLICER_WEBSERVER_URL:-?}"

python3 .github/scripts/slicer_remote_pcb_copper.py \
  --phase "$PHASE" \
  --volume pcb_ti_jetstream \
  --exclude-segment Segment_232 \
  --max-steps "$MAX_STEPS" \
  --out-dir "$OUT_DIR"

echo "==> Done: $OUT_DIR"
