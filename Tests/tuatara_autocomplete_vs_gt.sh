#!/usr/bin/env bash
# Full pipeline: optional GT-guided clicks (needs GT file), then compare vs mesh GT.
#
# For parallel work, prefer splitting:
#   make tuatara-click-to-completion   # Jetstream only — no GT
#   make tuatara-gt-labelmap           # Mac only — voxelize .ply
#   make tuatara-score-100click        # after both finish
#
# Set CLICK_PHASE_ONLY=1 to skip guided clicks and only score the live scene.
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
GT="data/sample/tuatara_skull_000358663_gt_labelmap.nrrd"
CT="data/sample/tuatara_skull_000358663_ct.nrrd"
RUN_DIR="${RUN_DIR:-runs/tuatara_autocomplete_$(date +%Y%m%dT%H%M%S)}"
SCORE_DIR="${SCORE_DIR:-$RUN_DIR/final_vs_gt}"
MAX_STEPS="${MAX_STEPS:-200}"
TARGET_DICE="${TARGET_DICE:-0.95}"
VOLUME="${SLICER_VOLUME_NAME:-}"

mkdir -p "$RUN_DIR"

echo "=== 0) Probe Slicer ==="
code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 8 \
  "${SLICER_WEBSERVER_URL%/}/slicer/screenshot" || echo 000)
if [[ "$code" != "200" ]]; then
  echo "ERROR: Slicer :2016 not reachable (HTTP $code). Start Web Server on Jetstream."
  exit 2
fi

if [[ "${CLICK_PHASE_ONLY:-0}" != "1" && "${SCORE_PHASE_ONLY:-0}" != "1" ]]; then
  echo "=== 1) Voxelize skull .ply → GT labelmap (skip if SKIP_GT_BUILD=1) ==="
  if [[ "${SKIP_GT_BUILD:-0}" != "1" && ! -f "$GT" ]]; then
    export MORPHOCLAW_FORCE_MAC_VOXELIZE=1
    make tuatara-gt-labelmap &
    GT_PID=$!
    echo "GT voxelization running in background (pid $GT_PID). Jetstream clicks do not need it."
  fi
fi

if [[ "${SCORE_PHASE_ONLY:-0}" == "1" ]]; then
  : # jump to scoring below
elif [[ "${CLICK_PHASE_ONLY:-0}" == "1" ]]; then
  exec Tests/tuatara_click_to_completion.sh
fi

if [[ -z "$VOLUME" ]]; then
  echo "=== 2) Detect active tuatara volume name ==="
  VOLUME=$("$PY" -c "
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path('.github/scripts').resolve()))
from slicer_remote_score_scene import post_python, LIST_VOLUMES_SRC
r = post_python(os.environ['SLICER_WEBSERVER_URL'].rstrip('/'), LIST_VOLUMES_SRC, timeout=30)
vols = r.get('volumes') or []
pick = [v for v in vols if 'tuatara' in v.lower()]
print(pick[0] if pick else (vols[0] if vols else ''))
")
  if [[ -z "$VOLUME" ]]; then
    echo "ERROR: no scalar volume in Slicer scene. Set SLICER_VOLUME_NAME."
    exit 2
  fi
fi
echo "Using volume: $VOLUME"

if [[ -f "$GT" ]]; then
  echo "=== 3) GT-guided autocomplete (no reset; needs GT file) ==="
  "$PY" .github/scripts/slicer_remote_gt_guided.py \
    --volume "$VOLUME" \
    --gt-path "$GT" \
    --ct-path "$CT" \
    --max-steps "$MAX_STEPS" \
    --target-dice "$TARGET_DICE" \
    --paper-tag tuatara_autocomplete \
    --out-dir "$RUN_DIR/gt_guided" \
    "$@"
else
  echo "=== 3) GT not ready — bright-seed to completion instead (no reset) ==="
  CLICK_PHASE_ONLY=1 OUT_DIR="$RUN_DIR/bright_seed" MAX_STEPS="$MAX_STEPS" \
    Tests/tuatara_click_to_completion.sh
  if [[ -n "${GT_PID:-}" ]]; then
    echo "Waiting for GT voxelization (pid $GT_PID)…"
    wait "$GT_PID" || true
  fi
fi

if [[ ! -f "$GT" ]]; then
  echo "ERROR: GT missing at $GT — cannot score yet. Re-run:"
  echo "  SCORE_PHASE_ONLY=1 OUT_DIR=$SCORE_DIR make tuatara-autocomplete-vs-gt"
  exit 2
fi

echo "=== 4) Final export + Dice vs mesh GT (budgets + full union) ==="
"$PY" .github/scripts/slicer_remote_score_scene.py \
  --gt-path "$GT" \
  --ct-path "$CT" \
  --budgets "${BUDGETS:-10,25,50,100,200}" \
  --out-dir "$SCORE_DIR"

echo ""
echo "=== Complete ==="
echo "Guided run : $RUN_DIR/gt_guided/summary.json"
echo "Comparison : $SCORE_DIR/results.csv"
cat "$SCORE_DIR/results.csv" 2>/dev/null || true
