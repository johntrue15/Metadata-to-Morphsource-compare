#!/usr/bin/env bash
# Continue PCB bright-seed ON the Jetstream box (Guacamole terminal).
# Uses localhost :2016 — bypasses the Apache/Exosphere ~60s proxy timeout
# that causes HTTP 504 on long nnInteractive clicks from your Mac.
#
# Prerequisite: 3D Slicer open with Web Server on :2016 (Slicer API exec ON),
# volume pcb_ti_jetstream loaded, existing segmentation (~201 segments) intact.
# Do NOT reset segmentation before running.
#
#   bash scripts/dev/run_pcb_on_jetstream_localhost.sh
#
# Optional env:
#   MAX_STEPS=10000
#   OUT_DIR=~/pcb_runs/to_completion_$(date +%Y%m%dT%H%M%S)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

export SLICER_WEBSERVER_URL="${SLICER_WEBSERVER_URL:-http://127.0.0.1:2016/}"
VOLUME="${PCB_VOLUME_NAME:-pcb_ti_jetstream}"
MAX_STEPS="${MAX_STEPS:-10000}"
OUT_DIR="${OUT_DIR:-${HOME}/pcb_runs/to_completion_$(date +%Y%m%dT%H%M%S)}"
mkdir -p "$OUT_DIR"

echo "==> Local Slicer: $SLICER_WEBSERVER_URL"
echo "==> Volume:       $VOLUME"
echo "==> max_steps:    $MAX_STEPS"
echo "==> out:          $OUT_DIR"

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
