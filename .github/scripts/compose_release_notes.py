#!/usr/bin/env python3
"""Compose GitHub Release notes (markdown) for a skull-visuals release.

Usage::

    compose_release_notes.py <job_id> <run_dir> <visuals.json> <status_line_json>

Prints markdown to stdout. Kept as a standalone script so the workflow does not
embed a python heredoc inside a YAML block scalar.
"""

from __future__ import annotations

import json
import sys


def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 4:
        print("usage: compose_release_notes.py <job_id> <run_dir> "
              "<visuals.json> <status_line_json>", file=sys.stderr)
        return 2
    job_id, run_dir, vpath, status_line = argv[:4]

    try:
        v = json.load(open(vpath))
    except Exception:
        v = {}
    try:
        st = json.loads(status_line)
    except Exception:
        st = {}

    stop = st.get("stop_reason") or {}
    reason = stop.get("reason") if isinstance(stop, dict) else stop

    lines = [
        f"# {job_id} — segmented skull",
        "",
        f"Auto-generated 3D visuals from the Jetstream nnInteractive bright-seed "
        f"run **{run_dir}**.",
        "",
        "## Run",
        f"- Steps (clicks): **{_fmt(st.get('steps'))}**",
        f"- Stop reason: **{reason}**",
        f"- Union voxels: **{_fmt(st.get('union_voxels'))}**",
        f"- Segments: **{_fmt(st.get('n_segments'))}**",
        "",
        "## Mesh",
        f"- Vertices: **{_fmt(v.get('mesh_vertices'))}**, "
        f"Faces: **{_fmt(v.get('mesh_faces'))}**",
        f"- Voxel spacing (mm): {v.get('spacing_mm_xyz')}",
        f"- Bounds (mm): {v.get('bounds_mm')}",
        "",
        "## Assets",
        "- `.glb` — open in any 3D / AR viewer (renders inline on GitHub)",
        "- `.usdz` — Apple Quick Look / AR (drag onto an iPhone/Mac)",
        "- `.stl` — universal mesh interchange",
        "- `.png` / `_turntable.gif` — preview renders (best-effort)",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
