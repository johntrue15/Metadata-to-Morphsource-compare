# Project 358382 offline replay tier

Compute planning: [RUNNER_TOPOLOGY.md](RUNNER_TOPOLOGY.md).

> **Architecture in one breath.** The Mac mini is a *driver*. Live
> Slicer + nnInteractive runs on **Jetstream2** (heavy) or the
> **DellXPS-wsl-gpu** self-hosted runner (lighter GPU work). The
> *offline* path described in this doc runs on stock `python3` —
> on the Mac mini, on `ubuntu-latest` in CI, or anywhere else with
> a working Python 3.9+. No SimpleITK / vtk / numpy required.

## Compute matrix

| Mode                              | Where it runs                              | Heavy deps?           | What it asserts                                                                                       |
| --------------------------------- | ------------------------------------------ | --------------------- | ----------------------------------------------------------------------------------------------------- |
| `make test-eval-replay-offline`   | Mac mini, ubuntu-latest, anything with Python 3.9+ | No (stdlib + optional pytest) | Driver surface + JSONL replay roundtrip + committed fixtures parse cleanly                           |
| `make test-eval-replay-full`      | ubuntu-latest, Dell GPU runner             | Yes (SimpleITK, vtk, numpy) | End-to-end pilot replay against committed fixtures                                                    |
| `make test-eval-record-mock`      | ubuntu-latest, Dell GPU runner             | Yes (SimpleITK, vtk, numpy) | Regenerates committed JSONL fixtures via the in-process `mock_slicer_server`                          |
| `make test-eval-jetstream`        | Jetstream2 self-hosted runner              | Yes (live Slicer + nnI server) | Full live pilot                                                                                       |
| `make test-eval-dellgpu`          | DellXPS-wsl-gpu self-hosted runner         | Yes (CUDA + Slicer)    | Lighter pilot variant (mesh-only morphometrics, smaller models)                                       |

## How the offline path works

The pilot orchestrator (`.github/scripts/eval_project358382_pilot.py`) can run
end-to-end **without MorphoSource downloads or a live Jetstream GPU** by
combining three pieces:

1. **Cached specimen manifest** — `Tests/fixtures/jetstream_replay/cached_specimens.json`
   pins mesh-side media IDs and uses synthetic CT placeholders.
2. **Synthetic staging** — `metadata_to_morphsource.jetstream_replay.build_replay_bundle`
   writes tiny CT/mesh/GT volumes under `<out_dir>/specimens/<slug>/`.
   *(Requires SimpleITK + numpy; only runs in heavy modes.)*
3. **HTTP replay** — committed JSONL transcripts in
   `Tests/fixtures/jetstream_replay/sessions/` are served via
   `JETSTREAM_REPLAY` when `--replay-from` is set. *(Driver-only.)*

The driver-only mode validates pieces 1 and 3 without touching
piece 2, so it runs anywhere Python runs.

## Driver-only smoke (Mac mini)

```bash
make test-eval-replay-offline
# or, equivalently, raw:
NNI_PY_BIN=/nonexistent NNINTERACTIVE_HOME=/nonexistent \
    RUN_REPLAY=1 Tests/test_eval_project358382.sh
```

Sets the script's "no nninteractive venv available" path explicitly,
runs Tier 1 (parse + import + recipe compile), then Tier 4
(driver-only replay). Total ~1s on a warm cache.

## Heavy replay (CI / Dell GPU)

```bash
make test-eval-replay-full
# or:
RUN_REPLAY=1 RUN_FULL_REPLAY=1 Tests/test_eval_project358382.sh
```

Drives the full pilot end-to-end against the committed fixtures. The
shell script will refuse to run this tier if no SimpleITK/numpy/vtk
interpreter is on the box (`NNI_PY_BIN`).

The CI counterpart is `.github/workflows/eval_project358382_replay.yml`
job `full-replay`.

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

## Recording fixtures

### From the in-process mock (preferred for CI)

```bash
make test-eval-record-mock
# or:
python -m metadata_to_morphsource.jetstream_replay.record_fixtures
```

Drives the orchestrator against `mock_slicer_server` (an in-process
HTTP server that fakes Slicer's `/slicer/exec` API with deterministic
canned responses) and writes JSONL under
`Tests/fixtures/jetstream_replay/sessions/`. Fully hermetic — no
secrets, no Jetstream, no MorphoSource. The committed fixtures
*(3 files × 20 calls each)* were generated this way.

CI: `.github/workflows/eval_project358382_record_mock.yml`.

### From live Jetstream (paper-grade fixtures)

Either:

- Trigger `Actions → Project 358382 pilot — Jetstream2 runner`
  with `record_fixtures: true` (workflow opens a PR with the
  refreshed JSONLs), or
- Set `JETSTREAM_RECORD=<path>.jsonl` / pass `--record-to <dir>` on
  the orchestrator while pointed at a live Slicer Web Server. See
  [JETSTREAM_UNSHELVE.md](JETSTREAM_UNSHELVE.md).

## CI workflows

| Workflow                                    | Runner            | Trigger                                                    | What it asserts                                                |
| ------------------------------------------- | ----------------- | ---------------------------------------------------------- | -------------------------------------------------------------- |
| `eval_project358382_replay.yml`             | `ubuntu-latest`   | every PR touching the pilot or replay package              | Driver-only Tier 1+4 + heavy Tier 4-FULL                       |
| `eval_project358382_record_mock.yml`        | `ubuntu-latest`   | manual / on changes to `mock_slicer_server` and friends    | Refreshes committed fixtures; uploads as artefact / opens PR   |
| `eval_project358382_dellgpu.yml`            | `[self-hosted, gpu, nninteractive]` | `workflow_dispatch`                            | Light-GPU live pilot variant                                   |
| `eval_project358382_jetstream.yml`          | `[self-hosted, jetstream]`          | `workflow_dispatch`                            | Heavy live pilot (full Slicer + nnI server)                    |

## Environment variables

| Variable               | Purpose                                                                                         |
| ---------------------- | ----------------------------------------------------------------------------------------------- |
| `JETSTREAM_REPLAY`     | Path to JSONL fixture; set automatically per-specimen by `--replay-from`                        |
| `JETSTREAM_RECORD`     | Append HTTP transcripts while hitting a real/mock server                                        |
| `SLICER_WEBSERVER_URL` | Base URL validated at startup (a dummy URL is fine in replay mode)                              |
| `NNI_PY_BIN`           | Path to the nnInteractive venv Python; smoke script falls back to `python3` when absent         |
| `NNINTERACTIVE_HOME`   | Same fallback semantics; useful for forcing the smoke script onto the driver-only path          |

## Pilot manifest (live runs)

For live evaluation against real CT/mesh pairs (Jetstream / Dell GPU),
use `Tests/fixtures/project358382_pilot3.json` with `--specimens-manifest`:

```bash
python .github/scripts/eval_project358382_pilot.py \
  --specimens-manifest Tests/fixtures/project358382_pilot3.json \
  --specimens 3 \
  --out-dir runs/pilot_live
```

See [ITERATIVE_SEGMENTATION.md](ITERATIVE_SEGMENTATION.md) for the broader
segmentation pipeline and [RUNNER_TOPOLOGY.md](RUNNER_TOPOLOGY.md) for the
driver / tool split.
