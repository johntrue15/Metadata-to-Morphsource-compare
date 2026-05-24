"""Unit tests for the record / replay / passthrough sessions.

We monkey-patch ``urllib.request.urlopen`` for the duration of each
test so no real network is hit. The fake ``urlopen`` echoes a canned
response and records the call args for assertions.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from metadata_to_morphsource.jetstream_replay.recorder import (  # noqa: E402
    HTTPCall,
    PassThroughSession,
    RecordingSession,
    ReplayMismatch,
    ReplaySession,
    _CannedResponse,
    _decode_body,
    _materialise_request,
    get_active_session,
    make_session_from_env,
    reset_active_session,
    set_active_session,
    urlopen_via_session,
)


# ---------------------------------------------------------------------------
# Fake urllib.request.urlopen
# ---------------------------------------------------------------------------


class _FakeUrlopen:
    """Records every call and returns canned responses in order."""

    def __init__(self, responses: list[tuple[bytes, int]]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, request, data=None, timeout=60):
        method, url, headers, body = _materialise_request(request, data)
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": body,
             "timeout": timeout}
        )
        if not self._responses:
            raise RuntimeError("FakeUrlopen ran out of canned responses")
        payload, status = self._responses.pop(0)
        return _CannedResponse(payload, status=status,
                                headers={"Content-Type": "application/json"})


@pytest.fixture
def fake_urlopen(monkeypatch):
    fake = _FakeUrlopen([])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_session():
    """Make sure no test leaks a global session into the next."""
    reset_active_session()
    yield
    reset_active_session()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_decode_body_text_passes_through():
    text, b64 = _decode_body(b'{"hello": "world"}')
    assert text == '{"hello": "world"}'
    assert b64 is None


def test_decode_body_binary_falls_back_to_b64():
    raw = b"\x00\x01\xff\xfe\xfd"
    text, b64 = _decode_body(raw)
    assert text is None
    assert b64 is not None
    import base64
    assert base64.b64decode(b64) == raw


def test_decode_body_empty():
    text, b64 = _decode_body(b"")
    assert text == ""
    assert b64 is None


def test_materialise_request_from_url_string():
    method, url, headers, body = _materialise_request(
        "https://example.com/foo", data=b"hello",
    )
    assert method == "POST"
    assert url == "https://example.com/foo"
    assert body == b"hello"


def test_materialise_request_from_request_object():
    req = urllib.request.Request(
        "https://example.com/foo", data=b"hi",
        headers={"Content-Type": "text/plain"}, method="POST",
    )
    method, url, headers, body = _materialise_request(req, data=None)
    assert method == "POST"
    assert url == "https://example.com/foo"
    assert body == b"hi"
    assert headers.get("Content-type") in ("text/plain", "text/plain; charset=utf-8") \
        or "Content-type" in headers \
        or any(k.lower() == "content-type" for k in headers)


# ---------------------------------------------------------------------------
# PassThroughSession
# ---------------------------------------------------------------------------


def test_passthrough_session_calls_real_urlopen(fake_urlopen):
    fake_urlopen._responses.append((b'{"ok": 1}', 200))
    session = PassThroughSession()
    with session.urlopen("https://x/y", timeout=5) as resp:
        assert resp.status == 200
        assert resp.read() == b'{"ok": 1}'
    assert len(fake_urlopen.calls) == 1
    assert fake_urlopen.calls[0]["url"] == "https://x/y"


# ---------------------------------------------------------------------------
# RecordingSession
# ---------------------------------------------------------------------------


def test_recording_session_writes_jsonl(fake_urlopen, tmp_path):
    fake_urlopen._responses.extend([
        (b'{"reply": "first"}', 200),
        (b'{"reply": "second"}', 201),
    ])
    rec_path = tmp_path / "rec.jsonl"
    session = RecordingSession(rec_path)

    with session.urlopen("https://server/api/a", data=b"payload-a") as r:
        assert r.read() == b'{"reply": "first"}'

    req = urllib.request.Request(
        "https://server/api/b", data=b"payload-b",
        headers={"Content-Type": "text/plain"}, method="POST",
    )
    with session.urlopen(req) as r:
        assert r.read() == b'{"reply": "second"}'
        assert r.status == 201

    lines = [
        json.loads(line)
        for line in rec_path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == 2
    assert lines[0]["i"] == 0
    assert lines[0]["method"] == "POST"
    assert lines[0]["path"] == "/api/a"
    assert lines[0]["request_text"] == "payload-a"
    assert lines[0]["response_text"] == '{"reply": "first"}'
    assert lines[0]["status"] == 200
    assert lines[1]["i"] == 1
    assert lines[1]["status"] == 201
    assert lines[1]["path"] == "/api/b"


def test_recording_session_truncates_existing_file(fake_urlopen, tmp_path):
    rec_path = tmp_path / "rec.jsonl"
    rec_path.write_text("OLD GARBAGE\n")
    fake_urlopen._responses.append((b"ok", 200))
    sess = RecordingSession(rec_path)
    sess.urlopen("https://server/x").read()
    content = rec_path.read_text()
    assert "OLD GARBAGE" not in content


def test_recording_session_handles_binary_response(fake_urlopen, tmp_path):
    fake_urlopen._responses.append((b"\x00\x01\xff", 200))
    rec_path = tmp_path / "rec.jsonl"
    sess = RecordingSession(rec_path)
    sess.urlopen("https://server/bin").read()
    line = json.loads(rec_path.read_text().strip())
    assert line["response_text"] is None
    assert line["response_b64"] is not None


def test_recording_session_response_is_re_readable(fake_urlopen, tmp_path):
    """The response object returned to the caller must still be readable
    after the recorder has consumed its body for capture."""
    fake_urlopen._responses.append((b'{"k": 1}', 200))
    sess = RecordingSession(tmp_path / "rec.jsonl")
    with sess.urlopen("https://s/x") as resp:
        body = resp.read()
    assert body == b'{"k": 1}'


# ---------------------------------------------------------------------------
# ReplaySession
# ---------------------------------------------------------------------------


def _write_fixture(path: Path, calls: list[HTTPCall]) -> Path:
    with path.open("w") as fp:
        for c in calls:
            fp.write(json.dumps(c.to_dict()) + "\n")
    return path


def test_replay_session_returns_canned_response(tmp_path):
    fix = _write_fixture(tmp_path / "f.jsonl", [
        HTTPCall(
            i=0, method="POST", url="https://s/api", path="/api",
            headers={}, request_text="hello",
            request_b64=None, request_sha256="x",
            status=200, response_text='{"ok": true}',
            response_b64=None, response_sha256="y",
            dt_s=0.01, match_keys=["method", "path"],
        ),
    ])
    sess = ReplaySession(fix)
    with sess.urlopen("https://s/api", data=b"hello") as resp:
        assert resp.status == 200
        assert resp.read() == b'{"ok": true}'
    assert sess.remaining == 0


def test_replay_session_does_not_call_real_urlopen(tmp_path, monkeypatch):
    boom = lambda *a, **k: pytest.fail("real urlopen was called!")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    fix = _write_fixture(tmp_path / "f.jsonl", [
        HTTPCall(
            i=0, method="GET", url="https://s/x", path="/x",
            headers={}, request_text="", request_b64=None, request_sha256="",
            status=200, response_text="ok", response_b64=None,
            response_sha256="", dt_s=0,
        ),
    ])
    sess = ReplaySession(fix)
    with sess.urlopen("https://s/x") as resp:
        assert resp.read() == b"ok"


def test_replay_session_method_mismatch_raises(tmp_path):
    fix = _write_fixture(tmp_path / "f.jsonl", [
        HTTPCall(
            i=0, method="GET", url="https://s/x", path="/x",
            headers={}, request_text="", request_b64=None,
            request_sha256="", status=200, response_text="ok",
            response_b64=None, response_sha256="", dt_s=0,
        ),
    ])
    sess = ReplaySession(fix)
    with pytest.raises(ReplayMismatch) as ei:
        sess.urlopen("https://s/x", data=b"foo")  # data => POST
    assert "method mismatch" in str(ei.value)


def test_replay_session_path_mismatch_raises(tmp_path):
    fix = _write_fixture(tmp_path / "f.jsonl", [
        HTTPCall(
            i=0, method="GET", url="https://s/x", path="/x",
            headers={}, request_text="", request_b64=None,
            request_sha256="", status=200, response_text="ok",
            response_b64=None, response_sha256="", dt_s=0,
        ),
    ])
    sess = ReplaySession(fix)
    with pytest.raises(ReplayMismatch) as ei:
        sess.urlopen("https://s/different")
    assert "path mismatch" in str(ei.value)


def test_replay_session_request_sha_check(tmp_path):
    """When match_keys includes request_sha256, body bytes must match."""
    import hashlib
    body_text = "exact match"
    sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    fix = _write_fixture(tmp_path / "f.jsonl", [
        HTTPCall(
            i=0, method="POST", url="https://s/x", path="/x",
            headers={}, request_text=body_text, request_b64=None,
            request_sha256=sha, status=200, response_text="ok",
            response_b64=None, response_sha256="", dt_s=0,
            match_keys=["method", "path", "request_sha256"],
        ),
    ])
    sess = ReplaySession(fix)
    # Matching body succeeds.
    with sess.urlopen("https://s/x", data=body_text.encode("utf-8")) as r:
        assert r.read() == b"ok"

    # Replay another with mismatched body -> ReplayMismatch.
    fix2 = _write_fixture(tmp_path / "f2.jsonl", [
        HTTPCall(
            i=0, method="POST", url="https://s/x", path="/x",
            headers={}, request_text=body_text, request_b64=None,
            request_sha256=sha, status=200, response_text="ok",
            response_b64=None, response_sha256="", dt_s=0,
            match_keys=["method", "path", "request_sha256"],
        ),
    ])
    sess2 = ReplaySession(fix2)
    with pytest.raises(ReplayMismatch) as ei:
        sess2.urlopen("https://s/x", data=b"different bytes")
    assert "sha256 mismatch" in str(ei.value)


def test_replay_session_exhausted_transcript(tmp_path):
    fix = _write_fixture(tmp_path / "f.jsonl", [])
    sess = ReplaySession(fix)
    with pytest.raises(ReplayMismatch) as ei:
        sess.urlopen("https://s/x")
    assert "exhausted" in str(ei.value) or "out of fixture" in str(ei.value)


def test_replay_session_assert_drained(tmp_path):
    fix = _write_fixture(tmp_path / "f.jsonl", [
        HTTPCall(
            i=0, method="GET", url="https://s/a", path="/a",
            headers={}, request_text="", request_b64=None,
            request_sha256="", status=200, response_text="x",
            response_b64=None, response_sha256="", dt_s=0,
        ),
        HTTPCall(
            i=1, method="GET", url="https://s/b", path="/b",
            headers={}, request_text="", request_b64=None,
            request_sha256="", status=200, response_text="y",
            response_b64=None, response_sha256="", dt_s=0,
        ),
    ])
    sess = ReplaySession(fix)
    sess.urlopen("https://s/a").read()
    with pytest.raises(ReplayMismatch) as ei:
        sess.assert_drained()
    assert "1 fixture entries unconsumed" in str(ei.value)
    sess.urlopen("https://s/b").read()
    sess.assert_drained()


def test_replay_session_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplaySession(tmp_path / "nope.jsonl")


# ---------------------------------------------------------------------------
# Factory + global state
# ---------------------------------------------------------------------------


def test_make_session_from_env_passthrough(monkeypatch):
    monkeypatch.delenv("JETSTREAM_RECORD", raising=False)
    monkeypatch.delenv("JETSTREAM_REPLAY", raising=False)
    s = make_session_from_env()
    assert isinstance(s, PassThroughSession)


def test_make_session_from_env_record(monkeypatch, tmp_path):
    rec = tmp_path / "x.jsonl"
    monkeypatch.setenv("JETSTREAM_RECORD", str(rec))
    monkeypatch.delenv("JETSTREAM_REPLAY", raising=False)
    s = make_session_from_env()
    assert isinstance(s, RecordingSession)
    assert s.record_path == rec


def test_make_session_from_env_replay_takes_precedence(monkeypatch, tmp_path):
    fix = _write_fixture(tmp_path / "f.jsonl", [])
    monkeypatch.setenv("JETSTREAM_REPLAY", str(fix))
    monkeypatch.setenv("JETSTREAM_RECORD", str(tmp_path / "rec.jsonl"))
    s = make_session_from_env()
    assert isinstance(s, ReplaySession)


def test_urlopen_via_session_uses_active_session(fake_urlopen):
    fake_urlopen._responses.append((b"alive", 200))
    # Default = passthrough.
    with urlopen_via_session("https://s/y") as resp:
        assert resp.read() == b"alive"
    assert len(fake_urlopen.calls) == 1


def test_urlopen_via_session_routes_through_replay(tmp_path, monkeypatch):
    fix = _write_fixture(tmp_path / "f.jsonl", [
        HTTPCall(
            i=0, method="GET", url="https://s/y", path="/y",
            headers={}, request_text="", request_b64=None,
            request_sha256="", status=200, response_text="canned",
            response_b64=None, response_sha256="", dt_s=0,
        ),
    ])
    monkeypatch.setenv("JETSTREAM_REPLAY", str(fix))
    reset_active_session()  # force re-read of env
    boom = lambda *a, **k: pytest.fail("real urlopen called!")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with urlopen_via_session("https://s/y") as resp:
        assert resp.read() == b"canned"


def test_set_active_session_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JETSTREAM_REPLAY", str(tmp_path / "missing.jsonl"))
    reset_active_session()
    sess = PassThroughSession()
    set_active_session(sess)
    assert get_active_session() is sess


# ---------------------------------------------------------------------------
# CannedResponse contract
# ---------------------------------------------------------------------------


def test_canned_response_supports_context_manager_and_headers():
    r = _CannedResponse(b"hi", status=204, headers={"X-Foo": "bar"})
    with r as resp:
        assert resp.status == 204
        assert resp.code == 204
        assert resp.read() == b"hi"
        assert resp.getheader("x-foo") == "bar"
        assert resp.getheader("missing", "fallback") == "fallback"
        assert ("x-foo", "bar") in resp.getheaders()
