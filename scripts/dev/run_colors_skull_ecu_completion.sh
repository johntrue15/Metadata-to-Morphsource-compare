#!/usr/bin/env bash
# Colors of Skull (Crotalus) bright-seed to completion via Jetstream ECU.
#
#   set -a && source .env && set +a
#   export MORPHOCLAW_ECU_URL=https://http-<ip-dashes>-18765.proxy-js2-iu.exosphere.app/
#   bash scripts/dev/run_colors_skull_ecu_completion.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

MAX_STEPS="${MAX_STEPS:-10000}"
OUT_DIR="${OUT_DIR:-runs/colors_skull_bright_$(date +%Y%m%dT%H%M%S)}"

python3 .github/scripts/jetstream_controller.py run --wait --label colors-skull-completion -- \
  bash -lc "export OUT_DIR='${OUT_DIR}' MAX_STEPS='${MAX_STEPS}' && mkdir -p \"\$OUT_DIR\" && python3 <<'PY'
import os, sys, tempfile, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, '.github/scripts')
import remote_volume_io as rvi
import slicer_remote_bright_seed as bs

base = os.environ.get('SLICER_WEBSERVER_URL', 'http://127.0.0.1:2016/')
ct_url = 'https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/data/sample/crotalus_skull_000445108_ct.nrrd'
volume_name = 'crotalus_skull_000445108_ct'
out_dir = Path(os.environ['OUT_DIR'])
max_steps = int(os.environ.get('MAX_STEPS', '10000'))

cache = Path(tempfile.gettempdir()) / 'morphoclaw_github_samples'
cache.mkdir(parents=True, exist_ok=True)
local = cache / Path(urllib.parse.urlparse(ct_url).path).name
if not local.is_file() or local.stat().st_size == 0:
    print(f'Downloading {ct_url} -> {local}')
    urllib.request.urlretrieve(ct_url, local)
else:
    print(f'Using cached {local} ({local.stat().st_size:,} bytes)')

loaded = rvi.load_volume_from_remote_path(base, str(local), volume_name, timeout=600.0)
print('load:', loaded)
if loaded.get('status') != 'ok':
    sys.exit(4)

os.environ['SLICER_WEBSERVER_URL'] = base
rc = bs.main([
    '--volume', volume_name,
    '--reset-first',
    '--max-steps', str(max_steps),
    '--intensity-percentile', '99.0',
    '--no-stop-rules',
    '--no-screenshots',
    '--skip-remote-env',
    '--skip-failed-steps',
    '--label', 'crotalus_skull_completion',
    '--out-dir', str(out_dir),
])
sys.exit(rc)
PY"

echo "==> Done (artifacts on Jetstream under ${OUT_DIR})"
