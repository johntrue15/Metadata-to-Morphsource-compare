#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# test-runner.sh
#
# Local end-to-end smoke test for the WSL2/CUDA self-hosted runner. Run this
# from inside the WSL Ubuntu distribution after `setup-wsl-runner.sh` has
# finished (or any time you want to re-verify the install).
#
# What it exercises (each step is a separate check; failure of any one
# prints a clearly-marked [FAIL] and the script exits non-zero):
#
#   1. nvidia-smi sees the GPU.
#   2. The actions-runner is configured (./.runner exists) and its .env has
#      ANACONDA_BIN / SLICER_BIN / NNINTERACTIVE_HOME.
#   3. The nnInteractive venv exists and imports nnInteractive.
#   4. PyTorch reports torch.cuda.is_available() == True.
#   5. A real `nnInteractiveInferenceSession(device='cuda')` constructs and
#      loads the v1.0 model weights.
#   6. (Best-effort) A 32^3 synthetic-sphere forward pass with one point
#      prompt returns >0 segmented voxels.
#   7. The SLICER_BIN wrapper launches Slicer headlessly and prints a version.
#   8. Inside Slicer, SlicerMorph (`import GPA`) imports.
#
# Manual start, just like the runner: no service modifications, no daemons.
# Re-run safely; nothing here mutates state beyond temp files.
#
# Environment variables (all optional):
#   RUNNER_DIR            override the runner directory (default: ~/actions-runner-morphoclaw)
#   NNINTERACTIVE_HOME    override venv root (read from runner .env by default)
#   SLICER_BIN            override the Slicer wrapper (read from runner .env by default)
#   SKIP_INFERENCE        1 to skip step 6 (the real CUDA forward pass)
#   SKIP_SLICER           1 to skip steps 7 & 8
# ---------------------------------------------------------------------------

set -uo pipefail

RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-morphoclaw}"
SKIP_INFERENCE="${SKIP_INFERENCE:-0}"
SKIP_SLICER="${SKIP_SLICER:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

fails=0
passes=0

step()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()    { printf '    \033[1;32m[ok]\033[0m   %s\n' "$*"; passes=$((passes+1)); }
warn()  { printf '    \033[1;33m[warn]\033[0m %s\n' "$*"; }
fail()  { printf '    \033[1;31m[FAIL]\033[0m %s\n' "$*"; fails=$((fails+1)); }

# Load the runner .env so we use exactly the same paths as a real job would.
if [ -f "$RUNNER_DIR/.env" ]; then
    # shellcheck disable=SC1091
    set -o allexport
    . "$RUNNER_DIR/.env"
    set +o allexport
fi

# ---------------------------------------------------------------------------
# 1. nvidia-smi
# ---------------------------------------------------------------------------
step "1/8  nvidia-smi sees the GPU"
if command -v nvidia-smi >/dev/null 2>&1; then
    if NVIDIA_OUT="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>&1)"; then
        ok "$NVIDIA_OUT"
    else
        fail "nvidia-smi exited non-zero: $NVIDIA_OUT"
    fi
else
    fail "nvidia-smi not on PATH. Driver/passthrough not configured."
fi

# ---------------------------------------------------------------------------
# 2. Runner configuration
# ---------------------------------------------------------------------------
step "2/8  actions-runner configured in $RUNNER_DIR"
if [ -f "$RUNNER_DIR/.runner" ]; then
    NAME="$(jq -r .agentName "$RUNNER_DIR/.runner" 2>/dev/null || echo '?')"
    REPO="$(jq -r .gitHubUrl "$RUNNER_DIR/.runner" 2>/dev/null || echo '?')"
    ok "runner '$NAME' registered against $REPO"
else
    fail "$RUNNER_DIR/.runner missing -- runner not configured."
