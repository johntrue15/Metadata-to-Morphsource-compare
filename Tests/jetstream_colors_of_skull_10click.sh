#!/usr/bin/env bash
# Run on the Jetstream box (repo cloned, Slicer Web Server on :2016).
# Loads the Colors of Skull Crotalus sample from GitHub raw URL, runs 10 clicks.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export SLICER_WEBSERVER_URL="${SLICER_WEBSERVER_URL:-http://127.0.0.1:2016/}"
PY="${NNI_PY_BIN:-${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

echo "=== Probe Slicer :2016 ==="
code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 8 \
  "${SLICER_WEBSERVER_URL%/}/slicer/screenshot" || echo 000)
echo "GET /slicer/screenshot -> HTTP $code"
if [[ "$code" != "200" ]]; then
  echo "ERROR: start Slicer Web Server (port 2016) before running this script."
  exit 2
fi

echo "=== Load CT from GitHub + 10-click bright-seed ==="
"$PY" .github/scripts/jetstream_10click_from_url.py \
  --fixture data/sample/colors_of_skull_urls.json \
  --max-steps 10 \
  --no-screenshots \
  "$@"

echo "OK"
