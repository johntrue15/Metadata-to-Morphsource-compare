#!/usr/bin/env python3
"""
Reattach to an unshelved Jetstream2 / MorphoCloud instance and bring the
SlicerNNInteractive stack back online without manual `.env` editing.

This is the LOCAL half of `make unshelve IP=...`. It runs on your Mac and:

1. Builds the two Exosphere proxy URLs from the new public IP.
2. Verifies key-based SSH to ``exouser@<ip>`` works.
3. Rewrites ``JETSTREAM_PUBLIC_IP`` / ``NNI_REMOTE_URL`` / ``SLICER_WEBSERVER_URL``
   in the local ``.env`` (with a timestamped ``.env.bak.<ts>``).
4. (``--nninteractive``) scps and runs
   ``.github/scripts/jetstream_remote_restart.sh`` on the box, which starts
   ``nninteractive-slicer-server --host 0.0.0.0 --port 1527`` inside a
   detached tmux session.
5. Probes both ``:1527/docs`` (FastAPI) and (with ``--webserver``)
   ``:2016/slicer/screenshot`` (Slicer Web Server) through the Exosphere
   proxy. If the :2016 probe fails, prints the exact Python snippet to
   paste into Slicer's Python Interactor (sourced from
   ``slicer_start_webserver.py``), waits for Enter, and re-probes.
6. Prints a READY banner with the next-step pilot command.

Pure stdlib (no `requests`) so the system Python can run it without the
nnInteractive venv.

CLI
---
    python .github/scripts/jetstream_unshelve_start.py --ip 149.165.171.178 \\
        [--user exouser] [--ssh-key PATH] \\
        [--nninteractive] [--webserver] \\
        [--no-env-update] [--reuse-server] [--dry-run]

Scope: "already configured -> restart". Cold-bootstrap (no venv yet) and
auto-launching 3D Slicer over SSH are deferred to v2 — see
``docs/JETSTREAM_UNSHELVE.md`` for the planned shape.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = REPO_ROOT / ".env"
REMOTE_SH_LOCAL = REPO_ROOT / ".github" / "scripts" / "jetstream_remote_restart.sh"
SLICER_SNIPPET_PATH = REPO_ROOT / ".github" / "scripts" / "slicer_start_webserver.py"
REMOTE_SH_DEST = "/tmp/jetstream_remote_restart.sh"

DEFAULT_NNI_PORT = 1527
DEFAULT_WEBSERVER_PORT = 2016

ENV_KEYS_TO_REWRITE = (
    "JETSTREAM_PUBLIC_IP",
    "NNI_REMOTE_URL",
    "SLICER_WEBSERVER_URL",
)

IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def info(msg: str) -> None:
    print(f"[unshelve] {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n[unshelve] === {msg} ===", flush=True)


def warn(msg: str) -> None:
    print(f"[unshelve] WARN: {msg}", file=sys.stderr, flush=True)


def fail(msg: str, code: int = 1) -> None:
    print(f"[unshelve] FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def exosphere_url(ip: str, port: int) -> str:
    """Build the Exosphere any-port HTTPS proxy URL for ``ip`` + ``port``."""
    return f"https://http-{ip.replace('.', '-')}-{port}.proxy-js2-iu.exosphere.app/"


def validate_ip(ip: str) -> None:
    if not IPV4_RE.match(ip):
        fail(f"--ip must be an IPv4 address (got {ip!r})", code=2)
    for octet in ip.split("."):
        if not (0 <= int(octet) <= 255):
            fail(f"--ip {ip!r} has out-of-range octet", code=2)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _ssh_base(user: str, ip: str, ssh_key: Optional[str]) -> list[str]:
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd.append(f"{user}@{ip}")
    return cmd


def _scp_base(user: str, ip: str, ssh_key: Optional[str],
              src: str, dest: str) -> list[str]:
    cmd = [
        "scp",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd += [src, f"{user}@{ip}:{dest}"]
    return cmd


def ssh_preflight(user: str, ip: str, ssh_key: Optional[str]) -> None:
    """Confirm key-based SSH works. Print actionable hints on failure."""
    cmd = _ssh_base(user, ip, ssh_key) + ["true"]
    info(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        fail(f"SSH timed out connecting to {user}@{ip}. Check the IP and "
             f"that the instance is actually unshelved.", code=3)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        fail(
            f"SSH to {user}@{ip} failed (exit {proc.returncode}).\n"
            f"  - Confirm the public IP is correct (it changes on unshelve).\n"
            f"  - Confirm key-based auth is set up:\n"
            f"      ssh-copy-id {user}@{ip}\n"
            f"  - Or pass --ssh-key /path/to/private_key if the key isn't the default.\n"
            f"  - Confirm your local public key is in the instance's "
            f"~/.ssh/authorized_keys (Jetstream injects it on the first boot).",
            code=3,
        )
    info("SSH OK.")


# ---------------------------------------------------------------------------
# .env rewriting
# ---------------------------------------------------------------------------

def rewrite_env(env_path: Path, ip: str,
                nni_url: str, ws_url: str,
                dry_run: bool) -> Optional[Path]:
    """Replace IP + URL fields in ``env_path``. Returns backup path (or None
    if dry-run / no changes / file missing)."""
    if not env_path.exists():
        warn(f".env not found at {env_path} — skipping .env rewrite. "
             f"You'll need to export the URLs manually.")
        return None

    original = env_path.read_text()
    new_values = {
        "JETSTREAM_PUBLIC_IP": ip,
        "NNI_REMOTE_URL": nni_url,
        "SLICER_WEBSERVER_URL": ws_url,
    }

    lines = original.splitlines(keepends=False)
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        replaced = False
        for key, val in new_values.items():
            # Match `KEY=...` (with optional leading `# `) but ONLY if it's
            # the active assignment (i.e. not commented out — we leave
            # comments alone so the .env.example-style docs survive).
            if stripped.startswith(f"{key}="):
                new_lines.append(f"{key}={val}")
                seen.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    # Append any keys we didn't see, with a header so they're easy to find.
    missing = [k for k in new_values if k not in seen]
    if missing:
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append("# Added by jetstream_unshelve_start.py")
        for key in missing:
            new_lines.append(f"{key}={new_values[key]}")

    new_text = "\n".join(new_lines)
    if not original.endswith("\n"):
        # Preserve "no trailing newline" if that's how the file was.
        pass
    else:
        new_text += "\n"

    if new_text == original:
        info(".env already has the target values — no rewrite needed.")
        return None

    if dry_run:
        info(f"[dry-run] would rewrite {env_path} (keys: "
             f"{', '.join(new_values)}).")
        return None

    ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup = env_path.with_suffix(env_path.suffix + f".bak.{ts}")
    shutil.copy2(env_path, backup)
    env_path.write_text(new_text)
    info(f"Rewrote {env_path}  (backup: {backup.name})")
    return backup


# ---------------------------------------------------------------------------
# Remote restart
# ---------------------------------------------------------------------------

def remote_restart(user: str, ip: str, ssh_key: Optional[str],
                   reuse: bool, dry_run: bool) -> None:
    if not REMOTE_SH_LOCAL.exists():
        fail(f"missing remote half: {REMOTE_SH_LOCAL}", code=4)

    scp_cmd = _scp_base(user, ip, ssh_key, str(REMOTE_SH_LOCAL), REMOTE_SH_DEST)
    info(f"$ {' '.join(scp_cmd)}")
    if dry_run:
        info("[dry-run] skipping scp + ssh.")
        return

    proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        fail(f"scp failed (exit {proc.returncode})", code=4)

    env_prefix = "NNI_REUSE=1 " if reuse else ""
    remote_cmd = (
        _ssh_base(user, ip, ssh_key)
        + ["bash", "-lc", f"{env_prefix}bash {REMOTE_SH_DEST}"]
    )
    info(f"$ {' '.join(remote_cmd)}")
    # Stream output so the operator sees the tmux/probe progress live.
    proc = subprocess.run(remote_cmd)
    if proc.returncode != 0:
        fail(f"remote restart script exited {proc.returncode}", code=5)


# ---------------------------------------------------------------------------
# HTTPS probes (stdlib)
# ---------------------------------------------------------------------------

def _http_get_status(url: str, timeout: float = 8.0) -> int:
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "jetstream-unshelve/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Drain a few bytes so the connection closes cleanly.
            resp.read(256)
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def probe_with_retry(url: str, label: str, *,
                     max_attempts: int = 6, delay: float = 5.0,
                     ok_codes: tuple[int, ...] = (200,)) -> bool:
    info(f"Probing {label}: GET {url}")
    for i in range(1, max_attempts + 1):
        code = _http_get_status(url)
        if code in ok_codes:
            info(f"  [OK]  attempt {i}/{max_attempts} -> HTTP {code}")
            return True
        info(f"  [..]  attempt {i}/{max_attempts} -> HTTP {code or 'no-response'}; "
             f"sleeping {delay:.0f}s")
        if i < max_attempts:
            time.sleep(delay)
    warn(f"{label} did not respond after {max_attempts} attempts.")
    return False


# ---------------------------------------------------------------------------
# Slicer Web Server fallback prompt
# ---------------------------------------------------------------------------

def print_slicer_snippet_prompt(ws_url: str) -> None:
    print("")
    print("=" * 74)
    print("Slicer Web Server is not responding on :2016.")
    print("Paste the snippet below into Slicer's Python Interactor")
    print("(View > Python Interactor in the Guacamole desktop), then press Enter:")
    print("=" * 74)
    if SLICER_SNIPPET_PATH.exists():
        print(SLICER_SNIPPET_PATH.read_text().rstrip())
    else:
        # Fallback inline snippet if the file isn't there for some reason.
        print(_INLINE_SLICER_FALLBACK)
    print("=" * 74)
    print(f"After pasting, the Web Server module should report listening on port "
          f"{DEFAULT_WEBSERVER_PORT}.")
    print(f"Then this script will re-probe: {ws_url}")
    print("=" * 74)


_INLINE_SLICER_FALLBACK = """\
import slicer
slicer.util.selectModule('WebServer')
ws = slicer.modules.WebServerWidget
ws.advancedCollapsibleButton.collapsed = False
ws.slicerAPICheckBox.checked = True
if not ws.startStopButton.checked:
    ws.startStopButton.click()
