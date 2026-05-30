#!/usr/bin/env python3
"""Harvest a git-friendly subset of ``runs/`` into ``results/`` and push.

Runs ON the Jetstream box (cwd = the ECU repo checkout, default
``/media/volume/MyData/MorphoClaw``). ``runs/`` is gitignored and ~tens of
GB (per-segment NIfTI exports + raw CT inputs), so we do NOT commit it
wholesale. Instead, for each named run we copy:

  * the small scientific record (``.json``/``.csv``/``.md``/``.txt``/
    ``.jsonl``/``.sh``/``.png``/``.yaml``) up to ``--max-small-mb``, and
  * final composite labelmaps (``.nii.gz``/``.nrrd``) up to ``--max-lfs-mb``,
    tracked via **Git LFS** (per-segment dirs and raw ``ct_*`` inputs are
    skipped),

into ``results/<run_id>/`` (mirroring the run's layout), then commit and
push to ``origin`` using ``GITHUB_TOKEN`` (token-in-URL; never written to
disk or logged).

Examples (run via the controller)::

    jetstream_controller.py run --env GITHUB_TOKEN=ghp_xxx --wait -- \\
        python3 .github/scripts/jetstream_harvest_results.py \\
        --runs tuatara_fresh_proof2 chameleon_skull_000769445_ecu_20260529T092515 \\
        --commit --push
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# .github/scripts/<this> -> repo root is two parents up.
REPO = Path(__file__).resolve().parents[2]
RUNS = REPO / "runs"
RESULTS = REPO / "results"
REMOTE_SLUG = "johntrue15/MorphoClaw"

# Small text/record files worth committing directly.
CURATED_EXT = {".json", ".csv", ".md", ".txt", ".jsonl", ".sh", ".yaml", ".yml", ".png"}
# Volumetric masks worth keeping — tracked via Git LFS.
LFS_EXT = {".nii.gz", ".nrrd"}
# Never copy these (huge intermediates / redundant inputs).
SKIP_DIR_PARTS = {"per_segment"}
SKIP_NAME_PREFIXES = ("ct_",)  # raw CT inputs (big, already in data/sample or MorphoSource)
# Per-step debug dirs (step_00/..) hold ~5 screenshots + 1 json each; the full
# per-click history is already captured in bright_seed/events.jsonl, so we skip
# them by default to keep git lean. Use --include-steps to override.
SKIP_DIR_PREFIXES = ("step_",)


def _ext(p: Path) -> str:
    """Return the logical suffix, treating ``.nii.gz`` as one extension."""
    if p.name.endswith(".nii.gz"):
        return ".nii.gz"
    return p.suffix.lower()


def _log(msg: str) -> None:
    print(msg, flush=True)


def harvest_run(run_dir: Path, max_small_mb: float, max_lfs_mb: float,
                dry_run: bool, include_steps: bool = False) -> dict:
    out = RESULTS / run_dir.name
    rec = {"run": run_dir.name, "small": [], "lfs": [], "skipped_big": [],
           "skipped_path": []}
    for f in sorted(run_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(run_dir)
        skip_step = (not include_steps and
                     any(part.startswith(SKIP_DIR_PREFIXES) for part in rel.parts))
        if any(part in SKIP_DIR_PARTS for part in rel.parts) or \
                f.name.startswith(SKIP_NAME_PREFIXES) or skip_step:
            rec["skipped_path"].append(str(rel))
            continue
        ext = _ext(f)
        size = f.stat().st_size
        dest = out / rel
        if ext in CURATED_EXT and size <= max_small_mb * 1e6:
            bucket = "small"
        elif ext in LFS_EXT and size <= max_lfs_mb * 1e6:
            bucket = "lfs"
        else:
            rec["skipped_big"].append(f"{rel} ({size/1e6:.1f} MB)")
            continue
        rec[bucket].append(str(rel))
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)
        (out / "HARVEST.json").write_text(json.dumps(
            {**{k: (v if k == "run" else len(v)) for k, v in rec.items()},
             "harvested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "detail": rec}, indent=2))
    _log(f"  {run_dir.name}: small={len(rec['small'])} "
         f"lfs={len(rec['lfs'])} skipped_big={len(rec['skipped_big'])} "
         f"skipped_path={len(rec['skipped_path'])}")
    return rec


def ensure_gitattributes() -> bool:
    ga = REPO / ".gitattributes"
    needed = [
        "results/**/*.nrrd filter=lfs diff=lfs merge=lfs -text",
        "results/**/*.nii.gz filter=lfs diff=lfs merge=lfs -text",
    ]
    existing = ga.read_text().splitlines() if ga.exists() else []
    changed = False
    for line in needed:
        if line not in existing:
            existing.append(line)
            changed = True
    if changed:
        ga.write_text("\n".join(existing).rstrip("\n") + "\n")
    return changed


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=REPO, text=True,
                          capture_output=True)
    if proc.stdout.strip():
        _log(proc.stdout.rstrip())
    if proc.stderr.strip():
        _log(proc.stderr.rstrip())
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed (rc={proc.returncode})")
    return proc


def _token_url() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("ERROR: --push requires GITHUB_TOKEN in the env "
                         "(pass via controller --env GITHUB_TOKEN=...)")
    return f"https://x-access-token:{token}@github.com/{REMOTE_SLUG}.git"


# Stable bot identity so commits / rebase never abort on a fresh box checkout.
_IDENTITY = ["-c", "user.name=MorphoClaw ECU",
             "-c", "user.email=ecu@users.noreply.github.com"]


def commit_and_push(paths: list[str], message: str, branch: str = "main",
                    push: bool = True) -> bool:
    """Stage ``paths``, commit, optionally rebase-pull + push to origin.

    Shared by the harvester and the ECU worker so both use identical Git LFS /
    token-in-URL / autostash semantics. Returns True if a commit was created.
    The token is read from ``GITHUB_TOKEN`` and scrubbed from logs.
    """
    git("lfs", "install", "--local", check=False)
    ensure_gitattributes()
    git("add", ".gitattributes")
    git("add", "--", *paths)
    status = git("status", "--porcelain", "--", *paths, ".gitattributes",
                 check=False)
    if not status.stdout.strip():
        _log("Nothing new to commit.")
        return False
    git(*_IDENTITY, "commit", "-m", message)
    if not push:
        _log("Committed locally (no push).")
        return True
    git("pull", "--rebase", "--autostash", "origin", branch, check=False)
    url = _token_url()
    proc = subprocess.run(["git", "push", url, f"HEAD:{branch}"],
                          cwd=REPO, text=True, capture_output=True)
    scrub = lambda s: s.replace(os.environ.get("GITHUB_TOKEN", "\0"), "***")
    if proc.stdout.strip():
        _log(scrub(proc.stdout.rstrip()))
    if proc.stderr.strip():
        _log(scrub(proc.stderr.rstrip()))
    if proc.returncode != 0:
        raise SystemExit(f"git push failed (rc={proc.returncode})")
    _log(f"Pushed to origin/{branch}.")
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", nargs="+", required=True,
                   help="run directory names under runs/ (e.g. tuatara_fresh_proof2)")
    p.add_argument("--max-small-mb", type=float, default=25.0,
                   help="max size for committed text/record files (default 25)")
    p.add_argument("--max-lfs-mb", type=float, default=200.0,
                   help="max size for LFS-tracked composite labelmaps (default 200)")
    p.add_argument("--commit", action="store_true", help="git add + commit results/")
    p.add_argument("--push", action="store_true",
                   help="push to origin/<branch> using GITHUB_TOKEN (implies --commit)")
    p.add_argument("--branch", default="main")
    p.add_argument("--message", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be copied; copy/commit nothing")
    p.add_argument("--include-steps", action="store_true",
                   help="also copy per-step debug dirs (step_*/ screenshots+json)")
    args = p.parse_args(argv)

    if args.push:
        args.commit = True

    missing = [r for r in args.runs if not (RUNS / r).is_dir()]
    if missing:
        raise SystemExit(f"ERROR: run dir(s) not found under {RUNS}: {missing}")

    _log(f"Harvesting {len(args.runs)} run(s) into {RESULTS} "
         f"(small<= {args.max_small_mb} MB, lfs<= {args.max_lfs_mb} MB)"
         + (" [DRY RUN]" if args.dry_run else ""))
    records = [harvest_run(RUNS / r, args.max_small_mb, args.max_lfs_mb,
                           args.dry_run, args.include_steps) for r in args.runs]
    n_small = sum(len(r["small"]) for r in records)
    n_lfs = sum(len(r["lfs"]) for r in records)
    _log(f"Totals: {n_small} record files, {n_lfs} LFS masks.")

    if args.dry_run:
        return 0

    if not args.commit:
        _log("Done (no --commit). Files staged under results/ on disk.")
        return 0

    msg = args.message or (
        "harvest: " + ", ".join(args.runs)[:120]
        + f" ({n_small} records, {n_lfs} masks)")
    commit_and_push([f"results/{r}" for r in args.runs], msg,
                    branch=args.branch, push=args.push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
