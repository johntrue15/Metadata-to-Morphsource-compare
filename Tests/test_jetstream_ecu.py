"""Tests for MorphoClaw ECU job store (no network)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

from jetstream_ecu_server import JobStore  # noqa: E402


def test_job_store_create_and_persist(tmp_path: Path):
    store = JobStore(tmp_path)
    rec = store.create(
        ["python3", "-c", "print(1)"],
        str(tmp_path),
        {"FOO": "bar"},
        "test",
    )
    assert rec.status == "queued"
    meta = tmp_path / f"{rec.job_id}.json"
    assert meta.is_file()
    data = json.loads(meta.read_text())
    assert data["label"] == "test"
    assert data["argv"][0] == "python3"


def test_job_store_run_succeeds(tmp_path: Path):
    store = JobStore(tmp_path)
    rec = store.create(
        [sys.executable, "-c", "print('ok')"],
        str(tmp_path),
        {},
        "run",
    )
    store.start(rec)
    deadline = time.time() + 10
    while rec.status == "running" and time.time() < deadline:
        time.sleep(0.1)
    while rec.status == "queued" and time.time() < deadline:
        time.sleep(0.1)
    assert rec.status == "succeeded"
    assert rec.exit_code == 0
    assert "ok" in rec.log_path.read_text()
