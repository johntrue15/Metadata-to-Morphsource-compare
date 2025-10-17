"""Utilities for summarising MorphoSource API payloads."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence

from .router import QueryRequest, RouteDecision


@dataclass(frozen=True)
class Summary:
    narrative: str
    spotlight: Sequence[Dict[str, Any]]
    pagination: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "narrative": self.narrative,
            "spotlight": list(self.spotlight),
            "pagination": dict(self.pagination),
        }


def _extract_items(payload: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
    for key in ("media", "physical_objects", "assets"):
        items = payload.get(key)
        if isinstance(items, list):
            return key, items
    return "media", []


def _page_metadata(payload: Dict[str, Any], *, request: QueryRequest | None) -> Dict[str, Any]:
    pages = payload.get("pages")
    total_count = None
    total_pages = None
    per_page = None
    current_page = None
    if isinstance(pages, dict):
        total_count = pages.get("total_count")
        total_pages = pages.get("total_pages")
        per_page = pages.get("per_page")
        current_page = pages.get("page")

    if total_count is None:
        _, items = _extract_items(payload)
        total_count = len(items)
    if per_page is None:
        per_page = request.per_page if request and request.per_page else len(_extract_items(payload)[1]) or 0
    if current_page is None:
        current_page = request.page if request and request.page else 1
    if total_pages is None:
        total_pages = 0 if per_page == 0 else max(1, -(-total_count // max(per_page, 1))) if total_count else 0

    return {
        "total_count": total_count,
        "total_pages": total_pages,
        "per_page": per_page,
        "page": current_page,
        "has_next": bool(total_pages and current_page < total_pages),
        "has_previous": bool(total_pages and current_page > 1),
    }


def _item_title(item: Dict[str, Any]) -> str:
    for key in ("title", "name", "label"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    identifier = item.get("id") or item.get("uuid") or item.get("object_number")
    return str(identifier) if identifier is not None else "Unknown item"


def _item_description(item: Dict[str, Any]) -> str:
    for key in ("description", "summary", "taxonomy", "narrative"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    if "object_number" in item and item["object_number"]:
        return f"Catalog {item['object_number']}"
    return ""


def _item_permalink(item: Dict[str, Any]) -> str | None:
    for key in ("permalink", "url", "href"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    identifier = item.get("id") or item.get("uuid")
    if identifier:
        return f"https://www.morphosource.org/{identifier}"
    return None


def _spotlight_items(items: Iterable[Dict[str, Any]], *, limit: int = 3) -> List[Dict[str, Any]]:
    spotlight: List[Dict[str, Any]] = []
    for item in items:
        spotlight.append(
            {
                "title": _item_title(item),
                "description": _item_description(item),
                "permalink": _item_permalink(item),
            }
        )
        if len(spotlight) >= limit:
            break
    return spotlight


def _narrative(item_type: str, page_info: Dict[str, Any], *, request: QueryRequest | None,
               route: RouteDecision | None) -> str:
    total = page_info["total_count"]
    page = page_info["page"]
    per_page = page_info["per_page"]

    if total == 0:
        if request:
            return f"No {item_type.replace('_', ' ')} results were found for {request.taxon}."
        return f"No {item_type.replace('_', ' ')} results were found."

    showing = min(total, per_page if per_page else total)
    base = f"Found {total} {item_type.replace('_', ' ')} results. Showing {showing}"
    if total > showing:
        base += f" on page {page}"
    if request:
        base += f" for {request.taxon}"
    if route and route.fallbacks:
        base += "; fallback URLs are prepared for additional coverage"
    return base + "."


def summarize(payload: Dict[str, Any], *, request: QueryRequest | None = None,
              route: RouteDecision | None = None) -> Summary:
    item_type, items = _extract_items(payload)
    page_info = _page_metadata(payload, request=request)
    spotlight = _spotlight_items(items)
    narrative = _narrative(item_type, page_info, request=request, route=route)
    return Summary(narrative=narrative, spotlight=spotlight, pagination=page_info)


__all__ = ["Summary", "summarize"]
