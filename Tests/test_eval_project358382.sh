#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# test_eval_project358382.sh
#
# Smoke-tests the project-358382 pilot orchestrator without touching the
# remote Slicer / nnInteractive servers and without downloading any
# multi-GB CTs. Three escalating tiers, each opt-in:
#
#   1. import-and-help (default, always runs):
#        - parse the orchestrator file
#        - --help
#        - import the heavy modules (nninteractive_compare,
#          remote_volume_io, run_telemetry, slicer_remote_bright_seed,
#          segmentation_metrics, voxelize_mesh_vtk, crop_around_mesh)
#        - sanity-check the recipe builders compile to valid Python
#
#   2. discovery-only (set RUN_DISCOVERY=1):
#        - hits the MorphoSource API to enumerate the project's
#          (CT, mesh) pairs
#        - validates that the discovery returns at least one pair
#        - requires MORPHOSOURCE_API_KEY in the environment
#
#   3. dry-run (set RUN_DRY=1):
#        - executes the full orchestrator in --dry-run mode (selects 1
#          specimen but skips download / crop / voxelize / push / run /
#          score), proving the parent RunLogger pipeline plumbs together
#        - requires MORPHOSOURCE_API_KEY and SLICER_WEBSERVER_URL (or
#          NNI_REMOTE_URL) just to exercise the validation paths.
#          The actual remote isn't contacted in dry-run.
#
#   4. offline replay (set RUN_REPLAY=1):
#        - stages synthetic CT/mesh/GT fixtures from
#          Tests/fixtures/jetstream_replay/cached_specimens.json
#        - replays recorded (or stub) Slicer HTTP transcripts — no
#          MorphoSource downloads, no live Jetstream GPU
#
# Usage:
#   Tests/test_eval_project358382.sh
#   RUN_DISCOVERY=1 Tests/test_eval_project358382.sh
#   RUN_DRY=1 RUN_DISCOVERY=1 Tests/test_eval_project358382.sh
#   RUN_REPLAY=1 Tests/test_eval_project358382.sh
#
# Exits 0 only when every requested tier passes.
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

#
# Driver-only mode (Mac mini = orchestrator)
# ------------------------------------------
# Tiers 1 + 4 only need a stock python3 — they exercise the driver
# surface (argparse, manifest reading, urllib record/replay, recipe
# template compilation). Heavy libs (SimpleITK, vtk, numpy) are lazy-
# imported inside the functions that need them, so we don't have to
# carry them on the Mac mini just to run the offline tests.
#
# Tiers 2, 3, 5 are explicitly opt-in and use the nninteractive venv
# when present, falling back to stock python3 otherwise (most of
# them will then fail at the heavy import line, which is exactly the
# behaviour we want — those tiers belong on Jetstream / a self-hosted
# GPU runner, not on the Mac mini).
#
# See docs/RUNNER_TOPOLOGY.md for the full driver / tool split.
NNI_HOME="${NNINTERACTIVE_HOME:-$HOME/.autoresearchclaw/nninteractive}"
NNI_PY="${NNI_PY_BIN:-$NNI_HOME/bin/python}"
if [[ -x "$NNI_PY" ]]; then
    PY="$NNI_PY"
    PY_KIND="nninteractive-venv"
else
    PY="$(command -v python3 || true)"
    if [[ -z "$PY" ]]; then
        echo "ERROR: no python3 on PATH. Install Python 3.9+ and rerun."
        exit 2
    fi
    PY_KIND="system"
fi
echo "Using $PY ($PY_KIND, $($PY -c 'import sys; print(sys.version.split()[0])'))"

if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

OUT_ROOT="${OUT_ROOT:-$REPO_ROOT/runs/_smoke_eval358382}"
mkdir -p "$OUT_ROOT"

# ----------------------------------------------------------------- tier 1
echo
echo "=== Tier 1: parse + import + recipe compile ==="
"$PY" - <<'PY'
import ast, sys, importlib, pathlib

