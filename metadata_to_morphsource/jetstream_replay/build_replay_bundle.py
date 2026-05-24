"""Stage everything needed for an offline replay run of the pilot.

For each specimen in a cached-specimens manifest, this CLI:

1. Generates a tiny synthetic CT + mesh + voxelized GT using
   :func:`synthetic.make_fixture_specimen` and lays them out under
   ``<out_dir>/specimens/<slug>/`` exactly the way the orchestrator's
   own caching expects to find them.
2. Writes a recorded HTTP transcript stub at
   ``<out_dir>/sessions/<ct_media_id>.jsonl`` (created from a
   sibling fixture or generated programmatically — see
   :func:`build_minimal_transcript`).

The bundle is everything the pilot needs to run with::

    --cached-specimens <manifest.json>
    --no-download
    --replay-from <out_dir>/sessions

This module is the single source of truth for "what does an offline
replay bundle look like". The smoke test in
``Tests/test_eval_project358382.sh`` uses it; CI uses it; the dev
workflow in ``Makefile`` uses it.

Note on transcripts
-------------------
The committed JSONL fixtures are *recorded* with
:class:`recorder.RecordingSession` against a real Jetstream2 server.
When those don't yet exist, this builder falls back to a stub
transcript that's only good for exercising the replay plumbing — not
for asserting algorithmic correctness. A clear marker in each
generated stub flags it as synthetic so the consumer can decide to
fail loud if it expected a real recording.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .recorder import HTTPCall
from .stage_for_replay import _slug_for


def _resolve_local_mesh(repo_root: Path, manifest_entry: dict) -> Optional[Path]:
    """Return the absolute path to a cached mesh, if one exists locally."""
    rel = manifest_entry.get("_local_mesh_relpath")
    if rel:
        candidate = (
            Path(os.environ.get("AUTORESEARCHCLAW_HOME",
                                Path.home() / ".autoresearchclaw"))
            / "specimens" / rel
        )
        if candidate.exists():
            return candidate
    return None


def stage_synthetic_specimen(
    out_dir: Path,
    *,
    slug: str,
    ct_media_id: str,
    mesh_media_id: str,
    seed: int,
    shape: tuple[int, int, int],
    spacing_mm: tuple[float, float, float],
) -> dict:
    """Drop the synthetic CT/mesh/GT into the orchestrator's per-specimen layout.

    The orchestrator's existing caching skips:
      - ``download_pair``      when the per-specimen dir has both a
        prepared CT (``ct_volume.nii.gz``) and a stub mesh.
      - ``prepare_ct``         when ``ct_volume.nii.gz`` exists.
      - ``crop_and_voxelize``  when ``preprocessing/ct_cropped.nii.gz``
        and ``preprocessing/gt_voxelized.nii.gz`` exist.

    So we lay all four down up-front and the pipeline jumps straight
    to push_volume + bright_seed (which is where the replay layer
    serves its canned responses).
    """
    from .synthetic import make_fixture_specimen

    spec_dir = out_dir / "specimens" / slug
    prep_dir = spec_dir / "preprocessing"
    spec_dir.mkdir(parents=True, exist_ok=True)
    prep_dir.mkdir(parents=True, exist_ok=True)

    bundle = make_fixture_specimen(
        spec_dir,
        slug=slug,
        seed=seed,
        shape=shape,
        spacing_mm=spacing_mm,
        write_gt=True,
    )
    # Mirror download / preprocessing layout so download_pair and crop caches hit.
    dl_root = spec_dir / "downloads"
    ct_dl = dl_root / "ct_download" / f"morphosource_media-id-{ct_media_id}_offline"
    mesh_dl = dl_root / "mesh_download" / f"morphosource_media-id-{mesh_media_id}_offline"
    ct_dl.mkdir(parents=True, exist_ok=True)
    mesh_dl.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(bundle.ct_path, ct_dl / "ct_volume.nii.gz")
    shutil.copy2(bundle.mesh_path, mesh_dl / "mesh.ply")
    aligned_mesh = prep_dir / "mesh_aligned.ply"
    if not aligned_mesh.exists():
        shutil.copy2(bundle.mesh_path, aligned_mesh)
    # The orchestrator reads ct_cropped + voxelized from preprocessing/.
    # Reuse the synthetic CT/GT as both the prepared and the cropped
    # versions — the synthetic volume is already small.
    ct_cropped = prep_dir / "ct_cropped.nii.gz"
    gt_path = prep_dir / "gt_voxelized.nii.gz"
    if not ct_cropped.exists():
        ct_cropped.write_bytes(bundle.ct_path.read_bytes())
    if bundle.gt_label_path and not gt_path.exists():
        gt_path.write_bytes(bundle.gt_label_path.read_bytes())
    return {
        "slug": slug,
        "specimen_dir": str(spec_dir),
        "ct_volume": str(bundle.ct_path),
        "ct_cropped": str(ct_cropped),
        "gt_voxelized": str(gt_path),
        "mesh_path": str(bundle.mesh_path),
        "fingerprint": bundle.to_dict(),
    }


def build_minimal_transcript(
    session_path: Path,
    *,
    slug: str,
    ct_media_id: str,
    base_url: str = "https://jetstream.example",
) -> Path:
    """Programmatically build a stub JSONL transcript.

    This stub does NOT cover the full bright-seed protocol; it just
    contains a handful of representative calls so the
    :class:`ReplaySession` machinery can be exercised end-to-end.
    Real recording from a live Jetstream is preferred.
    """
    session_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()

    def _call(i, method, path, req_text, resp_obj, *, status=200):
        body = json.dumps(resp_obj, ensure_ascii=False)
        return HTTPCall(
            i=i, method=method, url=base_url + path, path=path,
            headers={"Content-Type": "text/plain"} if method == "POST" else {},
            request_text=req_text, request_b64=None,
            request_sha256=hashlib.sha256(
                (req_text or "").encode("utf-8")).hexdigest(),
            status=status,
            response_text=body, response_b64=None,
            response_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            dt_s=0.01,
            match_keys=["method", "path"],
        )

    fake_calls = [
        _call(0, "POST", "/slicer/exec",
              "import slicer\n# CAPTURE_REMOTE_ENV_SRC stub",
              {
                  "status": "ok",
                  "slicer_version": "5.6.2-stub",
                  "torch_version": "2.4.0+cu121",
                  "torch_cuda_available": True,
                  "torch_mps_available": False,
                  "nninteractive_version": "2.0.0",
              }),
        _call(1, "POST", "/slicer/exec",
              "# HASH_ACTIVE_VOLUME_SRC stub",
              {
                  "status": "ok",
                  "sha256_voxels": "0" * 64,
                  "shape_kji": [48, 48, 48],
                  "dtype": "uint16",
                  "spacing_mm": [1.0, 1.0, 1.0],
                  "_dt_s": 0.0,
              }),
        _call(2, "POST", "/slicer/exec",
              "# ENABLE_VISIBILITY_SRC stub",
              {"status": "ok", "nodes": 1, "_dt_s": 0.0}),
    ]
    with session_path.open("w") as fp:
        for c in fake_calls:
            fp.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
    # Mark the file as synthetic via a sidecar so consumers that care
    # about real-vs-synthetic provenance can detect it.
    sidecar = session_path.with_suffix(session_path.suffix + ".meta.json")
    sidecar.write_text(json.dumps({
        "kind": "synthetic_stub",
        "ct_media_id": ct_media_id,
        "slug": slug,
        "created_unix": now,
        "n_calls": len(fake_calls),
        "_doc": (
            "Generated by build_replay_bundle.build_minimal_transcript. "
            "This is a stub; replace with a real recording captured by "
            "RUN_RECORD=1 against Jetstream2 to exercise the full "
            "bright-seed protocol."
        ),
    }, indent=2))
    return session_path


def build_bundle(
    manifest_path: Path,
    out_dir: Path,
    *,
    seed: int = 0,
    shape: tuple[int, int, int] = (48, 48, 48),
    spacing_mm: tuple[float, float, float] = (1.0, 1.0, 1.0),
    use_existing_sessions: Optional[Path] = None,
) -> dict:
    """Stage synthetic specimens + (stub) sessions for the replay tier.

    Returns a manifest dict listing every artefact for downstream
    smoke-test assertions.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = out_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    raw = json.loads(manifest_path.read_text())
    if isinstance(raw, dict):
        rows = raw.get("specimens") or []
    else:
        rows = list(raw)

    out_rows: list[dict] = []
    for entry in rows:
        safe_slug = _slug_for(entry)
        staged = stage_synthetic_specimen(
            out_dir, slug=safe_slug,
            ct_media_id=entry.get("ct_media_id", safe_slug),
            mesh_media_id=entry.get("mesh_media_id", safe_slug),
            seed=seed, shape=shape,
            spacing_mm=spacing_mm,
        )
        ct_media_id = entry.get("ct_media_id", safe_slug)
        sess_path = sessions_dir / f"{ct_media_id}.jsonl"

        # If a real recording exists somewhere, prefer it.
        copied_real = False
        if use_existing_sessions is not None:
            real = Path(use_existing_sessions) / f"{ct_media_id}.jsonl"
            if real.exists() and real.stat().st_size > 0:
                sess_path.write_bytes(real.read_bytes())
                copied_real = True
        if not copied_real:
            build_minimal_transcript(
                sess_path, slug=safe_slug, ct_media_id=ct_media_id,
            )

        out_rows.append({
            **staged,
            "ct_media_id": ct_media_id,
            "session_path": str(sess_path),
            "session_kind": "real" if copied_real else "synthetic_stub",
        })

    bundle_manifest = {
        "manifest_source": str(manifest_path),
        "out_dir": str(out_dir),
        "specimens": out_rows,
        "fixture_seed": seed,
    }
    (out_dir / "bundle.json").write_text(
        json.dumps(bundle_manifest, indent=2)
    )
    return bundle_manifest


