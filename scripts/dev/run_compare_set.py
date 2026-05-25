#!/usr/bin/env python3
"""Iterate the nnInteractive comparison workflow over a manifest of
(CT, mesh) pairs, one job at a time, caching the resulting fixture
bundles back into the repo so each pair becomes a reusable PR-smoke
test target.

Designed for the project 000358382 "Colors of Skull Anatomy" batch
(``Tests/fixtures/nninteractive_compare/_manifest_358382.json``), but
works against any manifest that lists ``ct_media_id`` / ``mesh_media_id``
/ ``slug`` per pair.

Flow per pair:
    1. Skip if ``Tests/fixtures/nninteractive_compare/<slug>/baseline_metrics.json``
       already exists locally (resumable).
    2. Dispatch ``.github/workflows/nninteractive_compare.yml`` on the
       self-hosted runner with the pair's media IDs and the size-control
       flags (crop_around_mesh_mm + max_voxel_axis).
    3. Poll the run until it completes (~30-60 min/pair due to MorphoSource
       download throughput).
    4. Download the ``nninteractive-fixtures`` artifact and stage it under
       ``Tests/fixtures/nninteractive_compare/<slug>/``.
    5. ``git add`` + ``git commit`` (with ``[skip ci]`` so the noisy stale
       Run Tests workflow doesn't fire) and ``git push``.
    6. Append a row to ``runs/skull_batch_358382/progress.csv``.

Resumability: the script is idempotent. Killing it mid-batch and re-running
picks up exactly where it left off, skipping any pair whose fixture is
already on disk.

The orchestrator runs LOCALLY (on the operator's workstation); each compare
itself runs on the GitHub Actions self-hosted runner. The script needs
nothing more than the ``gh`` CLI and ``git``.

Usage::

    python scripts/dev/run_compare_set.py \\
        --manifest Tests/fixtures/nninteractive_compare/_manifest_358382.json \\
        --crop-around-mesh-mm 5 \\
        --max-voxel-axis 384 \\
        --voxelize-backend vtk

    # Resume after interruption: same command, will skip completed pairs.

    # Process only one specimen (validation):
    python scripts/dev/run_compare_set.py \\
        --manifest .../_manifest_358382.json \\
        --only-slug 000360672__Felis__000362550__000362581
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("compare_set")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "Tests" / "fixtures" / "nninteractive_compare"
DEFAULT_PROGRESS_DIR = REPO_ROOT / "runs" / "skull_batch_358382"
WORKFLOW_FILE = "nninteractive_compare.yml"
ARTIFACT_NAME = "nninteractive-fixtures"


# ---------------------------------------------------------------------------
# gh CLI wrappers
# ---------------------------------------------------------------------------

def _find_gh() -> str:
    """Locate the gh executable. Honours $GH_PATH, falls back to PATH lookup."""
    p = os.environ.get("GH_PATH")
    if p and Path(p).exists():
        return p
    candidates = [
        "gh",
        "gh.exe",
        r"C:\Users\DELL_\AppData\Local\Microsoft\WinGet\Packages"
        r"\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe",
    ]
    for c in candidates:
        try:
            subprocess.run([c, "--version"], check=True,
                           capture_output=True, text=True)
            return c
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("gh CLI not found on PATH or known locations")


def _run(cmd: list[str], *, check: bool = True, capture: bool = True,
         timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture,
                          text=True, timeout=timeout)


def _to_local_path(p: Path | str) -> str:
    """Translate a path to the form expected by the *local OS we're shelling
    to*. When the orchestrator runs in WSL but invokes the Windows ``gh.exe``,
    a path like ``/mnt/c/foo`` is meaningless to gh; it must become
    ``C:\\foo``. On native Linux/macOS or when the script runs in Windows
    Python, the path is returned unchanged.
    """
    s = str(p)
    if sys.platform != "linux":
        return s
    # In WSL, ``platform.uname().release`` contains "WSL"; detect cheaply via
    # /proc/version which is always populated on Linux.
    try:
        with open("/proc/version") as fh:
            kver = fh.read()
    except OSError:
        kver = ""
    is_wsl = "microsoft" in kver.lower() or "wsl" in kver.lower()
    if not is_wsl:
        return s
    if not s.startswith("/mnt/"):
        # Not a Windows-mounted path; leave alone (gh.exe will probably fail
        # but we don't have a sensible translation for native Linux paths).
        return s
    try:
        cp = subprocess.run(["wslpath", "-w", s], capture_output=True,
                            text=True, check=True)
        return cp.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return s


def gh_repo() -> str:
    return os.environ.get("GH_REPO", "johntrue15/MorphoClaw")


def gh_dispatch(gh: str, pair: "PairSpec", args: argparse.Namespace) -> str:
    """Dispatch the compare workflow on a single pair. Returns the run id."""
    inputs = [
        f"ct_media_id={pair.ct_media_id}",
        f"gt_media_id={pair.mesh_media_id}",
        f"goal={pair.goal()}",
        f"max_steps={args.max_steps}",
        f"voxelize_backend={args.voxelize_backend}",
        f"crop_around_mesh_mm={args.crop_around_mesh_mm}",
        f"max_voxel_axis={args.max_voxel_axis}",
        f"align_mesh_to_ct={args.align_mesh_to_ct}",
        f"paint_mode={args.paint_mode}",
    ]
    if args.paint_mode == "bright_seed":
        inputs.append(
            f"bright_seed_percentile={args.bright_seed_percentile}"
        )
        if args.bright_seed_no_stop_rules:
            inputs.append("bright_seed_no_stop_rules=true")
    cmd = [
        gh, "workflow", "run", WORKFLOW_FILE,
        "--repo", gh_repo(),
        "--ref", args.ref,
    ]
    for kv in inputs:
        cmd.extend(["-f", kv])
    log.info("Dispatching: %s", cmd)
    _run(cmd)

    # gh dispatch is async — wait a couple seconds, then take the newest run
    # of this workflow on this ref.
    for attempt in range(10):
        time.sleep(3)
        cp = _run([
            gh, "run", "list",
            "--repo", gh_repo(),
            "--workflow", WORKFLOW_FILE,
            "--branch", args.ref,
            "--event", "workflow_dispatch",
            "--limit", "3",
            "--json", "databaseId,createdAt,status",
        ])
        runs = json.loads(cp.stdout)
        # The most recent will sort first
        if runs and runs[0].get("status") in {"queued", "in_progress",
                                              "waiting", "pending"}:
            return str(runs[0]["databaseId"])
        log.debug("dispatch poll attempt %d: %s", attempt, runs)
    raise RuntimeError(f"Could not find a queued run for pair {pair.slug} after dispatch")


def gh_poll_until_done(gh: str, run_id: str, *,
                       poll_every_s: int = 30,
                       max_minutes: int = 240) -> dict:
    """Block until ``run_id`` reaches a terminal state. Returns the final state."""
    deadline = time.time() + max_minutes * 60
    last_status = ""
    while time.time() < deadline:
        cp = _run([
            gh, "run", "view", run_id,
            "--repo", gh_repo(),
            "--json", "status,conclusion,createdAt,updatedAt",
        ])
        state = json.loads(cp.stdout)
        if state.get("status") != last_status:
            log.info("  run %s: status=%s conclusion=%s",
                     run_id, state.get("status"), state.get("conclusion"))
            last_status = state.get("status", "")
        if state.get("status") == "completed":
            return state
        time.sleep(poll_every_s)
    raise TimeoutError(f"Run {run_id} did not complete within {max_minutes} min")


def gh_download_artifact(gh: str, run_id: str, dest: Path) -> None:
    """Download the nninteractive-fixtures artifact into ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    _run([
        gh, "run", "download", run_id,
        "--repo", gh_repo(),
        "--name", ARTIFACT_NAME,
        "--dir", _to_local_path(dest),
    ])


