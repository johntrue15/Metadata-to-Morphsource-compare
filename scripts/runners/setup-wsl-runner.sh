#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-wsl-runner.sh
#
# Bootstrap a MorphoClaw self-hosted GitHub Actions runner inside WSL2
# Ubuntu (or any Debian/Ubuntu Linux with NVIDIA CUDA passthrough).
#
# What it does (idempotently):
#   1. Verifies nvidia-smi works (CUDA passthrough sanity check).
#   2. Installs apt prerequisites (build tools, jq, xvfb, Slicer Qt libs, etc.).
#   3. Installs Miniforge into $HOME/miniforge3 (Python 3.12 + conda).
#   4. Downloads the latest actions/runner Linux tarball.
#   5. Prompts for a one-time runner registration token (or fetches it with
#      `gh api ... /actions/runners/registration-token` if you're logged in).
#   6. Configures the runner with labels: self-hosted, Linux, X64,
#      gpu, cuda, nvidia, wsl, slicer, nninteractive, plus a hostname tag.
#   7. Writes ANACONDA_BIN, SLICER_BIN, NNINTERACTIVE_HOME into the runner's
#      ./.env so every job picks up Linux paths automatically (the existing
#      Mac-mini-shaped workflows fall back to /opt/anaconda3 etc. when these
#      are unset).
#   8. Optionally pre-warms the nnInteractive venv via the repo's own
#      .github/scripts/install_nninteractive.sh.
#   9. Optionally installs 3D Slicer + SlicerMorph headlessly (via xvfb-run).
#
# Manual start, as you requested: this script does NOT install a systemd
# service. After it finishes, start the runner with:
#
#     cd ~/actions-runner-morphoclaw && ./run.sh
#
# Environment variables (override defaults):
#   GH_REPO              owner/repo (default: johntrue15/MorphoClaw)
#   RUNNER_DIR           runner install dir (default: ~/actions-runner-morphoclaw)
#   RUNNER_NAME          runner display name (default: <hostname>-wsl-gpu)
#   RUNNER_LABELS        comma-separated labels appended to the defaults
#   RUNNER_TOKEN         skip the interactive prompt by providing the token
#   SKIP_SLICER          1 to skip the Slicer install step
#   SKIP_NNINTERACTIVE   1 to skip the nnInteractive bootstrap step
#   SKIP_MINIFORGE       1 to skip the Miniforge install (use system python3.12)
#   ACTIONS_RUNNER_VERSION  pin to a specific actions/runner release, e.g. 2.328.0
#
# Exit codes: 0 on success, non-zero on hard failure.
# ---------------------------------------------------------------------------

set -euo pipefail

GH_REPO="${GH_REPO:-johntrue15/MorphoClaw}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-morphoclaw}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)-wsl-gpu}"
EXTRA_LABELS="${RUNNER_LABELS:-}"
SKIP_SLICER="${SKIP_SLICER:-0}"
SKIP_NNINTERACTIVE="${SKIP_NNINTERACTIVE:-0}"
SKIP_MINIFORGE="${SKIP_MINIFORGE:-0}"
ACTIONS_RUNNER_VERSION="${ACTIONS_RUNNER_VERSION:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '    \033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '    \033[1;31m[err]\033[0m  %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Pre-flight
# ---------------------------------------------------------------------------

if [ "$(id -u)" = "0" ]; then
    die "Do not run this as root. Run as your normal Ubuntu user; sudo will be invoked as needed."
fi

log "Pre-flight checks"
[ -f /etc/os-release ] || die "/etc/os-release missing -- is this really Linux?"
. /etc/os-release
ok "OS: ${PRETTY_NAME:-unknown}"

case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) ;;
    *) warn "This script targets Debian/Ubuntu. Detected ID=${ID:-?} ID_LIKE=${ID_LIKE:-?}. Proceeding anyway." ;;
esac

if ! command -v nvidia-smi >/dev/null 2>&1; then
    die "nvidia-smi not found inside this Linux. Make sure the Windows NVIDIA driver
            is recent (>= r495) and run 'wsl --shutdown' from PowerShell, then re-enter
            WSL. The driver is supplied by Windows; do NOT apt-install nvidia drivers
            inside WSL2."
