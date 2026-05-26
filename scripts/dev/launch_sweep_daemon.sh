#!/usr/bin/env bash
# Launch the 24/7 bright-seed sweep daemon against the Dell-XPS GPU
# venv. Idempotent: if a daemon is already running it just attaches a
# 'tail -f' to the daemon log instead of starting a second one.
#
# Usage:
#   ./scripts/dev/launch_sweep_daemon.sh                  # start (or attach)
#   ./scripts/dev/launch_sweep_daemon.sh --once           # drain queue + exit
#   ./scripts/dev/launch_sweep_daemon.sh --max-jobs 4     # cap and exit
#
# The daemon writes JSONL state + per-job outputs under
# ``paper_artifacts/sweep/``; tail that directory or run
# ``python .github/scripts/sweep_harness.py status`` for a quick
# snapshot.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STATE_DIR="${SWEEP_STATE_DIR:-$REPO_ROOT/paper_artifacts/sweep}"
NNI_PYTHON="${NNINTERACTIVE_PYTHON:-$HOME/.autoresearchclaw/nninteractive/bin/python}"
PARENT_PY="${PARENT_PYTHON:-/home/morphoclaw/miniforge3/bin/python}"

mkdir -p "$STATE_DIR"

DAEMON_LOG="$STATE_DIR/sweep_daemon.log"
PID_FILE="$STATE_DIR/sweep_daemon.pid"

# If a daemon is already running, just show its log.
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE")"
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Sweep daemon already running (pid=$OLD_PID). Tailing log..."
        echo "  log: $DAEMON_LOG"
        echo "  Ctrl-C to detach; daemon keeps running."
        tail -F "$DAEMON_LOG"
        exit 0
    else
        echo "Stale pid file $PID_FILE (pid $OLD_PID not running). Cleaning up."
        rm -f "$PID_FILE"
    fi
fi

cd "$REPO_ROOT"

echo "Starting sweep daemon"
echo "  repo:       $REPO_ROOT"
echo "  state dir:  $STATE_DIR"
echo "  log:        $DAEMON_LOG"
echo "  parent py:  $PARENT_PY"
echo "  nni py:     $NNI_PYTHON"
echo "  args:       $*"

# `setsid` puts the worker into its own session so it survives the
# parent bash + WSL terminal exit (nohup alone is not enough under
# `wsl -d ... -- bash -lc ...`). The `</dev/null` cuts the stdin tie
# to the controlling terminal that would otherwise SIGHUP the child.
setsid bash -c "
    exec '$PARENT_PY' .github/scripts/sweep_harness.py \\
        --state-dir '$STATE_DIR' \\
        run \\
        --nni-python '$NNI_PYTHON' \\
        $* \\
        >>'$DAEMON_LOG' 2>&1
" </dev/null >>"$DAEMON_LOG" 2>&1 &

DAEMON_PID=$!
disown "$DAEMON_PID" 2>/dev/null || true
echo "$DAEMON_PID" >"$PID_FILE"
sleep 1
# Re-check: the setsid wrapper exits immediately, so the actual
# long-running pid is one of its children. Find it.
WORKER_PID="$(pgrep -P "$DAEMON_PID" -f sweep_harness | head -1 || true)"
if [[ -n "$WORKER_PID" ]]; then
    echo "$WORKER_PID" >"$PID_FILE"
    DAEMON_PID="$WORKER_PID"
fi
echo "Daemon started pid=$DAEMON_PID"
echo "Tail $DAEMON_LOG to watch."
