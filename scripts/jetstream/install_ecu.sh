#!/usr/bin/env bash
# MorphoClaw ECU — one-line install on Jetstream / MorphoCloud.
#
# Paste in the Guacamole terminal (or pipe over SSH):
#
#   curl -fsSL https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/scripts/jetstream/install_ecu.sh | bash
#
# Installs/updates the repo, optional Python deps, and starts the ECU job
# server (localhost Slicer driver) in tmux. Re-running is idempotent.

set -euo pipefail

MORPHOCLAW_REPO="${MORPHOCLAW_REPO:-https://github.com/johntrue15/MorphoClaw.git}"
MORPHOCLAW_REF="${MORPHOCLAW_REF:-main}"
MORPHOCLAW_ECU_DIR="${MORPHOCLAW_ECU_DIR:-}"

if [[ -z "$MORPHOCLAW_ECU_DIR" ]]; then
  if [[ -d /media/volume/MyData ]] && [[ -w /media/volume/MyData ]]; then
    MORPHOCLAW_ECU_DIR="/media/volume/MyData/MorphoClaw"
  else
    MORPHOCLAW_ECU_DIR="$HOME/MorphoClaw"
  fi
fi

ECU_PORT="${MORPHOCLAW_ECU_PORT:-18765}"
ECU_HOST="${MORPHOCLAW_ECU_HOST:-0.0.0.0}"
ECU_TMUX="${MORPHOCLAW_ECU_TMUX:-morphoclaw-ecu}"
ECU_LOG="${MORPHOCLAW_ECU_LOG:-$HOME/morphoclaw_ecu.log}"

log() { printf '[install-ecu] %s\n' "$*"; }
die() { printf '[install-ecu] ERROR: %s\n' "$*" >&2; exit 1; }

for tool in git curl tmux python3; do
  command -v "$tool" >/dev/null 2>&1 || die "missing: $tool"
done

log "ECU directory: $MORPHOCLAW_ECU_DIR"
mkdir -p "$(dirname "$MORPHOCLAW_ECU_DIR")"

if [[ -d "$MORPHOCLAW_ECU_DIR/.git" ]]; then
  log "Updating existing clone…"
  git -C "$MORPHOCLAW_ECU_DIR" fetch origin "$MORPHOCLAW_REF"
  git -C "$MORPHOCLAW_ECU_DIR" checkout "$MORPHOCLAW_REF"
  git -C "$MORPHOCLAW_ECU_DIR" pull --ff-only origin "$MORPHOCLAW_REF" || true
else
  log "Cloning $MORPHOCLAW_REPO ($MORPHOCLAW_REF)…"
  git clone --depth 1 --branch "$MORPHOCLAW_REF" "$MORPHOCLAW_REPO" "$MORPHOCLAW_ECU_DIR"
fi

# Lightweight deps for controller scripts run ON the box (LLM keys stay on Mac).
if [[ -f "$MORPHOCLAW_ECU_DIR/requirements.txt" ]]; then
  log "Installing Python deps (user pip)…"
  python3 -m pip install --user -q -r "$MORPHOCLAW_ECU_DIR/requirements.txt" 2>/dev/null || \
    log "WARN: pip install skipped (optional)"
fi
python3 -m pip install --user -q pillow openai 2>/dev/null || true

export MORPHOCLAW_ECU_DIR
export MORPHOCLAW_ECU_PORT="$ECU_PORT"
export MORPHOCLAW_ECU_HOST="$ECU_HOST"
export MORPHOCLAW_ECU_TMUX="$ECU_TMUX"
export MORPHOCLAW_ECU_LOG="$ECU_LOG"

log "Starting ECU server (tmux session: $ECU_TMUX)…"
bash "$MORPHOCLAW_ECU_DIR/scripts/jetstream/restart_ecu.sh"

PUBLIC_IP="${JETSTREAM_PUBLIC_IP:-$(curl -fsS --max-time 3 ifconfig.me 2>/dev/null || echo '<jetstream-ip>')}"
IP_DASHES="${PUBLIC_IP//./-}"

cat <<EOF

================================================================================
MorphoClaw ECU ready
================================================================================
  Repo     : $MORPHOCLAW_ECU_DIR
  tmux     : tmux attach -t $ECU_TMUX
  Log      : $ECU_LOG
  Local    : http://127.0.0.1:$ECU_PORT/health
  Proxy    : https://http-${IP_DASHES}-${ECU_PORT}.proxy-js2-iu.exosphere.app/health

On your Mac (.env):
  MORPHOCLAW_ECU_URL=https://http-${IP_DASHES}-${ECU_PORT}.proxy-js2-iu.exosphere.app/
  JETSTREAM_PUBLIC_IP=$PUBLIC_IP

Mac controller example:
  cd /path/to/MorphoClaw
  python3 .github/scripts/jetstream_controller.py health
  python3 .github/scripts/jetstream_controller.py run \\
    --label pcb-copper \\
    -- python3 .github/scripts/slicer_remote_pcb_copper.py \\
         --phase copper --volume pcb_ti_jetstream --max-steps 8 \\
         --noise-manifest runs/pcb_noise_export_20260527/noise_manifest.json \\
         --out-dir runs/pcb_copper_ecu_test

Ensure 3D Slicer Web Server is running on localhost:2016 with "Slicer API exec" ON.
================================================================================
EOF
