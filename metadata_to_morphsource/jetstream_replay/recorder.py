"""HTTP record / replay middleware for the project-358382 pilot.

The two scripts that actually hit Jetstream — ``slicer_remote_bright_seed``
and ``remote_volume_io`` — funnel every round-trip through one of two
helpers (``post_python`` / ``http_get`` and ``_post_python``). Each of
those helpers ultimately reaches ``urllib.request.urlopen``. Instead of
rewriting their wire formats, we expose a single function,
:func:`urlopen_via_session`, that the helpers call instead. In
production it's a one-line passthrough; in record/replay mode it
captures or returns canned bytes.

Sessions
--------

- :class:`PassThroughSession` — calls ``urllib.request.urlopen`` and
  returns its response unchanged. Default when no env vars are set.
- :class:`RecordingSession` — wraps a passthrough, additionally
  appending every (method, url, payload, status, body, dt_s) record
  as one JSONL line to ``record_path``.
- :class:`ReplaySession` — does **no** real I/O. Pops the next entry
  from ``replay_path`` and returns a synthetic file-like object that
  matches ``urlopen``'s contract. A request mismatch raises
  :class:`ReplayMismatch` with a unified diff so drift is visible
  immediately.

The :func:`make_session_from_env` factory inspects ``JETSTREAM_RECORD``
and ``JETSTREAM_REPLAY`` and returns the right session.

JSONL fixture format
--------------------

One JSON object per line, with these keys::

    {
      "i": 0,                            # 0-based call index
      "method": "POST",
      "url": "https://.../slicer/exec",
      "headers": {"Content-Type": "text/plain"},
      "request_text": "import slicer\\n...",   # decoded UTF-8 body
      "request_b64": null,                       # set if body wasn't UTF-8
      "status": 200,
      "response_text": "{\\"status\\": \\"ok\\"}",
      "response_b64": null,
      "dt_s": 0.31,
      "match_keys": ["method", "path"]   # how the replayer compares
    }

The ``match_keys`` field controls the matching strategy at replay
time. Default is ``["method", "path"]`` plus a checksum of the
request body. Per-call overrides are supported via
``RecordingSession.next_match_keys = [...]`` before a single call,
which makes capturing chunked uploads tractable (we don't want a huge
gzip blob to bite us if it differs by a few bytes between record and
replay).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union


log = logging.getLogger("jetstream_replay")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class HTTPCall:
    """One captured HTTP round-trip (request + response)."""

    i: int
    method: str
    url: str
    path: str
    headers: dict
    request_text: Optional[str]
    request_b64: Optional[str]
    request_sha256: str
    status: int
    response_text: Optional[str]
    response_b64: Optional[str]
    response_sha256: str
    dt_s: float
    match_keys: list[str] = field(default_factory=lambda: ["method", "path"])

    def to_dict(self) -> dict:
        return {
            "i": self.i,
            "method": self.method,
            "url": self.url,
            "path": self.path,
            "headers": dict(self.headers),
            "request_text": self.request_text,
            "request_b64": self.request_b64,
            "request_sha256": self.request_sha256,
            "status": self.status,
            "response_text": self.response_text,
            "response_b64": self.response_b64,
            "response_sha256": self.response_sha256,
            "dt_s": self.dt_s,
            "match_keys": list(self.match_keys),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HTTPCall":
        # Backfill request/response_sha256 if older fixtures don't have it.
        rid = d.get("request_sha256") or _sha256_of(
            d.get("request_text") or "", d.get("request_b64")
        )
        sid = d.get("response_sha256") or _sha256_of(
            d.get("response_text") or "", d.get("response_b64")
        )
        return cls(
            i=int(d.get("i", 0)),
            method=str(d.get("method", "GET")).upper(),
            url=str(d.get("url", "")),
            path=str(d.get("path", _url_path(d.get("url", "")))),
            headers=dict(d.get("headers") or {}),
            request_text=d.get("request_text"),
            request_b64=d.get("request_b64"),
            request_sha256=rid,
            status=int(d.get("status", 200)),
            response_text=d.get("response_text"),
            response_b64=d.get("response_b64"),
            response_sha256=sid,
            dt_s=float(d.get("dt_s", 0.0)),
            match_keys=list(d.get("match_keys") or ["method", "path"]),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_path(url: str) -> str:
    """Return just the URL path for matching (drops scheme/host/query)."""
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    return parsed.path or "/"


def _sha256_of(text: str | None, b64: str | None) -> str:
    h = hashlib.sha256()
    if text is not None:
        h.update(text.encode("utf-8", "replace"))
    elif b64:
        try:
            h.update(base64.b64decode(b64))
        except Exception:
            h.update(b64.encode("ascii", "replace"))
    return h.hexdigest()


def _decode_body(raw: bytes) -> tuple[Optional[str], Optional[str]]:
    """Return (text, b64). One of them is set, the other is None.

    Plain ASCII / UTF-8 stays as ``request_text``; binary or invalid
    UTF-8 lands in ``request_b64`` so the JSONL stays human-readable
    when possible but lossless when not.
    """
    if raw is None or len(raw) == 0:
        return "", None
    try:
        text = raw.decode("utf-8")
        # Reject the decode if the round-trip changes bytes (rare on
        # binary that happens to start with valid UTF-8 prefixes).
        if text.encode("utf-8") == raw:
            return text, None
    except UnicodeDecodeError:
        pass
    return None, base64.b64encode(raw).decode("ascii")


def _materialise_request(
    request: Union[str, urllib.request.Request],
    data: Optional[bytes],
) -> tuple[str, str, dict, bytes]:
    """Normalise (method, url, headers, body) from either signature.

    ``urllib.request.urlopen`` accepts both a bare URL string and a
    Request object. We always want to capture both; here we coerce
    them into a single shape.
    """
    if isinstance(request, urllib.request.Request):
        method = (request.get_method() or "GET").upper()
        url = request.full_url
        # urllib lower-cases header names and stores them; surface a
        # capitalised view for the fixture for readability.
        headers = {k: v for k, v in request.header_items()}
        body = request.data or data or b""
    else:
        method = "POST" if data is not None else "GET"
        url = request
        headers = {}
        body = data or b""
    if not isinstance(body, (bytes, bytearray)):
        body = bytes(body)
    return method, url, headers, bytes(body)


# ---------------------------------------------------------------------------
# Synthetic ``urlopen`` response (used by ReplaySession)
# ---------------------------------------------------------------------------


class _CannedResponse(io.BytesIO):
    """File-like that mimics ``http.client.HTTPResponse``'s public API.

    Supports ``read()``, ``status``, context-manager use, and
    ``getheader()``. Anything more exotic than that and we extend.
    """

    def __init__(self, payload: bytes, *, status: int, headers: dict):
        super().__init__(payload)
        self.status = status
        self.code = status
        self._headers = {k.lower(): v for k, v in headers.items()}

    # urllib's response objects define `read()` that returns the body.
    # BytesIO already provides it.

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def getheader(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self._headers.get(name.lower(), default)

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())

    @property
    def headers(self) -> dict:
        return dict(self._headers)


# ---------------------------------------------------------------------------
# Session ABC + concrete implementations
# ---------------------------------------------------------------------------


class Session:
    """Base class. Subclasses override :meth:`urlopen`."""

    name = "base"

    def urlopen(
        self,
        request: Union[str, urllib.request.Request],
        data: Optional[bytes] = None,
        timeout: float = 60,
    ):
        raise NotImplementedError

    def close(self) -> None:
        """Flush + release any held resources."""

    # Allow `with session: ...` ergonomics in tests.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class PassThroughSession(Session):
    """The default. Calls ``urllib.request.urlopen`` unchanged."""

    name = "passthrough"

    def urlopen(self, request, data=None, timeout=60):
        return urllib.request.urlopen(request, data=data, timeout=timeout)


class ReplayMismatch(AssertionError):
    """Raised when a replayed request doesn't match its fixture entry."""


