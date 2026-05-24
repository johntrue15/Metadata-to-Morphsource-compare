#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke_nninteractive_compare.sh - cached-fixture smoke for the nnInteractive
# comparison pipeline.
#
# This is the LOCAL dev-loop equivalent of the .github/workflows/nninteractive_smoke.yml
# PR gate. It runs `nninteractive_compare.py --from-fixture --pred-from-fixture`
# against the committed Tests/fixtures/nninteractive_compare/chameleon_stapes
# fixture and asserts metrics haven't regressed vs the bundled baseline.
#
# No GPU, no MorphoSource download, no OpenAI quota. Completes in ~2s.
#
# Intended workflow:
#   1. Edit .github/scripts/nninteractive_compare.py (or any of the
#      pipeline scripts: segmentation_metrics.py, voxelize_mesh_vtk.py,
#      crop_around_mesh.py).
#   2. Run this script. Get pass/fail in ~2s.
#   3. Iterate.
#   4. Push to a PR; nninteractive_smoke.yml re-runs the same gate on
#      ubuntu-latest as a hard PR-merge gate.
#
# Usage:
#     Tests/smoke_nninteractive_compare.sh
#     Tests/smoke_nninteractive_compare.sh --tol 0.05         # looser tol
#     Tests/smoke_nninteractive_compare.sh --fixture-dir DIR  # alternate fixture
#     PYTHON=/some/python Tests/smoke_nninteractive_compare.sh
#
# Env overrides:
#     NNINTERACTIVE_HOME    venv whose python has SimpleITK/numpy/matplotlib/scipy.
#                           Defaults to ~/.autoresearchclaw/nninteractive when present,
#                           else falls back to $PYTHON / python3.
#     PYTHON                explicit python interpreter (wins over NNINTERACTIVE_HOME).
#     SMOKE_OUTPUT_DIR      where to write metrics/overlay/report (default mktemp).
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_FIXTURE="Tests/fixtures/nninteractive_compare/chameleon_stapes"

FIXTURE_DIR="$DEFAULT_FIXTURE"
TOL="0.01"
ASSERT_DICE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fixture-dir)  FIXTURE_DIR="$2";   shift 2 ;;
        --tol)          TOL="$2";           shift 2 ;;
        --assert-dice)  ASSERT_DICE="$2";   shift 2 ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "smoke_nninteractive_compare.sh: unknown arg: $1" >&2
            exit 64
            ;;
    esac
done

# --- locate a python with the required pure-CPU deps -----------------------
PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
    if [[ -x "$NNI_HOME/bin/python" ]]; then
        PYTHON_BIN="$NNI_HOME/bin/python"
    else
        PYTHON_BIN="python3"
    fi
fi

echo "==> python: $PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"

# Sanity-check deps before we burn time on a more cryptic ImportError downstream.
if ! "$PYTHON_BIN" -c "import SimpleITK, numpy, matplotlib, scipy" 2>/dev/null; then
    echo "ERROR: $PYTHON_BIN is missing one of SimpleITK / numpy / matplotlib / scipy." >&2
    echo "       Either:" >&2
    echo "         * bootstrap the nnInteractive venv: .github/scripts/install_nninteractive.sh" >&2
    echo "         * or PYTHON=/path/to/python Tests/smoke_nninteractive_compare.sh" >&2
    exit 70
fi

# --- resolve fixture --------------------------------------------------------
if [[ "$FIXTURE_DIR" != /* ]]; then
    FIXTURE_DIR="$REPO_ROOT/$FIXTURE_DIR"
fi
if [[ ! -d "$FIXTURE_DIR" ]]; then
    echo "ERROR: fixture dir not found: $FIXTURE_DIR" >&2
    exit 66
fi

for required in ct.nii.gz gt_voxelized.nii.gz pred.nii.gz baseline_metrics.json fixture.json; do
    if [[ ! -f "$FIXTURE_DIR/$required" ]]; then
        echo "ERROR: fixture is missing required file: $FIXTURE_DIR/$required" >&2
        exit 66
    fi
done

echo "==> fixture: $FIXTURE_DIR"

# --- run --------------------------------------------------------------------
OUT="${SMOKE_OUTPUT_DIR:-$(mktemp -d -t nni_smoke.XXXXXX)}"
mkdir -p "$OUT"
echo "==> output:  $OUT"
echo ""

ARGS=(
    --from-fixture       "$FIXTURE_DIR"
    --pred-from-fixture  "$FIXTURE_DIR/pred.nii.gz"
    --baseline-metrics   "$FIXTURE_DIR/baseline_metrics.json"
    --regression-tol     "$TOL"
    --output-dir         "$OUT"
)
if [[ -n "$ASSERT_DICE" ]]; then
    ARGS+=(--assert-dice "$ASSERT_DICE")
fi

set +e
"$PYTHON_BIN" "$REPO_ROOT/.github/scripts/nninteractive_compare.py" "${ARGS[@]}"
EXIT_CODE=$?
set -e

echo ""
case $EXIT_CODE in
    0)  echo "==> SMOKE PASSED (compare exit 0, regression gates within tol)" ;;
    3)  echo "==> SMOKE FAILED: regression gate (compare exit 3). See $OUT/" >&2 ;;
    *)  echo "==> SMOKE FAILED: compare.py exit $EXIT_CODE. See $OUT/" >&2 ;;
esac

exit $EXIT_CODE
