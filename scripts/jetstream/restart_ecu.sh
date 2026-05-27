#!/usr/bin/env bash
# Start / restart MorphoClaw ECU job server in tmux (Jetstream box).
set -euo pipefail

REPO="${MORPHOCLAW_ECU_DIR:-${HOME}/MorphoClaw}"
PORT="${MORPHOCLAW_ECU_PORT:-18765}"
HOST="${MORPHOCLAW_ECU_HOST:-0.0.0.0}"
SESSION="${MORPHOCLAW_ECU_TMUX:-morphoclaw-ecu}"
LOG="${MORPHOCLAW_ECU_LOG:-$HOME/morphoclaw_ecu.log}"

log() { printf '[restart-ecu] %s\n' "$*"; }
die() { printf '[restart-ecu] ERROR: %s\n' "$*" >&2; exit 1; }

command -v tmux >/dev/null 2>&1 || die "tmux required"
[[ -f "$REPO/.github/scripts/jetstream_ecu_server.py" ]] || \
  die "repo not found at $REPO — run install_ecu.sh first"

probe() {
  curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
}

if tmux has-session -t "$SESSION" 2>/dev/null && probe; then
  log "ECU already healthy in tmux:$SESSION"
  exit 0
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true

tmux new-session -d -s "$SESSION" \
  "cd '$REPO' && python3 .github/scripts/jetstream_ecu_server.py --host '$HOST' --port '$PORT' 2>&1 | tee -a '$LOG'"

for _ in $(seq 1 12); do
  sleep 1
  if probe; then
    log "ECU listening on $HOST:$PORT"
    exit 0
  fi
done

die "ECU failed health probe — check: tmux attach -t $SESSION"
