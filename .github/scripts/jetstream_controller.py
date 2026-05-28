#!/usr/bin/env python3
"""
Mac-side controller for the MorphoClaw Jetstream ECU.

Submits jobs to the ECU HTTP API (short proxy-friendly requests). The ECU
runs ``slicer_remote_*`` scripts against ``http://127.0.0.1:2016/`` locally.

Env:
  MORPHOCLAW_ECU_URL   https://http-<ip-dashes>-18765.proxy-js2-iu.exosphere.app/
  MORPHOCLAW_ECU_TOKEN optional bearer token (must match ECU)

Examples::

    python3 .github/scripts/jetstream_controller.py health

    python3 .github/scripts/jetstream_controller.py run --wait -- \\
        python3 .github/scripts/slicer_remote_pcb_copper.py \\
        --phase copper --volume pcb_ti_jetstream --max-steps 8 \\
        --out-dir runs/pcb_copper_ecu

    python3 .github/scripts/jetstream_controller.py preset pcb-copper-test
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
# Jobs run on Jetstream; cwd "." resolves to the ECU server's repo checkout.
ECU_CWD = "."

# Pushed to Jetstream via ``deploy`` (base64 over ECU — no SSH required).
DEPLOY_FILES = (
    ".github/scripts/slicer_remote_pcb_copper.py",
    ".github/scripts/pcb_preprocess_layers.py",
    ".github/scripts/pcb_figure_gt.py",
    ".github/scripts/jetstream_ecu_server.py",
    ".github/scripts/jetstream_controller.py",
    ".github/scripts/slicer_remote_bright_seed.py",
    ".github/scripts/slicer_remote_loop.py",
    ".github/scripts/remote_volume_io.py",
    ".github/scripts/run_telemetry.py",
    "scripts/jetstream/restart_ecu.sh",
    "scripts/jetstream/install_ecu.sh",
)


def _ecu_url() -> str:
    url = os.environ.get("MORPHOCLAW_ECU_URL", "").strip()
    if not url:
        ip = os.environ.get("JETSTREAM_PUBLIC_IP", "").strip()
        if ip:
            port = os.environ.get("MORPHOCLAW_ECU_PORT", "18765")
            url = f"https://http-{ip.replace('.', '-')}-{port}.proxy-js2-iu.exosphere.app/"
    if not url:
        sys.exit("ERROR: set MORPHOCLAW_ECU_URL (or JETSTREAM_PUBLIC_IP) in .env")
    return url.rstrip("/")


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    tok = os.environ.get("MORPHOCLAW_ECU_TOKEN", "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _request(method: str, path: str, body: Optional[dict] = None,
             timeout: float = 30) -> dict:
    url = _ecu_url() + path
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = {"error": raw[:500]}
        raise RuntimeError(f"HTTP {e.code} {path}: {detail}") from e


def cmd_health(_: argparse.Namespace) -> int:
    r = _request("GET", "/health")
    print(json.dumps(r, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.remote_argv:
        sys.exit("ERROR: pass remote command after --")
    argv = list(args.remote_argv)
    if argv[0] == "--":
        argv = argv[1:]
    body: dict[str, Any] = {
        "argv": argv,
        "cwd": str(args.cwd) if args.cwd is not None else ECU_CWD,
        "label": args.label or "",
        "env": {},
    }
    if args.env:
        for pair in args.env:
            if "=" not in pair:
                sys.exit(f"bad --env {pair!r}, want KEY=VAL")
            k, v = pair.split("=", 1)
            body["env"][k] = v
    # Forward OpenAI key only when explicitly requested (default: ECU scripts
    # that need LLM get key from Mac via --env OPENAI_API_KEY=...).
    r = _request("POST", "/v1/jobs", body)
    job_id = r["job_id"]
    print(f"submitted job {job_id}  status={r.get('status')}")
    if not args.wait:
        print(f"poll: python3 .github/scripts/jetstream_controller.py status --job {job_id}")
        return 0
    return _wait_job(job_id, args.poll, args.tail)


def _wait_job(job_id: str, poll: float, tail: int) -> int:
    last_log_len = 0
    while True:
        st = _request("GET", f"/v1/jobs/{job_id}")
        status = st.get("status")
        if status in ("succeeded", "failed"):
            lg = _request("GET", f"/v1/jobs/{job_id}/log?tail={tail}")
            log = lg.get("log", "")
            if log:
                print(log)
            print(f"job {job_id} finished: {status} exit={st.get('exit_code')}")
            return 0 if status == "succeeded" else 1
        lg = _request("GET", f"/v1/jobs/{job_id}/log?tail={tail}")
        log = lg.get("log", "")
        if len(log) > last_log_len:
            sys.stdout.write(log[last_log_len:])
            sys.stdout.flush()
            last_log_len = len(log)
        time.sleep(poll)


def cmd_status(args: argparse.Namespace) -> int:
    r = _request("GET", f"/v1/jobs/{args.job}")
    print(json.dumps(r, indent=2))
    if args.log:
        lg = _request("GET", f"/v1/jobs/{args.job}/log?tail={args.tail}")
        print("--- log ---")
        print(lg.get("log", ""))
    return 0


def cmd_preset(args: argparse.Namespace) -> int:
    max_steps = args.max_steps
    ct_volume = os.environ.get(
        "PCB_CT_VOLUME", "/home/exouser/Desktop/pcb_ti_jetstream.nii.gz"
    )
    gt_manifest = os.environ.get(
        "PCB_FIGURE_GT_MANIFEST",
        "data/pcb/reference/generated/figure_gt_manifest.json",
    )
    gt_labelmap = os.environ.get(
        "PCB_GT_LABELMAP",
        "runs/pcb_gt_registered/latest/top_copper_gt_registered.nii.gz",
    )
    presets = {
        "pcb-copper-test": [
            "python3", ".github/scripts/slicer_remote_pcb_copper.py",
            "--phase", "copper",
            "--volume", "pcb_ti_jetstream",
            "--remote-volume-path", "/home/exouser/Desktop/pcb_ti_jetstream.nii.gz",
            "--max-steps", str(max_steps if max_steps is not None else 8),
            "--noise-manifest", "runs/pcb_noise_export_20260527/noise_manifest.json",
            "--out-dir", f"runs/pcb_copper_ecu_{time.strftime('%Y%m%dT%H%M%S')}",
        ],
        "pcb-export-noise": [
            "python3", ".github/scripts/slicer_remote_pcb_copper.py",
            "--phase", "export-noise",
            "--volume", "pcb_ti_jetstream",
            "--remote-volume-path", "/home/exouser/Desktop/pcb_ti_jetstream.nii.gz",
            "--exclude-segment", "Segment_232",
            "--out-dir", f"runs/pcb_noise_ecu_{time.strftime('%Y%m%dT%H%M%S')}",
        ],
        "pcb-bright-100": [
            "python3", ".github/scripts/slicer_remote_bright_seed.py",
            "--volume", "pcb_ti_jetstream",
            "--reset-first",
            "--max-steps", "100",
            "--no-stop-rules",
            "--no-screenshots",
            "--skip-remote-env",
            "--label", "pcb_ti",
            "--out-dir", f"runs/pcb_bright_ecu_{time.strftime('%Y%m%dT%H%M%S')}",
        ],
        "colors-skull-completion": [
            "python3", ".github/scripts/jetstream_10click_from_url.py",
            "--fixture", "data/sample/colors_of_skull_urls.json",
            "--max-steps", str(max_steps if max_steps is not None else 10000),
            "--no-screenshots",
            "--reset-first",
            "--skip-remote-env",
            "--skip-failed-steps",
            "--label", "crotalus_skull_completion",
            "--out-dir", f"runs/colors_skull_bright_{time.strftime('%Y%m%dT%H%M%S')}",
        ],
        "pcb-preprocess-layers": [
            "python3", ".github/scripts/pcb_preprocess_layers.py",
            "--input-volume", ct_volume,
            "--out-dir", f"runs/pcb_preprocess_{time.strftime('%Y%m%dT%H%M%S')}",
            "--dewarp-mode", "normalize",
            "--stitch-mode", "identity",
            "--flatten-layers", "4",
        ],
        "pcb-register-gt": [
            "python3", ".github/scripts/pcb_figure_gt.py",
            "register",
            "--manifest", gt_manifest,
            "--ct-volume", ct_volume,
            "--out-dir", f"runs/pcb_gt_registered_{time.strftime('%Y%m%dT%H%M%S')}",
            "--refine-translation",
        ],
        "pcb-iterate-score": [
            "python3", ".github/scripts/slicer_remote_pcb_copper.py",
            "--phase", "copper",
            "--volume", "pcb_ti_jetstream",
            "--remote-volume-path", ct_volume,
            "--max-steps", str(max_steps if max_steps is not None else 20),
            "--noise-manifest", "runs/pcb_noise_export_20260527/noise_manifest.json",
            "--gt-labelmap", gt_labelmap,
            "--score-each-step",
            "--score-no-surface",
            "--out-dir", f"runs/pcb_iterative_score_{time.strftime('%Y%m%dT%H%M%S')}",
        ],
    }
    if args.name not in presets:
        sys.exit(f"unknown preset {args.name!r}; choose from {list(presets)}")
    ns = argparse.Namespace(
        remote_argv=presets[args.name],
        cwd=None,
        label=args.name,
        env=[],
        wait=args.wait,
        poll=args.poll,
        tail=args.tail,
    )
    if args.name in ("pcb-copper-test", "pcb-iterate-score") and os.environ.get("OPENAI_API_KEY"):
        ns.env = [f"OPENAI_API_KEY={os.environ['OPENAI_API_KEY']}"]
    return cmd_run(ns)


def _wait_health(timeout: float = 60.0, poll: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = _request("GET", "/health", timeout=5)
            if r.get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def cmd_deploy(args: argparse.Namespace) -> int:
    """Sync local scripts to the Jetstream ECU repo checkout; optionally restart ECU."""
    import textwrap

    files = list(args.files or DEPLOY_FILES)
    missing = [f for f in files if not (REPO_ROOT / f).is_file()]
    if missing:
        sys.exit(f"deploy: missing files: {missing}")

    print(f"Deploying {len(files)} file(s) to Jetstream ECU repo…")
    for rel in files:
        path = REPO_ROOT / rel
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        snippet = textwrap.dedent(f"""
            import base64, pathlib
            p = pathlib.Path({rel!r})
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode({b64!r}))
            print("wrote", p, p.stat().st_size)
        """).strip()
        body = {
            "argv": ["python3", "-c", snippet],
            "cwd": ECU_CWD,
            "label": f"deploy-{Path(rel).name}",
            "env": {},
        }
        r = _request("POST", "/v1/jobs", body)
        job_id = r["job_id"]
        if args.wait:
            rc = _wait_job(job_id, args.poll, args.tail)
            if rc != 0:
                return rc
        else:
            print(f"  {rel} -> job {job_id}")

    if args.restart:
        print("Restarting ECU (tmux)…")
        body = {
            "argv": ["bash", "scripts/jetstream/restart_ecu.sh"],
            "cwd": ECU_CWD,
            "label": "restart-ecu",
            "env": {},
        }
        try:
            r = _request("POST", "/v1/jobs", body, timeout=15)
            if args.wait:
                _wait_job(r["job_id"], args.poll, args.tail)
        except Exception as e:
            print(f"  restart job submit: {e!r} (ECU may have recycled)")
        print("Waiting for ECU /health…")
        if _wait_health(timeout=90.0, poll=3.0):
            print("ECU healthy.")
        else:
            print("WARN: ECU health probe failed — run restart_ecu.sh on the box.")
            return 1

    if not args.wait:
        print("Deploy jobs submitted. Poll with: status --job <id>")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ph = sub.add_parser("health", help="GET /health on ECU")
    ph.set_defaults(func=cmd_health)

    pr = sub.add_parser("run", help="submit arbitrary argv to ECU")
    pr.add_argument("remote_argv", nargs="*", help="command after --")
    pr.add_argument("--cwd", type=Path, default=None,
                    help="working dir on Jetstream (default: repo root)")
    pr.add_argument("--label", default="")
    pr.add_argument("--env", action="append", default=[],
                    help="KEY=VAL passed to remote job env")
    pr.add_argument("--wait", action="store_true", help="stream log until done")
    pr.add_argument("--poll", type=float, default=5.0)
    pr.add_argument("--tail", type=int, default=131072)
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("status", help="job status")
    ps.add_argument("--job", required=True)
    ps.add_argument("--log", action="store_true")
    ps.add_argument("--tail", type=int, default=65536)
    ps.set_defaults(func=cmd_status)

    pp = sub.add_parser("preset", help="named remote workflows")
    pp.add_argument("name", choices=[
        "pcb-copper-test", "pcb-export-noise", "pcb-bright-100",
        "colors-skull-completion", "pcb-preprocess-layers",
        "pcb-register-gt", "pcb-iterate-score",
    ])
    pp.add_argument("--max-steps", type=int, default=None)
    pp.add_argument("--wait", action="store_true")
    pp.add_argument("--poll", type=float, default=5.0)
    pp.add_argument("--tail", type=int, default=131072)
    pp.set_defaults(func=cmd_preset)

    pd = sub.add_parser(
        "deploy",
        help="sync local scripts to Jetstream ECU checkout (base64 via ECU API)",
    )
    pd.add_argument(
        "files", nargs="*",
        help="repo-relative paths (default: PCB + ECU script bundle)",
    )
    pd.add_argument("--restart", action="store_true",
                    help="run restart_ecu.sh after upload")
    pd.add_argument("--wait", action="store_true", help="wait for each deploy job")
    pd.add_argument("--poll", type=float, default=5.0)
    pd.add_argument("--tail", type=int, default=65536)
    pd.set_defaults(func=cmd_deploy)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