fi
ok "nvidia-smi found at: $(command -v nvidia-smi)"
GPU_LINE=$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null | head -1)
ok "GPU: $GPU_LINE"

# ---------------------------------------------------------------------------
# 1. apt prerequisites
# ---------------------------------------------------------------------------

log "Installing apt prerequisites (build tools, jq, xvfb, Slicer Qt deps)"
sudo apt-get update -y
APT_PKGS=(
    build-essential
    ca-certificates
    curl
    git
    git-lfs
    jq
    unzip
    pkg-config
    tar
    libicu74
    xvfb
    libgl1
    libglu1-mesa
    libegl1
    libxrender1
    libxcomposite1
    libxcursor1
    libxi6
    libxtst6
    libnss3
    libxss1
    libasound2t64
    libxkbcommon-x11-0
    libxcb-icccm4
    libxcb-image0
    libxcb-keysyms1
    libxcb-randr0
    libxcb-render-util0
    libxcb-shape0
    libxcb-sync1
    libxcb-xfixes0
    libxcb-xinerama0
    libxcb-xkb1
    libxkbfile1
    fonts-dejavu-core
    fonts-freefont-ttf
)
# Older Ubuntu releases ship libasound2 instead of libasound2t64. Detect.
if ! apt-cache show libasound2t64 >/dev/null 2>&1; then
    APT_PKGS=("${APT_PKGS[@]/libasound2t64/libasound2}")
fi
if ! apt-cache show libicu74 >/dev/null 2>&1; then
    # Ubuntu 22.04 ships libicu70; the dotnet runtime inside the GH runner
    # links against whichever icu the distro provides.
    APT_PKGS=("${APT_PKGS[@]/libicu74/libicu70}")
fi
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${APT_PKGS[@]}"
ok "apt prerequisites installed"

# ---------------------------------------------------------------------------
# 2. Miniforge (Python 3.12 + conda) so $ANACONDA_BIN lines up with what the
#    Mac-mini workflows expect.
# ---------------------------------------------------------------------------

MINIFORGE_DIR="$HOME/miniforge3"
ANACONDA_BIN="$MINIFORGE_DIR/bin"

if [ "$SKIP_MINIFORGE" = "1" ]; then
    warn "SKIP_MINIFORGE=1 -- using system python3"
    ANACONDA_BIN="$(dirname "$(command -v python3)")"
elif [ -x "$MINIFORGE_DIR/bin/conda" ]; then
    ok "Miniforge already installed at $MINIFORGE_DIR"
else
    log "Installing Miniforge into $MINIFORGE_DIR"
    MF_ARCH="$(uname -m)"
    case "$MF_ARCH" in
        x86_64)  MF_URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh" ;;
        aarch64) MF_URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh" ;;
        *) die "Unsupported architecture for Miniforge: $MF_ARCH" ;;
    esac
    TMP_INSTALLER="$(mktemp --suffix=.sh)"
    curl -fsSL "$MF_URL" -o "$TMP_INSTALLER"
    bash "$TMP_INSTALLER" -b -p "$MINIFORGE_DIR"
    rm -f "$TMP_INSTALLER"
    ok "Miniforge installed"
fi

if [ -x "$ANACONDA_BIN/python3" ]; then
    ok "Python: $("$ANACONDA_BIN/python3" --version)"
fi

# ---------------------------------------------------------------------------
# 3. Download + extract actions/runner
# ---------------------------------------------------------------------------

if [ -z "$ACTIONS_RUNNER_VERSION" ]; then
    log "Resolving latest actions/runner release"
    ACTIONS_RUNNER_VERSION="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
        | jq -r '.tag_name' | sed 's/^v//')" \
        || die "Failed to query the actions/runner latest release. Set ACTIONS_RUNNER_VERSION manually."
fi
ok "actions/runner version: $ACTIONS_RUNNER_VERSION"

RUNNER_ARCH_TAG=""
case "$(uname -m)" in
    x86_64)  RUNNER_ARCH_TAG="linux-x64" ;;
    aarch64) RUNNER_ARCH_TAG="linux-arm64" ;;
    *) die "Unsupported architecture for actions/runner: $(uname -m)" ;;
