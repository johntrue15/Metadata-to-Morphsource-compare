#!/usr/bin/env python3
"""
MorphoClaw ECU — job runner on Jetstream (runs next to Slicer on localhost).

The Mac **controller** submits short HTTP requests; the ECU runs long
``slicer_remote_*`` / ``nninteractive_*`` commands with::

    SLICER_WEBSERVER_URL=http://127.0.0.1:2016/

so nnInteractive clicks never traverse the Apache/Exosphere proxy.

API (bind ``0.0.0.0:18765`` by default)::

    GET  /health
    POST /v1/jobs          {"argv": [...], "cwd": "...", "env": {...}, "label": "..."}
    GET  /v1/jobs/{id}     status, exit_code, log_bytes
    GET  /v1/jobs/{id}/log?tail=65536

Optional auth: set ``MORPHOCLAW_ECU_TOKEN`` on the box; pass header
``Authorization: Bearer <token>`` from the controller.

Start (usually via ``scripts/jetstream/restart_ecu.sh``)::

    python3 .github/scripts/jetstream_ecu_server.py --host 0.0.0.0 --port 18765
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JOBS_ROOT = REPO_ROOT / ".ecu" / "jobs"
DEFAULT_SLICER_URL = "http://127.0.0.1:2016/"


class JobRecord:
    __slots__ = (
        "job_id", "label", "argv", "cwd", "env", "status",
        "exit_code", "created_at", "started_at", "finished_at",
        "log_path", "pid",
    )

    def __init__(
        self,
        job_id: str,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        label: str,
        log_path: Path,
    ):
        self.job_id = job_id
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.label = label
        self.status = "queued"
        self.exit_code: Optional[int] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.log_path = log_path
        self.pid: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "label": self.label,
            "argv": self.argv,
            "cwd": self.cwd,
            "status": self.status,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_bytes": self.log_path.stat().st_size if self.log_path.is_file() else 0,
            "pid": self.pid,
        }


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}

    def create(self, argv: list[str], cwd: str, env: dict[str, str],
               label: str) -> JobRecord:
        job_id = time.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        log_path = self.root / f"{job_id}.log"
        meta_path = self.root / f"{job_id}.json"
        rec = JobRecord(job_id, argv, cwd, env, label, log_path)
        with self._lock:
            self._jobs[job_id] = rec
        meta_path.write_text(json.dumps(rec.to_dict(), indent=2), encoding="utf-8")
        return rec

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            rec = self._jobs.get(job_id)
        if rec is not None:
            return rec
        meta = self.root / f"{job_id}.json"
        if not meta.is_file():
            return None
        data = json.loads(meta.read_text())
        log_path = self.root / f"{job_id}.log"
        rec = JobRecord(
            job_id,
            data.get("argv") or [],
            data.get("cwd") or str(REPO_ROOT),
            data.get("env") or {},
            data.get("label") or "",
            log_path,
        )
        rec.status = data.get("status", "unknown")
        rec.exit_code = data.get("exit_code")
        rec.created_at = data.get("created_at", rec.created_at)
        rec.started_at = data.get("started_at")
        rec.finished_at = data.get("finished_at")
        rec.pid = data.get("pid")
        with self._lock:
            self._jobs[job_id] = rec
        return rec

    def _persist(self, rec: JobRecord) -> None:
        meta_path = self.root / f"{rec.job_id}.json"
        meta_path.write_text(json.dumps(rec.to_dict(), indent=2), encoding="utf-8")

    def start(self, rec: JobRecord) -> None:
        def _run() -> None:
            rec.status = "running"
            rec.started_at = time.time()
            self._persist(rec)
            merged = os.environ.copy()
            merged.update(rec.env)
            merged.setdefault("SLICER_WEBSERVER_URL", DEFAULT_SLICER_URL)
            merged.setdefault("PYTHONUNBUFFERED", "1")
            with rec.log_path.open("w", encoding="utf-8") as logf:
                logf.write(f"# job {rec.job_id} label={rec.label!r}\n")
                logf.write(f"# cwd={rec.cwd}\n")
                logf.write(f"# argv={json.dumps(rec.argv)}\n")
                logf.write(f"# SLICER_WEBSERVER_URL={merged.get('SLICER_WEBSERVER_URL')}\n\n")
                logf.flush()
                try:
                    proc = subprocess.Popen(
                        rec.argv,
                        cwd=rec.cwd,
                        env=merged,
                        stdout=logf,
                        stderr=subprocess.STDOUT,
                    )
                    rec.pid = proc.pid
                    self._persist(rec)
                    rc = proc.wait()
                    rec.exit_code = rc
                    rec.status = "succeeded" if rc == 0 else "failed"
                except Exception as e:
                    logf.write(f"\n# ECU runner exception: {e!r}\n")
                    rec.exit_code = 127
                    rec.status = "failed"
                finally:
                    rec.finished_at = time.time()
                    self._persist(rec)

        threading.Thread(target=_run, daemon=True).start()


def _auth_ok(headers, token: str) -> bool:
    if not token:
        return True
    auth = headers.get("Authorization", "")
    if auth == f"Bearer {token}":
        return True
    return headers.get("X-MorphoClaw-Token", "") == token


def make_handler(store: JobStore, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MorphoClawECU/1.0"

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self) -> None:
            if not _auth_ok(self.headers, token):
                self._json(401, {"error": "unauthorized"})
                return
            path = urlparse(self.path).path
            if path == "/health":
                self._json(200, {
                    "status": "ok",
                    "repo": str(REPO_ROOT),
                    "slicer_url_default": DEFAULT_SLICER_URL,
                })
                return
            if path.startswith("/v1/jobs/") and path.endswith("/log"):
                job_id = path.split("/")[3]
                rec = store.get(job_id)
                if rec is None:
                    self._json(404, {"error": "not found"})
                    return
                qs = parse_qs(urlparse(self.path).query)
                tail = int((qs.get("tail") or ["65536"])[0])
                text = ""
                if rec.log_path.is_file():
                    data = rec.log_path.read_bytes()
                    if len(data) > tail:
                        data = data[-tail:]
                    text = data.decode("utf-8", errors="replace")
                self._json(200, {"job_id": job_id, "log": text})
                return
            if path.startswith("/v1/jobs/"):
                job_id = path.split("/")[-1]
                rec = store.get(job_id)
                if rec is None:
                    self._json(404, {"error": "not found"})
                    return
                self._json(200, rec.to_dict())
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not _auth_ok(self.headers, token):
                self._json(401, {"error": "unauthorized"})
                return
            path = urlparse(self.path).path
            if path != "/v1/jobs":
                self._json(404, {"error": "not found"})
                return
            body = self._read_body()
            argv = body.get("argv")
            if not argv or not isinstance(argv, list):
                self._json(400, {"error": "argv required (list of strings)"})
                return
            argv = [str(x) for x in argv]
            cwd = str(body.get("cwd") or REPO_ROOT)
            env_in = body.get("env") or {}
            if not isinstance(env_in, dict):
                self._json(400, {"error": "env must be an object"})
                return
            env = {str(k): str(v) for k, v in env_in.items()}
            label = str(body.get("label") or "")
            rec = store.create(argv, cwd, env, label)
            store.start(rec)
            self._json(202, rec.to_dict())

    return Handler


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.environ.get("MORPHOCLAW_ECU_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("MORPHOCLAW_ECU_PORT", "18765")))
    p.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_ROOT)
    args = p.parse_args(argv)

    token = os.environ.get("MORPHOCLAW_ECU_TOKEN", "").strip()
    store = JobStore(args.jobs_dir)
    handler = make_handler(store, token)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"[ecu] repo={REPO_ROOT}", flush=True)
    print(f"[ecu] listening on {args.host}:{args.port}", flush=True)
    print(f"[ecu] default SLICER_WEBSERVER_URL={DEFAULT_SLICER_URL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[ecu] shutdown", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
