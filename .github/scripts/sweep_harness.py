#!/usr/bin/env python3
"""
24/7 sweep harness for bright-seed parameter exploration on the Dell
GPU.

Three roles share a single state directory
(``paper_artifacts/sweep/`` by default):

* ``seed`` — generate jobs by taking the cross product of a small
  specimen list against a small parameter list, append each job as a
  line to ``sweep_queue.jsonl``.
* ``run``  — long-running daemon. Each loop iteration: scan the queue
  for the next pending job (one whose ``job_id`` is not yet in
  ``sweep_results.jsonl``), claim it via a lock file, invoke the
  bright-seed runner with ``--autopilot`` + the job's overrides,
  record the result, drop the lock, repeat.
* ``status`` — pretty-print the queue, the in-flight job, and the
  results so a human (or the dashboard) can see what's happening.

Job format (one JSON object per line of ``sweep_queue.jsonl``):

    {
      "job_id": "mouse__floor_0.10__density_0.40",
      "input_path": "/absolute/path/to/ct.nrrd",
      "media_id": "impc_mouse",
      "params_override": {
        "intensity_drop_floor_frac": 0.10,
        "min_local_density": 0.40
      },
      "tags": ["mouse", "param_sweep_v1"],
      "priority": 0,
      "enqueued_at": "2026-05-25T20:00:00Z"
    }

Result format (one JSON object per line of ``sweep_results.jsonl``):

    {
      "job_id": "...",
      "status": "success" | "failure",
      "started_at": "...", "finished_at": "...",
      "duration_s": 134.5,
      "n_clicks": 8,
      "union_voxels": 218000,
      "stop_reason": "intensity_below_obvious",
      "output_dir": "<paper_artifacts/sweep/done/<job_id>>",
      "labelmap_path": "...",
      "summary_path": "...",
      "params_used": {...},
      "subprocess_exit_code": 0,
      "log_path": "..."
    }

The harness is intentionally file-based (jsonl + lock files) so the
queue/results can be inspected with ``cat`` or git-tracked, and so
the runner can be SIGTERM'd and restarted without losing state.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import itertools
import json
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / "paper_artifacts" / "sweep"
DEFAULT_NNI_PYTHON = (
    Path.home() / ".autoresearchclaw" / "nninteractive" / "bin" / "python"
)
BRIGHT_SEED_SCRIPT = SCRIPT_DIR / "nninteractive_bright_seed.py"

log = logging.getLogger("sweep_harness")


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _utc_compact() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# State helpers


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file into a list of dicts. Missing file -> []."""
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed line in %s: %s",
                            path, exc)
    return out


