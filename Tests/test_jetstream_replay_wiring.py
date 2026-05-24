"""Verify the .github/scripts wiring routes through the session factory.

We don't actually drive Slicer; we just confirm that:

1. ``slicer_remote_bright_seed.post_python`` and
   ``slicer_remote_bright_seed.http_get`` go through
   :func:`urlopen_via_session` (and therefore get recorded / replayed
   when the env vars are set).
2. ``remote_volume_io._post_python`` does the same.

We do this by replacing the module-level ``urlopen_via_session``
binding with a fake collector and inspecting the captured calls.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from metadata_to_morphsource.jetstream_replay.recorder import (  # noqa: E402
    HTTPCall,
    ReplaySession,
    set_active_session,
    reset_active_session,
)


@pytest.fixture(autouse=True)
def _reset_session():
    reset_active_session()
    yield
    reset_active_session()


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, request, data=None, timeout=60):
        # Preserve the same coercion the recorder uses for assertions.
        from metadata_to_morphsource.jetstream_replay.recorder import (
            _materialise_request, _CannedResponse,
        )
        method, url, headers, body = _materialise_request(request, data)
        self.calls.append({"method": method, "url": url, "body": body})
        return _CannedResponse(b'{"status": "ok"}', status=200,
                                headers={"Content-Type": "application/json"})


def test_remote_volume_io_post_python_is_routed(monkeypatch):
    """``_post_python`` should hit our session, not raw urlopen."""
    import remote_volume_io as rvi
    rec = _Recorder()
    monkeypatch.setattr(rvi, "urlopen_via_session", rec)
    # Boom if real urlopen is touched.
    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("real urlopen called from rvi!"),
    )
    out = rvi._post_python("https://example.com/", "print('hi')", timeout=5)
    assert out["status"] == "ok"
    assert len(rec.calls) == 1
    assert rec.calls[0]["url"].endswith("/slicer/exec")
    assert rec.calls[0]["body"] == b"print('hi')"


def test_slicer_remote_bright_seed_post_python_is_routed(monkeypatch):
    import slicer_remote_bright_seed as srbs
    rec = _Recorder()
    monkeypatch.setattr(srbs, "urlopen_via_session", rec)
    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("real urlopen called from srbs!"),
    )
    out = srbs.post_python("https://example.com", "print('hello')")
    assert out["status"] == "ok"
    assert len(rec.calls) == 1
    assert rec.calls[0]["url"].endswith("/slicer/exec")
    assert rec.calls[0]["body"] == b"print('hello')"


def test_slicer_remote_bright_seed_http_get_is_routed(monkeypatch):
    import slicer_remote_bright_seed as srbs
    rec = _Recorder()
    monkeypatch.setattr(srbs, "urlopen_via_session", rec)
    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("real urlopen called!"),
    )
    body = srbs.http_get("https://example.com/screenshot")
    assert body == b'{"status": "ok"}'
    assert len(rec.calls) == 1
    assert rec.calls[0]["method"] == "GET"


def test_replay_session_drives_post_python_end_to_end(tmp_path, monkeypatch):
    """Wire a real ReplaySession into srbs.post_python and consume one entry."""
    import slicer_remote_bright_seed as srbs
    fix_path = tmp_path / "session.jsonl"
    with fix_path.open("w") as fp:
        fp.write(json.dumps({
            "i": 0,
            "method": "POST",
            "url": "https://server/slicer/exec",
            "path": "/slicer/exec",
            "headers": {"Content-Type": "text/plain"},
            "request_text": "ping",
            "request_b64": None,
            "request_sha256": "",
            "status": 200,
            "response_text": json.dumps({"status": "ok", "value": 42}),
            "response_b64": None,
            "response_sha256": "",
            "dt_s": 0.0,
            "match_keys": ["method", "path"],
        }) + "\n")
    sess = ReplaySession(fix_path)
    set_active_session(sess)

    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("real urlopen called during replay!"),
    )
    out = srbs.post_python("https://server", "ping")
    assert out["status"] == "ok"
    assert out["value"] == 42
    sess.assert_drained()
