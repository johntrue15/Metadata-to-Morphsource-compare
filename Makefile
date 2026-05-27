# Makefile for Metadata-to-Morphsource-Compare
# Provides convenient shortcuts for common development tasks

.PHONY: help install install-dev test test-cov test-seg-train test-seg-train-full test-seg-train-live nni-smoke lint format clean pre-commit all unshelve pilot-dell pilot-jetstream tail test-eval358382 test-eval-replay test-eval-replay-offline test-eval-replay-full test-eval-record-mock test-eval-jetstream test-eval-dellgpu build-replay-fixtures unshelve-dry-run ecu-install ecu-health stage-sample stage-sample-ct stage-sample-gt stage-sample-debug stage-colors-skull-ct stage-colors-skull-gt jetstream-10click-smoke colors-skull-fresh-start colors-skull-complete colors-skull-gt-labelmap tuatara-gt-labelmap tuatara-gt-guided-train tuatara-fresh-start tuatara-click-to-completion tuatara-score-100click tuatara-autocomplete-vs-gt

# Default target - show help
help:
	@echo "Metadata-to-Morphsource-Compare - Development Commands"
	@echo ""
	@echo "Available targets:"
	@echo "  make install       - Install production dependencies"
	@echo "  make install-dev   - Install development dependencies"
	@echo "  make test          - Run tests"
	@echo "  make test-cov      - Run tests with coverage report"
	@echo "  make test-seg-train      - Run iterative segmentation smoke tests"
	@echo "  make test-seg-train-full - Same as above, including numpy-marked tests"
	@echo "  make test-seg-train-live - Real end-to-end on chameleon stapes (~10 min)"
	@echo "  make nni-smoke     - Cached-fixture smoke for nninteractive_compare.py (~2s, no GPU)"
	@echo "  make unshelve IP=A.B.C.D - Reattach to an unshelved Jetstream2 instance"
	@echo "  make ecu-install IP=...   - One-line ECU install over SSH (needs key auth)"
	@echo "  make ecu-health           - Probe MorphoClaw ECU /health (uses .env)"
	@echo "                            (rewrites .env URLs, restarts nninteractive-slicer-server"
	@echo "                             over SSH, probes :1527, :2016, :18765). See docs/JETSTREAM_UNSHELVE.md."
	@echo "  Compute plan: docs/RUNNER_TOPOLOGY.md (Mac=driver, Jetstream=heavy, Dell=light GPU)"
	@echo "  make pilot-dell  [SPECIMENS=3] [BUDGETS=10,25,50,100] [MANIFEST=...] [RECORD_TO=...]"
	@echo "                            - Dispatch the project-358382 pilot on the Dell GPU runner"
	@echo "                              via gh workflow run. ** Use this instead of running"
	@echo "                              eval_project358382_pilot.py in a local Mac terminal. **"
	@echo "  make pilot-jetstream [SPECIMENS=3] [BUDGETS=10,25,50,100] [MANIFEST=...] [RECORD_TO=...]"
	@echo "                            - Same, on Jetstream2 (needs the JS2 GHA runner registered)"
	@echo "  make tail [RUN_ID=N] [WORKFLOW=name.yml]  - Watch a GH Actions run live (gh run watch)"
	@echo "  make unshelve-dry-run IP=... - Print unshelve steps without SSH or .env writes"
	@echo "  make test-eval358382          - Tier-1 smoke for eval_project358382_pilot.py"
	@echo "  make test-eval-replay-offline - Driver-only replay (RUN_REPLAY=1, no SimpleITK/numpy/vtk)"
	@echo "  make test-eval-replay-full    - Heavy replay (RUN_FULL_REPLAY=1, needs nninteractive venv)"
	@echo "  make test-eval-replay         - Alias for test-eval-replay-offline"
	@echo "  make test-eval-record-mock    - Regenerate JSONL fixtures via mock Slicer"
	@echo "  make test-eval-jetstream      - Dispatch the Jetstream2 self-hosted pilot workflow"
	@echo "  make test-eval-dellgpu        - Dispatch the Dell GPU self-hosted pilot workflow"
	@echo "  make build-replay-fixtures    - Record mock Slicer JSONL into Tests/fixtures/"
	@echo "  make stage-sample-ct   - Mac-mini-safe: download CT TIFFs + write the"
	@echo "                            cropped, downsampled CT NRRD into data/sample/."
	@echo "                            No voxelization. ~30 s + download time."
	@echo "  make stage-sample-gt   - Dell/Jetstream only: voxelize the mesh onto the"
	@echo "                            staged CT NRRD's grid. Refuses to run on macOS"
	@echo "                            without MORPHOCLAW_FORCE_MAC_VOXELIZE=1."
	@echo "  make stage-sample      - Full CT + GT pipeline in one process. GPU host"
	@echo "                            only (Dell or Jetstream). On the Mac, prefer"
	@echo "                            stage-sample-ct, then run stage-sample-gt remotely."
	@echo "  make stage-sample-debug - Same as stage-sample but with vtk_stencil backend"
	@echo "                             and aggressive decimation (for comparison)."
	@echo "  make stage-colors-skull-ct - Colors of Skull (Crotalus 000445108) CT NRRD"
	@echo "  make stage-colors-skull-gt - Colors of Skull GT labelmap (GPU host only)"
	@echo "  make jetstream-10click-smoke - On Jetstream: load GitHub CT URL + 10 clicks"
	@echo "  make colors-skull-fresh-start - Crotalus clean slate (Mac → Jetstream)"
	@echo "  make colors-skull-complete    - Crotalus load + 100-click + Dice vs GT"
	@echo "  make colors-skull-gt-labelmap - Voxelize Crotalus mesh → GT labelmap"
	@echo "  make tuatara-fresh-start      - Tuatara clean slate on Jetstream"
	@echo "  make tuatara-click-to-completion - Continue tuatara clicks (no reset)"
	@echo "  make tuatara-score-100click   - Export live tuatara scene + Dice vs GT"
	@echo "  make lint          - Run linting checks (flake8, mypy, bandit)"
	@echo "  make format        - Format code (black, isort)"
	@echo "  make format-check  - Check code formatting without changes"
	@echo "  make pre-commit    - Run pre-commit hooks on all files"
	@echo "  make clean         - Remove build artifacts and caches"
	@echo "  make all           - Run format, lint, and test"
	@echo ""

