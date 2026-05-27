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
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        "cwd": str(args.cwd or REPO_ROOT),
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
    presets = {
        "pcb-copper-test": [
            "python3", ".github/scripts/slicer_remote_pcb_copper.py",
            "--phase", "copper",
            "--volume", "pcb_ti_jetstream",
            "--max-steps", str(args.max_steps),
            "--noise-manifest", "runs/pcb_noise_export_20260527/noise_manifest.json",
            "--out-dir", f"runs/pcb_copper_ecu_{time.strftime('%Y%m%dT%H%M%S')}",
        ],
        "pcb-export-noise": [
            "python3", ".github/scripts/slicer_remote_pcb_copper.py",
            "--phase", "export-noise",
            "--volume", "pcb_ti_jetstream",
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
    }
    if args.name not in presets:
        sys.exit(f"unknown preset {args.name!r}; choose from {list(presets)}")
    ns = argparse.Namespace(
        remote_argv=presets[args.name],
        cwd=REPO_ROOT,
        label=args.name,
        env=[],
        wait=args.wait,
        poll=args.poll,
        tail=args.tail,
    )
    if args.name.startswith("pcb-copper") and os.environ.get("OPENAI_API_KEY"):
        ns.env = [f"OPENAI_API_KEY={os.environ['OPENAI_API_KEY']}"]
    return cmd_run(ns)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="GET /health on ECU")

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
    ])
    pp.add_argument("--max-steps", type=int, default=8)
    pp.add_argument("--wait", action="store_true")
    pp.add_argument("--poll", type=float, default=5.0)
    pp.add_argument("--tail", type=int, default=131072)
    pp.set_defaults(func=cmd_preset)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
