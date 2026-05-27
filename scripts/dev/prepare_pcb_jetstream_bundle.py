#!/usr/bin/env python3
"""Package PCB CT + bright-seed run script for MorphoCloud / Jetstream2.

Creates ``.local/pcb_jetstream/bundle/`` with:
  - ``pcb_ti_gpu.nii.gz``     — copy of the Dell-safe volume (for reference)
  - ``pcb_ti_jetstream.nii.gz`` — slightly higher-res decimation (default max-axis 512)
  - ``run_bright_seed.sh``    — run on the remote box after ``scp``
  - ``manifest.json``         — paths, voxel counts, commands
  - ``README.txt``            — upload + execute steps

Usage::

    python scripts/dev/prepare_pcb_jetstream_bundle.py
    python scripts/dev/prepare_pcb_jetstream_bundle.py --input .local/pcb_data/pcb_ti.nii.gz
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dev"))

log = logging.getLogger("pcb_jetstream_bundle")

DEFAULT_INPUT = REPO_ROOT / ".local" / "pcb_data" / "pcb_ti.nii.gz"
DEFAULT_STACK = Path(r"/mnt/c/Users/DELL_/OneDrive/Documents/PCB/TI tiff stack")
BUNDLE_DIR = REPO_ROOT / ".local" / "pcb_jetstream" / "bundle"


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                   help="Dell-safe volume (already decimated)")
    p.add_argument("--stack-dir", type=Path, default=DEFAULT_STACK,
                   help="Original TIFF stack (for higher-res Jetstream volume)")
    p.add_argument("--max-axis-jetstream", type=int, default=512)
    p.add_argument("--max-steps", type=int, default=500)
    p.add_argument("--media-id", default="pcb_ti")
    p.add_argument("--output-dir", type=Path, default=BUNDLE_DIR)
    args = p.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    import subprocess

    jet_path = args.output_dir / f"{args.media_id}_jetstream.nii.gz"
    prep_script = REPO_ROOT / "scripts" / "dev" / "prepare_pcb_volume.py"
    if args.stack_dir.is_dir():
        log.info("Building Jetstream volume (max-axis=%d)", args.max_axis_jetstream)
        subprocess.run(
            [
                sys.executable,
                str(prep_script),
                "--input-dir", str(args.stack_dir),
                "--output-dir", str(args.output_dir),
                "--slug", f"{args.media_id}_jetstream",
                "--max-axis", str(args.max_axis_jetstream),
            ],
            check=True,
        )
    else:
        log.warning("TIFF stack not found at %s — copying Dell volume only",
                    args.stack_dir)
        shutil.copy2(args.input, jet_path)

    gpu_copy = args.output_dir / f"{args.media_id}_gpu.nii.gz"
    if args.input.is_file():
        shutil.copy2(args.input, gpu_copy)

    vol_for_run = jet_path if jet_path.is_file() else gpu_copy
    run_sh = args.output_dir / "run_bright_seed.sh"
    run_sh.write_text(
        f"""#!/usr/bin/env bash
# Run on Jetstream / MorphoCloud (GPU box) after uploading this bundle.
set -euo pipefail
BUNDLE_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
NNI_HOME="${{NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}}"
NNI_PY="$NNI_HOME/bin/python"
REPO="${{MORPHOCLAW_DIR:-$HOME/MorphoClaw}}"
BRIGHT="$REPO/.github/scripts/nninteractive_bright_seed.py"
VOL="$BUNDLE_DIR/{vol_for_run.name}"
OUT="$BUNDLE_DIR/output_$(date +%Y%m%d_%H%M%S)"

if [[ ! -x "$NNI_PY" ]]; then
  echo "Bootstrap nnInteractive first:" >&2
  echo "  bash $REPO/.github/scripts/install_nninteractive_remote.sh" >&2
  exit 1
fi
mkdir -p "$OUT"
"$NNI_PY" "$BRIGHT" \\
  --input "$VOL" \\
  --output-dir "$OUT" \\
  --media-id {args.media_id} \\
  --autopilot \\
  --max-steps {args.max_steps}
echo "Done. Results in $OUT"
""",
        encoding="utf-8",
    )
    run_sh.chmod(0o755)

    readme = args.output_dir / "README.txt"
    readme.write_text(
        f"""PCB bright-seed — Jetstream / MorphoCloud run pack
Generated: {datetime.now(timezone.utc).isoformat()}

1) Upload bundle to your GPU instance (replace HOST):

   scp -r "{args.output_dir}" exouser@HOST:~/pcb_bundle/

2) On the remote box (once): bootstrap nnInteractive if needed:

   curl -fsSL https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/.github/scripts/install_nninteractive_remote.sh | bash

3) Run segmentation:

   ssh exouser@HOST 'bash ~/pcb_bundle/run_bright_seed.sh'

4) Pull results back:

   scp -r exouser@HOST:~/pcb_bundle/output_* .local/pcb_brightseed/jetstream/

Alternative — WebSocket server (no scp of results needed if proxy works):
  On Jetstream: NNI_WS_TOKEN=... bash start_remote_server.sh
  On Dell: set NNI_REMOTE_WS + NNI_WS_TOKEN in .env, then run compare with
  --input pointing at the local volume (upload handled by remote_volume_io).

Volumes in this bundle:
  - {gpu_copy.name}  — matches Dell decimated run
  - {jet_path.name}  — higher-res for Jetstream GPU
""",
        encoding="utf-8",
    )

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "media_id": args.media_id,
        "max_steps": args.max_steps,
        "max_axis_jetstream": args.max_axis_jetstream,
        "files": {},
    }
    for p in sorted(args.output_dir.iterdir()):
        if p.is_file():
            manifest["files"][p.name] = {
                "path": str(p),
                "bytes": p.stat().st_size,
                "sha256": _sha256(p) if p.suffix in (".gz", ".nrrd", ".nii") else None,
            }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    log.info("Bundle ready: %s", args.output_dir)
    log.info("  scp -r %s exouser@<jetstream-host>:~/pcb_bundle/", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
