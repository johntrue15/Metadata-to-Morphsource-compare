#!/usr/bin/env bash
# Phase A only: keep clicking on Jetstream until MAX_STEPS (no reset, no GT needed).
# Compare vs mesh .ply afterward: make tuatara-score-100click (or tuatara-autocomplete-vs-gt
# runs phase B only if GT exists).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Prefer .env; fall back to JETSTREAM_PUBLIC_IP or the current instance IP.
_IP="${JETSTREAM_PUBLIC_IP:-149.165.155.127}"
export SLICER_WEBSERVER_URL="${SLICER_WEBSERVER_URL:-https://http-${_IP//./-}-2016.proxy-js2-iu.exosphere.app/}"
# Survive Exosphere ~60s proxy limits: retry 5xx and recover clicks after 504.
export MORPHOCLAW_HTTP_RETRIES="${MORPHOCLAW_HTTP_RETRIES:-8}"
export MORPHOCLAW_HTTP_RETRY_SLEEP="${MORPHOCLAW_HTTP_RETRY_SLEEP:-25}"
export MORPHOCLAW_STEP_TIMEOUT="${MORPHOCLAW_STEP_TIMEOUT:-180}"
# Pause clicks when Jetstream RAM is low; exit cleanly instead of wedging Slicer.
export MORPHOCLAW_MIN_AVAILABLE_GB="${MORPHOCLAW_MIN_AVAILABLE_GB:-2.0}"
export MORPHOCLAW_PING_TIMEOUT="${MORPHOCLAW_PING_TIMEOUT:-25}"
export MORPHOCLAW_PING_RETRIES="${MORPHOCLAW_PING_RETRIES:-3}"
export MORPHOCLAW_PING_RETRY_SLEEP="${MORPHOCLAW_PING_RETRY_SLEEP:-10}"
PY="${NNI_PY_BIN:-$(command -v python3)}"
MAX_STEPS="${MAX_STEPS:-200}"
OUT="${OUT_DIR:-runs/tuatara_click_to_completion_$(date +%Y%m%dT%H%M%S)}"
VOLUME="${SLICER_VOLUME_NAME:-}"

echo "SLICER_WEBSERVER_URL=$SLICER_WEBSERVER_URL"
# Lightweight ping — heavy CAPTURE_REMOTE_ENV can wedge an overloaded Slicer.
echo "Waiting for Slicer web API…"
if ! "$PY" -c "
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path('.github/scripts').resolve()))
from slicer_remote_bright_seed import post_python
from run_telemetry import PING_SLICER_SRC
u = os.environ['SLICER_WEBSERVER_URL'].rstrip('/')
retries = int(os.environ.get('MORPHOCLAW_PING_RETRIES', '8'))
delay = float(os.environ.get('MORPHOCLAW_PING_RETRY_SLEEP', '20'))
timeout = float(os.environ.get('MORPHOCLAW_PING_TIMEOUT', '25'))
for attempt in range(1, retries + 1):
    try:
        r = post_python(u, PING_SLICER_SRC, timeout=timeout, retries=0)
        if r.get('status') == 'ok':
            print(f'Slicer responsive (attempt {attempt})', flush=True)
            sys.exit(0)
    except Exception as e:
        print(f'  attempt {attempt}/{retries}: {e!r}', flush=True)
    if attempt < retries:
        time.sleep(delay)
sys.exit(1)
"; then
  echo "ERROR: Slicer web API not reachable at $SLICER_WEBSERVER_URL"
  echo "  Restart 3D Slicer on Jetstream (Guacamole) and ensure Web Server is on port 2016."
  echo "  (If you moved instances, update JETSTREAM_PUBLIC_IP / SLICER_WEBSERVER_URL in .env)"
  exit 2
fi

if [[ -z "$VOLUME" ]]; then
  VOLUME=$("$PY" -c "
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path('.github/scripts').resolve()))
from slicer_remote_score_scene import post_python, LIST_VOLUMES_SRC
r = post_python(os.environ['SLICER_WEBSERVER_URL'].rstrip('/'), LIST_VOLUMES_SRC, timeout=30)
vols = [v for v in (r.get('volumes') or []) if 'tuatara' in v.lower()]
print(vols[0] if vols else ((r.get('volumes') or [''])[0]))
")
fi
[[ -n "$VOLUME" ]] || { echo "ERROR: set SLICER_VOLUME_NAME"; exit 2; }

echo "Phase A: click to completion on Jetstream (no GT, no reset)"
echo "  volume=$VOLUME  max_steps=$MAX_STEPS  out=$OUT"
echo "  http_retries=$MORPHOCLAW_HTTP_RETRIES  retry_sleep=${MORPHOCLAW_HTTP_RETRY_SLEEP}s  step_timeout=${MORPHOCLAW_STEP_TIMEOUT}s"
echo "  min_avail_gb=$MORPHOCLAW_MIN_AVAILABLE_GB (exit when below)"
mkdir -p "$OUT"

# Does NOT pass --reset-first — existing segments are preserved.
# --skip-remote-env / --skip-volume-hash: avoid wedging an overloaded Slicer at startup.
"$PY" .github/scripts/slicer_remote_bright_seed.py \
  --volume "$VOLUME" \
  --max-steps "$MAX_STEPS" \
  --no-stop-rules \
  --no-screenshots \
  --skip-remote-env \
  --skip-volume-hash \
  --label tuatara_to_completion \
  --out-dir "$OUT" \
  2>&1 | tee "$OUT/run.log"

echo ""
echo "Phase A done. summary: $OUT/summary.json"
echo "When mesh voxelization finishes on the Mac, run Phase B:"
echo "  make tuatara-score-100click OUT_DIR=runs/tuatara_final_vs_gt"
