"""Offline-flag tests for eval_project358382_pilot.

We don't drive the full pilot here — that's the job of
``Tests/test_eval_project358382.sh`` tier 4. This file exercises the
small, fast bits: argument parsing, the ``--cached-specimens`` /
``--replay-from`` / ``--no-download`` plumbing, and the
:func:`download_pair` short-circuit logic.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import eval_project358382_pilot as pilot  # noqa: E402


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_new_flags_default_off():
    ns = pilot._parse_args(["--out-dir", "/tmp/x"])
    assert ns.cached_specimens is None
    assert ns.replay_from is None
    assert ns.record_to is None
    assert ns.no_download is False


def test_cached_specimens_alias_parsed():
    ns = pilot._parse_args([
        "--cached-specimens", "/tmp/m.json",
        "--replay-from", "/tmp/fixtures",
        "--no-download",
        "--out-dir", "/tmp/x",
    ])
    assert ns.cached_specimens == Path("/tmp/m.json")
    assert ns.replay_from == Path("/tmp/fixtures")
    assert ns.no_download is True
    assert ns.record_to is None


# ---------------------------------------------------------------------------
# download_pair short-circuit
# ---------------------------------------------------------------------------


def _fake_pair(ct_id="ct1", mesh_id="mesh1") -> pilot.SpecimenPair:
    return pilot.SpecimenPair(
        physical_object_id="po-1",
        physical_object_title="t",
        taxonomy="Test species",
        ct_media_id=ct_id,
        ct_title="ct",
        ct_media_type="ct image series",
        ct_file_size=None,
        mesh_media_id=mesh_id,
        mesh_title="mesh",
        mesh_media_type="mesh",
        mesh_file_size=None,
        project_query="Test",
    )


def test_download_pair_no_download_fails_when_nothing_staged(tmp_path):
    pair = _fake_pair()
    with pytest.raises(pilot.NoDownloadError) as ei:
        pilot.download_pair(pair, tmp_path / "downloads",
                             no_download=True,
                             specimen_dir=tmp_path / "specimen")
    assert "neither cached" in str(ei.value).lower() or "no-download" in str(ei.value)


def test_download_pair_no_download_succeeds_with_prepared_ct_and_stub_mesh(tmp_path):
    spec_dir = tmp_path / "specimen"
    spec_dir.mkdir()
    # Pre-stage prepared CT (any nonzero file is fine for this test).
    (spec_dir / "ct_volume.nii.gz").write_bytes(b"\x00\x01\x02")
    # Pre-stage a stub mesh that the helper should copy into mesh_download.
    (spec_dir / "mesh.ply").write_text("ply ascii\n")
    out = pilot.download_pair(_fake_pair(), tmp_path / "downloads",
                                no_download=True,
                                specimen_dir=spec_dir)
    assert out["no_download"] is True
    assert out["ct"]["success"] is True
    assert out["mesh"]["success"] is True
    # The mesh_download dir should now contain a stub.
    mesh_dir = Path(out["mesh_dir"])
    assert any(mesh_dir.rglob("*.ply"))


def test_download_pair_no_download_succeeds_with_cached_extracted(tmp_path):
    """When the morphosource_media-id-* extracted dir is already there,
    --no-download accepts it without complaint."""
    out_dir = tmp_path / "downloads"
    ct_dir = out_dir / "ct_download"
    mesh_dir = out_dir / "mesh_download"
    extracted_ct = ct_dir / "morphosource_media-id-ct1_download-deadbeef"
    extracted_mesh = mesh_dir / "morphosource_media-id-mesh1_download-d00d"
    for d in (extracted_ct, extracted_mesh):
        d.mkdir(parents=True, exist_ok=True)
    (extracted_ct / "stub.tif").write_text("TIFF")
    (extracted_mesh / "stub.ply").write_text("ply ascii\n")
    spec_dir = tmp_path / "specimen"
    spec_dir.mkdir()

    out = pilot.download_pair(_fake_pair(), out_dir,
                                no_download=True,
                                specimen_dir=spec_dir)
    assert out["no_download"] is True


def test_download_pair_default_path_unchanged_when_offline_off(monkeypatch, tmp_path):
    """Without --no-download, download_pair still calls download_media
    (we monkey-patch it so the test stays offline)."""
    calls: list[tuple[str, str]] = []

    def fake_download_media(media_id, target_dir):
        calls.append((media_id, str(target_dir)))
        # Mimic a successful download without producing any zip.
        return {"success": True, "media_id": media_id,
                "download_dir": str(target_dir)}

    fake_mod = type(sys)("morphosource_api_download")
    fake_mod.download_media = fake_download_media
    monkeypatch.setitem(sys.modules, "morphosource_api_download", fake_mod)

    out = pilot.download_pair(_fake_pair(), tmp_path / "downloads",
                                no_download=False,
                                specimen_dir=tmp_path / "specimen")
    assert out.get("no_download") is None  # the no_download branch did not fire
    assert len(calls) == 2  # one for CT, one for mesh