def _main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True,
                   help="Cached-specimens manifest (e.g. "
                        "Tests/fixtures/jetstream_replay/cached_specimens.json)")
    p.add_argument("--out-dir", type=Path, required=True,
                   help="Where to stage the bundle. Will create "
                        "<out>/specimens/<slug>/... and <out>/sessions/...")
    p.add_argument("--seed", type=int, default=0,
                   help="Deterministic fixture seed (default 0)")
    p.add_argument("--shape", default="48,48,48",
                   help="Fixture volume shape (default 48,48,48)")
    p.add_argument("--spacing-mm", default="1.0,1.0,1.0",
                   help="Fixture spacing in mm (default 1.0,1.0,1.0)")
    p.add_argument("--use-existing-sessions", type=Path, default=None,
                   help="If set, copy real <ct_media_id>.jsonl recordings "
                        "from this directory in preference to generating "
                        "synthetic stubs.")
    args = p.parse_args(argv)

    shape = tuple(int(s) for s in args.shape.split(","))
    spacing = tuple(float(s) for s in args.spacing_mm.split(","))
    bundle = build_bundle(
        args.manifest, args.out_dir,
        seed=args.seed, shape=shape, spacing_mm=spacing,
        use_existing_sessions=args.use_existing_sessions,
    )
    print(f"staged {len(bundle['specimens'])} specimens into {args.out_dir}")
    for row in bundle["specimens"]:
        print(f"  - {row['slug']}  (session: {row['session_kind']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
