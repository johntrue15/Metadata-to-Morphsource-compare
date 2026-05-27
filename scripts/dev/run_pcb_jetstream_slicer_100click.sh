#!/usr/bin/env bash
# Load PCB CT into Jetstream Slicer and run 100 bright-seed clicks from this Mac.
#
# Prerequisite: the Dell Jetstream bundle (or any pcb_ti*.nii.gz) on the box, e.g.:
#   scp -r .local/pcb_jetstream/bundle exouser@149.165.170.184:~/pcb_bundle/
#
# Then (after `make unshelve IP=149.165.170.184` or a current .env):
#   REMOTE_PCB_PATH=/home/exouser/pcb_bundle/pcb_ti_jetstream.nii.gz \
#     bash scripts/dev/run_pcb_jetstream_slicer_100click.sh
#
# Or push from this Mac if you have the volume locally:
#   LOCAL_PCB_PATH=.local/pcb_data/pcb_ti.nii.gz \
#     bash scripts/dev/run_pcb_jetstream_slicer_100click.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

REMOTE_PCB_PATH="${REMOTE_PCB_PATH:-/home/exouser/pcb_bundle/pcb_ti_jetstream.nii.gz}"
LOCAL_PCB_PATH="${LOCAL_PCB_PATH:-}"
VOLUME_NAME="${PCB_VOLUME_NAME:-pcb_ti_jetstream}"
MAX_STEPS="${MAX_STEPS:-100}"
OUT_DIR="${OUT_DIR:-runs/pcb_jetstream_${MAX_STEPS}click_$(date +%Y%m%dT%H%M%S)}"

if [[ -z "${SLICER_WEBSERVER_URL:-}" ]]; then
  echo "Set SLICER_WEBSERVER_URL in .env (or run: make unshelve IP=<jetstream-ip>)" >&2
  exit 1
fi

echo "==> Slicer: ${SLICER_WEBSERVER_URL}"
echo "==> Out:    ${OUT_DIR}"

python3 <<PY
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(".github/scripts").resolve()))
from remote_volume_io import list_volumes, load_volume_from_remote_path, push_volume

base = os.environ["SLICER_WEBSERVER_URL"].rstrip("/")
local = os.environ.get("LOCAL_PCB_PATH", "").strip()
remote = os.environ.get("REMOTE_PCB_PATH", "").strip()
vol_name = os.environ["PCB_VOLUME_NAME"]

vols = list_volumes(base)
names = [v.get("name") for v in vols.get("volumes") or []]
if vol_name in names:
    print(f"Volume already loaded: {vol_name}")
else:
    if local:
        p = Path(local)
        if not p.is_file():
            raise SystemExit(f"LOCAL_PCB_PATH not found: {p}")
        print(f"==> Pushing {p} ({p.stat().st_size:,} bytes) …")
        loaded = push_volume(base, p, name=vol_name)
    else:
        print(f"==> Loading from Jetstream path: {remote}")
        loaded = load_volume_from_remote_path(base, remote, name=vol_name)
    if loaded.get("status") != "ok":
        raise SystemExit(f"load failed: {loaded!r}")
    print(f"Loaded shape_kji={loaded.get('shape_kji')} spacing={loaded.get('spacing_mm')}")
PY

python3 .github/scripts/slicer_remote_bright_seed.py \
  --volume "$VOLUME_NAME" \
  --reset-first \
  --max-steps "$MAX_STEPS" \
  --no-stop-rules \
  --label pcb_ti \
  --out-dir "$OUT_DIR"

echo "==> Done. Artifacts: $OUT_DIR"
