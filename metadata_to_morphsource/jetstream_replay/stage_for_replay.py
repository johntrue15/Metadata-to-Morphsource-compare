"""Stage synthetic specimens for the offline replay tier.

Given a curated ``cached_specimens.json`` manifest (the
``Tests/fixtures/jetstream_replay/cached_specimens.json`` format),
this helper builds a per-specimen on-disk layout that the
``eval_project358382_pilot.py`` orchestrator can run against in
``--no-download --cached-specimens --replay-from`` mode without
hitting MorphoSource or Jetstream.

Per-specimen layout produced under ``<out_dir>/specimens/<slug>/``::

    ct_volume.nii.gz                              # synthetic CT
    mesh.ply                                      # synthetic sphere mesh
    downloads/
      ct_download/
        morphosource_media-id-<ctid>_offline/
          ct_volume.nii.gz                        # copy for find_ct_volume
      mesh_download/
        morphosource_media-id-<meshid>_offline/
          mesh.ply                                # copy for find_mesh
    preprocessing/
      ct_cropped.nii.gz                           # = ct_volume (already small)
      gt_voxelized.nii.gz                         # synthetic GT
      mesh_aligned.ply                            # = mesh

This wins us cache-hits on:

- ``download_pair`` (sees the offline extract dirs and ``--no-download``
  honours them)
- ``prepare_ct`` (the per-specimen ``ct_volume.nii.gz`` already exists)
- ``crop_and_voxelize`` (cropped + voxelised + aligned-mesh all
  pre-staged)

so the only remaining work is the bright-seed loop plus pre-step
calls (volume-push, hash, capture-env, etc.) — exactly the bit we
want to exercise via the recorded JSONL.

Usage::

    python -m metadata_to_morphsource.jetstream_replay.stage_for_replay \\
        --manifest Tests/fixtures/jetstream_replay/cached_specimens.json \\
        --out-dir runs/replay_smoke
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .synthetic import FixtureBundle, make_fixture_specimen


@dataclass
class StagedSpecimen:
    slug: str
    ct_media_id: str
    mesh_media_id: str
    specimen_dir: Path
    ct_volume_path: Path
    mesh_path: Path
    cropped_path: Path
    voxelized_path: Path

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "ct_media_id": self.ct_media_id,
            "mesh_media_id": self.mesh_media_id,
            "specimen_dir": str(self.specimen_dir),
            "ct_volume_path": str(self.ct_volume_path),
            "mesh_path": str(self.mesh_path),
            "cropped_path": str(self.cropped_path),
            "voxelized_path": str(self.voxelized_path),
        }


def _slug_for(entry: dict) -> str:
    """Mirror SpecimenPair.slug without depending on the heavy module."""
    physical = entry["physical_object_id"]
    taxon = (entry.get("taxonomy") or "specimen").strip().split()[0]
    safe = "".join(c if c.isalnum() else "_" for c in (taxon or "specimen"))
    return f"{physical}__{safe}__{entry['ct_media_id']}__{entry['mesh_media_id']}"


def stage_specimen(
    entry: dict,
    out_root: Path,
    *,
    seed: int = 0,
    shape: tuple[int, int, int] = (48, 48, 48),
    spacing_mm: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> StagedSpecimen:
    """Build the per-specimen tree for one manifest entry."""
    slug = _slug_for(entry)
    specimen_dir = out_root / "specimens" / slug
    specimen_dir.mkdir(parents=True, exist_ok=True)

    bundle = make_fixture_specimen(
        specimen_dir,
        slug=slug,
        seed=seed,
        shape=shape,
        spacing_mm=spacing_mm,
    )

    # Mirror the synthetic outputs into the orchestrator's expected
    # download / preprocessing layout so its caches hit.
    dl_root = specimen_dir / "downloads"
    ct_dl = dl_root / "ct_download" / f"morphosource_media-id-{entry['ct_media_id']}_offline"
    mesh_dl = dl_root / "mesh_download" / f"morphosource_media-id-{entry['mesh_media_id']}_offline"
    ct_dl.mkdir(parents=True, exist_ok=True)
    mesh_dl.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle.ct_path, ct_dl / "ct_volume.nii.gz")
    shutil.copy2(bundle.mesh_path, mesh_dl / "mesh.ply")

    pre = specimen_dir / "preprocessing"
    pre.mkdir(parents=True, exist_ok=True)
    cropped = pre / "ct_cropped.nii.gz"
    voxelized = pre / "gt_voxelized.nii.gz"
    aligned_mesh = pre / "mesh_aligned.ply"
    shutil.copy2(bundle.ct_path, cropped)
    if bundle.gt_label_path is not None:
        shutil.copy2(bundle.gt_label_path, voxelized)
    shutil.copy2(bundle.mesh_path, aligned_mesh)

    return StagedSpecimen(
        slug=slug,
        ct_media_id=entry["ct_media_id"],
        mesh_media_id=entry["mesh_media_id"],
        specimen_dir=specimen_dir,
        ct_volume_path=bundle.ct_path,
        mesh_path=bundle.mesh_path,
        cropped_path=cropped,
        voxelized_path=voxelized,
    )


def stage_manifest(
    manifest_path: Path,
    out_root: Path,
    *,
    seed: int = 0,
    shape: Optional[tuple[int, int, int]] = None,
    spacing_mm: Optional[tuple[float, float, float]] = None,
) -> list[StagedSpecimen]:
    """Stage every specimen in a manifest. Returns the list of staged dirs."""
    raw = json.loads(Path(manifest_path).read_text())
    if isinstance(raw, dict):
        entries = raw.get("specimens", [])
        manifest_seed = int(raw.get("fixture_seed", seed))
        manifest_shape = tuple(raw.get("fixture_shape_ijk", [48, 48, 48]))
        manifest_spacing = tuple(raw.get("fixture_spacing_mm", [1.0, 1.0, 1.0]))
    else:
        entries = list(raw)
        manifest_seed = seed
        manifest_shape = (48, 48, 48)
        manifest_spacing = (1.0, 1.0, 1.0)

    if shape is None:
        shape = manifest_shape  # type: ignore[assignment]
    if spacing_mm is None:
        spacing_mm = manifest_spacing  # type: ignore[assignment]

    staged: list[StagedSpecimen] = []
    for i, entry in enumerate(entries):
        staged.append(stage_specimen(
            entry, out_root,
            seed=manifest_seed + i,
            shape=shape,
            spacing_mm=spacing_mm,
        ))
    return staged


def _main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    staged = stage_manifest(args.manifest, args.out_dir, seed=args.seed)
    summary = {
        "manifest": str(args.manifest),
        "out_dir": str(args.out_dir),
        "n_staged": len(staged),
        "specimens": [s.to_dict() for s in staged],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
