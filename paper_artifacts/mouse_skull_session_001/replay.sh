#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${SLICER_WEBSERVER_URL:?set SLICER_WEBSERVER_URL or source .env first}"
python3 ./replay.py \
    --bundle . \
    --target-volume "IMPC_sample_data" \
    "$@"