# Install production dependencies
install:
	pip install -r requirements.txt

# Install development dependencies
install-dev:
	pip install -r requirements-dev.txt
	pip install -e ".[dev]"
	pre-commit install

# Run tests
test:
	pytest tests/ -v

# Run tests with coverage
test-cov:
	pytest tests/ --cov=. --cov-report=html --cov-report=term
	@echo ""
	@echo "Coverage report generated in htmlcov/index.html"

# Run the iterative-segmentation smoke tests (skips the numpy-marked
# tests so it works on hosts with broken Anaconda numpy).
test-seg-train:
	bash Tests/smoke_seg_train.sh

# Same as test-seg-train but also runs the numpy/SimpleITK-marked tests.
test-seg-train-full:
	bash Tests/smoke_seg_train.sh --include-numpy

# Live end-to-end test on the chameleon-stapes pair: real
# MorphoSource download, real Slicer/VTK voxelisation, real
# nnInteractive paint loop. Requires MORPHOSOURCE_API_KEY,
# OPENAI_API_KEY and a bootstrapped nnInteractive venv. Takes ~5–15 min.
test-seg-train-live:
	bash Tests/test_chameleon_stapes_iterative.sh

# Cached-fixture smoke for the nnInteractive comparison pipeline. Runs the
# same code path the nninteractive_smoke.yml PR gate runs on ubuntu-latest,
# but locally against committed fixtures. ~2s, no GPU, no MorphoSource,
# no OpenAI. Use while iterating on .github/scripts/nninteractive_compare.py.
nni-smoke:
	bash Tests/smoke_nninteractive_compare.sh

# Reattach to an unshelved Jetstream2 / MorphoCloud instance after its
# public IP has changed. Rewrites .env URLs, restarts the SlicerNNInteractive
# FastAPI server on :1527 over SSH (idempotent tmux), and probes both
# :1527 (FastAPI) and :2016 (Slicer Web Server) through the Exosphere
# proxy. Usage:
#     make unshelve IP=149.165.171.178
# See docs/JETSTREAM_UNSHELVE.md for prerequisites (SSH key + persistent
# venv at /media/volume/MyData/nninteractive/).
unshelve:
	@if [ -z "$(IP)" ]; then \
		echo "Usage: make unshelve IP=<public-ip>"; \
		echo "Example: make unshelve IP=149.165.171.178"; \
		exit 2; \
	fi
	python3 .github/scripts/jetstream_unshelve_start.py \
	    --ip $(IP) --nninteractive --webserver --probe-ecu $(UNSHELVE_FLAGS)

