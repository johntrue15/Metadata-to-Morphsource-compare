# Development Guide

This guide covers the development workflow, tools, and best practices for contributing to Metadata-to-Morphsource-Compare.

## Table of Contents

- [Quick Start](#quick-start)
- [Development Environment](#development-environment)
- [Code Quality Tools](#code-quality-tools)
- [Testing](#testing)
- [Documentation](#documentation)
- [Release Process](#release-process)

## Quick Start

### 1. Clone and Setup

```bash
# Clone the repository
git clone https://github.com/johntrue15/Metadata-to-Morphsource-compare.git
cd Metadata-to-Morphsource-compare

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode with all dependencies
pip install -e ".[dev]"
```

### 2. Configure Environment

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env and add your API keys
# MORPHOSOURCE_API_KEY=your_key_here
# OPENAI_API_KEY=your_key_here
```

### 3. Set Up Pre-commit Hooks

```bash
# Install pre-commit hooks
pre-commit install

# Test pre-commit hooks
pre-commit run --all-files
```

## Development Environment

### Required Tools

- **Python 3.9+**: Required Python version
- **pip**: Package installer for Python
- **git**: Version control
- **Virtual environment**: Recommended for isolation

### Recommended IDE Setup

#### Visual Studio Code

Install these extensions:
- Python (Microsoft)
- Pylance
- Black Formatter
- isort
- Flake8
- autoDocstring

#### PyCharm

Enable these features:
- Black integration
- isort integration
- Type checking with mypy
- Code inspections

### Directory Structure

```
Metadata-to-Morphsource-compare/
├── .github/
│   ├── scripts/          # GitHub Actions helper scripts
│   └── workflows/        # CI/CD workflows
├── data/
│   ├── csv/             # Input CSV files
│   ├── json/            # MorphoSource data
│   └── output/          # Generated results
├── docs/                # Documentation (optional)
├── morpho/              # Main package
├── tests/               # Test suite
├── compare.py           # Main comparison script
├── verify_pixel_spacing.py  # Verification script
├── run_comparison.py    # Local runner
├── requirements.txt     # Production dependencies
├── requirements-dev.txt # Development dependencies
├── requirements-test.txt # Test dependencies
├── setup.py            # Package setup
├── pyproject.toml      # Modern Python config
├── pytest.ini          # Pytest configuration
├── .flake8             # Flake8 configuration
├── .pre-commit-config.yaml  # Pre-commit hooks
├── .env.example        # Environment variable template
├── LICENSE             # MIT License
├── CONTRIBUTING.md     # Contribution guidelines
├── SECURITY.md         # Security policy
└── README.md           # Main documentation
```

## Code Quality Tools

### Black (Code Formatting)

Black is our code formatter. It enforces a consistent style.

```bash
# Format all files
black .

# Check formatting without making changes
black --check .

# Format specific files
black compare.py verify_pixel_spacing.py
```

Configuration in `pyproject.toml`:
- Line length: 100
- Target Python versions: 3.9, 3.10, 3.11

### isort (Import Sorting)

isort organizes your imports.

```bash
# Sort imports in all files
isort .

# Check import sorting
isort --check-only .

# Sort imports in specific files
isort compare.py
```

Configuration: Black-compatible profile with line length 100

### Flake8 (Linting)

Flake8 checks for code quality issues.

```bash
# Run flake8 on all files
flake8 .

# Run on specific files
flake8 compare.py

# Show statistics
flake8 . --statistics
```

Configuration in `.flake8`:
- Max line length: 100
- Max complexity: 15
- Excludes: `.git`, `__pycache__`, `venv`, `build`, `dist`

### mypy (Type Checking)

mypy checks type hints (optional but recommended).

```bash
# Run type checking
mypy .

# Run on specific files
mypy compare.py
```

Configuration in `pyproject.toml`

### Bandit (Security Checking)

Bandit finds common security issues.

```bash
# Run security checks
bandit -r .

# Exclude tests
bandit -r . --exclude ./tests

# Generate JSON report
bandit -r . -f json -o bandit-report.json
```

### Pre-commit Hooks

Pre-commit runs checks before each commit.

```bash
# Install hooks
pre-commit install

# Run manually on all files
pre-commit run --all-files

# Run on specific files
pre-commit run --files compare.py

# Update hooks to latest versions
pre-commit autoupdate

# Skip hooks for a specific commit (use sparingly)
git commit --no-verify
```

### Running All Quality Checks

```bash
# Run all checks at once
black --check . && \
isort --check-only . && \
flake8 . && \
mypy . && \
bandit -r . --exclude ./tests && \
pytest tests/
```

Or use a Makefile (see below).

## Testing

### Running Tests

```bash
# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run specific test file
pytest tests/test_compare.py

# Run specific test
pytest tests/test_compare.py::TestMorphosourceMatcher::test_initialization

# Run with coverage
pytest tests/ --cov=. --cov-report=html
# Open htmlcov/index.html to see coverage report

# Run tests in parallel (if you have pytest-xdist)
pytest tests/ -n auto
```

### Test Structure

```
tests/
├── __init__.py
├── test_compare.py              # Tests for compare.py
├── test_verify_pixel_spacing.py # Tests for verification
├── test_run_comparison.py       # Tests for runner
├── test_query_processor_scripts.py  # Tests for query scripts
└── ...
```

### Writing Tests

```python
import pytest

def test_example():
    """Test description."""
    # Arrange
    input_value = "test"
    
    # Act
    result = function_to_test(input_value)
    
    # Assert
    assert result == expected_value

def test_with_fixture(tmp_path):
    """Test using pytest fixture."""
    test_file = tmp_path / "test.csv"
    test_file.write_text("data")
    # ... test code
```

### Test Coverage Goals

- Aim for >80% code coverage
- All new features must have tests
- Bug fixes should include regression tests

## Documentation

### Docstring Style

Use Google-style docstrings:

```python
def function_name(param1: str, param2: int) -> bool:
    """Short description.
    
    Longer description if needed.
    
    Args:
        param1: Description of param1
        param2: Description of param2
        
    Returns:
        Description of return value
        
    Raises:
        ValueError: When something goes wrong
        
    Example:
        >>> function_name("test", 42)
        True
    """
    pass
```

### Updating Documentation

- Update README.md for user-facing changes
- Update CHANGELOG.md for all changes
- Update docstrings for code changes
- Update CONTRIBUTING.md for process changes

## Release Process

### Version Numbering

We follow [Semantic Versioning](https://semver.org/):
- MAJOR.MINOR.PATCH (e.g., 1.2.3)
- MAJOR: Breaking changes
- MINOR: New features (backward compatible)
- PATCH: Bug fixes

### Creating a Release

1. **Update version numbers**:
   - `setup.py`
   - `pyproject.toml`
   - `morpho/__init__.py` (if exists)

2. **Update CHANGELOG.md**:
   ```markdown
   ## [1.1.0] - 2024-10-21
   
   ### Added
   - New feature description
   
   ### Changed
   - Changed feature description
   
   ### Fixed
   - Bug fix description
   ```

3. **Run all tests and checks**:
   ```bash
   pytest tests/
   black --check .
   flake8 .
   ```

4. **Create a git tag**:
   ```bash
   git tag -a v1.1.0 -m "Release version 1.1.0"
   git push origin v1.1.0
   ```

5. **Create a GitHub Release**:
   - Go to GitHub → Releases → New Release
   - Select the tag
   - Add release notes (from CHANGELOG.md)
   - Publish release

## Common Tasks

### Adding a New Dependency

1. Add to `requirements.txt` (production) or `requirements-dev.txt` (dev)
2. Run `pip install -r requirements.txt` or `pip install -e ".[dev]"`
3. Update `setup.py` and `pyproject.toml` if needed
4. Document in README if it affects users

### Adding a New Feature

1. Create a new branch: `git checkout -b feature/my-feature`
2. Write tests first (TDD)
3. Implement the feature
4. Update documentation
5. Run all quality checks
6. Submit a pull request

### Fixing a Bug

1. Create a new branch: `git checkout -b fix/bug-description`
2. Write a failing test that reproduces the bug
3. Fix the bug
4. Ensure the test passes
5. Run all tests
6. Submit a pull request

## Troubleshooting

### Common Issues

**Import errors after installing**:
```bash
# Reinstall in development mode
pip install -e ".[dev]"
```

**Pre-commit hooks failing**:
```bash
# Update pre-commit
pre-commit autoupdate

# Clear cache
pre-commit clean
```

**Tests failing**:
```bash
# Clear pytest cache
rm -rf .pytest_cache

# Reinstall dependencies
pip install -r requirements-test.txt
```

## Additional Resources

- [Python Packaging Guide](https://packaging.python.org/)
- [PEP 8 Style Guide](https://pep8.org/)
- [Pytest Documentation](https://docs.pytest.org/)
- [Pre-commit Documentation](https://pre-commit.com/)
- [Semantic Versioning](https://semver.org/)

## Getting Help

- Check [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines
- Open an issue on GitHub for bugs or questions
- Read existing documentation and issues first
- Be respectful and provide context

---

Thank you for contributing to Metadata-to-Morphsource-Compare! 🎉
