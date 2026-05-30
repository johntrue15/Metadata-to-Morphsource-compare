#!/usr/bin/env python3
"""Batch-stage Colors-of-Skull CTs and queue 200-cap segmentation jobs.

For each eligible specimen in the project manifest that is not already done,
this script (run on the Jetstream box):

  1. stages the CT as a GitHub-loadable NRRD via
     ``stage_morphosource_sample.py --full-volume`` (no GT mesh, so it never
     fails on mesh<->CT bbox alignment),
  2. writes a GitHub-URL fixture + a ``jobs/queue/<slug>-200.json`` spec,
  3. commits + pushes CT + fixture + spec to ``main``,
  4. deletes the (multi-GB) download cache to free disk,

then moves on. The poll-mode ECU worker picks up each spec as it lands, and the
Release Skull Visuals workflow publishes a release on completion. Pipelining
this way means skull N segments while skull N+1 downloads.

Run on the box::

    GITHUB_TOKEN=... MORPHOSOURCE_API_KEY=... \
      /media/volume/MyData/stagevenv/bin/python \
      .github/scripts/stage_and_queue_skulls.py --max-ct-gb 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from jetstream_harvest_results import commit_and_push  # noqa: E402

DEFAULT_MANIFEST = REPO / "Tests" / "fixtures" / "nninteractive_compare" / "_manifest_358382.json"
SAMPLE_DIR = REPO / "data" / "sample"
QUEUE_DIR = REPO / "jobs" / "queue"
STATUS_DIR = REPO / "jobs" / "status"
DOWNLOAD_ROOT = REPO / "data"


def _log(msg: str) -> None:
    print(f"[stage-queue {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _slug(pair: dict) -> str:
    genus = re.sub(r"[^a-z0-9]", "", (pair.get("taxonomy") or "taxon").split()[0].lower())
    return f"{genus}_skull_{pair['physical_object_id']}"


def _already_done(slug: str) -> bool:
    """True if this specimen already has a staged CT or a completed job."""
    if (SAMPLE_DIR / f"{slug}_ct.nrrd").exists():
        return True
    return False


def _write_fixture(pair: dict, slug: str) -> Path:
    path = SAMPLE_DIR / f"{slug}_urls.json"
    fixture = {
        "_doc": [
            "GitHub raw URL fixture for Colors of Skull Anatomy (project 358382).",
            f"data/sample/{slug}_ct.nrrd must be on the default branch.",
            "Staged with stage_morphosource_sample.py --full-volume (no GT mesh).",
        ],
        "github_repo": "johntrue15/MorphoClaw",
        "github_ref": "main",
        "slug": slug,
        "project_id": "000358382",
        "project_query": "Colors of Skull Anatomy",
        "physical_object_id": pair["physical_object_id"],
        "taxonomy": pair.get("taxonomy", ""),
        "ct_media_id": pair["ct_media_id"],
        "mesh_media_id": pair.get("mesh_media_id", ""),
        "files": {
            "ct_nrrd": f"data/sample/{slug}_ct.nrrd",
            "provenance_json": f"data/sample/{slug}.provenance.json",
        },
        "slicer_volume_name": f"{slug}_ct",
        "pilot_defaults": {"budgets": [10, 25, 50, 100], "max_steps": 200,
                           "intensity_percentile": 99.0},
    }
    path.write_text(json.dumps(fixture, indent=2) + "\n")
    return path


def _write_spec(slug: str, max_steps: int) -> tuple[str, Path]:
    job_id = f"{slug}-{max_steps}"
    path = QUEUE_DIR / f"{job_id}.json"
    spec = {
        "id": job_id,
        "fixture": f"data/sample/{slug}_urls.json",
        "max_steps": max_steps,
        "headless": True,
        "fast": True,
        "batch_size": 8,
        "_doc": f"Colors-of-Skull {slug}: clear scene -> load CT -> bright-seed "
                f"to {max_steps} clicks or saturation, headless+batched, "
                f"checkpointing to GitHub.",
    }
    path.write_text(json.dumps(spec, indent=2) + "\n")
    return job_id, path


def _stage_ct(pair: dict, slug: str, stage_python: str, max_axis: int) -> bool:
    cmd = [
        stage_python, "-u",
        str(SCRIPT_DIR / "stage_morphosource_sample.py"),
        "--phase", "ct-only", "--full-volume",
        "--ct-media-id", pair["ct_media_id"],
        "--slug", slug,
        "--max-axis", str(max_axis),
        "--download-root", str(DOWNLOAD_ROOT),
    ]
    _log(f"staging CT for {slug} (ct={pair['ct_media_id']}) …")
    proc = subprocess.run(cmd, cwd=str(REPO))
    ok = proc.returncode == 0 and (SAMPLE_DIR / f"{slug}_ct.nrrd").exists()
    if not ok:
        _log(f"FAILED staging {slug} (rc={proc.returncode})")
    return ok


def _free_download_cache(pair: dict) -> None:
    for mid in (pair.get("ct_media_id"), pair.get("mesh_media_id")):
        if not mid:
            continue
        d = DOWNLOAD_ROOT / f"morphosource-download-{mid}"
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            _log(f"freed cache {d.name}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--stage-python",
                   default="/media/volume/MyData/stagevenv/bin/python")
    p.add_argument("--max-ct-gb", type=float, default=5.0,
                   help="skip specimens whose raw CT exceeds this size")
    p.add_argument("--max-axis", type=int, default=384)
    p.add_argument("--max-steps", type=int, default=200)
    p.add_argument("--skip-ids", default="",
                   help="comma-separated physical_object_ids to skip")
    p.add_argument("--only-ids", default="",
                   help="comma-separated physical_object_ids to restrict to")
    p.add_argument("--limit", type=int, default=0,
                   help="stop after staging N specimens (0 = all)")
    p.add_argument("--branch", default="main")
    p.add_argument("--no-push", action="store_true")
    args = p.parse_args(argv)

    if not os.environ.get("GITHUB_TOKEN") and not args.no_push:
        sys.exit("ERROR: GITHUB_TOKEN not set (needed to push). Use --no-push to skip.")
    os.environ.setdefault("GIT_LFS_SKIP_SMUDGE", "1")

    manifest = json.loads(Path(args.manifest).read_text())
    pairs = manifest.get("pairs", [])
    skip = {s.strip() for s in args.skip_ids.split(",") if s.strip()}
    only = {s.strip() for s in args.only_ids.split(",") if s.strip()}

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    staged = 0
    for pair in pairs:
        pid = pair.get("physical_object_id", "")
        if only and pid not in only:
            continue
        if pid in skip:
            continue
        if not pair.get("eligible", False):
            _log(f"skip {pid} {pair.get('taxonomy')}: not eligible "
                 f"({pair.get('skip_reason', 'n/a')})")
            continue
        if (pair.get("ct_file_size") or 0) > args.max_ct_gb * 1e9:
            _log(f"skip {pid} {pair.get('taxonomy')}: CT "
                 f"{pair.get('ct_file_size')/1e9:.1f} GB > {args.max_ct_gb} GB")
            continue

        slug = _slug(pair)
        if _already_done(slug):
            _log(f"skip {slug}: CT already staged")
            continue

        if not _stage_ct(pair, slug, args.stage_python, args.max_axis):
            _free_download_cache(pair)
            continue

        fx = _write_fixture(pair, slug)
        job_id, spec = _write_spec(slug, args.max_steps)
        ct = SAMPLE_DIR / f"{slug}_ct.nrrd"
        prov = SAMPLE_DIR / f"{slug}.provenance.json"
        paths = [str(pp.relative_to(REPO)) for pp in (ct, prov, fx, spec)
                 if pp.exists()]
        _log(f"queued {job_id}; committing {paths}")
        msg = (f"queue {job_id}: stage CT + fixture + job spec "
               f"({pair.get('taxonomy')})")
        for attempt in range(1, 4):  # retry: the ECU worker may commit concurrently
            try:
                commit_and_push(paths, msg, branch=args.branch,
                                push=not args.no_push)
                break
            except SystemExit as e:
                _log(f"push attempt {attempt}/3 failed for {job_id}: {e}")
                time.sleep(10)
        _free_download_cache(pair)
        staged += 1
        if args.limit and staged >= args.limit:
            _log(f"reached --limit {args.limit}; stopping")
            break

    _log(f"done: staged+queued {staged} specimen(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
