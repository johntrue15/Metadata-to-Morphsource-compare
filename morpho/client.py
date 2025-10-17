"""Resilient HTTP client for the :mod:`morpho` package."""
from __future__ import annotations

import logging
import time
from typing import Mapping, Optional, Sequence, Tuple

import requests

from .schemas import APIRequest, APIResponse

LOGGER = logging.getLogger(__name__)


class MorphoClient:
    """Thin wrapper around :mod:`requests` with retry and logging support."""

    def __init__(
        self,
        base_timeout: float = 10.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = base_timeout
        self.max_retries = max(1, max_retries)
        self.backoff_factor = backoff_factor
        self.session = session or requests.Session()
        self.session.headers.setdefault("Accept", "application/json")
        self.session.headers.setdefault("User-Agent", "morpho-query-toolkit/1.0")

    # ------------------------------------------------------------------
    def _prepare_params(self, params: Mapping[str, object]) -> Sequence[Tuple[str, object]]:
        prepared: list[Tuple[str, object]] = []
        for key, value in params.items():
            if isinstance(value, (list, tuple, set)):
                prepared.extend((key, item) for item in value)
            elif value is not None:
                prepared.append((key, value))
        return prepared

    # ------------------------------------------------------------------
    def execute(self, request: APIRequest) -> APIResponse:
        """Execute *request* and return a structured :class:`APIResponse`."""

        params = self._prepare_params(request.params)
        attempt = 0
        last_error: Optional[str] = None
        while attempt < self.max_retries:
            try:
                response = self.session.request(
                    method=request.method,
                    url=request.path,
                    params=params,
                    timeout=self.timeout,
                )
                data: Optional[Mapping[str, object]] = None
                error: Optional[str] = None
                if response.content:
                    try:
                        data = response.json()
                    except ValueError:
                        # Attempt to decode textual payloads for diagnostics.
                        try:
                            text = response.text
                        except Exception:  # pragma: no cover - extremely defensive
                            text = "<unavailable>"
                        error = f"Non-JSON response received: {text[:200]}"
                return APIResponse(
                    request=request,
                    status_code=response.status_code,
                    data=data,
                    error=error,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                attempt += 1
                if attempt >= self.max_retries:
                    break
                sleep_time = self.backoff_factor * (2 ** (attempt - 1))
                LOGGER.debug("Retrying request %s in %.2fs due to %s", request.path, sleep_time, exc)
                time.sleep(sleep_time)

        return APIResponse(request=request, status_code=None, data=None, error=last_error)

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "MorphoClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - convenience only
        self.close()


__all__ = ["MorphoClient"]
