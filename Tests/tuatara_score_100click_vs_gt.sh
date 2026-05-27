#!/usr/bin/env bash
# Score the *live* 100-segment tuatara scene on Jetstream — no reset, no new clicks.
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
OUT="${OUT_DIR:-runs/tuatara_100click_vs_gt}"

echo "=== 1) Mesh GT labelmap (required for comparison only) ==="
if [[ ! -f "$GT" ]]; then
  echo "Building GT from skull .ply (vtk_stencil)…"
  export MORPHOCLAW_FORCE_MAC_VOXELIZE=1
  make tuatara-gt-labelmap
fi
[[ -f "$GT" ]] || { echo "ERROR: GT not ready at $GT"; exit 2; }

echo "=== 2) Export live segmentation + Dice at budgets (no new clicks) ==="
"$PY" .github/scripts/slicer_remote_score_scene.py \
  --gt-path "$GT" \
  --ct-path "$CT" \
  --budgets "${BUDGETS:-10,25,50,100}" \
  --out-dir "$OUT"

echo "=== Done (scene unchanged on Jetstream) ==="
cat "$OUT/results.csv" 2>/dev/null || true
