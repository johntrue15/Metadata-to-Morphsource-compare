"""Result summarisation helpers for :mod:`morpho`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence

from .schemas import QueryIntent, SummarisedResult


@dataclass
class SamplingConfig:
    """Configuration values for :class:`AdaptiveSummariser`."""

    max_samples: int = 12
    threshold: int = 30


class AdaptiveSummariser:
    """Adaptive sampling of large result sets.

    The summariser serves two goals: provide a digestible preview of potentially
    large result sets and expose enough metadata for downstream notebooks to
    decide whether a follow-up API call is worth making.
    """

    def __init__(self, config: Optional[SamplingConfig] = None) -> None:
        self.config = config or SamplingConfig()

    def summarise(
        self,
        intent: QueryIntent,
        records: Sequence[Mapping[str, object]],
        total_available: Optional[int] = None,
    ) -> SummarisedResult:
        """Return a :class:`SummarisedResult` for *records*.

        Parameters
        ----------
        intent:
            Source intent.  Currently unused but included to simplify future
            behaviour adjustments (e.g. dynamic sampling per media type).
        records:
            Materialised records fetched by the router, already capped by the
            requested limit.
        total_available:
            Optional total number of records reported by the API.  When missing
            we fall back to ``len(records)``.
        """

        total = total_available if isinstance(total_available, int) else len(records)
        notes: Optional[str] = None

        if total > len(records):
            notes = (
                f"Result truncated to {len(records)} of {total} records; use a lower limit or apply more filters "
                "for exhaustive data."
            )
        elif total >= self.config.threshold:
            notes = (
                notes
                or f"Displaying a representative sample of {min(len(records), self.config.max_samples)} records out of {total}."
            )

        sample = self._sample(records)
        return SummarisedResult(total_records=total, sample=sample, notes=notes)

    # ------------------------------------------------------------------
    def _sample(self, records: Sequence[Mapping[str, object]]) -> List[Mapping[str, object]]:
        size = len(records)
        if size <= self.config.max_samples:
            return list(records)

        head = min(self.config.max_samples // 2, size)
        tail = self.config.max_samples - head
        sample = list(records[:head])
        if tail:
            sample.extend(records[-tail:])
        return sample


__all__ = ["AdaptiveSummariser", "SamplingConfig"]