print(slicer.modules.WebServerWidget.logic.server)
"""


# ---------------------------------------------------------------------------
# READY banner
# ---------------------------------------------------------------------------

def ready_banner(ip: str, nni_url: str, ws_url: str,
                 nni_ok: bool, ws_ok: Optional[bool]) -> None:
    print("")
    print("=" * 74)
    print("READY")
    print("=" * 74)
    print(f"  Public IP            : {ip}")
    print(f"  NNI server (:1527)   : {nni_url}  [{'OK' if nni_ok else 'NOT REACHABLE'}]")
    if ws_ok is None:
        print(f"  Slicer WS (:2016)    : {ws_url}  [skipped]")
    else:
        print(f"  Slicer WS (:2016)    : {ws_url}  [{'OK' if ws_ok else 'NOT REACHABLE'}]")
    print("")
    print("Local `.env` is updated. Next:")
    print("")
    print("  # 10-click bright-seed pilot against the remote (uses .env automatically):")
    print('  set -a && source .env && set +a')
    print('  "$HOME/.autoresearchclaw/nninteractive/bin/python" \\')
    print('      .github/scripts/eval_project358382_pilot.py \\')
    print('      --project-id 000358382 \\')
    print('      --project-query "Colors of Skull Anatomy" \\')
    print('      --specimens 3 \\')
    print('      --budgets 10,25,50,100 \\')
    print('      --out-dir "runs/pilot_project358382_$(date +%Y%m%dT%H%M%S)"')
    print("=" * 74)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jetstream_unshelve_start",
        description="Reattach to an unshelved JS2 instance: rewrite local "
                    ".env URLs, restart nninteractive-slicer-server over "
                    "SSH, probe both proxied endpoints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--ip", required=True,
                   help="New public IPv4 of the unshelved instance.")
    p.add_argument("--user", default="exouser",
                   help="SSH username (default: exouser).")
    p.add_argument("--ssh-key", default="",
                   help="Optional path to private key for SSH/scp.")
    p.add_argument("--nninteractive", action="store_true",
                   help="Restart the FastAPI nninteractive-slicer-server "
                        "on :1527 via the remote half.")
    p.add_argument("--webserver", action="store_true",
                   help="Also probe the Slicer Web Server on :2016. If "
                        "unreachable, print the Python Interactor snippet.")
    p.add_argument("--no-env-update", action="store_true",
                   help="Skip rewriting the local .env (URLs are still built "
                        "and printed).")
    p.add_argument("--reuse-server", action="store_true",
                   help="If the remote tmux session is already healthy, "
                        "leave it alone (faster than always restarting).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan; do not run scp/ssh or rewrite .env.")
    p.add_argument("--non-interactive", action="store_true",
                   help="Don't prompt for the Slicer Web Server fallback; "
                        "exit non-zero if :2016 is unreachable.")
    p.add_argument("--probe-attempts", type=int, default=6,
                   help="HTTPS probe retry budget (default: 6).")
    p.add_argument("--probe-delay", type=float, default=5.0,
                   help="Seconds between HTTPS probe attempts (default: 5).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    validate_ip(args.ip)

    nni_url = exosphere_url(args.ip, DEFAULT_NNI_PORT)
    ws_url = exosphere_url(args.ip, DEFAULT_WEBSERVER_PORT)
    ssh_key = args.ssh_key or None

    step(f"Target: {args.user}@{args.ip}")
    info(f"  NNI URL : {nni_url}")
    info(f"  WS URL  : {ws_url}")

    # Only require SSH if we're going to use it — pure-local runs (no
    # --nninteractive) should still be able to rewrite .env and probe the
    # public URLs from machines that have never SSH'd to the box.
    if args.dry_run:
        step("SSH preflight")
        info("[dry-run] skipping SSH preflight.")
    elif args.nninteractive:
        step("SSH preflight")
        ssh_preflight(args.user, args.ip, ssh_key)
    else:
        info("(no remote action requested: skipping SSH preflight)")

    step(".env rewrite")
    if args.no_env_update:
        info("--no-env-update: skipping .env rewrite.")
    else:
        rewrite_env(ENV_PATH, args.ip, nni_url, ws_url, args.dry_run)

    if args.nninteractive:
        step("Restart nninteractive-slicer-server on :1527 (remote)")
        remote_restart(args.user, args.ip, ssh_key,
                       reuse=args.reuse_server, dry_run=args.dry_run)
    else:
        info("(--nninteractive not set: skipping remote restart)")

    if args.dry_run:
        info("[dry-run] skipping HTTPS probes.")
        return 0

    step("HTTPS health probes through Exosphere proxy")
    nni_ok = probe_with_retry(
        nni_url + "docs", "nnInteractive FastAPI (:1527)",
        max_attempts=args.probe_attempts, delay=args.probe_delay,
    )

    ws_ok: Optional[bool] = None
    if args.webserver:
        ws_ok = probe_with_retry(
            ws_url + "slicer/screenshot", "Slicer Web Server (:2016)",
            max_attempts=args.probe_attempts, delay=args.probe_delay,
        )
        if not ws_ok:
            if args.non_interactive:
                warn("--non-interactive: not prompting for Slicer fallback.")
            else:
                print_slicer_snippet_prompt(ws_url)
                try:
                    input("Press Enter after pasting the snippet into Slicer "
                          "(or Ctrl-C to skip): ")
                except (EOFError, KeyboardInterrupt):
                    print("")
                    info("(skipped Slicer Web Server start)")
                else:
                    ws_ok = probe_with_retry(
                        ws_url + "slicer/screenshot",
                        "Slicer Web Server (:2016, after manual start)",
                        max_attempts=args.probe_attempts,
                        delay=args.probe_delay,
                    )

    ready_banner(args.ip, nni_url, ws_url, nni_ok, ws_ok)

    if not nni_ok:
        return 6
    if args.webserver and ws_ok is False:
        return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