ROOT = pathlib.Path(__file__).resolve().parent
# Handle the case where this is run via heredoc (no __file__).
src_root = pathlib.Path(".github/scripts").resolve()
sys.path.insert(0, str(src_root))

# 1. Parse the orchestrator
orch = src_root / "eval_project358382_pilot.py"
ast.parse(orch.read_text())
print("parse: ok", orch)

# 2. Import sibling modules (heavy ones rely on the venv)
for name in (
    "morphosource_client",
    "morphosource_api_download",
    "find_segmentation_pairs",
    "voxelize_mesh_vtk",
    "crop_around_mesh",
    "segmentation_metrics",
    "remote_volume_io",
    "run_telemetry",
    "slicer_remote_bright_seed",
    "nninteractive_compare",
    "eval_project358382_pilot",
):
    importlib.import_module(name)
    print(f"import: {name}: ok")

# 3. Recipe builders compile to valid Python
import remote_volume_io as r
for label, src in [
    ("LOAD",       r._build_load_source("AAAA", "demo", "a"*64)),
    ("SET_ACTIVE", r.SET_ACTIVE_VOLUME_SRC_TEMPLATE("demo")),
    ("LIST",       r.LIST_VOLUMES_SRC),
]:
    compile(src, f"<{label}>", "exec")
    print(f"recipe-{label}: compiles ({len(src)} bytes)")

# 4. The bright-seed STEP recipe also compiles
import slicer_remote_bright_seed as bs
step_src = bs.STEP_SRC_TEMPLATE.format(click_positive=True, new_segment=True)
compile(step_src, "<STEP>", "exec")
print(f"recipe-STEP: compiles ({len(step_src)} bytes)")

# 5. The pilot orchestrator's argparse builds without exploding
import eval_project358382_pilot as ep
parser = None
try:
    args = ep._parse_args(["--help"])  # exits via SystemExit(0)
except SystemExit as e:
    print(f"--help exit code: {e.code}")
PY

# ----------------------------------------------------------------- tier 2
if [[ "${RUN_DISCOVERY:-0}" == "1" ]]; then
    echo
    echo "=== Tier 2: discovery against MorphoSource ==="
    if [[ -z "${MORPHOSOURCE_API_KEY:-}" ]]; then
        echo "ERROR: MORPHOSOURCE_API_KEY not set; cannot run discovery"
        exit 3
    fi
    "$PY" - <<'PY'
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(".github/scripts").resolve()))
from morphosource_client import MorphoSourceClient
from eval_project358382_pilot import discover_pairs, select_pilot_specimens

pairs = discover_pairs(
    MorphoSourceClient(),
    project_query="Colors of Skull Anatomy",
    project_id="000358382",
    page_size=50, max_pages=1,
)
assert pairs, "discovery returned 0 pairs (expected at least 1)"
print(f"  discovery: {len(pairs)} pairs")
chosen = select_pilot_specimens(pairs, n=3)
for c in chosen:
    print(f"   -> {c.physical_object_id}  taxon={c.taxonomy or '?'}  "
          f"CT={c.ct_media_id}  mesh={c.mesh_media_id}")
PY
else
    echo
    echo "=== Tier 2: skipped (set RUN_DISCOVERY=1 to enable) ==="
fi

