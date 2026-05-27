"""Unit tests for sweep_harness: queue, results, locking, job
construction. The bright-seed subprocess is stubbed out via
monkeypatching so the tests run without any GPU.

Coverage:

* ``build_jobs`` produces the cross product of specimens x params
  and the slugs are deterministic.
* ``enqueue`` deduplicates against the queue AND against results.
* ``_pick_next_job`` honors priority, ignores done + in-flight.
* ``_claim_lock`` is exclusive: a second claim returns None.
* ``_build_bright_seed_cmd`` injects ``--autopilot`` and overrides.
* ``run_one_job`` records a success result with the parsed summary.
* ``render_status`` reports done/in_flight/pending counts.
"""

import json
import os
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent.parent / ".github" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import sweep_harness as sh  # noqa: E402


# --------------------------------------------------------------------- helpers


def _new_paths(tmp_path) -> sh.SweepPaths:
    p = sh.SweepPaths.from_dir(tmp_path / "sweep")
    p.ensure()
    return p


def _spec(name: str, path: str = "/tmp/fake.nrrd") -> dict:
    return {"input_path": path, "media_id": name, "tags": ["unit"]}


# --------------------------------------------------------------------- tests


def test_build_jobs_cross_product():
    jobs = sh.build_jobs(
        specimens=[_spec("mouse"), _spec("felis")],
        param_grid={
            "intensity_drop_floor_frac": [0.1, 0.5],
            "min_local_density": [0.4],
        },
        tag="t1",
    )
    assert len(jobs) == 4  # 2 specimens * 2 floors * 1 density
    ids = sorted(j["job_id"] for j in jobs)
    assert "mouse__intensity_drop_floor_frac_0p1__min_local_density_0p4" in ids
    assert "felis__intensity_drop_floor_frac_0p5__min_local_density_0p4" in ids
    for j in jobs:
        assert "t1" in j["tags"]
        assert "unit" in j["tags"]
        assert "params_override" in j
        assert "enqueued_at" in j


def test_build_jobs_with_empty_grid():
    jobs = sh.build_jobs(
        specimens=[_spec("mouse")],
        param_grid={},
        tag="defaults",
    )
    assert len(jobs) == 1
    assert jobs[0]["params_override"] == {}
    assert jobs[0]["job_id"] == "mouse"


def test_enqueue_dedupes_against_queue_and_results(tmp_path):
    paths = _new_paths(tmp_path)
    jobs = sh.build_jobs(specimens=[_spec("mouse")],
                         param_grid={"intensity_drop_floor_frac": [0.1, 0.2]})
    n1 = sh.enqueue(paths, jobs)
    assert n1 == 2
    # Second enqueue is a no-op.
    n2 = sh.enqueue(paths, jobs)
    assert n2 == 0
    # If we move one of them into results, it's still considered
    # "already known".
    sh._append_jsonl(paths.results_file, {"job_id": jobs[0]["job_id"],
                                          "status": "success"})
    n3 = sh.enqueue(paths, jobs)
    assert n3 == 0


def test_enqueue_allow_duplicates(tmp_path):
    paths = _new_paths(tmp_path)
    jobs = sh.build_jobs(specimens=[_spec("mouse")])
    sh.enqueue(paths, jobs)
    n2 = sh.enqueue(paths, jobs, allow_duplicates=True)
    assert n2 == 1


def test_pick_next_job_skips_done_and_inflight(tmp_path):
    paths = _new_paths(tmp_path)
    jobs = sh.build_jobs(
        specimens=[_spec("mouse"), _spec("felis")],
        param_grid={"intensity_drop_floor_frac": [0.1]},
    )
    sh.enqueue(paths, jobs)
    # Mark mouse as done.
    sh._append_jsonl(paths.results_file, {"job_id": jobs[0]["job_id"],
                                          "status": "success"})
    next_job = sh._pick_next_job(paths)
    assert next_job["job_id"] == jobs[1]["job_id"]

    # Now claim it as in-flight; next pick should be None.
    sh._claim_lock(paths, jobs[1]["job_id"])
    assert sh._pick_next_job(paths) is None


def test_pick_next_job_honors_priority(tmp_path):
    paths = _new_paths(tmp_path)
    low = {"job_id": "low",  "input_path": "/x", "priority": 0,
           "enqueued_at": "2026-05-25T19:00:00Z"}
    high = {"job_id": "high", "input_path": "/x", "priority": 5,
            "enqueued_at": "2026-05-25T19:01:00Z"}
    sh._append_jsonl(paths.queue_file, low)
    sh._append_jsonl(paths.queue_file, high)
    nxt = sh._pick_next_job(paths)
    assert nxt["job_id"] == "high"


def test_claim_lock_is_exclusive(tmp_path):
    paths = _new_paths(tmp_path)
    lock1 = sh._claim_lock(paths, "abc")
    assert lock1 is not None and lock1.exists()
    lock2 = sh._claim_lock(paths, "abc")
    assert lock2 is None
    sh._release_lock(lock1)
    lock3 = sh._claim_lock(paths, "abc")
    assert lock3 is not None and lock3.exists()