# ---------------------------------------------------------------------------
# Manifest + per-pair plumbing
# ---------------------------------------------------------------------------

@dataclass
class PairSpec:
    slug: str
    physical_object_id: str
    taxonomy: str
    ct_media_id: str
    mesh_media_id: str
    ct_file_size: int
    mesh_file_size: int
    title: str
    eligible: bool
    skip_reason: Optional[str] = None

    def goal(self) -> str:
        # The .ply meshes in project 358382 are whole-skull derivative meshes
        # of the CT specimen. The paint-loop goal is to segment what the mesh
        # represents — for a skull-anatomy project, the whole skull.
        genus = (self.taxonomy or "specimen").split()[0]
        return (
            f"Segment the cranial bone (skull) of this {genus} specimen "
            f"({self.title}). The bone is the dense bright tissue in the CT."
        )


def load_manifest(path: Path, only_slug: Optional[str] = None,
                  include_all: bool = False) -> list[PairSpec]:
    data = json.loads(path.read_text())
    pairs: list[PairSpec] = []
    for entry in data["pairs"]:
        p = PairSpec(
            slug=entry["slug"],
            physical_object_id=entry["physical_object_id"],
            taxonomy=entry.get("taxonomy", ""),
            ct_media_id=entry["ct_media_id"],
            mesh_media_id=entry["mesh_media_id"],
            ct_file_size=int(entry.get("ct_file_size") or 0),
            mesh_file_size=int(entry.get("mesh_file_size") or 0),
            title=entry.get("title", ""),
            eligible=bool(entry.get("eligible", True)),
            skip_reason=entry.get("skip_reason"),
        )
        if only_slug and p.slug != only_slug:
            continue
        if not include_all and not p.eligible:
            log.info("Skipping %s (%s)", p.slug, p.skip_reason or "ineligible")
            continue
        pairs.append(p)
    # Smallest CT first so failures surface quickly
    pairs.sort(key=lambda p: p.ct_file_size)
    return pairs


