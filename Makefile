# Makefile for Metadata-to-Morphsource-Compare
# Provides convenient shortcuts for common development tasks

.PHONY: help install install-dev test test-cov test-seg-train test-seg-train-full test-seg-train-live nni-smoke lint format clean pre-commit all unshelve test-eval358382 test-eval-replay build-replay-fixtures unshelve-dry-run

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
	@echo "                            (rewrites .env URLs, restarts nninteractive-slicer-server"
	@echo "                             over SSH, probes :1527 and :2016). See docs/JETSTREAM_UNSHELVE.md."
	@echo "  make unshelve-dry-run IP=... - Print unshelve steps without SSH or .env writes"
	@echo "  make test-eval358382   - Tier-1 smoke for eval_project358382_pilot.py"
	@echo "  make test-eval-replay  - Tier-1 + offline replay (RUN_REPLAY=1)"
	@echo "  make build-replay-fixtures - Record mock Slicer JSONL into Tests/fixtures/"
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
	    --ip $(IP) --nninteractive --webserver $(UNSHELVE_FLAGS)

unshelve-dry-run:
	@if [ -z "$(IP)" ]; then \
		echo "Usage: make unshelve-dry-run IP=<public-ip>"; \
		exit 2; \
	fi
	python3 .github/scripts/jetstream_unshelve_start.py \
	    --ip $(IP) --nninteractive --webserver --dry-run $(UNSHELVE_FLAGS)

test-eval358382:
	Tests/test_eval_project358382.sh

test-eval-replay:
	RUN_REPLAY=1 Tests/test_eval_project358382.sh

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
