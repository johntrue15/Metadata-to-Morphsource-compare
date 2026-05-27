#!/usr/bin/env bash
# Tuatara: voxelize mesh GT → GT-guided clicks on Jetstream → Dice → seg_train ledger.
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
RUN_DIR="${RUN_DIR:-runs/tuatara_gt_guided_$(date +%Y%m%dT%H%M%S)}"
GT_NRRD="data/sample/tuatara_skull_000358663_gt_labelmap.nrrd"
CT_NRRD="data/sample/tuatara_skull_000358663_ct.nrrd"
VOLUME_NAME="${SLICER_VOLUME_NAME:-tuatara_skull_000358663_ct}"
MAX_STEPS="${MAX_STEPS:-50}"
LEDGER_DIR="${LEDGER_DIR:-runs/tuatara_seg_train}"

mkdir -p "$RUN_DIR" "$LEDGER_DIR"

echo "=== 1) Mesh → GT labelmap (if missing) ==="
if [[ ! -f "$GT_NRRD" ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    export MORPHOCLAW_FORCE_MAC_VOXELIZE=1
  fi
  make stage-sample-gt
fi

echo "=== 2) Load CT into Slicer (if needed) ==="
"$PY" .github/scripts/jetstream_10click_from_url.py \
  --ct-url "https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/data/sample/tuatara_skull_000358663_ct.nrrd" \
  --volume-name "$VOLUME_NAME" \
  --max-steps 0 \
  --skip-bright-seed 2>&1 | tee "$RUN_DIR/load_ct.log" || true
# If Slicer suffixed the name, pick it up from load log:
if grep -q "loaded '.*_ct_" "$RUN_DIR/load_ct.log" 2>/dev/null; then
  VOLUME_NAME="$(grep -o "loaded '[^']*'" "$RUN_DIR/load_ct.log" | tail -1 | tr -d "'")"
  VOLUME_NAME="${VOLUME_NAME#loaded }"
fi

echo "=== 3) GT-guided click loop (max_steps=$MAX_STEPS, volume=$VOLUME_NAME) ==="
"$PY" .github/scripts/slicer_remote_gt_guided.py \
  --volume "$VOLUME_NAME" \
  --gt-path "$GT_NRRD" \
  --ct-path "$CT_NRRD" \
  --max-steps "$MAX_STEPS" \
  --target-dice 0.92 \
  --reset-first \
  --paper-tag tuatara_skull_v1 \
  --out-dir "$RUN_DIR" \
  --append-ledger "$LEDGER_DIR" \
  "$@"

echo "=== 4) Metrics report ==="
if [[ -f "$RUN_DIR/prediction_composite.nii.gz" ]]; then
  "$PY" .github/scripts/segmentation_metrics.py \
    --pred "$RUN_DIR/prediction_composite.nii.gz" \
    --gt "$GT_NRRD" \
    --volume "$CT_NRRD" \
    --output "$RUN_DIR/final_metrics.json" \
    --overlay "$RUN_DIR/comparison_overlay.png" || true
fi

echo "=== Done ==="
echo "Run dir : $RUN_DIR"
echo "Ledger  : $LEDGER_DIR/ledger.jsonl"
echo ""
echo "Train student on this scan (after a few specimens, same flow):"
echo "  python3 -m metadata_to_morphsource.seg_train round \\"
echo "    --specimens Tests/fixtures/tuatara_seg_train.json \\"
echo "    --run-dir $LEDGER_DIR --paper-tag tuatara_skull_v1"
