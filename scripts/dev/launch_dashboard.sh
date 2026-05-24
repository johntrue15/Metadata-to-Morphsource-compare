#!/usr/bin/env bash
# Launch the localhost dashboard for the project-358382 skull batch.
# Re-uses the same python the orchestrator runs under so it works on the
# Dell XPS WSL setup without extra venv plumbing.
#
# Usage:
#   bash scripts/dev/launch_dashboard.sh            # foreground, opens browser
#   bash scripts/dev/launch_dashboard.sh --port 8765
#   bash scripts/dev/launch_dashboard.sh --no-browser
#
# Once running, point a Windows browser at http://localhost:7860/ - WSL2
# auto-forwards 127.0.0.1 ports to the Windows host.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
if [[ -x "$NNI_HOME/bin/python" ]]; then
    PY="$NNI_HOME/bin/python"
else
    PY="${PYTHON:-python3}"
fi

exec "$PY" scripts/dev/dashboard.py "$@"