esac
RUNNER_TARBALL="actions-runner-${RUNNER_ARCH_TAG}-${ACTIONS_RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${ACTIONS_RUNNER_VERSION}/${RUNNER_TARBALL}"

mkdir -p "$RUNNER_DIR"
if [ ! -x "$RUNNER_DIR/config.sh" ]; then
    log "Downloading $RUNNER_TARBALL into $RUNNER_DIR"
    curl -fsSL -o "$RUNNER_DIR/$RUNNER_TARBALL" "$RUNNER_URL"
    tar -xzf "$RUNNER_DIR/$RUNNER_TARBALL" -C "$RUNNER_DIR"
    rm -f "$RUNNER_DIR/$RUNNER_TARBALL"
    ok "actions/runner extracted into $RUNNER_DIR"
else
    ok "actions/runner already present in $RUNNER_DIR"
fi

# Install distro-level prereqs declared by the runner (skip-quietly on minimal images)
if [ -x "$RUNNER_DIR/bin/installdependencies.sh" ]; then
    log "Running runner's bin/installdependencies.sh (idempotent)"
    sudo "$RUNNER_DIR/bin/installdependencies.sh" >/dev/null 2>&1 || \
        warn "installdependencies.sh exited non-zero; continuing"
fi

# ---------------------------------------------------------------------------
# 4. Compute labels + token
# ---------------------------------------------------------------------------

# Default labels target the routing pattern from docs/self-hosted-gpu-runner.md:
#   - GPU/nnInteractive jobs use [self-hosted, gpu]
#   - Slicer-only jobs use [self-hosted, slicer]
DEFAULT_LABELS="gpu,cuda,nvidia,wsl,nninteractive,slicer"
LABELS="$DEFAULT_LABELS"
if [ -n "$EXTRA_LABELS" ]; then
    LABELS="$LABELS,$EXTRA_LABELS"
fi
# Add a host-specific label so workflows can pin to one machine if they need to
LABELS="$LABELS,host-$(hostname | tr '[:upper:]' '[:lower:]')"
ok "Runner labels: $LABELS"
ok "Runner name:   $RUNNER_NAME"
ok "Repo:          $GH_REPO"

if [ -f "$RUNNER_DIR/.runner" ]; then
    warn "$RUNNER_DIR already contains a configured runner."
    warn "If you want to re-register with new labels, first run:"
    warn "    cd $RUNNER_DIR && ./config.sh remove --token <removal-token>"
    warn "Skipping the config.sh step."
else
    if [ -z "${RUNNER_TOKEN:-}" ]; then
        if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
            log "Fetching runner registration token via gh CLI"
            RUNNER_TOKEN="$(gh api -X POST "repos/${GH_REPO}/actions/runners/registration-token" \
                --jq .token 2>/dev/null || true)"
        fi
    fi
    if [ -z "${RUNNER_TOKEN:-}" ]; then
        echo ''
        echo 'A one-time runner registration token is required. Get it from:'
        echo "    https://github.com/${GH_REPO}/settings/actions/runners/new"
        echo '(Click "New self-hosted runner", choose Linux x64, and copy the token'
        echo 'argument from the ./config.sh line in the GitHub instructions.)'
        echo ''
        read -r -p 'Paste runner registration token: ' RUNNER_TOKEN
        if [ -z "$RUNNER_TOKEN" ]; then
            die "No runner token provided. Re-run when you have one."
        fi
    fi

    log "Registering runner with GitHub"
    pushd "$RUNNER_DIR" >/dev/null
    ./config.sh \
        --unattended \
        --url    "https://github.com/${GH_REPO}" \
        --token  "$RUNNER_TOKEN" \
        --name   "$RUNNER_NAME" \
        --labels "$LABELS" \
        --work   "_work" \
        --replace
    popd >/dev/null
    ok "Runner registered"
fi

# ---------------------------------------------------------------------------
# 5. Write per-runner .env so existing Mac-shaped workflows resolve to Linux
# ---------------------------------------------------------------------------

NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
SLICER_BIN_DEFAULT="$HOME/bin/Slicer"