def test_build_bright_seed_cmd_passes_autopilot_and_overrides(tmp_path):
    job = {
        "job_id": "j",
        "input_path": "/tmp/ct.nrrd",
        "media_id": "j_media",
        "params_override": {
            "intensity_drop_floor_frac": 0.2,
            "min_local_density": 0.5,
            "max_steps": 100,
        },
    }
    cmd = sh._build_bright_seed_cmd(
        nni_python=Path("/usr/bin/python3"),
        job=job,
        out_dir=tmp_path / "out",
        job_log=tmp_path / "out" / "log",
    )
    # Skeleton flags
    assert "--autopilot" in cmd
    assert "--no-previews" in cmd
    assert "--input" in cmd and "/tmp/ct.nrrd" in cmd
    assert "--media-id" in cmd and "j_media" in cmd
    # Overrides
    assert "--intensity-drop-floor-frac" in cmd
    assert "0.2" in cmd
    assert "--min-local-density" in cmd
    assert "0.5" in cmd
    assert "--max-steps" in cmd
    assert "100" in cmd


def test_run_one_job_records_summary(tmp_path, monkeypatch):
    paths = _new_paths(tmp_path)
    job = sh.build_jobs(
        specimens=[_spec("mouse", str(tmp_path / "ct.nrrd"))],
        param_grid={"intensity_drop_floor_frac": [0.2]},
    )[0]

    # Fake the subprocess: write the bright-seed summary.json
    # straight to the expected path.
    media_id = job["media_id"]

    def fake_subprocess_run(cmd, **kwargs):
        out_dir_idx = cmd.index("--output-dir")
        out_dir = Path(cmd[out_dir_idx + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "n_clicks": 7,
            "union_voxels": 12345,
            # Mirror the real bright-seed shape: stop_reason is a dict.
            "stop_reason": {"reason": "intensity_below_obvious",
                            "step": 7, "intensity": 177.0},
            "params": {"intensity_drop_floor_frac": 0.2},
            "labelmap_path": str(out_dir
                                 / f"{media_id}_nni_labelmap.nii.gz"),
        }
        (out_dir / f"{media_id}_nni_summary.json").write_text(
            json.dumps(summary)
        )

        class _Proc:
            returncode = 0
        return _Proc()

    monkeypatch.setattr(sh.subprocess, "run", fake_subprocess_run)

    result = sh.run_one_job(paths, job, nni_python=Path(sys.executable))
    assert result["status"] == "success"
    assert result["subprocess_exit_code"] == 0
    assert result["n_clicks"] == 7
    assert result["union_voxels"] == 12345
    assert result["stop_reason"] == "intensity_below_obvious"
    assert result["stop_block"]["step"] == 7
    # File markers + paths exist
    assert Path(result["output_dir"]).exists()
    assert Path(result["log_path"]).exists()


def test_run_one_job_records_failure_on_nonzero_exit(tmp_path, monkeypatch):
    paths = _new_paths(tmp_path)
    job = sh.build_jobs(specimens=[_spec("mouse")])[0]

    def fake_subprocess_run(cmd, **kwargs):
        class _Proc:
            returncode = 3
        return _Proc()

    monkeypatch.setattr(sh.subprocess, "run", fake_subprocess_run)
    result = sh.run_one_job(paths, job, nni_python=Path(sys.executable))
    assert result["status"] == "failure"
    assert result["subprocess_exit_code"] == 3
    assert result.get("summary_missing")


def test_run_daemon_loops_through_queue_once(tmp_path, monkeypatch):
    paths = _new_paths(tmp_path)
    jobs = sh.build_jobs(
        specimens=[_spec("a"), _spec("b")],
        param_grid={"intensity_drop_floor_frac": [0.2]},
    )
    sh.enqueue(paths, jobs)

    def fake_subprocess_run(cmd, **kwargs):
        out_dir_idx = cmd.index("--output-dir")
        out_dir = Path(cmd[out_dir_idx + 1])
        media_idx = cmd.index("--media-id")
        media_id = cmd[media_idx + 1]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{media_id}_nni_summary.json").write_text(
            json.dumps({"n_clicks": 5, "union_voxels": 1000,
                        "stop_reason": {"reason":
                                        "candidates_exhausted"},
                        "params": {}, "labelmap_path": "x"})
        )

        class _Proc:
            returncode = 0
        return _Proc()

    monkeypatch.setattr(sh.subprocess, "run", fake_subprocess_run)
    # The daemon won't launch without a real nni_python file on disk.
    # Use sys.executable as a stand-in: it always exists.
    n = sh.run_daemon(
        paths, nni_python=Path(sys.executable),
        once=True, sleep_when_idle_s=0,
    )
    assert n == 2
    results = sh._read_jsonl(paths.results_file)
    assert len(results) == 2
    assert {r["job_id"] for r in results} == {j["job_id"] for j in jobs}


def test_render_status_counts(tmp_path):
    paths = _new_paths(tmp_path)
    jobs = sh.build_jobs(
        specimens=[_spec("a"), _spec("b"), _spec("c")],
        param_grid={"intensity_drop_floor_frac": [0.2]},
    )
    sh.enqueue(paths, jobs)
    sh._append_jsonl(paths.results_file,
                     {"job_id": jobs[0]["job_id"], "status": "success",
                      "n_clicks": 5, "union_voxels": 1000,
                      "stop_reason": "candidates_exhausted",
                      "duration_s": 12.3})
    sh._claim_lock(paths, jobs[1]["job_id"])
    status = sh.render_status(paths)
    assert "queue:     3 jobs" in status
    assert "done:      1" in status
    assert "in_flight: 1" in status
    assert "pending:   1" in status
