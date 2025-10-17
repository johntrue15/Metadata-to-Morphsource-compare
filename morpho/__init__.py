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

"""High level orchestration helpers for the Morphosource comparison toolkit."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .client import MorphoClient
from .router import QueryRouter
from .schemas import QueryIntent, as_serialisable
from .summarize import AdaptiveSummariser, SamplingConfig
from .taxon_map import resolve_taxon
from .url_builder import DEFAULT_BASE_URL, MorphoURLBuilder

_STOPWORDS = {
    "find",
    "show",
    "list",
    "with",
    "for",
    "all",
    "the",
    "of",
    "and",
    "records",
    "datasets",
    "data",
    "need",
    "scans",
    "scan",
    "looking",
    "query",
}

_MEDIA_TOKEN_MAP = {
    "ct": "CTImageSeries",
    "ctscan": "CTImageSeries",
    "ct_scans": "CTImageSeries",
    "ctimage": "CTImageSeries",
    "mesh": "Mesh",
    "stl": "Mesh",
    "surface": "Mesh",
    "photo": "Photograph",
    "photograph": "Photograph",
    "xray": "XRayImage",
}


def _extract_media_types(text: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    values = {token: _MEDIA_TOKEN_MAP[token] for token in tokens if token in _MEDIA_TOKEN_MAP}
    return list(dict.fromkeys(values.values()))


def _extract_filters(text: str) -> Dict[str, str]:
    filters: Dict[str, str] = {}
    for match in re.finditer(r"\b([a-z_]+):\s*([\w\- ]+)", text):
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key in {"limit", "page", "pagesize", "page_size"}:
            continue
        if key == "media" or key == "media_type":
            continue
        canonical_key = "catalog_number" if key in {"catalog", "catalognumber"} else key
        if "," in value:
            values = [item.strip() for item in value.split(",") if item.strip()]
            filters[canonical_key] = values
        else:
            filters[canonical_key] = value
    return filters


def _strip_filters(text: str) -> str:
    return re.sub(r"\b[a-z_]+:\s*[\w\- ]+", "", text)


def _extract_limit(text: str, default: int) -> int:
    match = re.search(r"(?:limit|first|top)\s+(\d+)", text, flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    return default


def _extract_page_size(text: str, default: int) -> int:
    match = re.search(r"page\s*size\s*(\d+)", text, flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    return default


def _detect_taxon(text: str) -> Optional[str]:
    # Explicit filter takes precedence
    match = re.search(r"taxon:\s*([\w\- ]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()

    tokens = re.findall(r"[A-Za-z]+", text)
    for i in range(len(tokens) - 1):
        candidate = f"{tokens[i]} {tokens[i + 1]}"
        resolution = resolve_taxon(candidate)
        if resolution and resolution.confidence >= 0.6:
            return candidate
    return None


def _keyword_tokens(text: str, taxon: Optional[str]) -> List[str]:
    stripped = _strip_filters(text).lower()
    stripped = re.sub(r"(?:limit|first|top|page\s*size)\s+\d+", "", stripped)
    taxon_tokens = set()
    if taxon:
        taxon_tokens = set(re.findall(r"[a-z]+", taxon.lower()))
    words = re.findall(r"[a-z0-9]+", stripped)
    keywords: List[str] = []
    for word in words:
        if word in _STOPWORDS:
            continue
        if word in taxon_tokens:
            continue
        keywords.append(word)
    # Deduplicate preserving order
    seen = set()
    deduped: List[str] = []
    for word in keywords:
        if word not in seen:
            seen.add(word)
            deduped.append(word)
    return deduped


def parse_intent(user_text: str, *, default_limit: int = 50, default_page_size: int = 25) -> QueryIntent:
    """Convert *user_text* into a :class:`QueryIntent`."""

    limit = _extract_limit(user_text, default_limit)
    page_size = min(limit, _extract_page_size(user_text, default_page_size))

    filters = _extract_filters(user_text)
    media_types = _extract_media_types(user_text)

    taxon_text = _detect_taxon(user_text)
    taxon_resolution = resolve_taxon(taxon_text) if taxon_text else None

    keywords = _keyword_tokens(user_text, taxon_resolution.matched_name if taxon_resolution else None)

    return QueryIntent(
        raw_text=user_text,
        taxon=taxon_resolution,
        keywords=keywords,
        filters=filters,
        media_types=media_types,
        limit=limit,
        page_size=page_size,
        sort=None,
    )


def run_query(
    user_text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: Optional[MorphoClient] = None,
    sampling: Optional[SamplingConfig] = None,
) -> Dict[str, object]:
    """High level helper that resolves intent, routes the request and summarises results."""

    intent = parse_intent(user_text)
    builder = MorphoURLBuilder(base_url=base_url)
    summariser = AdaptiveSummariser(sampling)
    router = QueryRouter(builder=builder, summariser=summariser)

    close_client = False
    if client is None:
        client = MorphoClient()
        close_client = True

    try:
        plan = router.build_plan(intent)
        execution = router.execute(plan, client)
    finally:
        if close_client:
            client.close()

    return {
        "intent": as_serialisable(intent),
        "plan": execution.plan.to_dict(),
        "responses": [resp.to_dict() for resp in execution.responses],
        "records": execution.records,
        "summary": execution.summary.to_dict(),
    }


__all__ = [
    "parse_intent",
    "run_query",
    "MorphoClient",
    "QueryRouter",
    "MorphoURLBuilder",
    "AdaptiveSummariser",
    "SamplingConfig",
]
