"""URL construction helpers for the :mod:`morpho` orchestrator.

The Morphosource API exposes multiple endpoints with subtly different filtering
rules.  This module provides a small opinionated builder that translates an
abstract :class:`~morpho.schemas.QueryIntent` into request metadata understood by
:class:`requests.Session`.  The builder focuses on three concepts:

``Endpoint rules``
    Each supported endpoint declares a whitelist of filters that may be emitted
    to avoid unexpected HTTP 400 responses.

``Array filters``
    MorphoSource supports ``filter[foo][]=...`` style parameters.  The builder
    therefore converts Python iterables into the proper bracket notation so the
    downstream HTTP client can send repeated parameters.

``Pagination``
    Page number and size are set explicitly to make the router aware of how many
    records can be fetched per request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, MutableMapping, Optional, Tuple
from urllib.parse import urljoin

from .schemas import APIRequest, EndpointName, QueryIntent

DEFAULT_BASE_URL = "https://api.morphosource.org/v2/"


@dataclass(frozen=True)
class EndpointRule:
    """Validation metadata for an endpoint."""

    path: str
    allowed_filters: Tuple[str, ...]
    default_params: Mapping[str, str] = None

    def filter_params(self, values: Mapping[str, object]) -> Dict[str, object]:
        """Keep only filters allowed by the endpoint."""

        allowed = set(self.allowed_filters)
        return {k: v for k, v in values.items() if k in allowed}


_ENDPOINT_RULES: Mapping[EndpointName, EndpointRule] = {
    EndpointName.SEARCH: EndpointRule(
        path="search",
        allowed_filters=("taxon", "media_type", "institution", "publication"),
        default_params={"sort": "-created_at"},
    ),
    EndpointName.MEDIA: EndpointRule(
        path="media",
        allowed_filters=("taxon", "media_type", "project", "catalog_number"),
    ),
    EndpointName.COLLECTIONS: EndpointRule(
        path="collections",
        allowed_filters=("institution", "country", "project"),
    ),
}


def _apply_array_filters(params: Mapping[str, object]) -> Dict[str, object]:
    """Translate list-like filters into ``filter[key][]`` query params."""

    serialised: Dict[str, object] = {}
    for key, value in params.items():
        filter_key = f"filter[{key}]"
        if isinstance(value, (list, tuple, set)):
            serialised[f"{filter_key}[]"] = list(value)
        elif value is not None:
            serialised[filter_key] = value
    return serialised


def _base_params(intent: QueryIntent, page: int) -> Dict[str, object]:
    params: Dict[str, object] = {
        "page[number]": page,
        "page[size]": intent.page_size,
    }
    if intent.sort:
        params["sort"] = intent.sort
    return params


class MorphoURLBuilder:
    """Main entry point for building Morphosource API request objects."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url if base_url.endswith("/") else f"{base_url}/"

    def build_search_request(self, intent: QueryIntent, page: int = 1) -> APIRequest:
        """Create an :class:`APIRequest` for the search endpoint."""

        filters = dict(intent.filters)
        if intent.taxon and intent.taxon.matched_name:
            filters.setdefault("taxon", intent.taxon.matched_name)
        if intent.media_types:
            filters.setdefault("media_type", intent.media_types)
        return self._build_request(EndpointName.SEARCH, intent, filters, page, query=" ".join(intent.keywords))

    def build_media_request(self, intent: QueryIntent, page: int = 1) -> APIRequest:
        """Request media records for the current query intent."""

        filters = dict(intent.filters)
        if intent.taxon and intent.taxon.matched_name:
            filters.setdefault("taxon", intent.taxon.matched_name)
        if intent.media_types:
            filters.setdefault("media_type", intent.media_types)
        return self._build_request(EndpointName.MEDIA, intent, filters, page)

    def build_request(self, endpoint: EndpointName, intent: QueryIntent, page: int = 1) -> APIRequest:
        """Generic builder when the caller already knows the target endpoint."""

        filters = dict(intent.filters)
        return self._build_request(endpoint, intent, filters, page)

    # ------------------------------------------------------------------
    def _build_request(
        self,
        endpoint: EndpointName,
        intent: QueryIntent,
        filters: MutableMapping[str, object],
        page: int,
        query: Optional[str] = None,
    ) -> APIRequest:
        rule = _ENDPOINT_RULES[endpoint]
        allowed_filters = rule.filter_params(filters)
        params = _base_params(intent, page)
        if query:
            params["q"] = query

        if rule.default_params:
            for key, value in rule.default_params.items():
                params.setdefault(key, value)
        params.update(_apply_array_filters(allowed_filters))
        path = rule.path
        return APIRequest(
            endpoint=endpoint,
            method="GET",
            path=urljoin(self.base_url, path),
            params=params,
            page=page,
            page_size=intent.page_size,
        )


__all__ = ["MorphoURLBuilder", "DEFAULT_BASE_URL"]