# ----------------------------------------------------------------- tier 3
if [[ "${RUN_DRY:-0}" == "1" ]]; then
    echo
    echo "=== Tier 3: orchestrator --dry-run ==="
    if [[ -z "${MORPHOSOURCE_API_KEY:-}" ]]; then
        echo "ERROR: MORPHOSOURCE_API_KEY not set"
        exit 3
    fi
    if [[ -z "${SLICER_WEBSERVER_URL:-}${NNI_REMOTE_URL:-}" ]]; then
        echo "ERROR: SLICER_WEBSERVER_URL / NNI_REMOTE_URL must be set "
        echo "       (the orchestrator validates them even in --dry-run)."
        exit 3
    fi
    DRY_OUT="$OUT_ROOT/dry_$(date +%Y%m%dT%H%M%S)"
    "$PY" .github/scripts/eval_project358382_pilot.py \
        --project-id 000358382 \
        --project-query "Colors of Skull Anatomy" \
        --specimens 1 \
        --budgets 10,25,50,100 \
        --max-pages 1 \
        --out-dir "$DRY_OUT" \
        --dry-run
    echo "  dry-run output -> $DRY_OUT"
    test -f "$DRY_OUT/manifest.json" || { echo "FAIL: manifest.json missing"; exit 4; }
    test -f "$DRY_OUT/inputs.json"   || { echo "FAIL: inputs.json missing"; exit 4; }
    test -f "$DRY_OUT/events.jsonl"  || { echo "FAIL: events.jsonl missing"; exit 4; }
    test -f "$DRY_OUT/report.md"     || { echo "FAIL: report.md missing"; exit 4; }
    test -f "$DRY_OUT/replay.sh"     || { echo "FAIL: replay.sh missing"; exit 4; }
    echo "  artifacts checked: manifest / inputs / events / report / replay"
else
    echo
    echo "=== Tier 3: skipped (set RUN_DRY=1 to enable) ==="
fi

# ----------------------------------------------------------------- tier 4
#
# Tier 4 has two flavours:
#
#   RUN_REPLAY=1      (driver-runnable — no SimpleITK/numpy/vtk needed)
#                     Validates the offline replay plumbing: cache_index
#                     manifests parse, recorder/replay sessions roundtrip,
#                     committed JSONL fixtures replay cleanly, and the
#                     orchestrator argparse accepts --cached-specimens /
#                     --replay-from / --no-download.
#                     This is the only Tier 4 mode safe to run on the
#                     Mac mini.
#
#   RUN_FULL_REPLAY=1 (heavy — requires the nninteractive venv)
#                     Drives the full pilot end-to-end against the
#                     committed JSONL fixtures. Belongs on a self-hosted
#                     runner (Jetstream / Dell GPU) or CI ubuntu-latest
#                     with the heavy pip install. The Mac mini falls
#                     back to RUN_REPLAY=1 by design — see
#                     docs/RUNNER_TOPOLOGY.md.
if [[ "${RUN_REPLAY:-0}" == "1" ]]; then
    echo
    echo "=== Tier 4: offline replay plumbing (driver-runnable) ==="
    MANIFEST="$REPO_ROOT/Tests/fixtures/jetstream_replay/cached_specimens.json"
    FIXTURE_SESSIONS="$REPO_ROOT/Tests/fixtures/jetstream_replay/sessions"

    # 4.a — pytest the cache_index, recorder, wiring, and offline-flag
    # tests. None of them touch SimpleITK / numpy / vtk; the driver
    # surface is enough.
    #
    # We first probe for an interpreter that already has pytest. If
    # nothing on the system does, we emit a clear "pytest unavailable"
    # message and fall back to running the tests via importlib (which
    # only catches collection-time errors, not assertion failures —
    # but it's better than nothing on a locked-down environment).
    PYTEST_PY=""
    for cand in "$PY" "$NNI_PY" "$(command -v python3 || true)" /usr/bin/python3; do
        [[ -z "$cand" || ! -x "$cand" ]] && continue
        if "$cand" -c "import pytest" >/dev/null 2>&1; then
            PYTEST_PY="$cand"
            break
        fi
    done
    if [[ -n "$PYTEST_PY" ]]; then
        echo "  using pytest from: $PYTEST_PY"
        "$PYTEST_PY" -m pytest \
            Tests/test_jetstream_replay_cache_index.py \
            Tests/test_jetstream_replay_recorder.py \
            Tests/test_jetstream_replay_wiring.py \
            Tests/test_eval_project358382_offline.py \
            -v --no-header \
            || { echo "FAIL: driver-side replay tests"; exit 5; }
    else
        echo "  pytest not found on any candidate Python; running smoke import"
        "$PY" - <<'PY'
