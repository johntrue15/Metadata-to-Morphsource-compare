#!/usr/bin/env python3
"""Git-driven ECU worker: pick up committed jobs, run them, checkpoint to GitHub.

Runs ON the Jetstream box (cwd = the ECU repo checkout). Implements the
crash-resilient flow:

    pick up a committed job  (jobs/queue/<id>.json, fetched via git pull)
      -> claim it            (jobs/status/<id>.json = running, commit+push)
      -> run the working logic (jetstream_skull_fresh_start.py:
                                clear scene -> load CT -> bright-seed to
                                --max-steps / completion, headless+batched)
      -> checkpoint           (every --checkpoint-sec: snapshot the live union
                                mask + progress into results/<run>/, commit+push)
      -> detect completion    (subprocess exit + bright_seed summary/stop_reason)
      -> upload saved work     (final harvest of the run dir)
      -> commit to GitHub      (jobs/status/<id>.json = done/failed)

``runs/`` is gitignored (tens of GB of per-segment masks), so every commit goes
through the curated ``results/`` tree via ``jetstream_harvest_results`` (small
record + Git-LFS composite). The GitHub push uses ``GITHUB_TOKEN`` (token-in-URL,
scrubbed from logs).

Modes::

    # run one named job once and exit
    python3 .github/scripts/jetstream_ecu_worker.py --once --job crotalus-200

    # poll the git queue forever, picking up newly-committed jobs
    python3 .github/scripts/jetstream_ecu_worker.py --poll --poll-sec 60

Job spec (committed at ``jobs/queue/<id>.json``)::

    {"id": "crotalus-200", "fixture": "data/sample/colors_of_skull_urls.json",
     "max_steps": 200, "headless": true, "fast": true}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from jetstream_harvest_results import (  # noqa: E402
    REPO, RUNS, RESULTS, commit_and_push, harvest_run,
)
from slicer_remote_bright_seed import post_python, _read_url  # noqa: E402

QUEUE_DIR = REPO / "jobs" / "queue"
STATUS_DIR = REPO / "jobs" / "status"

# Snapshot the live union mask to an absolute path on the box (Slicer shares the
# filesystem, so no HTTP transfer of the volume). Reuses the incremental
# _BS_STATE["union_mask"] kept by the batched bright-seed loop; falls back to
# scanning segmentation nodes. __CKPT_PATH__ is substituted by the worker.
CHECKPOINT_EXPORT_SRC = r'''
import slicer, vtk, traceback, os
import numpy as np
try:
    out_path = r"__CKPT_PATH__"
    sel = slicer.app.applicationLogic().GetSelectionNode()
    vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
    if vol is None:
        vols = slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
        vol = vols[0] if vols else None
    if vol is None:
        __execResult = {"status": "no_active_volume"}
    else:
        arr = slicer.util.arrayFromVolume(vol)
        shape = arr.shape
        st = globals().get("_BS_STATE") or {}
        union = st.get("union_mask")
        if union is None or getattr(union, "shape", None) != shape:
            union = np.zeros(shape, dtype=bool)
            for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                if "do not touch" in sn.GetName().lower():
                    continue
                seg = sn.GetSegmentation()
                for ii in range(seg.GetNumberOfSegments()):
                    sid = seg.GetNthSegmentID(ii)
                    try:
                        a = slicer.util.arrayFromSegmentBinaryLabelmap(sn, sid)
                    except Exception:
                        a = None
                    if a is not None and a.shape == shape:
                        union |= (a > 0)
        m = vtk.vtkMatrix4x4(); vol.GetIJKToRASMatrix(m)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        lm = slicer.util.addVolumeFromArray(
            union.astype("uint8"), ijkToRAS=m, name="ckpt_union_lm",
            nodeClassName="vtkMRMLLabelMapVolumeNode")
        ok = slicer.util.saveNode(lm, out_path)
        slicer.mrmlScene.RemoveNode(lm)
        __execResult = {"status": "ok" if ok else "save_failed",
                        "voxels": int(union.sum()),
                        "n_segments": sum(
                            s.GetSegmentation().GetNumberOfSegments()
                            for s in slicer.util.getNodesByClass(
                                "vtkMRMLSegmentationNode")
                            if "do not touch" not in s.GetName().lower())}
except Exception as e:
    __execResult = {"status": "exception", "error": repr(e),
                    "traceback": traceback.format_exc()}
'''


def _log(msg: str) -> None:
    print(f"[worker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_status(job_id: str, **fields) -> Path:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    path = STATUS_DIR / f"{job_id}.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}
    data.update(fields)
    data["id"] = job_id
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def _count_events(run_dir: Path) -> int:
    ev = run_dir / "bright_seed" / "events.jsonl"
    if not ev.exists():
        return 0
    try:
        return sum(1 for line in ev.read_text().splitlines()
                   if '"step_end"' in line)
    except Exception:
        return 0


def _checkpoint(job_id: str, run_dir: Path, base_url: str, n: int,
                branch: str, push: bool) -> None:
    """Snapshot the live union mask + records and commit them to GitHub."""
    ckpt_path = run_dir / "bright_seed" / "checkpoint" / "composite_latest.nrrd"
    src = CHECKPOINT_EXPORT_SRC.replace("__CKPT_PATH__", str(ckpt_path))
    voxels = n_segs = None
    try:
        r = post_python(base_url, src, timeout=180, retries=1)
        if r.get("status") == "ok":
            voxels, n_segs = r.get("voxels"), r.get("n_segments")
            _log(f"checkpoint {n}: union voxels={voxels:,} segs={n_segs}")
        else:
            _log(f"checkpoint {n}: export status={r.get('status')} "
                 f"(committing records only)")
    except Exception as e:  # never let a checkpoint kill the run
        _log(f"checkpoint {n}: export failed ({e!r}); committing records only")

    steps = _count_events(run_dir)
    (run_dir / "bright_seed" / "checkpoint").mkdir(parents=True, exist_ok=True)
    (run_dir / "bright_seed" / "checkpoint" / "progress.json").write_text(
        json.dumps({"checkpoint": n, "steps": steps, "union_voxels": voxels,
                    "n_segments": n_segs, "running": True,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                   indent=2) + "\n")
    harvest_run(run_dir, max_small_mb=25.0, max_lfs_mb=400.0, dry_run=False)
    _write_status(job_id, state="running", checkpoints=n, steps=steps,
                  union_voxels=voxels, n_segments=n_segs, run_dir=run_dir.name)
    commit_and_push([f"results/{run_dir.name}", f"jobs/status/{job_id}.json"],
                    f"checkpoint {job_id}: ckpt #{n}, {steps} steps, "
                    f"{voxels if voxels is not None else '?'} voxels",
                    branch=branch, push=push)


def run_job(spec: dict, branch: str, push: bool, checkpoint_sec: float) -> int:
    job_id = spec["id"]
    fixture = spec.get("fixture", "data/sample/colors_of_skull_urls.json")
    max_steps = int(spec.get("max_steps", 200))
    headless = bool(spec.get("headless", True))
    fast = bool(spec.get("fast", True))
    batch_size = int(spec.get("batch_size", 8))

    ts = time.strftime("%Y%m%dT%H%M%S")
    run_name = f"{job_id}_{ts}"
    run_dir = RUNS / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    base_url = _read_url()

    _log(f"=== job {job_id}: fixture={fixture} max_steps={max_steps} "
         f"headless={headless} fast={fast} -> runs/{run_name} ===")
    _write_status(job_id, state="running", run_dir=run_name, max_steps=max_steps,
                  fixture=fixture, started_at=time.strftime(
                      "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), checkpoints=0)
    commit_and_push([f"jobs/status/{job_id}.json"],
                    f"claim {job_id}: running (max_steps={max_steps})",
                    branch=branch, push=push)

    cmd = [sys.executable,
           str(SCRIPT_DIR / "jetstream_skull_fresh_start.py"),
           "--fixture", fixture, "--max-steps", str(max_steps),
           "--out-dir", str(run_dir), "--batch-size", str(batch_size)]
    if headless:
        cmd.append("--headless")
    if fast:
        cmd.append("--fast")

    log_path = run_dir / "worker_run.log"
    _log(f"launching: {' '.join(cmd)}")
    with log_path.open("w") as logf:
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=logf,
                                stderr=subprocess.STDOUT)
        n_ckpt = 0
        last = time.time()
        while proc.poll() is None:
            time.sleep(2.0)
            if time.time() - last >= checkpoint_sec:
                n_ckpt += 1
                try:
                    _checkpoint(job_id, run_dir, base_url, n_ckpt, branch, push)
                except SystemExit as e:
                    _log(f"checkpoint commit error: {e}")
                last = time.time()
        rc = proc.wait()

    _log(f"run subprocess exited rc={rc}; finalizing…")
    # Completion: pull stop_reason / summary if bright_seed wrote them.
    stop_reason = None
    summary_path = run_dir / "bright_seed" / "summary.json"
    if summary_path.exists():
        try:
            stop_reason = json.loads(summary_path.read_text()).get("stop_reason")
        except Exception:
            stop_reason = None
    steps = _count_events(run_dir)
    state = "done" if rc == 0 else "failed"
    harvest_run(run_dir, max_small_mb=25.0, max_lfs_mb=400.0, dry_run=False)
    _write_status(job_id, state=state, exit_code=rc, steps=steps,
                  stop_reason=stop_reason, checkpoints=n_ckpt, run_dir=run_name,
                  finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    commit_and_push([f"results/{run_name}", f"jobs/status/{job_id}.json"],
                    f"complete {job_id}: {state} rc={rc}, {steps} steps, "
                    f"stop={(stop_reason or {}).get('reason', '?')}",
                    branch=branch, push=push)
    _log(f"=== job {job_id} {state} (rc={rc}, {steps} steps) ===")
    return rc


def _git_pull(branch: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    remote = (f"https://x-access-token:{token}@github.com/johntrue15/MorphoClaw.git"
              if token else "origin")
    subprocess.run(["git", "pull", "--rebase", "--autostash", remote, branch],
                   cwd=str(REPO), capture_output=True, text=True)


def _job_state(job_id: str) -> str:
    path = STATUS_DIR / f"{job_id}.json"
    if not path.exists():
        return "pending"
    try:
        return json.loads(path.read_text()).get("state", "pending")
    except Exception:
        return "pending"


def _next_pending() -> dict | None:
    if not QUEUE_DIR.exists():
        return None
    for spec_path in sorted(QUEUE_DIR.glob("*.json")):
        try:
            spec = json.loads(spec_path.read_text())
        except Exception:
            continue
        spec.setdefault("id", spec_path.stem)
        if _job_state(spec["id"]) in ("pending",):
            return spec
    return None


def _reclaim_orphaned_running(branch: str, push: bool) -> None:
    """Reset jobs stuck in ``running`` back to ``pending`` so they re-run.

    A job is only ever ``running`` while *this* worker is actively driving it.
    If we find one at startup, the previous worker died mid-run (e.g. the box
    was shelved / crashed), so the job is orphaned and will never finish. We
    reset it to ``pending`` here so the poll loop re-runs it from scratch. Only
    jobs that still have a queue spec (and so are re-runnable) are touched.

    Safe for the single-worker box model: nothing is genuinely running at
    startup. Do NOT call this mid-loop.
    """
    if not STATUS_DIR.exists():
        return
    reset: list[str] = []
    for sp in sorted(STATUS_DIR.glob("*.json")):
        jid = sp.stem
        if not (QUEUE_DIR / f"{jid}.json").exists():
            continue  # no spec -> not re-runnable here
        try:
            st = json.loads(sp.read_text())
        except Exception:
            continue
        if st.get("state") == "running":
            st["state"] = "pending"
            st["reclaimed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime())
            st["reclaim_note"] = ("worker restart: previous run interrupted "
                                  "(box shelved/crash); re-queued")
            sp.write_text(json.dumps(st, indent=2) + "\n")
            reset.append(jid)
    if reset:
        _log(f"reclaimed {len(reset)} orphaned running job(s) -> pending: {reset}")
        try:
            commit_and_push([f"jobs/status/{j}.json" for j in reset],
                            f"reclaim orphaned running -> pending: {', '.join(reset)}",
                            branch=branch, push=push)
        except SystemExit as e:
            _log(f"reclaim push failed (continuing): {e}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true",
                   help="run a single job (with --job) then exit")
    g.add_argument("--poll", action="store_true",
                   help="poll the git queue, picking up newly-committed jobs")
    p.add_argument("--job", default=None,
                   help="job id under jobs/queue/<id>.json (for --once)")
    p.add_argument("--branch", default="main")
    p.add_argument("--checkpoint-sec", type=float, default=120.0,
                   help="seconds between GitHub checkpoints (default 120)")
    p.add_argument("--poll-sec", type=float, default=60.0,
                   help="seconds between git-queue polls (default 60)")
    p.add_argument("--no-push", action="store_true",
                   help="commit locally but do not push (debug)")
    args = p.parse_args(argv)
    push = not args.no_push

    if args.once:
        if not args.job:
            sys.exit("ERROR: --once requires --job <id>")
        spec_path = QUEUE_DIR / f"{args.job}.json"
        if not spec_path.exists():
            sys.exit(f"ERROR: no job spec at {spec_path}")
        spec = json.loads(spec_path.read_text())
        spec.setdefault("id", args.job)
        return run_job(spec, args.branch, push, args.checkpoint_sec)

    _log(f"polling git queue every {args.poll_sec}s (branch {args.branch})…")
    _git_pull(args.branch)
    _reclaim_orphaned_running(args.branch, push)
    while True:
        _git_pull(args.branch)
        spec = _next_pending()
        if spec is None:
            time.sleep(args.poll_sec)
            continue
        _log(f"picked up job {spec['id']}")
        try:
            run_job(spec, args.branch, push, args.checkpoint_sec)
        except Exception as e:
            _log(f"job {spec['id']} crashed: {e!r}")
            _write_status(spec["id"], state="failed", error=repr(e))
            commit_and_push([f"jobs/status/{spec['id']}.json"],
                            f"fail {spec['id']}: worker exception",
                            branch=args.branch, push=push)


if __name__ == "__main__":
    raise SystemExit(main())