unshelve-dry-run:
	@if [ -z "$(IP)" ]; then \
		echo "Usage: make unshelve-dry-run IP=<public-ip>"; \
		exit 2; \
	fi
	python3 .github/scripts/jetstream_unshelve_start.py \
	    --ip $(IP) --nninteractive --webserver --dry-run $(UNSHELVE_FLAGS)

# Install MorphoClaw ECU on Jetstream (clone repo + job server :18765).
ecu-install:
	@if [ -z "$(IP)" ]; then \
		echo "Usage: make ecu-install IP=<public-ip>"; \
		exit 2; \
	fi
	python3 .github/scripts/jetstream_unshelve_start.py \
	    --ip $(IP) --ecu --probe-ecu --no-env-update $(UNSHELVE_FLAGS)

ecu-health:
	@set -a && [ -f .env ] && . ./.env && set +a; \
	python3 .github/scripts/jetstream_controller.py health

test-eval358382:
	Tests/test_eval_project358382.sh

# Driver-only offline replay. Forces the smoke script down the
# system-python path so we exercise exactly what the Mac mini runs.
# No SimpleITK / numpy / vtk required. ~1s.
test-eval-replay-offline:
	RUN_REPLAY=1 NNI_PY_BIN=/nonexistent NNINTERACTIVE_HOME=/nonexistent \
	    Tests/test_eval_project358382.sh

# Backwards-compatible alias.
test-eval-replay: test-eval-replay-offline

# Full pilot replay against committed fixtures. Requires the
# nnInteractive venv (SimpleITK/numpy/vtk). Belongs on a self-hosted
# runner or CI ubuntu-latest with the heavy pip install — see
# docs/RUNNER_TOPOLOGY.md.
test-eval-replay-full:
	RUN_REPLAY=1 RUN_FULL_REPLAY=1 Tests/test_eval_project358382.sh

# Regenerate JSONL fixtures by running the orchestrator against the
# in-process mock_slicer_server. Same dep footprint as
# test-eval-replay-full (SimpleITK + numpy + vtk).
test-eval-record-mock:
	python3 -m metadata_to_morphsource.jetstream_replay.record_fixtures