RUNNER_ENV="$RUNNER_DIR/.env"
log "Writing per-runner environment to $RUNNER_ENV"
{
    echo "# Managed by scripts/runners/setup-wsl-runner.sh -- safe to hand-edit."
    echo "# These vars are exported into every job before any workflow step runs."
    echo "ANACONDA_BIN=${ANACONDA_BIN}"
    echo "NNINTERACTIVE_HOME=${NNI_HOME}"
    echo "SLICER_BIN=${SLICER_BIN_DEFAULT}"
    # Make the runner's PATH match what the workflows expect. On WSL we also
    # need /usr/lib/wsl/lib up front so non-interactive job shells can see
    # `nvidia-smi` (it lives in the WSL CUDA-passthrough mount, not /usr/bin).
    WSL_LIB=""
    if [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
        WSL_LIB="/usr/lib/wsl/lib:"
    fi
    echo "PATH=${WSL_LIB}${ANACONDA_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
    # Stop the actions-runner from prepending its bundled node to PATH ahead of
    # whatever the workflows decide; the bundled node still works for actions.
    echo "RUNNER_ALLOW_RUNASROOT=0"
} > "$RUNNER_ENV"
chmod 600 "$RUNNER_ENV"
ok "Wrote $RUNNER_ENV"

# ---------------------------------------------------------------------------
# 6. Optional: 3D Slicer + SlicerMorph
# ---------------------------------------------------------------------------

if [ "$SKIP_SLICER" = "1" ]; then
    warn "SKIP_SLICER=1 -- not installing 3D Slicer."
else
    log "Installing 3D Slicer + SlicerMorph (calls install-slicer-linux.sh)"
    INSTALL_SLICER="$REPO_ROOT/scripts/runners/install-slicer-linux.sh"
    if [ -x "$INSTALL_SLICER" ] || [ -f "$INSTALL_SLICER" ]; then
        bash "$INSTALL_SLICER"
    else
        warn "$INSTALL_SLICER not found -- skipping. Re-run it manually later."
    fi
fi

# ---------------------------------------------------------------------------
# 7. Optional: pre-warm nnInteractive venv (reuses the repo's own installer)
# ---------------------------------------------------------------------------

if [ "$SKIP_NNINTERACTIVE" = "1" ]; then
    warn "SKIP_NNINTERACTIVE=1 -- nnInteractive will be bootstrapped on first workflow run."
else
    log "Bootstrapping nnInteractive venv via .github/scripts/install_nninteractive.sh"
    NNI_INSTALL="$REPO_ROOT/.github/scripts/install_nninteractive.sh"
    if [ -f "$NNI_INSTALL" ]; then
        chmod +x "$NNI_INSTALL"
        NNINTERACTIVE_PY="$ANACONDA_BIN/python3" \
        NNINTERACTIVE_HOME="$NNI_HOME" \
            bash "$NNI_INSTALL" || warn "install_nninteractive.sh exited non-zero; continuing"
    else
        warn "$NNI_INSTALL not found -- skipping."
    fi
fi

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------

cat <<EOF

============================================================================
 MorphoClaw self-hosted runner is configured.
============================================================================

  Repo:    https://github.com/${GH_REPO}
  Name:    ${RUNNER_NAME}
  Labels:  ${LABELS}
  Dir:     ${RUNNER_DIR}
  Python:  ${ANACONDA_BIN}/python3
  Slicer:  ${SLICER_BIN_DEFAULT}
  nnInt:   ${NNI_HOME}

Start the runner (manual, foreground) with:

    cd ${RUNNER_DIR}
    ./run.sh

To check that GitHub sees it: https://github.com/${GH_REPO}/settings/actions/runners

If you want it to start automatically (recommended), install the
service via the wrapper script — it adds Restart=on-failure so a crash
auto-recovers after 30s:

    bash scripts/runners/install-runner-service.sh

And on the Windows host, install the Task Scheduler watchdog so a
stopped WSL distro gets woken up automatically:

    pwsh scripts\runners\runner-ctl.ps1 watchdog install

You can re-run this script anytime; every step is idempotent.

EOF
