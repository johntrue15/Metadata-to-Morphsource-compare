"""Jetstream-side HTTP record/replay machinery for offline pilot tests.

This sub-package exists so the project-358382 ("Colors of Skull
Anatomy") segmentation pilot — which normally drives a live 3D Slicer
process on Jetstream2 over an Exosphere proxy — can be exercised
end-to-end **offline**, against the local specimen cache and a
recorded HTTP transcript.

The two consumer scripts that hit the Jetstream box today both live
under :mod:`.github.scripts`:

- ``slicer_remote_bright_seed.py`` — drives the deterministic
  bright-spot greedy seeder (no LLM).
- ``remote_volume_io.py`` — pushes / lists / hashes NIfTI volumes.

Both build their own ``urllib.request.urlopen`` calls. We don't want
to rewrite their wire formats; we just want a thin middleware that
either passes through, records, or replays each round-trip. That is
exactly the contract of :func:`recorder.urlopen_via_session` /
:class:`recorder.Session`.

Modules
-------
- :mod:`.recorder`     RecordingSession + ReplaySession + factory.
- :mod:`.cache_index`  Walks ``~/.autoresearchclaw/specimens/`` and
                        emits a JSON manifest compatible with
                        ``eval_project358382_pilot.py
                        --specimens-manifest``.
- :mod:`.synthetic`    Deterministic tiny-volume + tiny-mesh fixtures
                        used for the replay tier (since the cache is
                        mesh-only — no CT volumes are stored locally).

Environment contract (read by ``make_session_from_env``)
--------------------------------------------------------
- ``JETSTREAM_RECORD=<path>`` — pass-through; capture every
  ``(method, url, payload, response)`` to a JSONL.
- ``JETSTREAM_REPLAY=<path>`` — return canned responses from the
  recorded JSONL; hard-fail on a request mismatch.

Both unset = a no-op pass-through wrapper that calls
``urllib.request.urlopen(...)`` exactly as before.
"""

from .recorder import (  # noqa: F401
    HTTPCall,
    RecordingSession,
    ReplaySession,
    PassThroughSession,
    Session,
    make_session_from_env,
    urlopen_via_session,
)
from .cache_index import (  # noqa: F401
    CachedSpecimen,
    scan_specimens,
    write_manifest,
)


__all__ = [
    "HTTPCall",
    "RecordingSession",
    "ReplaySession",
    "PassThroughSession",
    "Session",
    "make_session_from_env",
    "urlopen_via_session",
    "CachedSpecimen",
    "scan_specimens",
    "write_manifest",
]
