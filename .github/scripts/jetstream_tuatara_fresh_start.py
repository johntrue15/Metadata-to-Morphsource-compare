#!/usr/bin/env python3
"""Clean-slate tuatara run on Jetstream: clear scene → load CT → reset → bright-seed.

Use when continuation runs piled up empty segments or wrong volume names.

Prerequisites on Jetstream:
  - Slicer Web Server :2016
  - nninteractive-slicer-server :1527

Usage::

    set -a && source .env && set +a
    python3 .github/scripts/jetstream_tuatara_fresh_start.py --max-steps 100
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

FIXTURE = REPO_ROOT / "data/sample/tuatara_sample_urls.json"
DEFAULT_CT_URL = (
    "https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/"
    "data/sample/tuatara_skull_000358663_ct.nrrd"
)
VOLUME_NAME = "tuatara_skull_000358663_ct"

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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ct-url", default=None)
    p.add_argument("--volume-name", default=VOLUME_NAME)
    p.add_argument("--max-steps", type=int, default=100)
    p.add_argument("--out-dir", type=Path,
                   default=Path("runs") / f"tuatara_fresh_{time.strftime('%Y%m%dT%H%M%S')}")
    p.add_argument("--no-screenshots", action="store_true")
    p.add_argument("--skip-clear", action="store_true",
                   help="Only load + reset + click (no volume/seg teardown)")
    args = p.parse_args(argv)

    ct_url = args.ct_url
    if not ct_url and FIXTURE.exists():
        raw = json.loads(FIXTURE.read_text())
        repo = os.environ.get("GITHUB_REPO", raw["github_repo"])
        ref = os.environ.get("GITHUB_REF", raw["github_ref"])
        rel = raw["files"]["ct_nrrd"]
        ct_url = f"https://raw.githubusercontent.com/{repo}/{ref}/{rel}"
    ct_url = ct_url or DEFAULT_CT_URL

    base_url = _read_url()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Slicer: {base_url}")
    print(f"CT URL: {ct_url}")
    print(f"Volume: {args.volume_name}")

    env = post_python(base_url, PING_SLICER_SRC, timeout=25, retries=1)
    print(f"Slicer ping: {env.get('status')}")
    print("NOTE: restart nninteractive-slicer-server :1527 for mask growth after manual stop.")

    if not args.skip_clear:
        print("Clearing scene (all volumes + segmentations)…")
        cleared = post_python(base_url, CLEAR_SCENE_SRC, timeout=120)
        print(f"  removed {len(cleared.get('removed', {}).get('volumes', []))} volumes, "
              f"{len(cleared.get('removed', {}).get('segmentations', []))} segmentations")
        if cleared.get("status") != "ok":
            print(f"  clear warning: {cleared}")
            return 3

    print("Loading tuatara CT from GitHub…")
    loaded = load_sample_in_slicer(base_url, ct_url, args.volume_name)
    if loaded.get("status") != "ok":
        print(f"ERROR load: {loaded!r}", file=sys.stderr)
        return 4
    vol = loaded.get("volume_name") or args.volume_name
    print(f"Loaded: {vol}  shape={loaded.get('shape_kji')}")

    os.environ["SLICER_WEBSERVER_URL"] = base_url
    from eval_project358382_pilot import run_bright_seed

    print(f"Bright-seed from scratch (--reset-first), max_steps={args.max_steps}")
    rc = run_bright_seed(
        volume_name=vol,
        out_dir=args.out_dir / "bright_seed",
        label="tuatara_fresh",
        max_steps=args.max_steps,
        reset_first=True,
        no_screenshots=args.no_screenshots,
    )
    if rc != 0:
        return rc
    print(f"Done. Artifacts: {args.out_dir / 'bright_seed'}")
    print("After GT labelmap exists: make tuatara-score-100click")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
