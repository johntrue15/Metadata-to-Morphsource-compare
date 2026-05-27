#!/usr/bin/env python3
"""Back-fill ``sweep_results.jsonl`` rows that were written before the
``_summarise_run`` filename-fix landed.

Old daemon revisions looked for ``<media>_bright_summary.json`` and
flagged every successful job as ``summary_missing=true`` because the
bright-seed runner actually writes ``<media>_nni_summary.json``. This
script re-reads each row's ``output_dir`` with the current
``sweep_harness._summarise_run`` and rewrites the file in place,
preserving order.

Usage::

    python scripts/dev/backfill_sweep_results.py \
        --state-dir paper_artifacts/sweep
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

# pylint: disable=wrong-import-position
from sweep_harness import _summarise_run  # noqa: E402

log = logging.getLogger("backfill")


def _backfill(path: Path) -> tuple[int, int]:
    if not path.exists():
        log.warning("no sweep_results.jsonl at %s", path)
        return (0, 0)
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            rows.append(json.loads(ln))

    updated = 0
    for row in rows:
        if row.get("status") != "success":
            continue
        if row.get("n_clicks") is not None:
            continue
        out_dir = row.get("output_dir")
        media_id = row.get("media_id")
        if not out_dir or not media_id:
            continue
        # _summarise_run will fall through to the correct
        # <media>_nni_summary.json filename and extract n_clicks /
        # union_voxels / stop_reason.
        new_fields = _summarise_run(Path(out_dir), media_id)
        # Drop the stale "summary_missing" / "summary_path" flags and
        # replace with the actual extracted ones so the dashboard
        # sees real numbers.
        for stale in ("summary_missing", "summary_searched",
                      "summary_path", "summary_error"):
            row.pop(stale, None)
        row.update(new_fields)
        if row.get("n_clicks") is not None:
            updated += 1

    tmp = path.with_suffix(path.suffix + ".bak")
    path.replace(tmp)
    try:
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        # Restore on failure.
        tmp.replace(path)
        raise
    else:
        log.info("backfilled %d/%d rows; original kept at %s",
                 updated, len(rows), tmp)
    return (updated, len(rows))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir",
                   default=str(REPO_ROOT / "paper_artifacts" / "sweep"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    state_dir = Path(args.state_dir).resolve()
    updated, total = _backfill(state_dir / "sweep_results.jsonl")
    print(f"backfilled {updated}/{total} rows in {state_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
