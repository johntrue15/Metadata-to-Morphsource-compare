#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# launch_skull_batch.sh - background driver for run_compare_set.py over the
# project 000358382 manifest. Runs in WSL and survives the parent terminal
# closing thanks to nohup + setsid.
#
# Usage:
#   bash scripts/dev/launch_skull_batch.sh           # start the batch
#   bash scripts/dev/launch_skull_batch.sh --status  # check liveness + progress.csv
#   bash scripts/dev/launch_skull_batch.sh --tail    # tail the log
#   bash scripts/dev/launch_skull_batch.sh --stop    # SIGTERM the orchestrator
#
# State lives under runs/skull_batch_358382/ (gitignored):
#   * orchestrator.pid     pid of run_compare_set.py
#   * orchestrator.log     stdout + stderr
#   * progress.csv         per-pair status (success / workflow_failed / ...)
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

RUNS_DIR="runs/skull_batch_358382"
mkdir -p "$RUNS_DIR"
PID_FILE="$RUNS_DIR/orchestrator.pid"
LOG_FILE="$RUNS_DIR/orchestrator.log"
PROGRESS_CSV="$RUNS_DIR/progress.csv"

# Pick the same python the rest of the project uses
NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
if [[ -x "$NNI_HOME/bin/python" ]]; then
    PY="$NNI_HOME/bin/python"
else
    PY="python3"
fi

# gh.exe lives in the Windows host
GH_DEFAULT="/mnt/c/Users/DELL_/AppData/Local/Microsoft/WinGet/Packages/GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe/bin/gh.exe"
export GH_PATH="${GH_PATH:-$GH_DEFAULT}"

cmd="${1:-start}"

is_alive() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    kill -0 "$pid" 2>/dev/null
}

case "$cmd" in
    --status|status)
        if is_alive; then
            pid="$(cat "$PID_FILE")"
            echo "RUNNING  pid=$pid  log=$LOG_FILE"
            ps -p "$pid" -o pid,etime,cmd
        else
            echo "NOT RUNNING (no pid file or process gone)"
        fi
        echo "--- progress.csv ---"
        if [[ -f "$PROGRESS_CSV" ]]; then
            wc -l "$PROGRESS_CSV"
            tail -5 "$PROGRESS_CSV"
        else
            echo "no progress.csv yet"
        fi
        ;;
    --tail|tail)
        tail -F "$LOG_FILE"
        ;;
    --stop|stop)
        if is_alive; then
            pid="$(cat "$PID_FILE")"
            echo "Stopping pid=$pid"
            kill "$pid"
            for _ in 1 2 3 4 5; do
                sleep 1
                kill -0 "$pid" 2>/dev/null || break
            done
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
        echo "stopped"
        ;;
    start|"")
        if is_alive; then
            echo "Already running (pid=$(cat "$PID_FILE")). Use --status / --tail / --stop."
            exit 0
        fi
        echo "==> launching run_compare_set.py in background"
        echo "    python:     $PY"
        echo "    gh:         $GH_PATH"
        echo "    manifest:   Tests/fixtures/nninteractive_compare/_manifest_358382.json"
        echo "    log:        $LOG_FILE"
        echo "    pid file:   $PID_FILE"

        nohup setsid "$PY" scripts/dev/run_compare_set.py \
            --manifest Tests/fixtures/nninteractive_compare/_manifest_358382.json \
            --crop-around-mesh-mm 5 \
            --max-voxel-axis 384 \
            --voxelize-backend vtk \
            --max-steps 12 \
            --align-mesh-to-ct centroid \
            --poll-every-s 30 \
            --max-minutes 240 \
            > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        sleep 2
        if is_alive; then
            echo "==> launched, pid=$(cat "$PID_FILE")"
            echo "==> tail with: bash scripts/dev/launch_skull_batch.sh --tail"
        else
            echo "ERR: orchestrator died immediately. See $LOG_FILE."
            tail -30 "$LOG_FILE" || true
            exit 1
        fi
        ;;
    *)
        echo "unknown command: $cmd" >&2
        exit 64
        ;;
esac