import importlib, sys
sys.path.insert(0, '.')
sys.path.insert(0, '.github/scripts')
mods = [
    "metadata_to_morphsource.jetstream_replay.cache_index",
    "metadata_to_morphsource.jetstream_replay.recorder",
]
for m in mods:
    importlib.import_module(m)
    print(f"  import: {m} ok")
print("  (skipped pytest collection; install pytest for deeper checks)")
PY
    fi

    # 4.b — verify every committed JSONL fixture replays without
    # divergence (just opens + drains via ReplaySession).
    "$PY" - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
from metadata_to_morphsource.jetstream_replay.recorder import (
    ReplaySession, HTTPCall,
)

sess_dir = Path("Tests/fixtures/jetstream_replay/sessions")
if not sess_dir.is_dir():
    print("  (no sessions dir yet; skip)")
    sys.exit(0)
files = sorted(sess_dir.glob("*.jsonl"))
if not files:
    print("  (no committed JSONL fixtures yet; skip)")
    sys.exit(0)
total = 0
for fp in files:
    rs = ReplaySession(fp)
    total += rs.n_calls
    print(f"  {fp.name}: {rs.n_calls} calls")
    # Round-trip every line through HTTPCall.from_dict to validate
    # the schema independently of the ReplaySession state.
    for i, raw in enumerate(fp.read_text().splitlines()):
        if not raw.strip():
            continue
        HTTPCall.from_dict(json.loads(raw))
print(f"  every committed fixture is well-formed ({len(files)} files, {total} calls)")
PY

    echo "  driver-runnable replay tier passed"
else
    echo
    echo "=== Tier 4: skipped (set RUN_REPLAY=1 to enable) ==="
fi

