# Project 358382 offline replay tier

The pilot orchestrator (`.github/scripts/eval_project358382_pilot.py`) can run
end-to-end **without MorphoSource downloads or a live Jetstream GPU** by
combining three pieces:

1. **Cached specimen manifest** — `Tests/fixtures/jetstream_replay/cached_specimens.json`
   pins mesh-side media IDs and uses synthetic CT placeholders.
2. **Synthetic staging** — `metadata_to_morphsource.jetstream_replay.build_replay_bundle`
   writes tiny CT/mesh/GT volumes under `<out_dir>/specimens/<slug>/`.
3. **HTTP replay** — recorded JSONL transcripts in `<out_dir>/sessions/` are
   served via `JETSTREAM_REPLAY` when `--replay-from` is set.

## Quick smoke (local)

```bash
# Tier 1 always runs; tier 4 is opt-in:
RUN_REPLAY=1 Tests/test_eval_project358382.sh

# Or via Makefile:
make test-eval-replay
```

Requires the nnInteractive venv Python (`~/.autoresearchclaw/nninteractive/bin/python`)
with SimpleITK and VTK installed.

## Manual replay run

```bash
MANIFEST=Tests/fixtures/jetstream_replay/cached_specimens.json
OUT=runs/replay_smoke

python -m metadata_to_morphsource.jetstream_replay.build_replay_bundle \
  --manifest "$MANIFEST" \
  --out-dir "$OUT" \
  --use-existing-sessions Tests/fixtures/jetstream_replay/sessions

export SLICER_WEBSERVER_URL=http://127.0.0.1:2016/
python .github/scripts/eval_project358382_pilot.py \
  --project-id 000358382 \
  --cached-specimens "$MANIFEST" \
  --replay-from "$OUT/sessions" \
  --specimens 1 \
  --budgets 10 \
  --no-screenshots \
  --out-dir "$OUT"
```

`--cached-specimens` implies `--no-download`.

## Recording fixtures from a mock or live server

**Mock (no GPU):** drives the orchestrator against an in-process mock Slicer
Web Server and writes JSONL under `Tests/fixtures/jetstream_replay/sessions/`:

```bash
python -m metadata_to_morphsource.jetstream_replay.record_fixtures
```

**Live Jetstream:** set `JETSTREAM_RECORD=<path>.jsonl` or use
`--record-to` on the orchestrator while pointed at a real instance. See
[JETSTREAM_UNSHELVE.md](JETSTREAM_UNSHELVE.md) for bringing the box online.

## CI

The workflow `.github/workflows/eval_project358382_replay.yml` runs
`RUN_REPLAY=1 Tests/test_eval_project358382.sh` on every pull request.
No secrets or GPU runners are required.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `JETSTREAM_REPLAY` | Path to JSONL fixture; set automatically by `--replay-from` |
| `JETSTREAM_RECORD` | Append HTTP transcripts while hitting a real/mock server |
| `SLICER_WEBSERVER_URL` | Base URL validated at startup (dummy URL is fine offline) |

## Pilot manifest (live runs)

For live evaluation against real CT/mesh pairs, use
`Tests/fixtures/project358382_pilot3.json` with `--specimens-manifest`:

```bash
python .github/scripts/eval_project358382_pilot.py \
  --specimens-manifest Tests/fixtures/project358382_pilot3.json \
  --specimens 3 \
  --out-dir runs/pilot_live
```

See [ITERATIVE_SEGMENTATION.md](ITERATIVE_SEGMENTATION.md) for the broader
segmentation pipeline.
