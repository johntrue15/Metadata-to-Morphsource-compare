#!/usr/bin/env bash
# Clean-slate tuatara on Jetstream: clear scene, reload CT, bright-seed from zero.
# Saves per-step state + final NIfTI export under runs/tuatara_fresh_*.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

_IP="${JETSTREAM_PUBLIC_IP:-149.165.155.127}"
export SLICER_WEBSERVER_URL="${SLICER_WEBSERVER_URL:-https://http-${_IP//./-}-2016.proxy-js2-iu.exosphere.app/}"
export MORPHOCLAW_HTTP_RETRIES="${MORPHOCLAW_HTTP_RETRIES:-8}"
export MORPHOCLAW_HTTP_RETRY_SLEEP="${MORPHOCLAW_HTTP_RETRY_SLEEP:-25}"
export MORPHOCLAW_STEP_TIMEOUT="${MORPHOCLAW_STEP_TIMEOUT:-180}"
# Resource exit criteria (stop cleanly when Jetstream runs out of RAM).
export MORPHOCLAW_MIN_AVAILABLE_GB="${MORPHOCLAW_MIN_AVAILABLE_GB:-2.0}"
export MORPHOCLAW_PING_TIMEOUT="${MORPHOCLAW_PING_TIMEOUT:-25}"
export MORPHOCLAW_PING_RETRIES="${MORPHOCLAW_PING_RETRIES:-3}"
export MORPHOCLAW_PING_RETRY_SLEEP="${MORPHOCLAW_PING_RETRY_SLEEP:-10}"
PY="${NNI_PY_BIN:-$(command -v python3)}"
MAX_STEPS="${MAX_STEPS:-100}"
WAIT_ATTEMPTS="${WAIT_ATTEMPTS:-60}"
WAIT_SLEEP="${WAIT_SLEEP:-20}"
OUT="${OUT_DIR:-runs/tuatara_fresh_$(date +%Y%m%dT%H%M%S)}"

echo "=== Tuatara fresh start (from scratch) ==="
echo "  SLICER_WEBSERVER_URL=$SLICER_WEBSERVER_URL"
echo "  max_steps=$MAX_STEPS  min_avail_gb=$MORPHOCLAW_MIN_AVAILABLE_GB"
echo "  out=$OUT"
echo ""
echo "Jetstream: restart nninteractive-slicer-server on :1527 before expecting mask growth."
echo "  (You stopped it manually — clicks add 0 voxels until it is back.)"
echo ""

echo "Waiting for Slicer web API…"
for ((i=1; i<=WAIT_ATTEMPTS; i++)); do
  if "$PY" -c "
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path('.github/scripts').resolve()))
from slicer_remote_bright_seed import post_python
from run_telemetry import PING_SLICER_SRC
post_python(os.environ['SLICER_WEBSERVER_URL'].rstrip('/'), PING_SLICER_SRC, timeout=20, retries=0)
" 2>/dev/null; then
    echo "  Slicer responsive (attempt $i)"
    break
  fi
  echo "  attempt $i/$WAIT_ATTEMPTS — sleep ${WAIT_SLEEP}s"
  if [[ $i -eq $WAIT_ATTEMPTS ]]; then
    echo "ERROR: Slicer not reachable. Restart Web Server :2016 in Guacamole."
    exit 2
  fi
  sleep "$WAIT_SLEEP"
done

mkdir -p "$OUT"

"$PY" .github/scripts/jetstream_tuatara_fresh_start.py \
  --max-steps "$MAX_STEPS" \
  --no-screenshots \
  --out-dir "$OUT" \
  "$@" \
  2>&1 | tee "$OUT/run.log"

echo ""
echo "Artifacts:"
echo "  log       $OUT/run.log"
echo "  events    $OUT/bright_seed/events.jsonl"
echo "  summary   $OUT/bright_seed/summary.json"
echo "  labelmaps $OUT/bright_seed/artifacts/"
