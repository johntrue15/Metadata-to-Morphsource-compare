#!/usr/bin/env bash
# Restart the SlicerNNInteractive FastAPI server inside an already-configured
# Jetstream2 / MorphoCloud instance after it has been unshelved.
#
# This is the REMOTE half of `make unshelve IP=...`. The LOCAL half
# (.github/scripts/jetstream_unshelve_start.py) scps this file to /tmp on the
# JS2 box and runs it over SSH. You can also paste it directly into the
# Guacamole terminal — it has no local-side dependencies and only uses tools
# that ship on the default JS2/MorphoCloud image (bash, tmux, curl, awk).
#
# Idempotent: re-running tears down the previous tmux session unless
# NNI_REUSE=1 is set, in which case it leaves a still-healthy server alone.
#
# Environment overrides (all optional — defaults match the standard
# MorphoCloud nninteractive volume layout):
#
#   NNI_VENV           Path to the existing Python venv with
#                      `nninteractive-slicer-server` installed.
#                      Default: /media/volume/MyData/nninteractive
#   NNI_PORT           Bind port for the FastAPI server.
#                      Default: 1527
#   NNI_HOST           Bind host. MUST be 0.0.0.0 (not 127.0.0.1) so the
#                      Exosphere proxy can reach the server.
#                      Default: 0.0.0.0
#   NNI_TMUX_SESSION   tmux session name (so the Guacamole user can
#                      `tmux attach -t <name>` to see live logs).
#                      Default: nninteractive
#   NNI_LOG            File the server's stdout/stderr is tee'd into.
#                      Default: $HOME/nninteractive_server.log
#   NNI_REUSE          If "1" and the tmux session is already healthy, do
#                      not touch it. Default: 0 (always restart).
#   NNI_PROBE_MAX      Health-probe retry budget. Default: 6 (~30 s total).
#   NNI_PROBE_DELAY    Seconds between probe attempts. Default: 5.
#
# This script is intentionally self-contained / copy-paste-ready so it can
# later be lifted into SlicerMorph/MorphoCloudInstances as part of an
# `/unshelve -nninteractive` add-on.

set -euo pipefail

NNI_VENV="${NNI_VENV:-/media/volume/MyData/nninteractive}"
NNI_PORT="${NNI_PORT:-1527}"
NNI_HOST="${NNI_HOST:-0.0.0.0}"
NNI_TMUX_SESSION="${NNI_TMUX_SESSION:-nninteractive}"
NNI_LOG="${NNI_LOG:-$HOME/nninteractive_server.log}"
NNI_REUSE="${NNI_REUSE:-0}"
NNI_PROBE_MAX="${NNI_PROBE_MAX:-6}"
NNI_PROBE_DELAY="${NNI_PROBE_DELAY:-5}"

log() { printf '[js2-restart] %s\n' "$*"; }
die() { printf '[js2-restart] ERROR: %s\n' "$*" >&2; exit 1; }

# Always probe loopback first; the Exosphere proxy is a separate concern that
# the local half checks from the Mac.
probe_local() {
    curl -fsS --max-time 5 "http://127.0.0.1:${NNI_PORT}/docs" >/dev/null 2>&1
}

# ── 0. sanity checks ──────────────────────────────────────────────────────
for tool in tmux curl awk; do
    command -v "$tool" >/dev/null 2>&1 \
        || die "missing prerequisite: $tool (install with: sudo apt-get install -y $tool)"
done

ACTIVATE="$NNI_VENV/bin/activate"
[ -f "$ACTIVATE" ] || die "venv activator not found: $ACTIVATE
       (expected an existing nninteractive venv at \$NNI_VENV; override with
        NNI_VENV=/path/to/venv. For a cold bootstrap use
        install_nninteractive_remote.sh instead.)"

if ! "$NNI_VENV/bin/python" - <<'PY' >/dev/null 2>&1
import importlib.util as u, sys
sys.exit(0 if u.find_spec("nninteractive_slicer_server") or
              u.find_spec("nninteractive-slicer-server") else 1)
PY
then
    # Module name varies across releases; fall back to checking the console
    # script (works for both pip and uv installs).
    [ -x "$NNI_VENV/bin/nninteractive-slicer-server" ] \
        || die "nninteractive-slicer-server is not installed inside $NNI_VENV
       (expected the console script at $NNI_VENV/bin/nninteractive-slicer-server).
       Either point NNI_VENV at the correct venv, or run
       install_nninteractive_remote.sh to bootstrap one."
fi

