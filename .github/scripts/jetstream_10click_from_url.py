#!/usr/bin/env python3
"""Load a GitHub-hosted sample CT into Jetstream Slicer and run bright-seed clicks.

Smoke test for the Colors of Skull / URL-load path before the full
``eval_project358382_pilot.py`` pipeline.

Steps:
  1. Probe ``SLICER_WEBSERVER_URL`` (default loopback :2016 on the box).
  2. ``POST /slicer/exec`` — download + load the CT NRRD from a raw GitHub URL.
  3. Invoke ``slicer_remote_bright_seed`` programmatically (default 10 clicks).

Usage (on Jetstream, after ``data/sample/*`` is pushed to GitHub)::

    set -a && source .env && set +a
    export SLICER_WEBSERVER_URL=http://127.0.0.1:2016/
    python3 .github/scripts/jetstream_10click_from_url.py \\
        --fixture data/sample/colors_of_skull_urls.json

Or explicit URLs::

    python3 .github/scripts/jetstream_10click_from_url.py \\
        --ct-url https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/data/sample/crotalus_skull_000445108_ct.nrrd \\
        --volume-name crotalus_skull_000445108_ct \\
        --max-steps 10 \\
        --out-dir runs/jetstream_10click_crotalus
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _raw_url(repo: str, ref: str, relpath: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{relpath.lstrip('/')}"


def _load_fixture(path: Path) -> dict:
    raw = json.loads(path.read_text())
    repo = os.environ.get("GITHUB_REPO", raw.get("github_repo", "johntrue15/MorphoClaw"))
    ref = os.environ.get("GITHUB_REF", raw.get("github_ref", "main"))
    files = raw.get("files") or {}
    ct_rel = files.get("ct_nrrd")
    if not ct_rel:
        raise ValueError(f"fixture missing files.ct_nrrd: {path}")
    return {
        "ct_url": _raw_url(repo, ref, ct_rel),
        "gt_url": _raw_url(repo, ref, files["gt_labelmap_nrrd"])
        if files.get("gt_labelmap_nrrd") else None,
        "volume_name": raw.get("slicer_volume_name")
        or Path(ct_rel).stem,
        "slug": raw.get("slug", "sample"),
        "pilot_defaults": raw.get("pilot_defaults") or {},
    }


def _probe_slicer(base_url: str) -> bool:
    import urllib.request

    url = base_url.rstrip("/") + "/slicer/screenshot"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def load_sample_in_slicer(base_url: str, ct_url: str,
                          volume_name: str) -> dict:
    """Download *ct_url* on the Jetstream box and load into Slicer."""
    import tempfile
    import urllib.parse
    import urllib.request

    from remote_volume_io import load_volume_from_remote_path

    cache_dir = Path(tempfile.gettempdir()) / "morphoclaw_github_samples"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fname = Path(urllib.parse.urlparse(ct_url).path).name
    local_path = cache_dir / fname
    if not local_path.is_file() or local_path.stat().st_size == 0:
        print(f"Downloading {ct_url} -> {local_path} …")
        urllib.request.urlretrieve(ct_url, local_path)
        print(f"Downloaded {local_path.stat().st_size:,} bytes.")
    else:
        print(f"Using cached {local_path} ({local_path.stat().st_size:,} bytes)")
    return load_volume_from_remote_path(
        base_url, str(local_path), name=volume_name, timeout=600.0,
    )


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixture", type=Path,
                   help="JSON fixture with github_repo + files.ct_nrrd "
                        "(default: data/sample/colors_of_skull_urls.json)")
    p.add_argument("--ct-url", type=str, help="Raw GitHub URL to CT NRRD")
    p.add_argument("--volume-name", type=str,
                   help="Slicer scene name for the loaded volume")
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--intensity-percentile", type=float, default=99.0)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--label", type=str, default=None)
    p.add_argument("--no-screenshots", action="store_true")
    p.add_argument("--reset-first", action="store_true",
                   help="reset segmentation before bright-seed")
    p.add_argument("--skip-remote-env", action="store_true",
                   help="skip slow Slicer env probe in bright-seed")
    p.add_argument("--skip-failed-steps", action="store_true",
                   help="log transport errors and continue bright-seed")
    p.add_argument("--skip-bright-seed", action="store_true",
                   help="Only load the CT; do not run bright-seed")
    args = p.parse_args(argv)

    base_url = (
        os.environ.get("SLICER_WEBSERVER_URL", "").strip()
        or os.environ.get("NNI_REMOTE_URL", "").strip()
    )
    if not base_url:
        print("ERROR: set SLICER_WEBSERVER_URL (e.g. http://127.0.0.1:2016/)",
              file=sys.stderr)
        return 2

    if args.fixture:
        fix = _load_fixture(args.fixture if args.fixture.is_absolute()
                            else REPO_ROOT / args.fixture)
        ct_url = args.ct_url or fix["ct_url"]
        volume_name = args.volume_name or fix["volume_name"]
        label = args.label or fix["slug"]
        defaults = fix.get("pilot_defaults") or {}
        max_steps = args.max_steps if args.max_steps != 10 else int(
            defaults.get("max_steps", args.max_steps))
        intensity = args.intensity_percentile
        if intensity == 99.0 and "intensity_percentile" in defaults:
            intensity = float(defaults["intensity_percentile"])
    else:
        if not args.ct_url or not args.volume_name:
            default_fix = REPO_ROOT / "data/sample/colors_of_skull_urls.json"
            if default_fix.exists():
                fix = _load_fixture(default_fix)
                ct_url = fix["ct_url"]
                volume_name = fix["volume_name"]
                label = args.label or fix["slug"]
                max_steps = args.max_steps
                intensity = args.intensity_percentile
            else:
                print("ERROR: pass --fixture or both --ct-url and --volume-name",
                      file=sys.stderr)
                return 2
        else:
            ct_url = args.ct_url
            volume_name = args.volume_name
            label = args.label or volume_name
            max_steps = args.max_steps
            intensity = args.intensity_percentile

    out_dir = args.out_dir
    if out_dir is None:
        out_dir = REPO_ROOT / "runs" / f"jetstream_10click_{label}"

    print(f"Slicer base URL : {base_url}")
    print(f"CT URL          : {ct_url}")
    print(f"Volume name     : {volume_name}")
    print(f"Out dir         : {out_dir}")

    if not _probe_slicer(base_url):
        print("ERROR: Slicer Web Server not reachable. Start Web Server on :2016.",
              file=sys.stderr)
        return 3

    loaded = load_sample_in_slicer(base_url, ct_url, volume_name)
    if loaded.get("status") != "ok":
        print(f"ERROR: failed to load CT: {loaded!r}", file=sys.stderr)
        return 4

    print("CT loaded in Slicer.")
    if args.skip_bright_seed:
        print("Skipping bright-seed (--skip-bright-seed).")
        return 0

    import slicer_remote_bright_seed as bs

    os.environ["SLICER_WEBSERVER_URL"] = base_url
    rc = bs.main([
        "--volume", volume_name,
        "--max-steps", str(max_steps),
        "--intensity-percentile", str(intensity),
        "--no-stop-rules",
        "--skip-remote-env",
        "--skip-volume-hash",
        "--out-dir", str(out_dir),
        *(["--no-screenshots"] if args.no_screenshots else []),
        *(["--reset-first"] if args.reset_first else []),
        *(["--skip-remote-env"] if args.skip_remote_env else []),
        *(["--skip-failed-steps"] if args.skip_failed_steps else []),
        *(["--label", label] if label else []),
    ])
    if rc != 0:
        print(f"bright-seed exited {rc}", file=sys.stderr)
        return rc
    print(f"Done. Artifacts under {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
