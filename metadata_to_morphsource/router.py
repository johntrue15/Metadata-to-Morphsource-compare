"""Routing helpers that decide which MorphoSource URLs to call."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from . import url_builder


@dataclass(frozen=True)
class QueryRequest:
    """Normalized request describing the desired MorphoSource search."""

    taxon: str
    intent: str  # 'media' or 'specimens'
    modality: str | None = None
    open_access: bool = False
    count_only: bool = False
    per_page: int | None = None
    page: int | None = None


@dataclass(frozen=True)
class RouteDecision:
    """The resolved query plan containing primary and fallback URLs."""

    primary: url_builder.URLTemplate
    fallbacks: Sequence[url_builder.URLTemplate] = field(default_factory=tuple)

    def urls(self) -> List[str]:
        return [self.primary.url, *[fallback.url for fallback in self.fallbacks]]


MEDIA_INTENTS = {"media", "ct", "scan", "scans"}
SPECIMEN_INTENTS = {"specimen", "specimens", "physical", "objects"}


def _media_plan(request: QueryRequest) -> RouteDecision:
    open_access = bool(request.open_access)
    primary = url_builder.media_ct_scan(
        request.taxon,
        open_access=open_access,
    )

    fallbacks: list[url_builder.URLTemplate] = []
    if request.count_only:
        fallbacks.append(url_builder.specimens_count(request.taxon))
    else:
        fallbacks.append(
            url_builder.specimens_browse(
                request.taxon,
                per_page=request.per_page or 12,
                page=request.page or 1,
            )
        )
    return RouteDecision(primary=primary, fallbacks=tuple(fallbacks))


def _specimen_plan(request: QueryRequest) -> RouteDecision:
    if request.count_only:
        primary = url_builder.specimens_count(request.taxon)
        fallbacks: Iterable[url_builder.URLTemplate] = (
            url_builder.specimens_browse(
                request.taxon,
                per_page=request.per_page or 12,
                page=request.page or 1,
            ),
        )
    else:
        primary = url_builder.specimens_browse(
            request.taxon,
            per_page=request.per_page or 12,
            page=request.page or 1,
        )
        fallbacks = (
            url_builder.specimens_count(request.taxon),
            url_builder.media_ct_scan(
                request.taxon,
                open_access=request.open_access,
            ),
        )
    return RouteDecision(primary=primary, fallbacks=tuple(fallbacks))


def route_request(request: QueryRequest) -> RouteDecision:
    """Return the URL plan for the provided query request."""

    normalized_intent = request.intent.lower().strip()
    if normalized_intent in MEDIA_INTENTS:
        return _media_plan(request)
    if normalized_intent in SPECIMEN_INTENTS:
        return _specimen_plan(request)

    # Unknown intent; default to media with a specimen fallback.
    media_request = QueryRequest(
        taxon=request.taxon,
        intent="media",
        modality=request.modality,
        open_access=request.open_access,
        count_only=request.count_only,
        per_page=request.per_page,
        page=request.page,
    )
    return _media_plan(media_request)


__all__ = ["QueryRequest", "RouteDecision", "route_request"]
