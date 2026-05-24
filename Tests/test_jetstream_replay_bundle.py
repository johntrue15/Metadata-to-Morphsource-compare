"""Tests for the build_replay_bundle staging CLI.

These tests need numpy + SimpleITK (for the synthetic fixture
generator). On environments where they aren't importable, the tests
are skipped cleanly so the rest of the suite stays green.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytest.importorskip("numpy")
pytest.importorskip("SimpleITK")


from metadata_to_morphsource.jetstream_replay import (  # noqa: E402
    build_replay_bundle as brb,
    recorder,
)
from metadata_to_morphsource.jetstream_replay.synthetic import (  # noqa: E402
    make_fixture_specimen,
)


@pytest.fixture
def manifest(tmp_path):
    """A 2-specimen cached manifest written to tmp_path."""
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "ct_provider": "synthetic",
        "specimens": [
            {
                "physical_object_id": "PO-1",
                "physical_object_title": "Test 1",
                "taxonomy": "Test species 1",
                "ct_media_id": "synth_ct_1",
                "ct_title": "ct1",
                "ct_media_type": "ct image series",
                "ct_file_size": None,
                "mesh_media_id": "mesh_1",
                "mesh_title": "m1",
                "mesh_media_type": "mesh",
                "mesh_file_size": None,
                "project_query": "Test",
            },
            {
                "physical_object_id": "PO-2",
                "physical_object_title": "Test 2",
                "taxonomy": "Test species 2",
                "ct_media_id": "synth_ct_2",
                "ct_title": "ct2",
                "ct_media_type": "ct image series",
                "ct_file_size": None,
                "mesh_media_id": "mesh_2",
                "mesh_title": "m2",
                "mesh_media_type": "mesh",
                "mesh_file_size": None,
                "project_query": "Test",
            },
        ],
    }))
    return p


# ---------------------------------------------------------------------------
# stage_synthetic_specimen
# ---------------------------------------------------------------------------


def test_stage_synthetic_specimen_lays_down_full_pipeline_inputs(tmp_path):
    info = brb.stage_synthetic_specimen(
        tmp_path, slug="test_slug",
        ct_media_id="ct1", mesh_media_id="m1",
        seed=0, shape=(16, 16, 16), spacing_mm=(1.0, 1.0, 1.0),
    )
    spec_dir = Path(info["specimen_dir"])
    assert (spec_dir / "ct_volume.nii.gz").exists()
    assert (spec_dir / "mesh.ply").exists()
    assert (spec_dir / "preprocessing" / "ct_cropped.nii.gz").exists()
    assert (spec_dir / "preprocessing" / "gt_voxelized.nii.gz").exists()
    # The download dirs must mirror morphosource_media-id-* layout so
    # download_pair's caching short-circuits on offline runs.
    assert any(
        (spec_dir / "downloads" / "ct_download").glob(
            "morphosource_media-id-*"
        )
    )
    assert any(
        (spec_dir / "downloads" / "mesh_download").glob(
            "morphosource_media-id-*"
        )
    )
    for p in [info["ct_volume"], info["ct_cropped"],
              info["gt_voxelized"], info["mesh_path"]]:
        assert Path(p).stat().st_size > 0


def test_stage_synthetic_is_deterministic(tmp_path):
    a = brb.stage_synthetic_specimen(
        tmp_path / "a", slug="x",
        ct_media_id="ct1", mesh_media_id="m1",
        seed=42, shape=(16, 16, 16), spacing_mm=(1.0, 1.0, 1.0),
    )
    b = brb.stage_synthetic_specimen(
        tmp_path / "b", slug="x",
        ct_media_id="ct1", mesh_media_id="m1",
        seed=42, shape=(16, 16, 16), spacing_mm=(1.0, 1.0, 1.0),
    )
    assert (
        Path(a["ct_volume"]).read_bytes()
        == Path(b["ct_volume"]).read_bytes()
    )


# ---------------------------------------------------------------------------
# build_minimal_transcript
# ---------------------------------------------------------------------------


def test_minimal_transcript_is_replayable(tmp_path):
    sess = brb.build_minimal_transcript(
        tmp_path / "stub.jsonl", slug="x", ct_media_id="ct1",
    )
    assert sess.exists()
    sidecar = sess.with_suffix(sess.suffix + ".meta.json")
    assert sidecar.exists()
    meta = json.loads(sidecar.read_text())
    assert meta["kind"] == "synthetic_stub"

    # Each line must round-trip through HTTPCall.from_dict.
    lines = [json.loads(l) for l in sess.read_text().splitlines() if l.strip()]
    assert lines, "stub transcript must not be empty"
    for d in lines:
        c = recorder.HTTPCall.from_dict(d)
        assert c.method in {"GET", "POST"}
        assert c.path.startswith("/")
        assert c.status == 200

    # ReplaySession can consume it. We pass each entry's recorded URL
    # back in verbatim (it already includes scheme+host+path); the
    # session matches on the parsed path component.
    rs = recorder.ReplaySession(sess)
    assert rs.n_calls == len(lines)
    for d in lines:
        body = (d.get("request_text") or "").encode("utf-8") if d["method"] == "POST" else None
        with rs.urlopen(d["url"], data=body) as resp:
            payload = resp.read()
            assert resp.status == 200
            json.loads(payload)
    rs.assert_drained()


# ---------------------------------------------------------------------------
# build_bundle (top-level)
# ---------------------------------------------------------------------------


def test_build_bundle_creates_specimen_dirs_and_sessions(tmp_path, manifest):
    out = tmp_path / "bundle"
    bundle = brb.build_bundle(manifest, out, shape=(16, 16, 16))
    assert (out / "bundle.json").exists()
    assert len(bundle["specimens"]) == 2

    seen_ct_ids = set()
    for row in bundle["specimens"]:
        seen_ct_ids.add(row["ct_media_id"])
        assert Path(row["specimen_dir"]).is_dir()
        for k in ("ct_volume", "ct_cropped", "gt_voxelized",
                  "mesh_path", "session_path"):
            assert Path(row[k]).exists(), f"missing {k}: {row}"
        assert row["session_kind"] in {"synthetic_stub", "real"}
    assert seen_ct_ids == {"synth_ct_1", "synth_ct_2"}


def test_build_bundle_prefers_real_sessions_when_provided(tmp_path, manifest):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    # Drop a real-recording sentinel for ct_1 only.
    real_for_one = real_dir / "synth_ct_1.jsonl"
    real_for_one.write_text(json.dumps({
        "i": 0, "method": "POST", "url": "http://x/",
        "path": "/", "headers": {}, "request_text": "real",
        "request_b64": None, "request_sha256": "",
        "status": 200, "response_text": "{\"k\":1}", "response_b64": None,
        "response_sha256": "", "dt_s": 0.0, "match_keys": ["method", "path"],
    }) + "\n")

    out = tmp_path / "bundle"
    bundle = brb.build_bundle(
        manifest, out, shape=(16, 16, 16),
        use_existing_sessions=real_dir,
    )
    by_id = {row["ct_media_id"]: row for row in bundle["specimens"]}
    assert by_id["synth_ct_1"]["session_kind"] == "real"
    assert by_id["synth_ct_2"]["session_kind"] == "synthetic_stub"
    assert (
        Path(by_id["synth_ct_1"]["session_path"]).read_text()
        == real_for_one.read_text()
    )


def test_build_bundle_committed_fixture_round_trip(tmp_path):
    """The committed cached_specimens.json bundles cleanly."""
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures" / "jetstream_replay" / "cached_specimens.json"
    )
    out = tmp_path / "bundle"
    bundle = brb.build_bundle(fixture, out, shape=(16, 16, 16))
    assert len(bundle["specimens"]) == 3
    for row in bundle["specimens"]:
        # Each session must be a valid JSONL transcript.
        for line in Path(row["session_path"]).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            recorder.HTTPCall.from_dict(d)  # validates