# ── 1. early-out if a healthy server is already running ──────────────────
SESSION_EXISTS=0
if tmux has-session -t "$NNI_TMUX_SESSION" 2>/dev/null; then
    SESSION_EXISTS=1
fi

if [ "$NNI_REUSE" = "1" ] && [ "$SESSION_EXISTS" = "1" ] && probe_local; then
    log "tmux session '$NNI_TMUX_SESSION' is already healthy on :${NNI_PORT} — reusing."
    REUSED=1
else
    REUSED=0

    # ── 2. tear down any prior server (tmux session + stray process) ──────
    if [ "$SESSION_EXISTS" = "1" ]; then
        log "Killing prior tmux session '$NNI_TMUX_SESSION'."
        tmux kill-session -t "$NNI_TMUX_SESSION" || true
    fi

    # Also catch stragglers started without tmux (matches both `python -m ...`
    # and the console script variants).
    if pgrep -f "nninteractive[-_]slicer[-_]server" >/dev/null 2>&1; then
        log "Killing stray nninteractive-slicer-server processes."
        pkill -f "nninteractive[-_]slicer[-_]server" 2>/dev/null || true
        sleep 1
    fi

    # Make sure the bind port is actually free before we try to launch.
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn "( sport = :${NNI_PORT} )" 2>/dev/null \
                | awk 'NR>1{exit 1}END{if(NR>1)exit 1}'; then
            : # nothing listening
        fi
    fi

    # ── 3. launch fresh server inside tmux ────────────────────────────────
    : > "$NNI_LOG" # truncate so the operator's tail is unambiguous
    log "Starting nninteractive-slicer-server in tmux session '$NNI_TMUX_SESSION'."
    log "  venv : $NNI_VENV"
    log "  bind : ${NNI_HOST}:${NNI_PORT}"
    log "  log  : $NNI_LOG"
    # The `exec` inside the shell wrapper means the venv's python becomes
    # PID 1 of the tmux pane, so `tmux list-panes -F '#{pane_pid}'` returns
    # the real server PID (handy for the local half's "READY" banner).
    tmux new-session -d -s "$NNI_TMUX_SESSION" \
        "source '$ACTIVATE' && exec nninteractive-slicer-server \
            --host '$NNI_HOST' --port '$NNI_PORT' 2>&1 | tee -a '$NNI_LOG'"
fi

# ── 4. health probe (loopback) ────────────────────────────────────────────
log "Probing http://127.0.0.1:${NNI_PORT}/docs (up to $((NNI_PROBE_MAX * NNI_PROBE_DELAY))s)…"
healthy=0
for i in $(seq 1 "$NNI_PROBE_MAX"); do
    if probe_local; then
        healthy=1
        log "  [OK]  probe ${i}/${NNI_PROBE_MAX} — server is answering."
        break
    fi
    if ! tmux has-session -t "$NNI_TMUX_SESSION" 2>/dev/null; then
        log "  [FAIL] tmux session vanished after launch."
        break
    fi
    log "  [..]  probe ${i}/${NNI_PROBE_MAX} — not ready, sleeping ${NNI_PROBE_DELAY}s."
    sleep "$NNI_PROBE_DELAY"
done

if [ "$healthy" != "1" ]; then
    log "Server did not become healthy. Last 60 lines of $NNI_LOG:"
    tail -60 "$NNI_LOG" 2>/dev/null || true
    die "nninteractive-slicer-server failed health probe on :${NNI_PORT}"
fi

# ── 5. summary ────────────────────────────────────────────────────────────
SERVER_PID=$(tmux list-panes -F '#{pane_pid}' -t "$NNI_TMUX_SESSION" 2>/dev/null | head -1)
PUBLIC_IP=""
if command -v curl >/dev/null; then
    PUBLIC_IP=$(curl -fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)
fi

cat <<EOF

==========================================================================
nninteractive-slicer-server is running.

  reused        : $REUSED
  tmux session  : $NNI_TMUX_SESSION
  pid           : ${SERVER_PID:-unknown}
  bind          : ${NNI_HOST}:${NNI_PORT}
  log           : $NNI_LOG
  public IP     : ${PUBLIC_IP:-<unknown — read from JS2 dashboard>}

Operator commands (inside the Guacamole terminal):
  tmux attach -t $NNI_TMUX_SESSION         # live tail (Ctrl-b d to detach)
  tail -f $NNI_LOG                         # passive tail
  tmux kill-session -t $NNI_TMUX_SESSION   # stop the server

Local Mac connects via:
  https://http-${PUBLIC_IP//./-}-${NNI_PORT}.proxy-js2-iu.exosphere.app/
==========================================================================
EOF
