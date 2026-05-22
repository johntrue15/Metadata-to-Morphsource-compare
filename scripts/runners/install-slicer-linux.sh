#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-slicer-linux.sh
#
# Install 3D Slicer (stable) and the SlicerMorph extension into a Linux
# (typically WSL2 Ubuntu) host so that MorphoClaw's `slicer-integration.yml`
# and friends can run headlessly via xvfb.
#
# Outputs:
#   /opt/slicer/Slicer-X.Y.Z-linux-amd64/    -- extracted Slicer
#   /usr/local/bin/slicer-raw                 -- symlink to the raw binary
#   ~/bin/Slicer                              -- xvfb-wrapped launcher
#                                               (SLICER_BIN in runner .env)
#
# Idempotent. Safe to re-run; will only re-download if the target version
# changes or the target dir is empty.
#
# Environment variables:
#   SLICER_VERSION         e.g. 5.10.0 (default: empty -> use download.slicer.org
#                          'release' redirect to whatever's current).
#   SLICER_DOWNLOAD_URL    explicit tarball URL (overrides SLICER_VERSION lookup).
#   SLICER_INSTALL_ROOT    where to unpack Slicer (default: /opt/slicer).
#   SLICER_WRAPPER         where to write the xvfb wrapper (default: ~/bin/Slicer).
#   SKIP_SLICERMORPH       1 to skip the SlicerMorph extension install.
# ---------------------------------------------------------------------------

set -euo pipefail

SLICER_VERSION="${SLICER_VERSION:-}"
SLICER_DOWNLOAD_URL="${SLICER_DOWNLOAD_URL:-}"
SLICER_INSTALL_ROOT="${SLICER_INSTALL_ROOT:-/opt/slicer}"
SLICER_WRAPPER="${SLICER_WRAPPER:-$HOME/bin/Slicer}"
SKIP_SLICERMORPH="${SKIP_SLICERMORPH:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '    \033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '    \033[1;31m[err]\033[0m  %s\n' "$*" >&2; exit 1; }

command -v curl  >/dev/null || die "curl is required"
command -v tar   >/dev/null || die "tar is required"
command -v xvfb-run >/dev/null || warn "xvfb-run not on PATH yet -- run setup-wsl-runner.sh first to apt-install xvfb"

# ---------------------------------------------------------------------------
# 1. Pick a download URL
# ---------------------------------------------------------------------------

_resolve_slicer_url_from_find() {
    # Use the official download.slicer.org "find" API. It returns JSON of the form:
    #   {"download_url":"/bitstream/<itemId>", "name":"Slicer_linux_amd64_<rev>",
    #    "revision":"<rev>", "version":"5.10.0", "size":434498865, ...}
    # We compose the absolute URL from download_url.
    local query="$1"   # e.g. "os=linux&stability=release&revision=34045"
    local json found
    if ! json="$(curl -fsSL "https://download.slicer.org/find?$query" 2>/dev/null)"; then
        echo "" ; return 1
    fi
    if ! command -v jq >/dev/null 2>&1; then
        # jq is part of our apt prereqs, but provide a regex fallback just in case.
        found="$(printf '%s' "$json" | grep -oE '"download_url":"[^"]+"' | sed 's/.*"\(\/[^"]*\)".*/\1/')"
    else
        found="$(printf '%s' "$json" | jq -r '.download_url // empty')"
    fi
    if [ -z "$found" ] || [ "$found" = "null" ]; then
        echo "" ; return 1
    fi
    printf 'https://download.slicer.org%s' "$found"
}

if [ -z "$SLICER_DOWNLOAD_URL" ]; then
    if [ -n "$SLICER_VERSION" ]; then
        log "Resolving download URL for Slicer $SLICER_VERSION via download.slicer.org/find"
        SLICER_DOWNLOAD_URL="$(_resolve_slicer_url_from_find "os=linux&stability=release&revision=${SLICER_VERSION}")" || true
        if [ -z "$SLICER_DOWNLOAD_URL" ]; then
            warn "Could not look up Slicer $SLICER_VERSION via /find; falling back to the latest release."
            SLICER_DOWNLOAD_URL="$(_resolve_slicer_url_from_find "os=linux&stability=release")" || true
        fi
    else
        log "Resolving latest stable Slicer Linux tarball via download.slicer.org/find"
        SLICER_DOWNLOAD_URL="$(_resolve_slicer_url_from_find "os=linux&stability=release")" || true
    fi
    if [ -z "$SLICER_DOWNLOAD_URL" ]; then
        die "Could not resolve a Slicer download URL. Set SLICER_DOWNLOAD_URL=<direct .tar.gz URL>
            and re-run. (You can find one by visiting https://download.slicer.org/ in a browser
            and copying the link from the Linux release row.)"
    fi
fi
ok "Slicer download URL: $SLICER_DOWNLOAD_URL"

# ---------------------------------------------------------------------------
# 2. Download + extract
# ---------------------------------------------------------------------------

sudo mkdir -p "$SLICER_INSTALL_ROOT"
sudo chown "$(id -un):$(id -gn)" "$SLICER_INSTALL_ROOT"

