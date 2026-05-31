#!/usr/bin/env python3
"""Stage MorphoSource CT media + queue a bright-seed segmentation job.

Generic, media-id-driven counterpart to ``stage_and_queue_skulls.py`` (which is
project-358382 manifest-driven). Give it one or more CT ``media_id`` values and
it will, for each:

  1. resolve the specimen (physical object id + taxonomy) from the MorphoSource
     API and build a slug,
  2. stage the CT as a GitHub-loadable NRRD via
     ``stage_morphosource_sample.py --full-volume`` (no GT mesh required, so it
     never fails on mesh<->CT alignment),
  3. write a GitHub-URL fixture + a ``jobs/queue/<slug>-<max_steps>.json`` spec,
  4. commit + push CT + fixture + spec to the branch.

The git-driven ECU worker on the Jetstream box picks up each spec and segments
it; the Release Skull Visuals workflow publishes a colored release on
completion. Used both by ``.github/workflows/queue_morphosource_segmentation.yml``
(CI staging) and directly on the box.

Examples::

    # In CI (uses the runner's python):
    GITHUB_TOKEN=... MORPHOSOURCE_API_KEY=... \\
      python .github/scripts/queue_ct_segmentation.py \\
      --ct-media-id 000841848 --ct-media-id 000099373 --max-steps 200

    # On the box (uses the staging venv for heavy deps):
    GITHUB_TOKEN=... MORPHOSOURCE_API_KEY=... \\
      python .github/scripts/queue_ct_segmentation.py --ct-media-id 000841848 \\
      --stage-python /media/volume/MyData/stagevenv/bin/python
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _helpers import safe_first  # noqa: E402
from stage_morphosource_sample import (  # noqa: E402
    MorphoSourceClient, _fetch_metadata,
)
from stage_and_queue_skulls import (  # noqa: E402
    _slug, _write_fixture, _write_spec, _stage_ct, _free_download_cache,
    SAMPLE_DIR, QUEUE_DIR, REPO,
)
from jetstream_harvest_results import commit_and_push  # noqa: E402


def _log(msg: str) -> None:
    print(f"[queue-ct {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _resolve_pair(ct_media_id: str, mesh_media_id: str, taxon_override: str,
                  pid_override: str) -> dict:
    """Resolve {physical_object_id, taxonomy, ct_media_id, mesh_media_id}."""
    pid = pid_override
    taxon = taxon_override
    if not (pid and taxon):
        client = MorphoSourceClient()
        meta = _fetch_metadata(client, ct_media_id)
        vis = safe_first(meta.get("visibility")).lower()
        if vis != "open":
            raise SystemExit(
                f"CT media {ct_media_id} visibility is {vis!r} -- must be 'open'")
        pid = pid or safe_first(meta.get("physical_object_id")) or ct_media_id
        taxon = taxon or safe_first(meta.get("physical_object_taxonomy_name")) \
            or "specimen"
    return {
        "physical_object_id": str(pid),
        "taxonomy": str(taxon),
        "ct_media_id": str(ct_media_id),
        "mesh_media_id": str(mesh_media_id or ""),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ct-media-id", action="append", default=[], required=True,
                   help="CT media id to stage+queue (repeatable)")
    p.add_argument("--mesh-media-id", default="",
                   help="optional GT mesh media id (recorded in the fixture; "
                        "only valid with a single --ct-media-id)")
    p.add_argument("--taxon", default="",
                   help="override taxonomy (else resolved from the API)")
    p.add_argument("--physical-object-id", default="",
                   help="override physical object id (else from the API)")
    p.add_argument("--max-steps", type=int, default=200)
    p.add_argument("--max-axis", type=int, default=384)
    p.add_argument("--stage-python", default=sys.executable,
                   help="python used to run stage_morphosource_sample.py "
                        "(default: this interpreter)")
    p.add_argument("--branch", default="main")
    p.add_argument("--no-push", action="store_true")
    args = p.parse_args(argv)

    if not os.environ.get("GITHUB_TOKEN") and not args.no_push:
        sys.exit("ERROR: GITHUB_TOKEN not set (needed to push). Use --no-push.")
    if not os.environ.get("MORPHOSOURCE_API_KEY"):
        sys.exit("ERROR: MORPHOSOURCE_API_KEY not set (needed to download).")
    os.environ.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
    if args.mesh_media_id and len(args.ct_media_id) != 1:
        sys.exit("ERROR: --mesh-media-id only valid with one --ct-media-id")

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    queued, failed = [], []
    for i, ct in enumerate(args.ct_media_id):
        if i > 0:
            time.sleep(20)  # be gentle on the MorphoSource API
        ct = ct.strip()
        if not ct:
            continue
        _log(f"resolving CT media {ct} …")
        try:
            pair = _resolve_pair(ct, args.mesh_media_id, args.taxon,
                                 args.physical_object_id)
        except SystemExit as e:
            _log(f"resolve failed for {ct}: {e}")
            failed.append(ct)
            continue
        slug = _slug(pair)
        job_id = f"{slug}-{args.max_steps}"
        if (QUEUE_DIR / f"{job_id}.json").exists() and \
                (SAMPLE_DIR / f"{slug}_ct.nrrd").exists():
            _log(f"{job_id}: already staged + queued; skipping")
            continue

        _log(f"staging {slug} (ct={ct}, taxon={pair['taxonomy']}) …")
        status = _stage_ct(pair, slug, args.stage_python, args.max_axis)
        if status != "ok":
            _log(f"staging {status} for {slug}; skipping")
            _free_download_cache(pair)
            failed.append(ct)
            continue

        fx = _write_fixture(pair, slug)
        _, spec = _write_spec(slug, args.max_steps)
        ctf = SAMPLE_DIR / f"{slug}_ct.nrrd"
        prov = SAMPLE_DIR / f"{slug}.provenance.json"
        paths = [str(pp.relative_to(REPO)) for pp in (ctf, prov, fx, spec)
                 if pp.exists()]
        msg = (f"queue {job_id}: stage CT + fixture + job spec "
               f"({pair['taxonomy']}, ct={ct})")
        _log(f"committing {paths}")
        pushed = False
        for attempt in range(1, 4):
            try:
                commit_and_push(paths, msg, branch=args.branch,
                                push=not args.no_push)
                pushed = True
                break
            except Exception as e:  # noqa: BLE001 - retry any git/network error
                _log(f"push attempt {attempt}/3 failed for {job_id}: {e}")
                time.sleep(10)
        _free_download_cache(pair)
        (queued if pushed else failed).append(job_id)

    _log(f"done: queued={queued} failed={failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