fi
if [ -f "$RUNNER_DIR/.env" ]; then
    missing=()
    for var in ANACONDA_BIN SLICER_BIN NNINTERACTIVE_HOME; do
        if ! grep -q "^${var}=" "$RUNNER_DIR/.env"; then
            missing+=("$var")
        fi
    done
    if [ ${#missing[@]} -eq 0 ]; then
        ok "runner .env exports ANACONDA_BIN, SLICER_BIN, NNINTERACTIVE_HOME"
    else
        fail "runner .env missing: ${missing[*]}"
    fi
else
    fail "$RUNNER_DIR/.env missing -- workflows will fall back to Mac defaults."
fi

# ---------------------------------------------------------------------------
# 3. nnInteractive venv
# ---------------------------------------------------------------------------
NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
step "3/8  nnInteractive venv at $NNI_HOME"
if [ -x "$NNI_HOME/bin/python" ]; then
    if "$NNI_HOME/bin/python" -c 'import nnInteractive; print(getattr(nnInteractive, "__version__", "ok"))' \
            > /tmp/nni_import.out 2>&1; then
        ok "import nnInteractive -> $(cat /tmp/nni_import.out)"
    else
        fail "import nnInteractive failed; see /tmp/nni_import.out"
        cat /tmp/nni_import.out
    fi
else
    fail "venv python not found at $NNI_HOME/bin/python -- run install_nninteractive.sh"
fi

# ---------------------------------------------------------------------------
# 4 + 5 + 6. Real GPU smoke (delegates to gpu_smoke.py)
# ---------------------------------------------------------------------------
step "4/8  PyTorch + CUDA, nnInteractiveInferenceSession, synthetic inference"
if [ "$SKIP_INFERENCE" = "1" ]; then
    warn "SKIP_INFERENCE=1 -- skipping the CUDA forward-pass test."
elif [ -x "$NNI_HOME/bin/python" ]; then
    GPU_SMOKE="$REPO_ROOT/scripts/runners/gpu_smoke.py"
    if [ ! -f "$GPU_SMOKE" ]; then
        fail "Helper $GPU_SMOKE not found (was the repo cloned correctly?)"
    else
        REPORT_PATH="/tmp/gpu_smoke_report.json"
        GPU_SMOKE_REPORT="$REPORT_PATH" \
            "$NNI_HOME/bin/python" "$GPU_SMOKE" 2>&1 | tee /tmp/gpu_smoke.log
        SMOKE_EXIT=${PIPESTATUS[0]}
        if [ "$SMOKE_EXIT" -eq 0 ]; then
            if [ -f "$REPORT_PATH" ]; then
                ok "gpu_smoke.py finished -- $(cat "$REPORT_PATH" | tr -d '\n' | head -c 240)"
            else
                ok "gpu_smoke.py finished (no JSON report; check /tmp/gpu_smoke.log)"
            fi
        else
            fail "gpu_smoke.py exited $SMOKE_EXIT; see /tmp/gpu_smoke.log"
        fi
    fi
else
    fail "Cannot run gpu_smoke.py without $NNI_HOME/bin/python"
fi

# ---------------------------------------------------------------------------
# 7. Slicer launches headlessly
# ---------------------------------------------------------------------------
step "5/8  Slicer launches headlessly"
SLICER_BIN_RESOLVED="${SLICER_BIN:-$HOME/bin/Slicer}"
if [ "$SKIP_SLICER" = "1" ]; then
    warn "SKIP_SLICER=1 -- skipping Slicer launch test."
elif [ ! -x "$SLICER_BIN_RESOLVED" ]; then
    fail "SLICER_BIN not executable: $SLICER_BIN_RESOLVED"
else
    if "$SLICER_BIN_RESOLVED" --no-splash --no-main-window --version \
            > /tmp/slicer-version.log 2>&1; then
        VER="$(grep -i '^Slicer' /tmp/slicer-version.log | head -1 || true)"
        ok "Slicer launched: ${VER:-$(head -1 /tmp/slicer-version.log)}"
    else
        fail "Slicer --version exited non-zero; see /tmp/slicer-version.log"
    fi
fi

# ---------------------------------------------------------------------------
# 8. SlicerMorph importable inside Slicer
# ---------------------------------------------------------------------------
step "6/8  SlicerMorph importable inside Slicer (import GPA)"
if [ "$SKIP_SLICER" = "1" ]; then
    warn "SKIP_SLICER=1 -- skipping SlicerMorph import test."
elif [ ! -x "$SLICER_BIN_RESOLVED" ]; then
    fail "Slicer wrapper missing; cannot test SlicerMorph"
else
    SM_PROBE="$(mktemp --suffix=.py)"
    cat > "$SM_PROBE" <<'PY'
import sys
try:
    import GPA  # noqa: F401
    print("SlicerMorph::OK")
    import slicer
    slicer.app.exit(0)
except Exception as exc:
    print(f"SlicerMorph::FAIL::{exc}")
    import slicer
    slicer.app.exit(1)
PY
    if "$SLICER_BIN_RESOLVED" --no-splash --no-main-window \
            --python-script "$SM_PROBE" > /tmp/slicermorph.log 2>&1; then
        if grep -q '^SlicerMorph::OK' /tmp/slicermorph.log; then
            ok "import GPA succeeded inside Slicer"
        else
            fail "SlicerMorph import did not return OK; tail of /tmp/slicermorph.log:"
            tail -20 /tmp/slicermorph.log
        fi
    else
        fail "Slicer exited non-zero during SlicerMorph probe; see /tmp/slicermorph.log"
        tail -20 /tmp/slicermorph.log
    fi
    rm -f "$SM_PROBE"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "============================================================================"
printf "  Smoke checks: \033[1;32m%d passed\033[0m  \033[1;31m%d failed\033[0m\n" \
    "$passes" "$fails"
echo "============================================================================"

if [ "$fails" -gt 0 ]; then
    echo
    echo "One or more checks failed. The runner is NOT ready for production"
    echo "workflows yet. See the marked [FAIL] lines above and the logs in /tmp/."
    exit 1
fi

cat <<EOF

All checks passed. This runner is ready for jobs.

Next:
    cd $RUNNER_DIR && ./run.sh

Then trigger the "Runner smoke test" workflow from the GitHub UI to verify
end-to-end routing:
    https://github.com/johntrue15/MorphoClaw/actions/workflows/runner-smoke.yml
EOF
