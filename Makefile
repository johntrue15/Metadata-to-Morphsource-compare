# Makefile for Metadata-to-Morphsource-Compare
# Provides convenient shortcuts for common development tasks

.PHONY: help install install-dev test test-cov lint format clean pre-commit all

# Default target - show help
help:
	@echo "Metadata-to-Morphsource-Compare - Development Commands"
	@echo ""
	@echo "Available targets:"
	@echo "  make install       - Install production dependencies"
	@echo "  make install-dev   - Install development dependencies"
	@echo "  make test          - Run tests"
	@echo "  make test-cov      - Run tests with coverage report"
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
