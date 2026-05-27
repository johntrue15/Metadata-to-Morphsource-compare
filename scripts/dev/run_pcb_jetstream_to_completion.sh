#!/usr/bin/env bash
# Continue PCB bright-seed on Jetstream until candidates are exhausted.
# Does NOT reset segmentation — picks up after a prior 100-click (or any) run.
#
# If Slicer :2016 stops responding, restart Web Server in the Guacamole desktop
# (Modules → Servers → Web Server → Stop → Start, with "Slicer API exec" enabled).
#
#   bash scripts/dev/run_pcb_jetstream_to_completion.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

VOLUME="${PCB_VOLUME_NAME:-pcb_ti_jetstream}"
MAX_STEPS="${MAX_STEPS:-10000}"
OUT_DIR="${OUT_DIR:-runs/pcb_jetstream_to_completion_$(date +%Y%m%dT%H%M%S)}"
mkdir -p "$OUT_DIR"

echo "==> Continue $VOLUME on ${SLICER_WEBSERVER_URL:-?}"
echo "==> max_steps=$MAX_STEPS  out=$OUT_DIR"

python3 .github/scripts/slicer_remote_bright_seed.py \
  --volume "$VOLUME" \
  --max-steps "$MAX_STEPS" \
  --no-stop-rules \
  --no-screenshots \
  --skip-remote-env \
  --skip-failed-steps \
  --label pcb_ti_completion \
  --out-dir "$OUT_DIR" \
  2>&1 | tee "$OUT_DIR/run.log"

echo "==> Done: $OUT_DIR"
echo "    Re-run the same script to continue if stop_reason is max_steps."
echo "    Slicer keeps the mask; do not use --reset-first."
