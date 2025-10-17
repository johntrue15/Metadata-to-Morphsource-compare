"""Helpers for constructing MorphoSource API URLs.

The templates mirror the exact examples that power the query formatter prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence
from urllib.parse import quote

MORPHOSOURCE_BASE = "https://www.morphosource.org/api"


@dataclass(frozen=True)
class URLTemplate:
    """Represents a concrete MorphoSource API URL template."""

    name: str
    endpoint: str
    query_parts: Sequence[str]

    @property
    def url(self) -> str:
        return f"{MORPHOSOURCE_BASE}/{self.endpoint}?" + "&".join(self.query_parts)

    def as_params(self) -> Mapping[str, str]:
        """Return the query string parts as an ordered mapping."""

        params: dict[str, str] = {}
        for part in self.query_parts:
            key, _, value = part.partition("=")
            params[key] = value
        return params


def _encode_taxon(taxon: str) -> str:
    return quote(taxon.strip(), safe="")


def media_ct_scan(taxon: str, *, open_access: bool = False,
                  per_page: int | None = None, page: int | None = None) -> URLTemplate:
    """Return the canonical CT media template for a given taxon."""

    encoded = _encode_taxon(taxon)
    parts: list[str] = [
        "f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography",
    ]
    if open_access:
        parts.append("f%5Bvisibility%5D%5B%5D=Open")
    parts.extend([
        f"f%5Btaxonomy_gbif%5D%5B%5D={encoded}",
        "locale=en",
        "search_field=all_fields",
    ])
    if per_page is not None:
        parts.append(f"per_page={per_page}")
    if page is not None:
        parts.append(f"page={page}")
    name = "media_ct_open" if open_access else "media_ct"
    return URLTemplate(name=name, endpoint="media", query_parts=tuple(parts))


def specimens_count(taxon: str) -> URLTemplate:
    """Return the specimen count template for the provided taxon."""

    encoded = _encode_taxon(taxon)
    parts = (
        "f%5Bobject_type%5D%5B%5D=BiologicalSpecimen",
        f"f%5Btaxonomy_gbif%5D%5B%5D={encoded}",
        "locale=en",
        "object_type=BiologicalSpecimen",
        "per_page=1",
        "page=1",
        f"taxonomy_gbif={encoded}",
    )
    return URLTemplate(name="specimen_count", endpoint="physical-objects", query_parts=parts)


def specimens_browse(taxon: str, *, per_page: int = 12, page: int = 1) -> URLTemplate:
    """Return the specimen browse template using the canonical pagination."""

    encoded = _encode_taxon(taxon)
    parts = (
        "f%5Bobject_type%5D%5B%5D=BiologicalSpecimen",
        f"f%5Btaxonomy_gbif%5D%5B%5D={encoded}",
        "locale=en",
        "object_type=BiologicalSpecimen",
        f"per_page={per_page}",
        f"page={page}",
        f"taxonomy_gbif={encoded}",
    )
    return URLTemplate(name="specimen_browse", endpoint="physical-objects", query_parts=parts)


__all__ = [
    "URLTemplate",
    "MORPHOSOURCE_BASE",
    "media_ct_scan",
    "specimens_count",
    "specimens_browse",
]
