#!/usr/bin/env bash
# Full Colors of Skull pilot: load Crotalus CT, bright-seed, Dice vs GT (Mac → Jetstream).
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
FIXTURE="${FIXTURE:-data/sample/colors_of_skull_urls.json}"
BUDGETS="${BUDGETS:-10,25,50,100}"
MAX_STEPS="${MAX_STEPS:-100}"

echo "=== 1) GT labelmap (local / Mac vtk_stencil) ==="
GT="data/sample/crotalus_skull_000445108_gt_labelmap.nrrd"
if [[ ! -f "$GT" ]]; then
  echo "Building GT…"
  export MORPHOCLAW_FORCE_MAC_VOXELIZE=1
  make colors-skull-gt-labelmap
fi

echo "=== 2) Load + bright-seed + budget Dice ==="
"$PY" .github/scripts/jetstream_skull_complete.py \
  --fixture "$FIXTURE" \
  --budgets "$BUDGETS" \
  --max-steps "$MAX_STEPS" \
  --no-screenshots \
  "$@"