def fixture_path(slug: str, root: Path = DEFAULT_FIXTURE_ROOT) -> Path:
    return root / slug


def is_complete(slug: str, root: Path = DEFAULT_FIXTURE_ROOT) -> bool:
    fx = fixture_path(slug, root)
    required = ["baseline_metrics.json", "ct.nii.gz", "fixture.json",
                "gt_voxelized.nii.gz", "pred.nii.gz"]
    return all((fx / f).exists() and (fx / f).stat().st_size > 0
               for f in required)


def stage_artifact(staging_dir: Path, fixture_dest: Path) -> dict:
    """Unzip artifact into ``fixture_dest`` and return a brief summary."""
    fixture_dest.mkdir(parents=True, exist_ok=True)
    # The artifact ZIP contains the 5 files at the top level. gh extracts them
    # directly into ``staging_dir``.
    expected = ["baseline_metrics.json", "ct.nii.gz", "fixture.json",
                "gt_voxelized.nii.gz", "pred.nii.gz"]
    found = {}
    for name in expected:
        src = staging_dir / name
        if not src.exists() or src.stat().st_size == 0:
            return {"error": f"missing artifact file: {name}",
                    "staging_dir_contents": [p.name for p in staging_dir.iterdir()]}
        shutil.copy2(src, fixture_dest / name)
        found[name] = (fixture_dest / name).stat().st_size

    metrics = json.loads((fixture_dest / "baseline_metrics.json").read_text())
    return {
        "files": found,
        "dice": metrics.get("dice"),
        "iou": metrics.get("iou"),
        "voxel_count_pred": metrics.get("voxel_count_pred"),
        "voxel_count_gt": metrics.get("voxel_count_gt"),
    }


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git(args: list[str], *, cwd: Path = REPO_ROOT, env: Optional[dict] = None,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command via WSL (so line endings stay sane on Windows)."""
    # On Windows we drive everything via WSL; on a native posix dev box we
    # call git directly.
    if sys.platform == "win32":
        wsl_path = "/mnt/c" + str(cwd).replace("\\", "/").replace("C:", "")
        cmd = ["wsl", "-d", "Ubuntu-24.04", "-e", "bash", "-lc",
               f"cd {shlex_quote(wsl_path)} && git " + " ".join(shlex_quote(a) for a in args)]
        return subprocess.run(cmd, check=check, capture_output=True, text=True, env=env)
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True, env=env)


def shlex_quote(s: str) -> str:
    if not s or any(c in s for c in " \t\"'\\$`(){}[]|&;<>"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def commit_and_push(slug: str, taxonomy: str, dice: Optional[float],
                    voxels_pred: Optional[int],
                    voxels_gt: Optional[int],
                    skip_push: bool = False) -> None:
    fx_rel = f"Tests/fixtures/nninteractive_compare/{slug}"
    _git(["add", fx_rel])
    # Detect whether there are any staged changes (idempotent re-stage may be empty)
    status = _git(["status", "--porcelain", fx_rel])
    if not status.stdout.strip():
        log.info("  nothing to commit for %s (already on tree)", slug)
        return
    msg_lines = [
        f"nninteractive: cache 358382 fixture for {taxonomy or slug} [skip ci]",
        "",
        f"Pair: {slug}",
        f"Voxels (pred/GT): {voxels_pred}/{voxels_gt}" if voxels_pred is not None else "",
        f"Dice: {dice:.4f}" if isinstance(dice, (int, float)) else "Dice: -",
        "",
        "[skip ci] tag added to suppress the noisy pre-existing Run Tests CI",
        "failure (unrelated stale tests; see commit 6c713c1).",
    ]
    commit_msg = "\n".join(line for line in msg_lines if line is not None)
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "John True",
        "GIT_AUTHOR_EMAIL": "johntrue15@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "John True",
        "GIT_COMMITTER_EMAIL": "johntrue15@users.noreply.github.com",
    })
    # Write the message to a tempfile inside the repo so WSL can read it.
    msg_file = REPO_ROOT / ".tmp_commit_msg.txt"
    msg_file.write_text(commit_msg, encoding="utf-8")
    try:
        _git(["commit", "-F", ".tmp_commit_msg.txt"], env=env)
    finally:
        msg_file.unlink(missing_ok=True)

    if skip_push:
        return
    token = os.environ.get("GITHUB_TOKEN") or _gh_auth_token()
    push_url = (
        f"https://x-access-token:{token}@github.com/{gh_repo()}.git"
    )
    _git(["push", push_url, "HEAD:main"])


def _gh_auth_token() -> str:
    gh = _find_gh()
    cp = _run([gh, "auth", "token"])
    return cp.stdout.strip()


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

PROGRESS_FIELDS = [
    "slug", "physical_object_id", "taxonomy", "ct_media_id", "mesh_media_id",
    "ct_file_size_gb", "mesh_file_size_mb",
    "status", "run_id", "started_at", "ended_at", "duration_s",
    "dice", "iou", "voxel_count_pred", "voxel_count_gt", "error",
]


def append_progress(progress_dir: Path, row: dict) -> None:
    progress_dir.mkdir(parents=True, exist_ok=True)
    progress_csv = progress_dir / "progress.csv"
    write_header = not progress_csv.exists()
    with open(progress_csv, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PROGRESS_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------------------
# Per-pair driver
# ---------------------------------------------------------------------------

def process_pair(gh: str, pair: PairSpec, args: argparse.Namespace) -> dict:
    fx_dest = fixture_path(pair.slug, args.fixture_root)

    if is_complete(pair.slug, args.fixture_root) and not args.force:
        log.info("[skip] %s already has fixture", pair.slug)
        # Re-read the metrics so we can include them in progress.csv
        try:
            metrics = json.loads(
                (fx_dest / "baseline_metrics.json").read_text()
            )
            return {
                "status": "skipped_cached",
                "dice": metrics.get("dice"),
                "iou": metrics.get("iou"),
                "voxel_count_pred": metrics.get("voxel_count_pred"),
                "voxel_count_gt": metrics.get("voxel_count_gt"),
            }
        except Exception as exc:
            log.warning("  could not read cached metrics: %s", exc)
            return {"status": "skipped_cached"}

    t0 = time.time()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))

    try:
        run_id = gh_dispatch(gh, pair, args)
    except Exception as exc:
        return {"status": "dispatch_failed", "error": repr(exc),
                "started_at": started, "ended_at": started, "duration_s": 0}

    log.info("  -> dispatched run %s for %s", run_id, pair.slug)
    log.info("     https://github.com/%s/actions/runs/%s", gh_repo(), run_id)

    try:
        state = gh_poll_until_done(gh, run_id,
                                   poll_every_s=args.poll_every_s,
                                   max_minutes=args.max_minutes)
    except TimeoutError as exc:
        return {"status": "timeout", "run_id": run_id, "error": repr(exc),
                "started_at": started,
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": round(time.time() - t0)}

    if state.get("conclusion") != "success":
        return {"status": "workflow_failed", "run_id": run_id,
                "error": f"conclusion={state.get('conclusion')}",
                "started_at": started,
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": round(time.time() - t0)}

    # Download artifact + stage
    staging = args.progress_dir / "_staging" / run_id
    if staging.exists():
        shutil.rmtree(staging)
    try:
        gh_download_artifact(gh, run_id, staging)
    except Exception as exc:
        return {"status": "artifact_dl_failed", "run_id": run_id,
                "error": repr(exc),
                "started_at": started,
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": round(time.time() - t0)}

    stage_summary = stage_artifact(staging, fx_dest)
    if "error" in stage_summary:
        return {"status": "stage_failed", "run_id": run_id,
                "error": stage_summary["error"],
                "started_at": started,
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": round(time.time() - t0)}

    # Commit + push
    try:
        commit_and_push(
            slug=pair.slug,
            taxonomy=pair.taxonomy,
            dice=stage_summary.get("dice"),
            voxels_pred=stage_summary.get("voxel_count_pred"),
            voxels_gt=stage_summary.get("voxel_count_gt"),
            skip_push=args.skip_push,
        )
    except subprocess.CalledProcessError as exc:
        log.error("  git commit/push failed: %s\nstderr=%s",
                  exc, getattr(exc, "stderr", ""))
        return {"status": "git_failed", "run_id": run_id,
                "error": repr(exc),
                **stage_summary,
                "started_at": started,
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": round(time.time() - t0)}

    return {"status": "success", "run_id": run_id,
            **stage_summary,
            "started_at": started,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_s": round(time.time() - t0)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", type=Path, required=True,
                   help="JSON manifest with 'pairs' list (see _manifest_358382.json)")
    p.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    p.add_argument("--progress-dir", type=Path, default=DEFAULT_PROGRESS_DIR)
    p.add_argument("--ref", default="main",
                   help="Branch the workflow runs on (default: main)")
    p.add_argument("--only-slug", default="",
                   help="Process only the pair with this slug (validation)")
    p.add_argument("--include-all", action="store_true",
                   help="Process pairs even when manifest marks them ineligible "
                        "(e.g. the >5 GB CTs)")
    p.add_argument("--force", action="store_true",
                   help="Re-run pairs that already have a cached fixture locally")
    p.add_argument("--crop-around-mesh-mm", type=float, default=5.0)
    p.add_argument("--max-voxel-axis", type=int, default=384)
    p.add_argument("--align-mesh-to-ct", default="centroid",
                   choices=["", "centroid", "auto"],
                   help="Apply mesh->CT translation before crop/voxelize. "
                        "Required for project 358382. Default 'centroid'.")
    p.add_argument("--voxelize-backend", default="vtk",
                   choices=["vtk", "slicer", "auto"])
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--paint-mode", default="llm",
                   choices=["llm", "bright_seed"],
                   help="Paint-loop strategy. 'llm' = original GPT loop. "
                        "'bright_seed' = deterministic bright-voxel greedy "
                        "(no OpenAI cost, handles thin-cortical-bone CTs "
                        "the LLM cannot). Default 'llm' for back-compat.")
    p.add_argument("--bright-seed-percentile", type=float, default=99.0,
                   help="Intensity percentile threshold for "
                        "--paint-mode bright_seed (default 99 = top 1%%).")
    p.add_argument("--bright-seed-no-stop-rules", action="store_true",
                   help="Disable bright-seed saturation + explosion guards "
                        "so it runs to --max-steps (mouse-skull behaviour).")
    p.add_argument("--poll-every-s", type=int, default=30)
    p.add_argument("--max-minutes", type=int, default=240,
                   help="Max wall time per pair (default 4h)")
    p.add_argument("--skip-push", action="store_true",
                   help="Commit locally but skip git push (debugging)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    gh = _find_gh()
    pairs = load_manifest(args.manifest,
                          only_slug=(args.only_slug or None),
                          include_all=args.include_all)
    if not pairs:
        log.error("No pairs to process (check --manifest / --only-slug / --include-all)")
        return 2

    log.info("=" * 70)
    log.info(" %d pair(s) queued, smallest CT first", len(pairs))
    log.info("=" * 70)
    for i, pr in enumerate(pairs, 1):
        log.info("  %2d  %-50s  %5.2f GB CT / %5.1f MB mesh",
                 i, pr.slug,
                 pr.ct_file_size / (1 << 30),
                 pr.mesh_file_size / (1 << 20))

    n_succeeded = 0
    n_skipped = 0
    n_failed = 0
    for i, pair in enumerate(pairs, 1):
        log.info("")
        log.info("--- [%d/%d] %s (%s) ---",
                 i, len(pairs), pair.slug, pair.taxonomy or "?")
        result = process_pair(gh, pair, args)
        row = {
            "slug": pair.slug,
            "physical_object_id": pair.physical_object_id,
            "taxonomy": pair.taxonomy,
            "ct_media_id": pair.ct_media_id,
            "mesh_media_id": pair.mesh_media_id,
            "ct_file_size_gb": round(pair.ct_file_size / (1 << 30), 3),
            "mesh_file_size_mb": round(pair.mesh_file_size / (1 << 20), 2),
            **result,
        }
        append_progress(args.progress_dir, row)
        log.info("  -> %s", row.get("status"))
        if row["status"] == "success":
            n_succeeded += 1
            log.info("     dice=%s iou=%s voxels=%s/%s",
                     row.get("dice"), row.get("iou"),
                     row.get("voxel_count_pred"), row.get("voxel_count_gt"))
        elif row["status"] == "skipped_cached":
            n_skipped += 1
        else:
            n_failed += 1

    log.info("")
    log.info("=" * 70)
    log.info(" DONE  succeeded=%d  skipped=%d  failed=%d  (out of %d)",
             n_succeeded, n_skipped, n_failed, len(pairs))
    log.info(" progress.csv: %s", args.progress_dir / "progress.csv")
    log.info("=" * 70)
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
