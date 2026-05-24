"""Unit tests for the cache_index walker.

These tests build a tiny synthetic ``~/.autoresearchclaw/specimens/``
tree under ``tmp_path`` and run :func:`scan_specimens` /
:func:`filter_skull_meshes` / :func:`write_manifest` against it. No
real cache, network, or heavy deps required.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# Make the package importable when running pytest from the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from metadata_to_morphsource.jetstream_replay.cache_index import (  # noqa: E402
    CachedSpecimen,
    filter_skull_meshes,
    scan_specimen_dir,
    scan_specimens,
    write_manifest,
)


def _make_media_dir(
    root: Path,
    media_id: str,
    *,
    mesh_name: str | None = None,
    inner_zip: bool = False,
    has_zip: bool = True,
    has_analysis: bool = True,
) -> Path:
    """Build one ``media_<id>/morphosource_.../<file>`` tree."""
    media = root / f"media_{media_id}"
    inner = media / f"morphosource_media-id-{media_id}_download-deadbeef"
    inner.mkdir(parents=True, exist_ok=True)
    if mesh_name:
        # nest under a "Media XXX - <element> Mesh Etc" subdir to mimic
        # the real cache layout.
        wrap = inner / f"Media {media_id} - {mesh_name.split('.')[0]} Mesh Etc"
        wrap.mkdir(parents=True, exist_ok=True)
        (wrap / mesh_name).write_text("ply ascii  fake mesh\n")
    if inner_zip:
        (inner / "extra.zip").write_bytes(b"PK\x03\x04stub")
    if has_zip:
        (media / f"morphosource_media-id-{media_id}_download-deadbeef.zip").write_bytes(b"PK\x03\x04")
    if has_analysis:
        analysis = media / "analysis"
        analysis.mkdir(parents=True, exist_ok=True)
        (analysis / "analysis.json").write_text(json.dumps({
            "media_id": media_id,
            "vertices": 1234,
            "distances": {
                "skull_length": 5.0,
                "skull_height": 4.0,
                "skull_width": 6.0,
            },
        }))
    return media


# ---------------------------------------------------------------------------
# scan_specimen_dir
# ---------------------------------------------------------------------------


def test_scan_specimen_dir_returns_none_for_unrelated_dirs(tmp_path):
    other = tmp_path / "not_a_media_dir"
    other.mkdir()
    assert scan_specimen_dir(other) is None


def test_scan_specimen_dir_picks_up_mesh(tmp_path):
    _make_media_dir(tmp_path, "000123456",
                    mesh_name="Pongo_skull-000123456.ply")
    spec = scan_specimen_dir(tmp_path / "media_000123456")
    assert spec is not None
    assert spec.media_id == "000123456"
    assert spec.kind == "mesh"
    assert spec.has_zip is True
    assert spec.has_extracted is True
    assert spec.primary_mesh is not None
    assert spec.primary_mesh.name == "Pongo_skull-000123456.ply"
    assert spec.analysis is not None
    assert spec.bytes_on_disk > 0


def test_scan_specimen_dir_no_zip_no_analysis(tmp_path):
    _make_media_dir(tmp_path, "000999111",
                    mesh_name="Foo-000999111.stl",
                    has_zip=False, has_analysis=False)
    spec = scan_specimen_dir(tmp_path / "media_000999111")
    assert spec is not None
    assert spec.has_zip is False
    assert spec.analysis is None
    assert spec.kind == "mesh"


def test_taxonomy_hint_skips_institution_codes(tmp_path):
    _make_media_dir(tmp_path, "000111222",
                    mesh_name="USNM_276657_Ateles_humR_smooth-000111222.ply")
    spec = scan_specimen_dir(tmp_path / "media_000111222")
    assert spec is not None
    # The hint should be the genus, not "USNM" (institution).
    assert spec.taxonomy_hint == "Ateles"


# ---------------------------------------------------------------------------
# scan_specimens (top-level)
# ---------------------------------------------------------------------------


def test_scan_specimens_sorted_and_skips_non_media(tmp_path):
    _make_media_dir(tmp_path, "000222333", mesh_name="x-000222333.ply")
    _make_media_dir(tmp_path, "000111222", mesh_name="y-000111222.ply")
    (tmp_path / "logs").mkdir()  # not a media_ dir, must be skipped
    (tmp_path / "stray.txt").write_text("noise")
    out = scan_specimens(tmp_path)
    assert [s.media_id for s in out] == ["000111222", "000222333"]


def test_scan_specimens_handles_missing_root(tmp_path):
    assert scan_specimens(tmp_path / "does_not_exist") == []


# ---------------------------------------------------------------------------
# filter_skull_meshes
# ---------------------------------------------------------------------------


def test_filter_skull_includes_skull_and_cranium(tmp_path):
    _make_media_dir(tmp_path, "000000001",
                    mesh_name="Macaca-MCZ-cranium-000000001.ply")
    _make_media_dir(tmp_path, "000000002",
                    mesh_name="Tarsius_tarsier_skull-000000002.ply")
    _make_media_dir(tmp_path, "000000003",
                    mesh_name="LemurFemur_smooth-000000003.ply")
    specs = scan_specimens(tmp_path)
    skulls = filter_skull_meshes(specs)
    assert {s.media_id for s in skulls} == {"000000001", "000000002"}


def test_filter_skull_rejects_postcranials(tmp_path):
    """`Postcranials` contains the substring `crani` — must NOT match."""
    _make_media_dir(tmp_path, "000000010",
                    mesh_name="MiscPostcranial-000000010.ply")
    specs = scan_specimens(tmp_path)
    assert filter_skull_meshes(specs) == []


# ---------------------------------------------------------------------------
# write_manifest
# ---------------------------------------------------------------------------


def test_write_manifest_round_trip(tmp_path):
    _make_media_dir(tmp_path, "000000077",
                    mesh_name="Saimiri-cranium-000000077.ply")
    specs = scan_specimens(tmp_path)
    out = write_manifest(tmp_path / "manifest.json", specs)
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["ct_provider"] == "synthetic"
    assert isinstance(data["specimens"], list)
    assert len(data["specimens"]) == 1
    row = data["specimens"][0]
    assert row["mesh_media_id"] == "000000077"
    assert row["ct_media_id"].startswith("synthetic_ct_for_")
    assert row["project_query"] == "Colors of Skull Anatomy"
    # SpecimenPair-required keys
    for k in ("physical_object_id", "physical_object_title", "taxonomy",
              "ct_media_id", "ct_title", "ct_media_type", "ct_file_size",
              "mesh_media_id", "mesh_title", "mesh_media_type",
              "mesh_file_size"):
        assert k in row, f"missing required SpecimenPair key {k!r}"


def test_committed_fixture_is_well_formed():
    """Sanity-check ``Tests/fixtures/jetstream_replay/cached_specimens.json``."""
    fix = (
        Path(__file__).resolve().parent
        / "fixtures" / "jetstream_replay" / "cached_specimens.json"
    )
    assert fix.exists()
    data = json.loads(fix.read_text())
    assert data["ct_provider"] == "synthetic"
    assert isinstance(data["specimens"], list)
    assert len(data["specimens"]) >= 3, "fixture must commit ≥3 specimens"
    seen_ct_ids: set[str] = set()
    seen_mesh_ids: set[str] = set()
    for row in data["specimens"]:
        for k in ("physical_object_id", "ct_media_id", "mesh_media_id",
                  "taxonomy", "_local_mesh_relpath", "_session_fixture"):
            assert k in row, f"row missing {k!r}: {row}"
        # IDs must be unique within the manifest.
        assert row["ct_media_id"] not in seen_ct_ids
        assert row["mesh_media_id"] not in seen_mesh_ids
        seen_ct_ids.add(row["ct_media_id"])
        seen_mesh_ids.add(row["mesh_media_id"])
        # Session fixture must point under sessions/ to keep the
        # checked-in tree predictable.
        assert row["_session_fixture"].startswith("sessions/")