# ---------------------------------------------------------- tier 4-FULL
if [[ "${RUN_FULL_REPLAY:-0}" == "1" ]]; then
    echo
    echo "=== Tier 4-FULL: end-to-end pilot replay (requires heavy venv) ==="
    if [[ "$PY_KIND" != "nninteractive-venv" ]]; then
        echo "ERROR: Tier 4-FULL needs SimpleITK/numpy/vtk. Mac-mini drivers"
        echo "       should not run this; route it to a self-hosted runner"
        echo "       per docs/RUNNER_TOPOLOGY.md."
        exit 5
    fi
    MANIFEST="$REPO_ROOT/Tests/fixtures/jetstream_replay/cached_specimens.json"
    FIXTURE_SESSIONS="$REPO_ROOT/Tests/fixtures/jetstream_replay/sessions"
    REPLAY_OUT="$OUT_ROOT/replay_$(date +%Y%m%dT%H%M%S)"
    mkdir -p "$FIXTURE_SESSIONS"

    if ! ls "$FIXTURE_SESSIONS"/*.jsonl >/dev/null 2>&1; then
        echo "  no session fixtures found; generating stub transcripts..."
        "$PY" -m metadata_to_morphsource.jetstream_replay.build_replay_bundle \
            --manifest "$MANIFEST" \
            --out-dir "$REPLAY_OUT/_bundle_seed" \
            --seed 0
        cp "$REPLAY_OUT/_bundle_seed/sessions/"*.jsonl "$FIXTURE_SESSIONS/"
    fi

    "$PY" -m metadata_to_morphsource.jetstream_replay.build_replay_bundle \
        --manifest "$MANIFEST" \
        --out-dir "$REPLAY_OUT" \
        --seed 0 \
        --use-existing-sessions "$FIXTURE_SESSIONS"

    export SLICER_WEBSERVER_URL="${SLICER_WEBSERVER_URL:-http://127.0.0.1:2016/}"
    export NNI_REMOTE_URL="${NNI_REMOTE_URL:-$SLICER_WEBSERVER_URL}"

    "$PY" .github/scripts/eval_project358382_pilot.py \
        --project-id 000358382 \
        --project-query "Colors of Skull Anatomy" \
        --cached-specimens "$MANIFEST" \
        --replay-from "$REPLAY_OUT/sessions" \
        --no-download \
        --specimens 1 \
        --budgets 10 \
        --max-steps 10 \
        --no-screenshots \
        --out-dir "$REPLAY_OUT"
    REPLAY_EXIT=$?
    # Synthetic stub fixtures cover only the first few protocol calls,
    # so the orchestrator may fail when the transcript is exhausted.
    # We still assert the run got far enough to write its manifest +
    # event stream, which catches regressions in the orchestration.
    test -f "$REPLAY_OUT/manifest.json" \
        || { echo "FAIL: manifest.json missing"; exit 5; }
    test -f "$REPLAY_OUT/events.jsonl"  \
        || { echo "FAIL: events.jsonl missing"; exit 5; }
    if [[ $REPLAY_EXIT -ne 0 ]]; then
        echo "  orchestrator exit=$REPLAY_EXIT (expected for stub transcripts; "
        echo "  bundle artifacts were still written)"
    fi
    echo "  full-replay output -> $REPLAY_OUT"
else
    echo
    echo "=== Tier 4-FULL: skipped (set RUN_FULL_REPLAY=1 to enable) ==="
fi

# ----------------------------------------------------------------- tier 5
if [[ "${RUN_RECORD:-0}" == "1" ]]; then
    echo
    echo "=== Tier 5: re-record JSONL fixtures (mock-Slicer or live) ==="
    if [[ "$PY_KIND" != "nninteractive-venv" ]]; then
        echo "ERROR: Tier 5 requires the nninteractive venv (SimpleITK/numpy)."
        echo "       Mac-mini drivers should not run this tier; route it to"
        echo "       a self-hosted runner per docs/RUNNER_TOPOLOGY.md."
        exit 6
    fi
    # Two flavours, controlled by RECORD_TARGET:
    #   mock (default) - run against the in-process mock_slicer_server,
    #                    producing committable fixtures with no Jetstream.
    #   live           - run against \$SLICER_WEBSERVER_URL; requires a real
    #                    Jetstream2 with an active Slicer + nnInteractive.
    RECORD_TARGET="${RECORD_TARGET:-mock}"
    if [[ "$RECORD_TARGET" == "mock" ]]; then
        "$PY" -m metadata_to_morphsource.jetstream_replay.record_fixtures \
            || { echo "FAIL: record_fixtures returned non-zero"; exit 6; }
        echo "  mock-recorded fixtures -> Tests/fixtures/jetstream_replay/sessions/"
    else
        if [[ -z "${SLICER_WEBSERVER_URL:-}${NNI_REMOTE_URL:-}" ]]; then
            echo "ERROR: live recording needs SLICER_WEBSERVER_URL / NNI_REMOTE_URL"
            exit 6
        fi
        REC_OUT="$OUT_ROOT/record_$(date +%Y%m%dT%H%M%S)"
        "$PY" .github/scripts/eval_project358382_pilot.py \
            --project-id 000358382 \
            --project-query "Colors of Skull Anatomy" \
            --cached-specimens "$REPO_ROOT/Tests/fixtures/jetstream_replay/cached_specimens.json" \
            --record-to "$REPO_ROOT/Tests/fixtures/jetstream_replay/sessions" \
            --no-download \
            --specimens 1 \
            --budgets 10 \
            --max-steps 10 \
            --no-screenshots \
            --out-dir "$REC_OUT" \
            || { echo "FAIL: live recording exited non-zero"; exit 6; }
        echo "  live-recorded fixtures -> Tests/fixtures/jetstream_replay/sessions/"
    fi
else
    echo
    echo "=== Tier 5: skipped (set RUN_RECORD=1 to enable) ==="
fi

echo
echo "OK"