def _append_jsonl(path: Path, obj: dict) -> None:
    """Append a single dict + newline to a JSONL file. Idempotent on
    parent-dir creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")


@dataclass
class SweepPaths:
    state_dir: Path
    queue_file: Path
    results_file: Path
    in_flight_dir: Path
    done_dir: Path
    daemon_log: Path

    @classmethod
    def from_dir(cls, state_dir: Path) -> "SweepPaths":
        return cls(
            state_dir=state_dir,
            queue_file=state_dir / "sweep_queue.jsonl",
            results_file=state_dir / "sweep_results.jsonl",
            in_flight_dir=state_dir / "in_flight",
            done_dir=state_dir / "done",
            daemon_log=state_dir / "sweep_daemon.log",
        )

    def ensure(self) -> None:
        for d in (self.state_dir, self.in_flight_dir, self.done_dir):
            d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Seeding


def _slug(value) -> str:
    return str(value).replace(".", "p").replace("/", "_").replace(" ", "_")


def _expand_param_grid(grid: dict) -> Iterable[dict]:
    """``{"a":[1,2], "b":[3]}`` -> ``[{"a":1,"b":3},{"a":2,"b":3}]``."""
    if not grid:
        yield {}
        return
    keys = list(grid.keys())
    for combo in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, combo))


def build_jobs(
    *,
    specimens: list[dict],
    param_grid: Optional[dict] = None,
    tag: str = "sweep",
) -> list[dict]:
    """Cross-product specimens x param-grid into a job list.

    ``specimens`` is a list of ``{"input_path": ..., "media_id": ...,
    "tags": [...]}`` dicts. ``param_grid`` is e.g.
    ``{"intensity_drop_floor_frac":[0.1, 0.2, 0.5]}``.
    """
    jobs: list[dict] = []
    for spec in specimens:
        for params in _expand_param_grid(param_grid or {}):
            slug = "__".join(
                [_slug(spec.get("media_id", "vol"))]
                + [f"{_slug(k)}_{_slug(v)}" for k, v in sorted(params.items())]
            )
            jobs.append({
                "job_id": slug,
                "input_path": str(spec["input_path"]),
                "media_id": spec.get("media_id", slug),
                "params_override": params,
                "tags": list(spec.get("tags", []) or []) + [tag],
                "priority": int(spec.get("priority", 0)),
                "enqueued_at": _now_iso(),
            })
    return jobs


def enqueue(paths: SweepPaths, jobs: list[dict],
            allow_duplicates: bool = False) -> int:
    """Append ``jobs`` to the queue file. Returns the number actually
    enqueued (duplicates are dropped unless ``allow_duplicates``)."""
    paths.ensure()
    existing_ids: set[str] = set()
    if not allow_duplicates:
        for q in _read_jsonl(paths.queue_file):
            existing_ids.add(q.get("job_id", ""))
        for r in _read_jsonl(paths.results_file):
            existing_ids.add(r.get("job_id", ""))
    n = 0
    for job in jobs:
        jid = job.get("job_id")
        if not allow_duplicates and jid in existing_ids:
            log.info("skip duplicate job_id=%s", jid)
            continue
        _append_jsonl(paths.queue_file, job)
        existing_ids.add(jid)
        n += 1
    return n


# ---------------------------------------------------------------------------
# Running


def _pick_next_job(paths: SweepPaths) -> Optional[dict]:
    """Return the next pending job (one whose job_id is not in
    results and not currently in_flight). Sorted by priority desc,
    then enqueued_at asc."""
    results = {r.get("job_id") for r in _read_jsonl(paths.results_file)}
    in_flight = {
        p.stem for p in paths.in_flight_dir.glob("*.lock")
    }
    queue = _read_jsonl(paths.queue_file)
    pending = [j for j in queue
               if j.get("job_id") not in results
               and j.get("job_id") not in in_flight]
    if not pending:
        return None
    pending.sort(
        key=lambda j: (-int(j.get("priority", 0)),
                       j.get("enqueued_at", "")),
    )
    return pending[0]


def _claim_lock(paths: SweepPaths, job_id: str) -> Optional[Path]:
    """Atomic file-lock claim. Returns the lock Path on success, None
    if another worker already claimed it.

    We use ``O_CREAT|O_EXCL`` on POSIX so two parallel runners on the
    same machine can't race; on Windows the same flag combo is honored
    by CPython via ``os.open``.
    """
    paths.in_flight_dir.mkdir(parents=True, exist_ok=True)
    lock_path = paths.in_flight_dir / f"{job_id}.lock"
    try:
        fd = os.open(str(lock_path),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps({
            "claimed_by": socket.gethostname(),
            "pid": os.getpid(),
            "claimed_at": _now_iso(),
        }))
    return lock_path


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _build_bright_seed_cmd(
    *,
    nni_python: Path,
    job: dict,
    out_dir: Path,
    job_log: Path,
) -> list[str]:
    """Translate a job dict into a ``nninteractive_bright_seed.py``
    invocation. ``--autopilot`` + ``--no-previews`` are always on (the
    sweep cares about metrics + saturation, not preview PNGs)."""
    cmd = [
        str(nni_python),
        str(BRIGHT_SEED_SCRIPT),
        "--input", str(job["input_path"]),
        "--output-dir", str(out_dir),
        "--media-id", str(job.get("media_id", "vol")),
        "--autopilot",
        "--no-previews",
    ]
    # Job-specific overrides come AFTER --autopilot so the
    # _user_passed() check inside bright-seed honors them.
    overrides = job.get("params_override") or {}
    flag_map = {
        "intensity_drop_floor_frac": "--intensity-drop-floor-frac",
        "min_local_density": "--min-local-density",
        "neighborhood_radius": "--neighborhood-radius",
        "min_segment_voxels": "--min-segment-voxels",
        "max_segment_voxels": "--max-segment-voxels",
        "max_steps": "--max-steps",
        "intensity_percentile": "--intensity-percentile",
        "min_clicks_before_drop_stop": "--min-clicks-before-drop-stop",
        "max_candidates": "--max-candidates",
        "min_local_density_radius": "--neighborhood-radius",
    }
    for key, flag in flag_map.items():
        if key in overrides:
            cmd += [flag, str(overrides[key])]
    return cmd


def _summarise_run(out_dir: Path, media_id: str) -> dict:
    """Pluck the headline numbers (n_clicks, union_voxels, stop_reason)
    out of the bright-seed summary JSON so the results log doesn't
    require re-loading the labelmap.

    The bright-seed runner writes its summary as
    ``<media_id>_nni_summary.json``; we also accept the older
    ``<media_id>_bright_summary.json`` name for results from earlier
    revisions that may already live on disk.
    """
    candidates = [
        out_dir / f"{media_id}_nni_summary.json",
        out_dir / f"{media_id}_bright_summary.json",
    ]
    summary_path = next((c for c in candidates if c.exists()), None)
    if summary_path is None:
        return {
            "summary_missing": True,
            "summary_searched": [str(c) for c in candidates],
        }
    try:
        summary = json.loads(summary_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"summary_error": str(exc),
                "summary_path": str(summary_path)}
    # The bright-seed summary has nested {stop_reason: {reason, ...}}
    # so the surface-level "stop_reason" field stays terse and the
    # full payload still lives in the on-disk summary file.
    stop_block = summary.get("stop_reason") or {}
    if isinstance(stop_block, dict):
        stop_reason_label = stop_block.get("reason")
    else:
        stop_reason_label = stop_block
    labelmap = (
        summary.get("labelmap_path")
        or str(out_dir / f"{media_id}_nni_labelmap.nii.gz")
    )
    multilabel = (
        summary.get("multilabel_path")
        or str(out_dir / f"{media_id}_nni_multilabel.nii.gz")
    )
    return {
        "n_clicks": summary.get("n_clicks") or summary.get("clicks_kept")
                    or len(summary.get("history") or []),
        "union_voxels": summary.get("union_voxels")
                        or summary.get("voxel_count"),
        "stop_reason": stop_reason_label,
        "stop_block": stop_block if isinstance(stop_block, dict) else None,
        "params_used": summary.get("params"),
        "labelmap_path": labelmap,
        "multilabel_path": multilabel,
        "summary_path": str(summary_path),
        "rejections": summary.get("rejections"),
    }


def run_one_job(paths: SweepPaths, job: dict,
                nni_python: Path = DEFAULT_NNI_PYTHON,
                timeout_s: Optional[int] = None) -> dict:
    """Execute one job synchronously. Returns the result dict that
    will be appended to ``sweep_results.jsonl``."""
    job_id = job["job_id"]
    out_dir = paths.done_dir / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    job_log = out_dir / "sweep_job.log"

    cmd = _build_bright_seed_cmd(
        nni_python=nni_python, job=job, out_dir=out_dir, job_log=job_log,
    )
    log.info("running job_id=%s -> %s", job_id, out_dir)
    log.info("  cmd: %s", " ".join(cmd))

    started = _now_iso()
    t0 = time.time()
    rc = -1
    timed_out = False
    try:
        with job_log.open("w", encoding="utf-8") as fh:
            fh.write(f"# sweep job {job_id} started {started}\n")
            fh.write(f"# cmd: {' '.join(cmd)}\n\n")
            fh.flush()
            try:
                proc = subprocess.run(
                    cmd, stdout=fh, stderr=subprocess.STDOUT,
                    timeout=timeout_s, check=False,
                )
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = -2
                timed_out = True
                fh.write("\n# subprocess timed out\n")
    except OSError as exc:
        rc = -3
        log.error("subprocess launch failed for %s: %s", job_id, exc)

    duration = round(time.time() - t0, 1)
    finished = _now_iso()

    summary = _summarise_run(out_dir, job.get("media_id", "vol"))

    result = {
        "job_id": job_id,
        "status": "success" if rc == 0 else "failure",
        "started_at": started,
        "finished_at": finished,
        "duration_s": duration,
        "subprocess_exit_code": rc,
        "timed_out": timed_out,
        "output_dir": str(out_dir),
        "log_path": str(job_log),
        "params_override": job.get("params_override") or {},
        "tags": job.get("tags") or [],
        "input_path": job.get("input_path"),
        "media_id": job.get("media_id"),
    }
    result.update(summary)
    return result


def run_daemon(
    paths: SweepPaths,
    *,
    nni_python: Path = DEFAULT_NNI_PYTHON,
    sleep_when_idle_s: int = 30,
    max_jobs: Optional[int] = None,
    per_job_timeout_s: Optional[int] = None,
    once: bool = False,
) -> int:
    """Long-running queue worker. Returns the number of jobs run."""
    paths.ensure()
    if not BRIGHT_SEED_SCRIPT.exists():
        log.error("missing bright-seed script: %s", BRIGHT_SEED_SCRIPT)
        return 0
    if not Path(nni_python).exists():
        log.error("missing nnInteractive venv python: %s", nni_python)
        return 0

    n_done = 0
    while True:
        job = _pick_next_job(paths)
        if job is None:
            if once:
                log.info("queue empty, --once was set -> exiting")
                return n_done
            log.info("queue empty, sleeping %ds before recheck",
                     sleep_when_idle_s)
            time.sleep(sleep_when_idle_s)
            continue

        lock = _claim_lock(paths, job["job_id"])
        if lock is None:
            log.info("job %s already claimed by another worker",
                     job["job_id"])
            time.sleep(2)
            continue

        try:
            result = run_one_job(
                paths, job, nni_python=nni_python,
                timeout_s=per_job_timeout_s,
            )
            _append_jsonl(paths.results_file, result)
            log.info(
                "job %s done in %.1fs -> status=%s clicks=%s union=%s "
                "stop=%s",
                result["job_id"], result["duration_s"],
                result["status"], result.get("n_clicks"),
                result.get("union_voxels"), result.get("stop_reason"),
            )
        finally:
            _release_lock(lock)
        n_done += 1
        if max_jobs is not None and n_done >= max_jobs:
            log.info("hit --max-jobs=%d -> exiting", max_jobs)
            return n_done


# ---------------------------------------------------------------------------
# Status


def render_status(paths: SweepPaths) -> str:
    queue = _read_jsonl(paths.queue_file)
    results = _read_jsonl(paths.results_file)
    results_by_id = {r.get("job_id"): r for r in results}
    in_flight_ids = {p.stem for p in paths.in_flight_dir.glob("*.lock")}
    n_total = len(queue)
    n_done = sum(1 for j in queue if j.get("job_id") in results_by_id)
    n_in_flight = sum(1 for j in queue
                      if j.get("job_id") in in_flight_ids)
    n_pending = n_total - n_done - n_in_flight
    lines = [
        f"=== sweep status @ {paths.state_dir} ===",
        f"  queue:     {n_total} jobs",
        f"  done:      {n_done}",
        f"  in_flight: {n_in_flight}  ({sorted(in_flight_ids)})",
        f"  pending:   {n_pending}",
        "",
    ]
    if results:
        recent = results[-10:]
        lines.append("Recent results (newest last):")
        for r in recent:
            lines.append(
                f"  {r.get('job_id'):40s}  "
                f"status={r.get('status'):8s}  "
                f"clicks={r.get('n_clicks')}  "
                f"union={r.get('union_voxels')}  "
                f"stop={r.get('stop_reason')}  "
                f"dur={r.get('duration_s')}s"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI


def _resolve_path(p: str, *, base: Path) -> str:
    """Spec paths are stored relative to the repo root so the spec file
    can be committed and used from any worker. Absolute paths pass
    through unchanged."""
    candidate = Path(p)
    if candidate.is_absolute():
        return str(candidate)
    return str((base / candidate).resolve())


def _cmd_seed(args) -> int:
    paths = SweepPaths.from_dir(Path(args.state_dir).resolve())
    paths.ensure()
    if args.spec:
        spec_path = Path(args.spec).resolve()
        spec = json.loads(spec_path.read_text())
        specimens = spec.get("specimens", [])
        param_grid = spec.get("param_grid", {})
        tag = spec.get("tag", "sweep")
        # Resolve relative paths against the repo root, not the spec
        # dir, because that's where the runner ends up running from.
        for s in specimens:
            if "input_path" in s:
                s["input_path"] = _resolve_path(
                    s["input_path"], base=REPO_ROOT
                )
    else:
        specimens = [{"input_path": _resolve_path(p, base=Path.cwd()),
                      "media_id": Path(p).stem,
                      "tags": []}
                     for p in (args.input or [])]
        param_grid = {}
        if args.floor_fracs:
            param_grid["intensity_drop_floor_frac"] = args.floor_fracs
        if args.densities:
            param_grid["min_local_density"] = args.densities
        tag = args.tag
    if not specimens:
        log.error("nothing to seed (no --input and no --spec)")
        return 2
    jobs = build_jobs(specimens=specimens, param_grid=param_grid, tag=tag)
    n = enqueue(paths, jobs, allow_duplicates=args.allow_duplicates)
    print(f"enqueued {n}/{len(jobs)} jobs to {paths.queue_file}")
    return 0


def _cmd_run(args) -> int:
    paths = SweepPaths.from_dir(Path(args.state_dir).resolve())
    n = run_daemon(
        paths,
        nni_python=Path(args.nni_python),
        sleep_when_idle_s=args.sleep_when_idle_s,
        max_jobs=args.max_jobs,
        per_job_timeout_s=args.per_job_timeout_s,
        once=args.once,
    )
    print(f"finished after running {n} jobs")
    return 0


def _cmd_status(args) -> int:
    paths = SweepPaths.from_dir(Path(args.state_dir).resolve())
    paths.ensure()
    print(render_status(paths))
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                   help="Directory for sweep_queue.jsonl + "
                        "sweep_results.jsonl + locks + outputs "
                        f"(default {DEFAULT_STATE_DIR}).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="Enqueue jobs.")
    s.add_argument("--spec", help="JSON spec file: "
                                  "{specimens:[...], param_grid:{...}}")
    s.add_argument("--input", nargs="+",
                   help="One or more CT paths (alt to --spec).")
    s.add_argument("--floor-fracs", nargs="+", type=float, default=None,
                   help="Sweep these intensity_drop_floor_frac values.")
    s.add_argument("--densities", nargs="+", type=float, default=None,
                   help="Sweep these min_local_density values.")
    s.add_argument("--tag", default="sweep")
    s.add_argument("--allow-duplicates", action="store_true")
    s.set_defaults(func=_cmd_seed)

    r = sub.add_parser("run", help="Run the daemon loop.")
    r.add_argument("--nni-python", default=str(DEFAULT_NNI_PYTHON))
    r.add_argument("--sleep-when-idle-s", type=int, default=30)
    r.add_argument("--max-jobs", type=int, default=None)
    r.add_argument("--per-job-timeout-s", type=int, default=None)
    r.add_argument("--once", action="store_true",
                   help="Exit as soon as the queue empties.")
    r.set_defaults(func=_cmd_run)

    st = sub.add_parser("status", help="Print queue + results.")
    st.set_defaults(func=_cmd_status)

    return p


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
