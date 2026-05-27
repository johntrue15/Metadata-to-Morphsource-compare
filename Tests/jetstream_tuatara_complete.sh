#!/usr/bin/env bash
# Mac driver: full tuatara bright-seed (100 clicks) + Dice vs staged GT.
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

echo "=== Tuatara complete (load + 100-click + Dice) ==="
"$PY" .github/scripts/jetstream_tuatara_complete.py "$@"
