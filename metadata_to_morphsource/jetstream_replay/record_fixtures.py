"""Record synthetic JSONL fixtures for the offline replay tier."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSIONS = ROOT / "Tests" / "fixtures" / "jetstream_replay" / "sessions"
MANIFEST = ROOT / "Tests" / "fixtures" / "jetstream_replay" / "cached_specimens.json"
ORCH = ROOT / ".github" / "scripts" / "eval_project358382_pilot.py"


def _nni_python() -> str:
    for cand in (
        os.environ.get("NNI_PY_BIN"),
        str(Path.home() / ".autoresearchclaw" / "nninteractive" / "bin" / "python"),
        sys.executable,
    ):
        if cand and Path(cand).exists():
            return cand
    return sys.executable


def main() -> int:
    from metadata_to_morphsource.jetstream_replay.build_replay_bundle import build_bundle
    from metadata_to_morphsource.jetstream_replay.mock_slicer_server import serve_in_background

    raw = json.loads(MANIFEST.read_text())
    entries = raw.get("specimens", raw) if isinstance(raw, dict) else raw
    if not entries:
        print("No specimens in manifest", file=sys.stderr)
        return 1

    SESSIONS.mkdir(parents=True, exist_ok=True)
    py = _nni_python()

    entry = entries[0]
    ct_id = entry["ct_media_id"]
    out_jsonl = SESSIONS / f"{ct_id}.jsonl"
    base_url, httpd, _thread = serve_in_background()

    with tempfile.TemporaryDirectory(prefix="replay_record_") as tmp:
        out_dir = Path(tmp) / "record_run"
        build_bundle(MANIFEST, out_dir, use_existing_sessions=SESSIONS if any(SESSIONS.glob("*.jsonl")) else None)

        env = os.environ.copy()
        env["SLICER_WEBSERVER_URL"] = base_url
        env["NNI_REMOTE_URL"] = base_url
        env.pop("JETSTREAM_RECORD", None)
        env.pop("JETSTREAM_REPLAY", None)

        cmd = [
            py, str(ORCH),
            "--project-id", "000358382",
            "--project-query", "Colors of Skull Anatomy",
            "--cached-specimens", str(MANIFEST),
            "--record-to", str(SESSIONS),
            "--specimens", "1",
            "--budgets", "10",
            "--max-steps", "10",
            "--no-screenshots",
            "--out-dir", str(out_dir),
        ]
        print("Recording:", " ".join(cmd), file=sys.stderr)
        proc = subprocess.run(cmd, env=env, cwd=str(ROOT))
        httpd.shutdown()

    if proc.returncode != 0 and out_jsonl.exists() and out_jsonl.stat().st_size > 0:
        print(f"Orchestrator exit {proc.returncode} but transcript written; continuing",
              file=sys.stderr)
    elif proc.returncode != 0:
        print(f"Recording failed with exit {proc.returncode}", file=sys.stderr)
        return proc.returncode
    if not out_jsonl.exists() or out_jsonl.stat().st_size == 0:
        print(f"No JSONL written to {out_jsonl}", file=sys.stderr)
        return 2
    print(f"Wrote {out_jsonl} ({out_jsonl.stat().st_size} bytes)")

    for other in entries[1:]:
        dest = SESSIONS / f"{other['ct_media_id']}.jsonl"
        if not dest.exists():
            dest.write_bytes(out_jsonl.read_bytes())
            print(f"Linked {dest.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
