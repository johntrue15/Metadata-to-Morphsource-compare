"""Routing logic coordinating the URL builder and HTTP client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional

from .client import MorphoClient
from .schemas import APIRequest, APIResponse, EndpointName, QueryIntent, RoutedQueryPlan, SummarisedResult
from .summarize import AdaptiveSummariser
from .url_builder import MorphoURLBuilder


@dataclass
class ExecutionResult:
    """Container returned by :class:`QueryRouter` after running a plan."""

    plan: RoutedQueryPlan
    responses: List[APIResponse]
    records: List[Mapping[str, object]]
    summary: SummarisedResult

    def to_dict(self) -> Mapping[str, object]:
        from .schemas import as_serialisable

        return {
            "plan": as_serialisable(self.plan),
            "responses": [as_serialisable(resp) for resp in self.responses],
            "records": self.records,
            "summary": as_serialisable(self.summary),
        }


class QueryRouter:
    """Translate :class:`QueryIntent` objects into executable query plans."""

    def __init__(self, builder: Optional[MorphoURLBuilder] = None, summariser: Optional[AdaptiveSummariser] = None) -> None:
        self.builder = builder or MorphoURLBuilder()
        self.summariser = summariser or AdaptiveSummariser()

    # ------------------------------------------------------------------
    def build_plan(self, intent: QueryIntent) -> RoutedQueryPlan:
        """Generate an ordered list of API requests for *intent*."""

        requests: List[APIRequest] = [self.builder.build_search_request(intent)]
        if intent.media_types:
            requests.append(self.builder.build_media_request(intent))
        if "institution" in intent.filters:
            requests.append(self.builder.build_request(EndpointName.COLLECTIONS, intent))
        return RoutedQueryPlan(intent=intent, requests=requests)

    # ------------------------------------------------------------------
    def execute(self, plan: RoutedQueryPlan, client: MorphoClient) -> ExecutionResult:
        responses: List[APIResponse] = []
        collected: List[Mapping[str, object]] = []
        total_available: Optional[int] = None

        for request in plan.requests:
            current_request = request
            while True:
                response = client.execute(current_request)
                responses.append(response)

                if response.data and isinstance(response.data, Mapping):
                    data_items = response.data.get("data") or []
                    if isinstance(data_items, list):
                        collected.extend(data_items)
                        if len(collected) > plan.intent.limit:
                            collected = collected[: plan.intent.limit]
                    if total_available is None:
                        total_available = self._extract_total(response.data)

                if len(collected) >= plan.intent.limit:
                    break

                next_page = self._next_page(response)
                if not next_page:
                    break
                current_request = self._clone_for_page(request, next_page)

            if len(collected) >= plan.intent.limit:
                break

        summary = self.summariser.summarise(plan.intent, collected, total_available)
        return ExecutionResult(plan=plan, responses=responses, records=collected, summary=summary)

    # ------------------------------------------------------------------
    @staticmethod
    def _clone_for_page(request: APIRequest, page: int) -> APIRequest:
        params = dict(request.params)
        params["page[number]"] = page
        return APIRequest(
            endpoint=request.endpoint,
            method=request.method,
            path=request.path,
            params=params,
            page=page,
            page_size=request.page_size,
        )

    @staticmethod
    def _extract_total(data: Mapping[str, object]) -> Optional[int]:
        meta = data.get("meta") if isinstance(data, Mapping) else None
        if isinstance(meta, Mapping):
            for key in ("total_results", "total", "count"):
                value = meta.get(key)
                if isinstance(value, int):
                    return value
            page_meta = meta.get("page") or meta.get("pagination")
            if isinstance(page_meta, Mapping):
                total = page_meta.get("total") or page_meta.get("total_pages")
                if isinstance(total, int):
                    return total
        return None

    @staticmethod
    def _next_page(response: APIResponse) -> Optional[int]:
        data = response.data if isinstance(response.data, Mapping) else None
        if not data:
            return None
        meta = data.get("meta")
        if isinstance(meta, Mapping):
            page_info = meta.get("page") or meta.get("pagination")
            if isinstance(page_info, Mapping):
                next_page = page_info.get("next")
                if isinstance(next_page, int):
                    return next_page
                if isinstance(next_page, str) and next_page.isdigit():
                    return int(next_page)
                current = page_info.get("number") or page_info.get("current")
                total = page_info.get("total_pages") or page_info.get("total")
                if isinstance(current, int) and isinstance(total, int) and current < total:
                    return current + 1
        return None


__all__ = ["QueryRouter", "ExecutionResult"]
