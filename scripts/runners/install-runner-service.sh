#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-runner-service.sh
#
# Promote the GitHub Actions runner from foreground `./run.sh` to a proper
# systemd service inside WSL Ubuntu, with auto-restart on crash.
#
# Run this once, inside WSL, as the user that owns ${RUNNER_DIR}
# (typically `morphoclaw`). After it finishes:
#
#   * the service is enabled (starts on boot of the WSL distro);
#   * the service is started;
#   * Restart=on-failure with a 30s backoff is configured via a drop-in;
#   * `runner-ctl.ps1 status` from the Windows host will report the
#     systemd-managed state instead of the foreground state.
#
# Idempotent: safe to re-run. The drop-in is rewritten each time.
#
# Prerequisites:
#   * setup-wsl-runner.sh has been run (so ${RUNNER_DIR} exists and the
#     runner is registered against GitHub).
#   * /etc/wsl.conf has [boot]\nsystemd=true and the distro has been
#     restarted at least once with `wsl --shutdown` since.
# ---------------------------------------------------------------------------

set -euo pipefail

RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-morphoclaw}"
RUNNER_NAME="${RUNNER_NAME:-DellXPS-wsl-gpu}"
GH_REPO="${GH_REPO:-johntrue15/MorphoClaw}"
# actions-runner builds the unit name from the repo + runner identity. The
# actual file is `actions.runner.<owner>-<repo>.<runner-name>.service`.
SERVICE_NAME="actions.runner.$(echo "$GH_REPO" | tr '/' '-').${RUNNER_NAME}.service"
DROP_IN_DIR="/etc/systemd/system/${SERVICE_NAME}.d"
DROP_IN="${DROP_IN_DIR}/restart.conf"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '    \033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '    \033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Sanity checks
# ---------------------------------------------------------------------------
step "1/5 Sanity checks"

if [ ! -d "$RUNNER_DIR" ]; then
    die "Runner dir not found: $RUNNER_DIR. Run setup-wsl-runner.sh first."
fi
if [ ! -x "$RUNNER_DIR/svc.sh" ]; then
    die "$RUNNER_DIR/svc.sh missing or not executable."
fi
if [ ! -f "$RUNNER_DIR/.runner" ]; then
    die "$RUNNER_DIR/.runner missing ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â runner is not configured. Re-run setup-wsl-runner.sh."
fi
if ! command -v systemctl >/dev/null 2>&1; then
    die "systemctl not found. Enable systemd in /etc/wsl.conf and run 'wsl --shutdown' from PowerShell first."
fi
if ! systemctl is-system-running --quiet 2>/dev/null && \
   [ "$(systemctl is-system-running 2>/dev/null || true)" != "degraded" ]; then
    # is-system-running prints things like "running", "degraded", "offline".
    # "offline" means systemd isn't pid 1 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â typical when /etc/wsl.conf is
    # missing systemd=true or the distro hasn't been restarted.
    state="$(systemctl is-system-running 2>/dev/null || echo unknown)"
    if [ "$state" = "offline" ] || [ "$state" = "unknown" ]; then
        die "systemd is not running inside this WSL distro (state=$state). Ensure /etc/wsl.conf has [boot]\\nsystemd=true and run 'wsl --shutdown' from PowerShell."
    fi
fi
ok "runner dir, svc.sh, .runner, systemctl all present"

# ---------------------------------------------------------------------------
# 2. Install the bundled systemd unit (idempotent)
# ---------------------------------------------------------------------------
step "2/5 Installing systemd unit via ./svc.sh install"

# If the unit already exists, svc.sh refuses to install again. Detect and
# skip rather than error out ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â we re-apply the drop-in regardless.
if systemctl list-unit-files --no-pager 2>/dev/null | grep -q "^${SERVICE_NAME}"; then
    ok "$SERVICE_NAME already installed ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â skipping ./svc.sh install"
else
    pushd "$RUNNER_DIR" >/dev/null
    sudo ./svc.sh install "$USER"
    popd >/dev/null
    ok "installed $SERVICE_NAME for user $USER"
fi

# ---------------------------------------------------------------------------
# 3. Drop in Restart=on-failure / backoff
# ---------------------------------------------------------------------------
step "3/5 Writing systemd drop-in $DROP_IN"

sudo mkdir -p "$DROP_IN_DIR"
sudo tee "$DROP_IN" >/dev/null <<EOF
# Managed by MorphoClaw scripts/runners/install-runner-service.sh.
# Re-runs of that script will rewrite this file.
#
# StartLimit* must live in [Unit] since systemd v229 â€” putting them in
# [Service] is silently ignored ("Unknown key name" in journal).
[Unit]
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Restart=on-failure
RestartSec=30
EOF
ok "drop-in written"

sudo systemctl daemon-reload
ok "systemctl daemon-reload"

# ---------------------------------------------------------------------------
# 4. Enable + start
# ---------------------------------------------------------------------------
step "4/5 Enabling and starting $SERVICE_NAME"

sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sleep 3

if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "$SERVICE_NAME is active"
else
    warn "$SERVICE_NAME not active ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â recent journal entries:"
    journalctl -u "$SERVICE_NAME" --no-pager -n 30 || true
    die "service failed to start"
fi

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
step "5/5 Done"

cat <<EOF

============================================================================
 MorphoClaw runner is now managed by systemd.
============================================================================

  Service: ${SERVICE_NAME}
  Drop-in: ${DROP_IN}
  Logs:    journalctl -u ${SERVICE_NAME} -f

Useful commands (run inside WSL):

  systemctl status ${SERVICE_NAME}
  sudo systemctl restart ${SERVICE_NAME}
  sudo systemctl stop ${SERVICE_NAME}

Or from the Windows host:

  pwsh scripts\\runners\\runner-ctl.ps1 status
  pwsh scripts\\runners\\runner-ctl.ps1 restart

If you ever uninstall the runner (./config.sh remove) or re-register it,
re-run this script to refresh the unit + drop-in.

EOF
