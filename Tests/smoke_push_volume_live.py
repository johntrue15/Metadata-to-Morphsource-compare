"""Live smoke test for the push_volume fix.

Directly exercises ``remote_volume_io.push_volume`` against the running
Jetstream Slicer Web Server, using the locally-staged 39 MB IMPC sample
volume. Bypasses MorphoSource (which may be sandboxed) and the pilot
orchestrator's cropping pipeline so we can isolate whether the new
chunked-with-retries defaults (2 MiB / 180 s / 3 retries) actually
unstick the upload that timed out in runs/pilot_project358382_20260524T140322/.

Usage:
    python Tests/smoke_push_volume_live.py
    # or override URL / volume:
    SLICER_WEBSERVER_URL=https://... python Tests/smoke_push_volume_live.py \
        --volume path/to/some.nii.gz

Exit codes:
    0  push_volume returned status=ok (fix works)
    1  push_volume returned a non-ok status (recipe bug)
    2  push_volume raised (transport failure even with retries)
    3  Could not find a volume file to push
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _read_env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    if val:
        return val
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return default


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=_read_env("SLICER_WEBSERVER_URL"),
                   help="Slicer Web Server URL. Defaults to "
                        "SLICER_WEBSERVER_URL env or .env value.")
    p.add_argument("--volume", type=Path,
                   default=ROOT / "data" / "sample" /
                            "tuatara_skull_000358663_ct.nrrd",
                   help="Path to the volume file to push.")
    p.add_argument("--name", default="smoke_push_volume_live",
                   help="Slicer scene node name (default: %(default)s)")
    args = p.parse_args(argv)

    if not args.url:
        print("ERROR: --url not given and SLICER_WEBSERVER_URL not in env/.env",
              file=sys.stderr)
        return 3
    if not args.volume.exists():
        print(f"ERROR: volume not found: {args.volume}", file=sys.stderr)
        return 3

    size_mb = args.volume.stat().st_size / (1024 * 1024)
    print(f"[smoke] volume : {args.volume}  ({size_mb:,.1f} MiB)")
    print(f"[smoke] target : {args.url}")
    print(f"[smoke] name   : {args.name}")
    print("")

    from remote_volume_io import push_volume

    last_pct = [-1]

    def _progress(sent: int, total: int) -> None:
        pct = int(100 * sent / max(1, total))
        if pct >= last_pct[0] + 10 or sent == total:
            print(f"  upload {pct:>3d}%  ({sent:,}/{total:,} bytes)")
            last_pct[0] = pct

    t0 = time.time()
    try:
        result = push_volume(args.url, args.volume,
                              name=args.name, progress=_progress)
    except Exception as e:
        elapsed = time.time() - t0
        print(f"\n[smoke] FAIL  push_volume raised after {elapsed:.1f}s: {e!r}",
              file=sys.stderr)
        return 2

    elapsed = time.time() - t0
    status = result.get("status")
    print("")
    print(f"[smoke] status    : {status}  (after {elapsed:.1f}s)")
    print(f"[smoke] sha256    : {result.get('local_sha256')}")
    print(f"[smoke] remote    : {result.get('remote_path')}")
    print(f"[smoke] shape_kji : {result.get('shape_kji')}")
    print(f"[smoke] spacing   : {result.get('spacing_mm')}")
    print(f"[smoke] n_chunks  : {result.get('n_chunks')}")
    timings = result.get("_timings", {}) or {}
    if timings.get("chunks"):
        retries_used = sum(c.get("retries_used", 0) for c in timings["chunks"])
        slowest = max(timings["chunks"], key=lambda c: c.get("dt_s") or 0)
        print(f"[smoke] retries   : {retries_used} (total across all chunks)")
        print(f"[smoke] slowest   : chunk {slowest.get('i')} -> "
              f"{slowest.get('dt_s'):.1f}s")
    if status != "ok":
        print(f"[smoke] FAIL  status={status!r}  result={result!r}",
              file=sys.stderr)
        return 1
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