class RecordingSession(Session):
    """Pass-through wrapper that tees every round-trip to a JSONL file."""

    name = "recording"

    def __init__(self, record_path: os.PathLike | str):
        self.record_path = Path(record_path)
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate at construction time so a fresh recording starts
        # cleanly; appends are not wanted because they'd silently
        # concat across runs.
        self.record_path.write_text("")
        self._lock = threading.Lock()
        self._i = 0
        self._next_match_keys: Optional[list[str]] = None

    def set_next_match_keys(self, keys: Iterable[str]) -> None:
        """Override match keys for the next single call.

        Useful when one call carries a giant binary body that we don't
        want to byte-match on at replay (e.g. a chunked-upload append
        that shifts by a few bytes between record and replay because
        of timestamps in headers).
        """
        self._next_match_keys = list(keys)

    def urlopen(self, request, data=None, timeout=60):
        method, url, headers, body = _materialise_request(request, data)
        t0 = time.time()
        resp = urllib.request.urlopen(request, data=data, timeout=timeout)
        # We have to consume the body to record it. Replace the
        # response with a re-readable buffer so the caller still works.
        try:
            payload = resp.read()
            status = getattr(resp, "status", getattr(resp, "code", 200))
            resp_headers = dict(getattr(resp, "headers", {}) or {})
        finally:
            try:
                resp.close()
            except Exception:
                pass
        dt_s = round(time.time() - t0, 3)

        req_text, req_b64 = _decode_body(body)
        resp_text, resp_b64 = _decode_body(payload)
        match_keys = self._next_match_keys or ["method", "path"]
        self._next_match_keys = None
        with self._lock:
            call = HTTPCall(
                i=self._i,
                method=method,
                url=url,
                path=_url_path(url),
                headers=dict(headers),
                request_text=req_text,
                request_b64=req_b64,
                request_sha256=_sha256_of(req_text, req_b64),
                status=int(status),
                response_text=resp_text,
                response_b64=resp_b64,
                response_sha256=_sha256_of(resp_text, resp_b64),
                dt_s=dt_s,
                match_keys=match_keys,
            )
            with self.record_path.open("a") as fp:
                fp.write(json.dumps(call.to_dict(), ensure_ascii=False) + "\n")
            self._i += 1

        return _CannedResponse(payload, status=int(status), headers=resp_headers)


