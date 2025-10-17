"""Morpho command-line helpers."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
import sys

__all__ = ["ensure_pipeline_imports"]


def ensure_pipeline_imports() -> None:
    """Ensure the legacy GitHub script modules are importable.

    The project originally stored the MorphoSource query pipeline inside
    ``.github/scripts`` so GitHub Actions could execute the helpers directly.
    The CLI reuses that code, so this helper mirrors the path mangling that the
    tests perform while keeping the logic in a single place.
    """

    script_dir = Path(__file__).resolve().parent.parent / ".github" / "scripts"
    script_dir_str = str(script_dir)
    if script_dir.exists() and script_dir_str not in sys.path:
        sys.path.insert(0, script_dir_str)


# Preload the modules when imported so that ``python -m morpho.cli`` works even
# if callers do not explicitly invoke :func:`ensure_pipeline_imports`.
ensure_pipeline_imports()


def __getattr__(name: str):  # pragma: no cover - convenience passthrough
    if name in {"query_formatter", "morphosource_api", "chatgpt_processor"}:
        ensure_pipeline_imports()
        return import_module(name)
    raise AttributeError(name)