# Trigger the self-hosted Jetstream2 pilot workflow via gh. Requires
# the `gh` CLI and a runner registered under the `jetstream` label.
# See docs/RUNNER_TOPOLOGY.md.
test-eval-jetstream:
	gh workflow run eval_project358382_jetstream.yml \
	    -f manifest=Tests/fixtures/jetstream_replay/cached_specimens.json \
	    -f specimens=1 -f budgets=10,25 \
	    -f slicer_url=$${SLICER_WEBSERVER_URL:-http://127.0.0.1:2016/}

# Trigger the self-hosted Dell GPU pilot workflow via gh.
test-eval-dellgpu:
	gh workflow run eval_project358382_dellgpu.yml \
	    -f manifest=Tests/fixtures/jetstream_replay/cached_specimens.json \
	    -f specimens=1 -f budgets=10,25

# ---------------------------------------------------------------------------
# Real-pilot dispatch wrappers (driver/tool doctrine: docs/RUNNER_TOPOLOGY.md)
#
# These are the *correct* way to launch the 10-click pilot from the Mac mini.
# They are deliberately distinct from `test-eval-{dellgpu,jetstream}` (which
# are 1-specimen smoke tests with hard-coded budgets) — `pilot-{dell,jetstream}`
# accept SPECIMENS / BUDGETS / MANIFEST / RECORD_TO overrides so they can
# substitute for the local-terminal `python eval_project358382_pilot.py …`
# command that the Darwin guard in that script now refuses.
#
# The intent: future you / future agents type `make pilot-dell` instead of
# pasting a `python ...` command into a Mac terminal. The script-level guard
# is the safety net; this Makefile target is the convenience layer.
# ---------------------------------------------------------------------------

# Defaults are chosen to match the 10-click pilot we keep wanting to run
# (3 specimens, full budget ladder, the cached project-358382 manifest).
PILOT_SPECIMENS  ?= 3
PILOT_BUDGETS    ?= 10,25,50,100
PILOT_MANIFEST   ?= Tests/fixtures/jetstream_replay/cached_specimens.json
PILOT_RECORD_TO  ?=

# Make-friendly aliases so callers can write `SPECIMENS=...` not `PILOT_SPECIMENS=...`.
SPECIMENS  ?= $(PILOT_SPECIMENS)
BUDGETS    ?= $(PILOT_BUDGETS)
MANIFEST   ?= $(PILOT_MANIFEST)
RECORD_TO  ?= $(PILOT_RECORD_TO)

# Internal: assemble the -f args. RECORD_TO is optional; only pass it if set.
_pilot_record_arg = $(if $(strip $(RECORD_TO)),-f record_to="$(RECORD_TO)",)

pilot-dell: ## Dispatch the project-358382 pilot on the Dell GPU runner via gh.
	@command -v gh >/dev/null 2>&1 || { \
	    echo "ERROR: 'gh' (GitHub CLI) is not installed."; \
	    echo "  Install: brew install gh && gh auth login"; \
	    exit 127; \
	}
	@echo "[pilot-dell] Dispatching eval_project358382_dellgpu.yml on [self-hosted, gpu, nninteractive]"
	@echo "[pilot-dell]   specimens : $(SPECIMENS)"
	@echo "[pilot-dell]   budgets   : $(BUDGETS)"
	@echo "[pilot-dell]   manifest  : $(MANIFEST)"
	@if [ -n "$(strip $(RECORD_TO))" ]; then echo "[pilot-dell]   record_to : $(RECORD_TO)"; fi
	gh workflow run eval_project358382_dellgpu.yml \
	    -f manifest="$(MANIFEST)" \
	    -f specimens="$(SPECIMENS)" \
	    -f budgets="$(BUDGETS)" \
	    $(_pilot_record_arg)
	@echo ""
	@echo "[pilot-dell] Dispatched. Tail with:"
	@echo "    gh run list --workflow eval_project358382_dellgpu.yml --limit 1"
	@echo "    make tail RUN_ID=<id>"

pilot-jetstream: ## Dispatch the project-358382 pilot on the JS2 runner via gh.
	@# Phase 3 of the runner-topology rollout will install a GHA agent on the
	@# JS2 box (labels: [self-hosted, jetstream]). Until that lands, the
	@# eval_project358382_jetstream.yml workflow has no runner to pick it up.
	@# We print the disclaimer + refuse by default so the operator isn't
	@# surprised; setting JETSTREAM_PILOT_FORCE=1 queues the workflow anyway
	@# (useful for testing the wf inputs themselves).
	@echo "[pilot-jetstream] WARNING: the [self-hosted, jetstream] GHA runner is"
	@echo "[pilot-jetstream] not registered yet (Phase 3 of the runner-topology"
	@echo "[pilot-jetstream] rollout — see docs/RUNNER_TOPOLOGY.md migration"
	@echo "[pilot-jetstream] checklist). The job will queue with no worker."
	@echo "[pilot-jetstream] For a result today, run: make pilot-dell"
	@echo "[pilot-jetstream]"
	@if [ -z "$(JETSTREAM_PILOT_FORCE)" ]; then \
	    echo "[pilot-jetstream] Refusing to dispatch. Set JETSTREAM_PILOT_FORCE=1"; \
	    echo "[pilot-jetstream] to queue the workflow anyway."; \
	    exit 2; \
	fi
	@command -v gh >/dev/null 2>&1 || { \
	    echo "ERROR: 'gh' (GitHub CLI) is not installed."; \
	    echo "  Install: brew install gh && gh auth login"; \
	    exit 127; \
	}
	@echo "[pilot-jetstream] JETSTREAM_PILOT_FORCE=1 set; dispatching anyway."
	gh workflow run eval_project358382_jetstream.yml \
	    -f manifest="$(MANIFEST)" \
	    -f specimens="$(SPECIMENS)" \
	    -f budgets="$(BUDGETS)" \
	    $(_pilot_record_arg)
	@echo ""
	@echo "[pilot-jetstream] Dispatched (queued; will idle until the JS2 runner is registered)."

# Watch a GitHub Actions run live. Defaults to the most recent run if RUN_ID
# is not set; pass WORKFLOW=name.yml to scope. Wraps `gh run watch` /
# `gh run list` so the operator doesn't have to remember the right invocation.
tail: ## Tail a GHA run. Usage: make tail [RUN_ID=N] [WORKFLOW=name.yml]
	@command -v gh >/dev/null 2>&1 || { \
	    echo "ERROR: 'gh' (GitHub CLI) is not installed."; \
	    exit 127; \
	}
	@if [ -n "$(RUN_ID)" ]; then \
	    echo "[tail] Watching run $(RUN_ID)…"; \
	    gh run watch "$(RUN_ID)" --exit-status; \
	elif [ -n "$(WORKFLOW)" ]; then \
	    rid=$$(gh run list --workflow "$(WORKFLOW)" --limit 1 --json databaseId -q '.[0].databaseId'); \
	    if [ -z "$$rid" ]; then \
	        echo "[tail] No runs found for workflow $(WORKFLOW)"; exit 1; \
	    fi; \
	    echo "[tail] Most recent $(WORKFLOW) run: $$rid"; \
	    gh run watch "$$rid" --exit-status; \
	else \
	    rid=$$(gh run list --limit 1 --json databaseId -q '.[0].databaseId'); \
	    if [ -z "$$rid" ]; then \
	        echo "[tail] No runs found"; exit 1; \
	    fi; \
	    echo "[tail] Most recent run: $$rid"; \
	    gh run watch "$$rid" --exit-status; \
	fi

# Build the paired CT + GT-labelmap NRRDs in data/sample/ that the
# README ships URL-loadable into Jetstream / 3D Slicer. The full
# pipeline is split into two phases that map onto the runner topology
# (see docs/RUNNER_TOPOLOGY.md):
#
#   stage-sample-ct   -- Mac-mini-friendly. Downloads MorphoSource
#                        000011009 (CT TIFF stack, ~5 GB) + 000358663
#                        (skull mesh, ~700 MB) into the gitignored
#                        data/morphosource-download-*/ cache, streams
#                        the TIFFs, crops + stride-downsamples around
#                        the mesh bbox, and writes the gzipped CT NRRD
#                        (~39 MB) + a partial provenance JSON.
#
#   stage-sample-gt   -- Dell/Jetstream-only. Reads the staged CT NRRD
#                        as the grid, applies the auto-detected
#                        signed-permutation transform to the mesh, and
#                        rasterizes onto the CT grid with the
#                        trimesh+rtree ray-caster. Refuses to run on
#                        macOS unless MORPHOCLAW_FORCE_MAC_VOXELIZE=1.
#
#   stage-sample      -- Convenience: both phases in one process. GPU
#                        host only.
#
# Requires MORPHOSOURCE_API_KEY in .env and the nnInteractive venv at
# ~/.autoresearchclaw/nninteractive (which provides trimesh, rtree,
# SimpleITK, VTK). Default voxelize backend is trimesh+rtree+rays.
PY ?= $(HOME)/.autoresearchclaw/nninteractive/bin/python
STAGE_RUN_DIR ?= runs

stage-sample-ct:
	mkdir -p $(STAGE_RUN_DIR)
	set -a; . ./.env; set +a; \
	$(PY) -u .github/scripts/stage_morphosource_sample.py \
	    --phase ct-only \
	    2>&1 | tee $(STAGE_RUN_DIR)/stage_sample_ct_$$(date +%Y%m%dT%H%M%S).log

stage-sample-gt:
	mkdir -p $(STAGE_RUN_DIR)
	set -a; . ./.env; set +a; \
	$(PY) -u .github/scripts/stage_morphosource_sample.py \
	    --phase voxelize-only \
	    2>&1 | tee $(STAGE_RUN_DIR)/stage_sample_gt_$$(date +%Y%m%dT%H%M%S).log

stage-sample:
	mkdir -p $(STAGE_RUN_DIR)
	set -a; . ./.env; set +a; \
	$(PY) -u .github/scripts/stage_morphosource_sample.py \
	    --phase all \
	    2>&1 | tee $(STAGE_RUN_DIR)/stage_sample_$$(date +%Y%m%dT%H%M%S).log

stage-sample-debug:
	mkdir -p $(STAGE_RUN_DIR)
	set -a; . ./.env; set +a; \
	$(PY) -u .github/scripts/stage_morphosource_sample.py \
	    --phase all \
	    --voxelize-backend vtk_stencil --mesh-decimate-to 100000 \
	    2>&1 | tee $(STAGE_RUN_DIR)/stage_sample_debug_$$(date +%Y%m%dT%H%M%S).log

# Colors of Skull Anatomy (project 358382) — smallest pilot3 specimen.
COLORS_SKULL_CT_MEDIA ?= 000445159
COLORS_SKULL_MESH_MEDIA ?= 000691954
COLORS_SKULL_SLUG ?= crotalus_skull_000445108

stage-colors-skull-ct:
	mkdir -p $(STAGE_RUN_DIR)
	set -a; . ./.env; set +a; \
	$(PY) -u .github/scripts/stage_morphosource_sample.py \
	    --phase ct-only \
	    --ct-media-id $(COLORS_SKULL_CT_MEDIA) \
	    --mesh-media-id $(COLORS_SKULL_MESH_MEDIA) \
	    --slug $(COLORS_SKULL_SLUG) \
	    2>&1 | tee $(STAGE_RUN_DIR)/stage_colors_skull_ct_$$(date +%Y%m%dT%H%M%S).log

stage-colors-skull-gt:
	mkdir -p $(STAGE_RUN_DIR)
	set -a; . ./.env; set +a; \
	$(PY) -u .github/scripts/stage_morphosource_sample.py \
	    --phase voxelize-only \
	    --ct-media-id $(COLORS_SKULL_CT_MEDIA) \
	    --mesh-media-id $(COLORS_SKULL_MESH_MEDIA) \
	    --slug $(COLORS_SKULL_SLUG) \
	    2>&1 | tee $(STAGE_RUN_DIR)/stage_colors_skull_gt_$$(date +%Y%m%dT%H%M%S).log

# Run on Jetstream after data/sample/crotalus_* is on main (see colors_of_skull_urls.json).
jetstream-10click-smoke:
	Tests/jetstream_colors_of_skull_10click.sh

# Colors of Skull — Crotalus pilot (Mac driver → Jetstream).
colors-skull-gt-labelmap: stage-colors-skull-gt

colors-skull-fresh-start:
	Tests/colors_skull_fresh_start.sh

colors-skull-complete:
	Tests/colors_skull_complete.sh

# Tuatara: voxelize skull .ply → GT labelmap, then GT-guided clicks on Jetstream.
tuatara-gt-labelmap:
	set -a; . ./.env; set +a; \
	MORPHOCLAW_FORCE_MAC_VOXELIZE=$${MORPHOCLAW_FORCE_MAC_VOXELIZE:-1} \
	$(PY) -u .github/scripts/stage_morphosource_sample.py \
	    --phase voxelize-only \
	    --voxelize-backend vtk_stencil \
	    --mesh-decimate-to 100000 \
	    2>&1 | tee runs/stage_tuatara_gt_vtk_$$(date +%Y%m%dT%H%M%S).log

tuatara-gt-guided-train:
	Tests/tuatara_gt_guided_train.sh

# Clean slate on Jetstream: clear scene, reload CT URL, reset, bright-seed from step 0.
tuatara-fresh-start:
	Tests/tuatara_fresh_start_jetstream.sh

# Phase A: Jetstream clicks only (no GT, no reset). Run while GT voxelizes on Mac.
tuatara-click-to-completion:
	Tests/tuatara_click_to_completion.sh

# Phase B: export live scene + Dice vs mesh GT (run after A + tuatara-gt-labelmap).
tuatara-score-100click:
	Tests/tuatara_score_100click_vs_gt.sh

# Full pipeline (or SCORE_PHASE_ONLY=1 / CLICK_PHASE_ONLY=1 to split).
tuatara-autocomplete-vs-gt:
	Tests/tuatara_autocomplete_vs_gt.sh

build-replay-fixtures:
	python3 -m metadata_to_morphsource.jetstream_replay.record_fixtures

# Run linting checks
lint:
	@echo "Running flake8..."
	flake8 .
	@echo ""
	@echo "Running mypy..."
	mypy --install-types --non-interactive --ignore-missing-imports .
	@echo ""
	@echo "Running bandit security checks..."
	bandit -r . --skip B101,B601 --exclude ./tests,./Tests

# Format code
format:
	@echo "Running black..."
	black .
	@echo ""
	@echo "Running isort..."
	isort .

# Check code formatting without making changes
format-check:
	@echo "Checking black formatting..."
	black --check .
	@echo ""
	@echo "Checking isort..."
	isort --check-only .

# Run pre-commit hooks
pre-commit:
	pre-commit run --all-files

# Run all quality checks
all: format lint test
	@echo ""
	@echo "✅ All checks passed!"

# Clean build artifacts and caches
clean:
	@echo "Cleaning build artifacts and caches..."
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	@echo "Clean complete!"

# Quick check before committing (format, lint, test)
quick: format-check lint test
	@echo ""
	@echo "✅ Quick check passed! Ready to commit."
