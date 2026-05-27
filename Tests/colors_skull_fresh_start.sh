#!/usr/bin/env bash
# Crotalus clean slate on Jetstream: wait for Slicer, upload local CT, 100-click bright-seed.
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
export MORPHOCLAW_MIN_AVAILABLE_GB="${MORPHOCLAW_MIN_AVAILABLE_GB:-2.0}"
PY="${NNI_PY_BIN:-$(command -v python3)}"
MAX_STEPS="${MAX_STEPS:-100}"
FIXTURE="${FIXTURE:-data/sample/colors_of_skull_urls.json}"
LOCAL_CT="${LOCAL_CT:-data/sample/crotalus_skull_000445108_ct.nrrd}"
WAIT_ATTEMPTS="${WAIT_ATTEMPTS:-90}"
WAIT_SLEEP="${WAIT_SLEEP:-30}"
OUT="${OUT_DIR:-runs/crotalus_fresh_$(date +%Y%m%dT%H%M%S)}"

echo "=== Colors of Skull: Crotalus fresh start → $MAX_STEPS clicks ==="
echo "  SLICER_WEBSERVER_URL=$SLICER_WEBSERVER_URL"
echo "  local CT=$LOCAL_CT"
echo "  out=$OUT"
echo ""
echo "If Slicer is wedged: Guacamole → quit 3D Slicer, restart Web Server :2016,"
echo "  start nninteractive-slicer-server on :1527, then this script will proceed."
echo ""

echo "Waiting for Slicer web API (up to $((WAIT_ATTEMPTS * WAIT_SLEEP / 60)) min)…"
for ((i=1; i<=WAIT_ATTEMPTS; i++)); do
  if "$PY" -c "
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path('.github/scripts').resolve()))
from slicer_remote_bright_seed import post_python
from run_telemetry import PING_SLICER_SRC
u = os.environ['SLICER_WEBSERVER_URL'].rstrip('/')
post_python(u, PING_SLICER_SRC, timeout=20, retries=0)
" 2>/dev/null; then
    echo "  Slicer responsive (attempt $i)"
    break
  fi
  echo "  attempt $i/$WAIT_ATTEMPTS — not ready, sleep ${WAIT_SLEEP}s"
  if [[ $i -eq $WAIT_ATTEMPTS ]]; then
    echo "ERROR: Slicer never became reachable. Restart on Jetstream and re-run."
    exit 2
  fi
  sleep "$WAIT_SLEEP"
done

mkdir -p "$OUT"

"$PY" .github/scripts/jetstream_skull_fresh_start.py \
  --fixture "$FIXTURE" \
  --local-ct-path "$LOCAL_CT" \
  --max-steps "$MAX_STEPS" \
  --no-screenshots \
  --out-dir "$OUT" \
  "$@" \
  2>&1 | tee "$OUT/run.log"

echo ""
echo "Done. summary: $OUT/bright_seed/summary.json"
echo "Score vs GT later: make colors-skull-complete OUT_DIR=... --skip-load (when GT ready)"
