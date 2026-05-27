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
ECU_INSTALL_LOCAL = REPO_ROOT / "scripts" / "jetstream" / "install_ecu.sh"
SLICER_SNIPPET_PATH = REPO_ROOT / ".github" / "scripts" / "slicer_start_webserver.py"
REMOTE_SH_DEST = "/tmp/jetstream_remote_restart.sh"

DEFAULT_NNI_PORT = 1527
DEFAULT_WEBSERVER_PORT = 2016
DEFAULT_ECU_PORT = 18765

# IMPC sample data — same .nrrd you load manually via Slicer's Sample Data
# module. Hosted in SlicerMorph/SampleData. Lives in Slicer's tempdir after
# first load, so re-runs are near-instant.
DEFAULT_SAMPLE_URL = (
    "https://raw.githubusercontent.com/SlicerMorph/SampleData/master/"
    "IMPC_sample_data.nrrd"
)
DEFAULT_SAMPLE_NAME = "IMPC_sample_data"

ENV_KEYS_TO_REWRITE = (
    "JETSTREAM_PUBLIC_IP",
    "NNI_REMOTE_URL",
    "SLICER_WEBSERVER_URL",
    "MORPHOCLAW_ECU_URL",
)

IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


# Slicer-side recipe to fetch a NRRD/NIfTI URL into the tempdir (cached
# across invocations by URL filename) and load it into the scene as the
# active background volume. Returns shape + spacing for confirmation in
# the READY banner. Mirrors the recipe shape used by
# .github/scripts/remote_volume_io.py:_LOAD_FROM_PATH_BODY.
LOAD_SAMPLE_FROM_URL_RECIPE = """\
import slicer, os, tempfile, urllib.request, traceback
out = {}
try:
    td = os.path.join(tempfile.gettempdir(), "ms_remote_samples")
    os.makedirs(td, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in _NAME)
    local_path = os.path.join(td, safe + os.path.splitext(_URL)[1])
    out["local_path"] = local_path
    if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
        with urllib.request.urlopen(_URL, timeout=120) as r:
            with open(local_path, "wb") as f:
                while True:
                    blk = r.read(1 << 20)
                    if not blk:
                        break
                    f.write(blk)
        out["downloaded"] = True
    else:
        out["downloaded"] = False
    out["size_bytes"] = os.path.getsize(local_path)
    # Replace any existing copy with the same name so re-runs don't pile up.
    for existing in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if existing.GetName() == _NAME:
            slicer.mrmlScene.RemoveNode(existing)
    node = slicer.util.loadVolume(local_path, properties={"name": _NAME})
    if node is None:
        out["status"] = "load_failed"
    else:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        sel.SetActiveVolumeID(node.GetID())
        slicer.app.applicationLogic().PropagateVolumeSelection(0)
        slicer.util.setSliceViewerLayers(background=node, fit=True)
        arr = slicer.util.arrayFromVolume(node)
        out["status"] = "ok"
        out["volume_id"] = node.GetID()
        out["volume_name"] = node.GetName()
        out["shape_kji"] = list(arr.shape)
        out["dtype"] = str(arr.dtype)
        out["spacing_mm"] = [round(s, 6) for s in node.GetSpacing()]
        out["origin"] = [round(o, 6) for o in node.GetOrigin()]
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""


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
                ecu_url: str,
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
        "MORPHOCLAW_ECU_URL": ecu_url,
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

def remote_ecu_install(user: str, ip: str, ssh_key: Optional[str],
                      dry_run: bool) -> None:
    """Run install_ecu.sh on the box over SSH (clone + start ECU server)."""
    install_url = (
        "https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/"
        "scripts/jetstream/install_ecu.sh"
    )
    remote_cmd = (
        _ssh_base(user, ip, ssh_key)
        + ["bash", "-lc", f"curl -fsSL {install_url!r} | bash"]
    )
    info("$ " + " ".join(remote_cmd))
    if dry_run:
        info("[dry-run] skipping ECU install.")
        return
    proc = subprocess.run(remote_cmd)
    if proc.returncode != 0:
        warn(f"ECU install exited {proc.returncode} — install manually in Guacamole.")


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


def post_slicer_exec(base_url: str, source: str,
                     timeout: float = 180.0) -> dict:
    """POST a Python source string to /slicer/exec and return parsed JSON.

    Stdlib-only mirror of remote_volume_io._post_python so the local
    half stays dependency-free (this script runs from the Mac's system
    Python, not the nnInteractive venv).
    """
    body = source.encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/slicer/exec",
        data=body, method="POST",
        headers={"Content-Type": "text/plain",
                 "User-Agent": "jetstream-unshelve/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            status = int(resp.status)
    except urllib.error.HTTPError as e:
        return {"status": "http_error", "http_code": e.code,
                "body_preview": (e.read() or b"")[:300].decode(
                    "utf-8", errors="replace")}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"status": "transport_error", "error": repr(e)}
    if status != 200:
        return {"status": "http_error", "http_code": status,
                "body_preview": content[:300].decode("utf-8", errors="replace")}
    try:
        import json as _json
        return _json.loads(content)
    except Exception as e:
        return {"status": "non_json_body", "error": repr(e),
                "body_preview": content[:300].decode("utf-8", errors="replace")}


def load_sample_in_slicer(ws_url: str, sample_url: str,
                          sample_name: str) -> dict:
    """Download `sample_url` inside Slicer and load it into the scene."""
    info(f"Loading sample into Slicer: {sample_name}")
    info(f"  URL: {sample_url}")
    # Prelude assigns recipe inputs as plain top-level vars (mirrors the
    # pattern remote_volume_io.py uses to avoid str.format brace headaches).
    prelude = (
        f"_URL = {sample_url!r}\n"
        f"_NAME = {sample_name!r}\n"
    )
    result = post_slicer_exec(ws_url, prelude + LOAD_SAMPLE_FROM_URL_RECIPE)
    status = result.get("status")
    if status == "ok":
        downloaded = result.get("downloaded", False)
        info(f"  [OK]  loaded {result.get('volume_name')!r}  "
             f"shape={result.get('shape_kji')}  "
             f"spacing={result.get('spacing_mm')}  "
             f"({'downloaded ' if downloaded else 'cached '}"
             f"{result.get('size_bytes', 0):,} bytes)")
    else:
        warn(f"  load_sample returned status={status!r}: {result}")
    return result


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

def ready_banner(ip: str, nni_url: str, ws_url: str, ecu_url: str,
                 nni_ok: bool, ws_ok: Optional[bool], ecu_ok: Optional[bool],
                 sample_loaded: Optional[dict] = None) -> None:
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
    if ecu_ok is None:
        print(f"  MorphoClaw ECU (:18765): {ecu_url}  [skipped]")
    else:
        print(f"  MorphoClaw ECU (:18765): {ecu_url}  [{'OK' if ecu_ok else 'NOT REACHABLE'}]")
    if sample_loaded is not None:
        if sample_loaded.get("status") == "ok":
            print(f"  Sample in scene      : {sample_loaded.get('volume_name')!r}  "
                  f"shape={sample_loaded.get('shape_kji')}")
        else:
            print(f"  Sample in scene      : NOT LOADED ({sample_loaded.get('status')!r})")
    print("")
    print("Controller (Mac) — run jobs on ECU with localhost Slicer:")
    print('  set -a && source .env && set +a')
    print('  python3 .github/scripts/jetstream_controller.py health')
    print('  python3 .github/scripts/jetstream_controller.py preset pcb-copper-test --wait')
    print("")
    print("One-line ECU install (Guacamole terminal):")
    print('  curl -fsSL https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/scripts/jetstream/install_ecu.sh | bash')
    print("")
    print("  # Dispatch the 10-click pilot via GitHub Actions on the Dell GPU")
    print("  # runner. Mac mini stays a driver (docs/RUNNER_TOPOLOGY.md).")
    print("  make pilot-dell")
    print("")
    print("  # Override the defaults (specimens, budgets, manifest, record_to):")
    print("  make pilot-dell SPECIMENS=5 BUDGETS=10,25")
    print("")
    print("  # Tail the most recent GH Actions run:")
    print("  make tail")
    print("")
    print("  # Once the JS2 GHA runner is registered (Phase 3 of")
    print("  # docs/RUNNER_TOPOLOGY.md), this replaces pilot-dell for runs")
    print("  # that should land on the JS2 box this unshelve just brought up:")
    print("  make pilot-jetstream")
    print("")
    print("  ! DO NOT run eval_project358382_pilot.py in this terminal. The")
    print("  ! script's Darwin guard refuses on macOS; see")
    print("  ! docs/RUNNER_TOPOLOGY.md \"2026-05-24 cautionary tale\".")
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
    p.add_argument("--load-sample", action="store_true",
                   help="After :2016 is healthy, POST a /slicer/exec recipe "
                        "that downloads + loads the IMPC sample data into "
                        "Slicer's scene (same effect as Sample Data > IMPC). "
                        "Implies --webserver.")
    p.add_argument("--sample-url", default=DEFAULT_SAMPLE_URL,
                   help=f"Override the sample URL used by --load-sample "
                        f"(default: {DEFAULT_SAMPLE_URL}).")
    p.add_argument("--sample-name", default=DEFAULT_SAMPLE_NAME,
                   help=f"Override the Slicer scene node name used by "
                        f"--load-sample (default: {DEFAULT_SAMPLE_NAME}).")
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
    p.add_argument("--ecu", action="store_true",
                   help="Install/start MorphoClaw ECU (:18765) on the remote "
                        "box via SSH (curl | bash install_ecu.sh).")
    p.add_argument("--probe-ecu", action="store_true",
                   help="Probe MorphoClaw ECU /health on :18765.")
    p.add_argument("--probe-attempts", type=int, default=6,
                   help="HTTPS probe retry budget (default: 6).")
    p.add_argument("--probe-delay", type=float, default=5.0,
                   help="Seconds between HTTPS probe attempts (default: 5).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    validate_ip(args.ip)

    # --load-sample inherently needs the Slicer Web Server probe to pass
    # (it POSTs to /slicer/exec), so promote --webserver automatically.
    if args.load_sample and not args.webserver:
        info("--load-sample implies --webserver; enabling it.")
        args.webserver = True

    nni_url = exosphere_url(args.ip, DEFAULT_NNI_PORT)
    ws_url = exosphere_url(args.ip, DEFAULT_WEBSERVER_PORT)
    ecu_url = exosphere_url(args.ip, DEFAULT_ECU_PORT)
    ssh_key = args.ssh_key or None

    step(f"Target: {args.user}@{args.ip}")
    info(f"  NNI URL : {nni_url}")
    info(f"  WS URL  : {ws_url}")
    info(f"  ECU URL : {ecu_url}")

    # Only require SSH if we're going to use it — pure-local runs (no
    # --nninteractive) should still be able to rewrite .env and probe the
    # public URLs from machines that have never SSH'd to the box.
    if args.dry_run:
        step("SSH preflight")
        info("[dry-run] skipping SSH preflight.")
    elif args.nninteractive or args.ecu:
        step("SSH preflight")
        ssh_preflight(args.user, args.ip, ssh_key)
    else:
        info("(no remote action requested: skipping SSH preflight)")

    step(".env rewrite")
    if args.no_env_update:
        info("--no-env-update: skipping .env rewrite.")
    else:
        rewrite_env(ENV_PATH, args.ip, nni_url, ws_url, ecu_url, args.dry_run)

    if args.nninteractive:
        step("Restart nninteractive-slicer-server on :1527 (remote)")
        remote_restart(args.user, args.ip, ssh_key,
                       reuse=args.reuse_server, dry_run=args.dry_run)
    else:
        info("(--nninteractive not set: skipping remote restart)")

    if args.ecu:
        step("Install MorphoClaw ECU on :18765 (remote)")
        remote_ecu_install(args.user, args.ip, ssh_key, dry_run=args.dry_run)

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

    ecu_ok: Optional[bool] = None
    if args.probe_ecu:
        ecu_ok = probe_with_retry(
            ecu_url + "health", "MorphoClaw ECU (:18765)",
            max_attempts=args.probe_attempts, delay=args.probe_delay,
        )

    sample_loaded: Optional[dict] = None
    if args.load_sample:
        if ws_ok:
            step(f"Load sample into Slicer ({args.sample_name})")
            sample_loaded = load_sample_in_slicer(
                ws_url, args.sample_url, args.sample_name,
            )
        else:
            warn("--load-sample requested but :2016 is not reachable; "
                 "skipping. Start the Slicer Web Server first and re-run "
                 "with --load-sample.")

    ready_banner(args.ip, nni_url, ws_url, ecu_url, nni_ok, ws_ok, ecu_ok,
                 sample_loaded=sample_loaded)

    if not nni_ok:
        return 6
    if args.webserver and ws_ok is False:
        return 7
    if args.probe_ecu and ecu_ok is False:
        return 8
    if args.load_sample and (sample_loaded is None
                              or sample_loaded.get("status") != "ok"):
        return 9
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
