#!/usr/bin/env bash
# Continue bright-seed clicking on Jetstream until max_steps — NO reset (keeps existing segments).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export SLICER_WEBSERVER_URL="${SLICER_WEBSERVER_URL:-https://http-149-165-155-127-2016.proxy-js2-iu.exosphere.app/}"
PY="${NNI_PY_BIN:-$(command -v python3)}"
MAX_STEPS="${MAX_STEPS:-200}"
OUT="${OUT_DIR:-runs/tuatara_continue_$(date +%Y%m%dT%H%M%S)}"
VOLUME="${SLICER_VOLUME_NAME:-}"

code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 8 \
  "${SLICER_WEBSERVER_URL%/}/slicer/screenshot" || echo 000)
[[ "$code" == "200" ]] || { echo "ERROR: Slicer not up (HTTP $code)"; exit 2; }

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

echo "Volume: $VOLUME  max_steps: $MAX_STEPS  out: $OUT"
echo "NOT using --reset-first (keeping your existing segments)."

mkdir -p "$OUT"
"$PY" .github/scripts/slicer_remote_bright_seed.py \
  --volume "$VOLUME" \
  --max-steps "$MAX_STEPS" \
  --no-stop-rules \
  --no-screenshots \
  --label tuatara_continue \
  --out-dir "$OUT" \
  2>&1 | tee "$OUT/run.log"

echo "Done. summary: $OUT/summary.json"
