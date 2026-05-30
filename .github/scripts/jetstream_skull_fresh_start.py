#!/usr/bin/env python3
"""Clean-slate skull run on Jetstream: clear scene → load CT → reset → bright-seed.

Fixture-driven (``data/sample/colors_of_skull_urls.json`` by default). Use for
each Colors of Skull specimen after pushing ``data/sample/<slug>_ct.nrrd`` to GitHub.

Usage::

    set -a && source .env && set +a
    python3 .github/scripts/jetstream_skull_fresh_start.py \\
        --fixture data/sample/colors_of_skull_urls.json \\
        --max-steps 100
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jetstream_unshelve_start import load_sample_in_slicer  # noqa: E402
from run_telemetry import PING_SLICER_SRC  # noqa: E402
from slicer_remote_bright_seed import post_python, _read_url  # noqa: E402

DEFAULT_FIXTURE = REPO_ROOT / "data/sample/colors_of_skull_urls.json"

CLEAR_SCENE_SRC = """\
import slicer
removed = {"volumes": [], "segmentations": []}
try:
    for sn in list(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")):
        if "do not touch" in sn.GetName().lower():
            continue
        removed["segmentations"].append(sn.GetName())
        slicer.mrmlScene.RemoveNode(sn)
    for vn in list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")):
        removed["volumes"].append(vn.GetName())
        slicer.mrmlScene.RemoveNode(vn)
    __execResult = {"status": "ok", "removed": removed}
except Exception as e:
    __execResult = {"status": "exception", "error": repr(e)}
"""


def _raw_url(repo: str, ref: str, relpath: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{relpath.lstrip('/')}"


def _load_fixture(path: Path) -> dict:
    raw = json.loads(path.read_text())
    repo = os.environ.get("GITHUB_REPO", raw["github_repo"])
    ref = os.environ.get("GITHUB_REF", raw["github_ref"])
    files = raw.get("files") or {}
    return {
        "ct_url": _raw_url(repo, ref, files["ct_nrrd"]),
        "volume_name": raw.get("slicer_volume_name") or Path(files["ct_nrrd"]).stem,
        "slug": raw.get("slug", "skull"),
        "pilot_defaults": raw.get("pilot_defaults") or {},
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    p.add_argument("--ct-url", default=None)
    p.add_argument("--volume-name", default=None)
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--local-ct-path", type=Path, default=None,
                   help="Upload CT from this Mac via chunked push (no GitHub URL)")
    p.add_argument("--no-screenshots", action="store_true")
    p.add_argument("--batch-size", type=int, default=1,
                   help="clicks per /slicer/exec call (>1 = server-side "
                        "batching + incremental union mask; much faster)")
    p.add_argument("--fast", action="store_true",
                   help="throughput preset: --batch-size 8 + --no-screenshots")
    p.add_argument("--headless", action="store_true",
                   help="display off during clicking; show combined surface at end")
    p.add_argument("--skip-clear", action="store_true")
    args = p.parse_args(argv)

    fix_path = args.fixture if args.fixture.is_absolute() else REPO_ROOT / args.fixture
    fix = _load_fixture(fix_path)
    ct_url = args.ct_url or fix["ct_url"]
    volume_name = args.volume_name or fix["volume_name"]
    slug = fix["slug"]
    defaults = fix["pilot_defaults"]
    max_steps = args.max_steps or int(defaults.get("max_steps", 100))

    base_url = _read_url()
    out_dir = args.out_dir or (
        Path("runs") / f"{slug}_fresh_{time.strftime('%Y%m%dT%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Slicer: {base_url}")
    print(f"Fixture: {fix_path}")
    print(f"CT URL: {ct_url}")
    print(f"Volume: {volume_name}")
    print(f"max_steps: {max_steps}")

    ping = post_python(base_url, PING_SLICER_SRC, timeout=25, retries=1)
    print(f"Slicer ping: {ping.get('status')}")

    if not args.skip_clear:
        print("Clearing scene…")
        cleared = post_python(base_url, CLEAR_SCENE_SRC, timeout=120, retries=1)
        print(f"  removed {len(cleared.get('removed', {}).get('volumes', []))} volumes")
        if cleared.get("status") != "ok":
            print(f"  clear warning: {cleared}")
            return 3

    if args.local_ct_path is not None:
        ct_path = args.local_ct_path if args.local_ct_path.is_absolute() else REPO_ROOT / args.local_ct_path
        if not ct_path.exists():
            print(f"ERROR: local CT not found: {ct_path}", file=sys.stderr)
            return 4
        from remote_volume_io import push_volume

        print(f"Uploading local CT ({ct_path.stat().st_size:,} bytes)…")
        loaded = push_volume(base_url, ct_path, name=volume_name)
        if loaded.get("status") != "ok":
            print(f"ERROR upload/load: {loaded!r}", file=sys.stderr)
            return 4
        vol = loaded.get("volume_name") or volume_name
        print(f"Loaded: {vol}  shape={loaded.get('shape_kji')}")
    else:
        print("Loading CT from GitHub…")
        loaded = load_sample_in_slicer(base_url, ct_url, volume_name)
        if loaded.get("status") != "ok":
            print(f"ERROR load: {loaded!r}", file=sys.stderr)
            return 4
        vol = loaded.get("volume_name") or volume_name
        print(f"Loaded: {vol}  shape={loaded.get('shape_kji')}")

    os.environ["SLICER_WEBSERVER_URL"] = base_url
    from eval_project358382_pilot import run_bright_seed

    print(f"Bright-seed from scratch (--reset-first), max_steps={max_steps}")
    rc = run_bright_seed(
        volume_name=vol,
        out_dir=out_dir / "bright_seed",
        label=f"{slug}_fresh",
        max_steps=max_steps,
        reset_first=True,
        no_screenshots=args.no_screenshots,
        batch_size=args.batch_size,
        fast=args.fast,
        headless=args.headless,
    )
    if rc != 0:
        return rc
    print(f"Done. Artifacts: {out_dir / 'bright_seed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