# Check if a Slicer install already exists in the target directory
EXISTING_SLICER_DIR="$(find "$SLICER_INSTALL_ROOT" -maxdepth 1 -type d -name 'Slicer-*' 2>/dev/null | sort | tail -1 || true)"
if [ -n "$EXISTING_SLICER_DIR" ] && [ -x "$EXISTING_SLICER_DIR/Slicer" ]; then
    ok "Slicer already installed at $EXISTING_SLICER_DIR"
    SLICER_DIR="$EXISTING_SLICER_DIR"
else
    TMP_TARBALL="$(mktemp --suffix=.tar.gz)"
    log "Downloading Slicer (this is ~400 MB)..."
    curl -fL --retry 3 --retry-delay 5 -o "$TMP_TARBALL" "$SLICER_DOWNLOAD_URL"

    log "Extracting tarball into $SLICER_INSTALL_ROOT"
    tar -xzf "$TMP_TARBALL" -C "$SLICER_INSTALL_ROOT"
    rm -f "$TMP_TARBALL"

    SLICER_DIR="$(find "$SLICER_INSTALL_ROOT" -maxdepth 1 -type d -name 'Slicer-*' 2>/dev/null | sort | tail -1 || true)"
    [ -n "$SLICER_DIR" ] || die "Extraction did not produce a Slicer-* directory under $SLICER_INSTALL_ROOT"
    [ -x "$SLICER_DIR/Slicer" ] || die "Extracted dir $SLICER_DIR has no executable 'Slicer' binary"
    ok "Slicer extracted into $SLICER_DIR"
fi

# Convenient raw symlink (without xvfb) for debugging
if ! [ -L /usr/local/bin/slicer-raw ] || [ "$(readlink /usr/local/bin/slicer-raw)" != "$SLICER_DIR/Slicer" ]; then
    log "Linking /usr/local/bin/slicer-raw -> $SLICER_DIR/Slicer"
    sudo ln -sfn "$SLICER_DIR/Slicer" /usr/local/bin/slicer-raw
fi

# ---------------------------------------------------------------------------
# 3. Headless wrapper script ($SLICER_WRAPPER)
#
# Every MorphoClaw workflow that touches Slicer reads $SLICER_BIN. We point
# that at this wrapper, which always launches Slicer under xvfb-run -- so the
# workflows don't have to know anything about WSL/headless quirks.
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "$SLICER_WRAPPER")"
cat > "$SLICER_WRAPPER" <<EOF
#!/usr/bin/env bash
# Auto-generated by scripts/runners/install-slicer-linux.sh
# Launch 3D Slicer headlessly under xvfb. The actual binary lives at:
#   $SLICER_DIR/Slicer
set -euo pipefail
SLICER_RAW="\${SLICER_RAW:-$SLICER_DIR/Slicer}"
if [ -t 1 ] && [ -z "\${DISPLAY:-}" ] && [ -z "\${SLICER_FORCE_XVFB:-}" ]; then
    # Foreground interactive shell with no DISPLAY -- still wrap with xvfb so
    # Slicer's QApplication can initialise. Same path CI takes.
    :
fi
if command -v xvfb-run >/dev/null 2>&1; then
    exec xvfb-run -a --server-args="-screen 0 1920x1080x24" "\$SLICER_RAW" "\$@"
else
    echo "[Slicer wrapper] xvfb-run not found; running Slicer directly. This"   >&2
    echo "                  will fail if no DISPLAY is available."              >&2
    exec "\$SLICER_RAW" "\$@"
fi
EOF
chmod +x "$SLICER_WRAPPER"
ok "Headless launcher at $SLICER_WRAPPER -> $SLICER_DIR/Slicer (via xvfb-run)"

# ---------------------------------------------------------------------------
# 4. Smoke test
# ---------------------------------------------------------------------------

log "Smoke-testing Slicer headless launch (--no-main-window --no-splash --version)"
if "$SLICER_WRAPPER" --no-splash --no-main-window --version 2>&1 | tee /tmp/slicer-version.log; then
    ok "Slicer reports a version successfully."
else
    warn "Slicer --version exited non-zero. Look at /tmp/slicer-version.log."
fi

# ---------------------------------------------------------------------------
# 5. SlicerMorph extension
# ---------------------------------------------------------------------------

if [ "$SKIP_SLICERMORPH" = "1" ]; then
    warn "SKIP_SLICERMORPH=1 -- not installing SlicerMorph."
    exit 0
fi

INSTALL_SLICERMORPH_PY="$REPO_ROOT/scripts/runners/slicer_install_slicermorph.py"
if [ ! -f "$INSTALL_SLICERMORPH_PY" ]; then
    warn "Helper $INSTALL_SLICERMORPH_PY missing; skipping SlicerMorph install."
    exit 0
fi

log "Installing SlicerMorph extension (and a few common companions)"
# Run the installer with the wrapper so xvfb-run handles the display.
if "$SLICER_WRAPPER" --no-splash --no-main-window --python-script "$INSTALL_SLICERMORPH_PY"; then
    ok "SlicerMorph install script finished."
else
    warn "SlicerMorph install script reported errors. Re-run later with:"
    warn "    $SLICER_WRAPPER --no-splash --python-script $INSTALL_SLICERMORPH_PY"
fi

ok "Done. Set SLICER_BIN=$SLICER_WRAPPER in the runner .env (setup-wsl-runner.sh does this for you)."