class ReplaySession(Session):
    """Replays a previously-recorded JSONL transcript."""

    name = "replay"

    def __init__(self, replay_path: os.PathLike | str, *, strict: bool = True):
        self.replay_path = Path(replay_path)
        if not self.replay_path.exists():
            raise FileNotFoundError(
                f"Replay fixture not found: {self.replay_path}"
            )
        with self.replay_path.open("r") as fp:
            self._calls: list[HTTPCall] = [
                HTTPCall.from_dict(json.loads(line))
                for line in fp
                if line.strip()
            ]
        self._lock = threading.Lock()
        self._i = 0
        self.strict = strict

    @property
    def n_calls(self) -> int:
        return len(self._calls)

    @property
    def remaining(self) -> int:
        return max(0, len(self._calls) - self._i)

    def assert_drained(self) -> None:
        """Caller can use this to fail loudly if not every call was consumed.

        Drift detection: if the algorithm under test stopped early, we
        want the test to flag the unconsumed fixture entries.
        """
        if self.remaining:
            raise ReplayMismatch(
                f"{self.remaining} fixture entries unconsumed at "
                f"{self.replay_path}"
            )

    def urlopen(self, request, data=None, timeout=60):
        method, url, headers, body = _materialise_request(request, data)
        path = _url_path(url)
        with self._lock:
            if self._i >= len(self._calls):
                raise ReplayMismatch(
                    f"out of fixture entries at call {self._i}; "
                    f"{method} {path} requested but transcript exhausted"
                )
            entry = self._calls[self._i]
            self._i += 1

        keys = set(entry.match_keys or ["method", "path"])
        problems: list[str] = []
        if "method" in keys and method != entry.method:
            problems.append(
                f"method mismatch: live={method} fixture={entry.method}"
            )
        if "path" in keys and path != entry.path:
            problems.append(
                f"path mismatch: live={path!r} fixture={entry.path!r}"
            )
        if "url" in keys and url != entry.url:
            problems.append(
                f"url mismatch: live={url!r} fixture={entry.url!r}"
            )
        if "request_sha256" in keys:
            req_text, req_b64 = _decode_body(body)
            live_sha = _sha256_of(req_text, req_b64)
            if live_sha != entry.request_sha256:
                problems.append(
                    "request body sha256 mismatch:\n"
                    f"  live    {live_sha}\n"
                    f"  fixture {entry.request_sha256}"
                )
        if problems and self.strict:
            raise ReplayMismatch(
                f"call #{entry.i} ({entry.method} {entry.path}) "
                "diverged from fixture:\n  - "
                + "\n  - ".join(problems)
            )

        if entry.response_text is not None:
            payload = entry.response_text.encode("utf-8")
        elif entry.response_b64:
            payload = base64.b64decode(entry.response_b64)
        else:
            payload = b""
        return _CannedResponse(
            payload, status=entry.status, headers={"Content-Type": "application/json"}
        )


# ---------------------------------------------------------------------------
# Module-level "current session" + factory
# ---------------------------------------------------------------------------


_current_session: Optional[Session] = None
_session_lock = threading.Lock()


def make_session_from_env(env: Optional[dict] = None) -> Session:
    """Return a :class:`Session` matching the env vars.

    Precedence: ``JETSTREAM_REPLAY`` > ``JETSTREAM_RECORD`` > pass-through.
    """
    e = os.environ if env is None else env
    replay = (e.get("JETSTREAM_REPLAY") or "").strip()
    record = (e.get("JETSTREAM_RECORD") or "").strip()
    if replay:
        log.info("jetstream_replay: ReplaySession(%s)", replay)
        return ReplaySession(replay)
    if record:
        log.info("jetstream_replay: RecordingSession(%s)", record)
        return RecordingSession(record)
    return PassThroughSession()


def get_active_session() -> Session:
    """Return the lazily-initialised session for the current process."""
    global _current_session
    with _session_lock:
        if _current_session is None:
            _current_session = make_session_from_env()
        return _current_session


def set_active_session(session: Optional[Session]) -> Session:
    """Override the active session (mostly for tests)."""
    global _current_session
    with _session_lock:
        _current_session = session
    return session


def reset_active_session() -> None:
    """Force the next call to re-read env vars."""
    global _current_session
    with _session_lock:
        _current_session = None


def urlopen_via_session(
    request: Union[str, urllib.request.Request],
    data: Optional[bytes] = None,
    timeout: float = 60,
):
    """Drop-in replacement for ``urllib.request.urlopen``.

    Routes through the active :class:`Session`. Behaviour is
    identical to ``urlopen`` when the session is :class:`PassThroughSession`
    (the default).
    """
    return get_active_session().urlopen(request, data=data, timeout=timeout)
