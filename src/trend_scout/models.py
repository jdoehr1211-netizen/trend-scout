"""Common data shapes shared by every collector."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RawResponse:
    """One raw API response, cached to disk before any parsing happens."""

    source: str
    endpoint: str
    params: dict[str, Any]          # secrets must be stripped by the collector
    payload: Any                    # decoded JSON body
    fetched_at: datetime = field(default_factory=utcnow)
    cache_ref: str = ""             # set by cache.save()


@dataclass
class TrendDatapoint:
    """The normalized row every source reduces to.

    metric is a namespaced free string (search_interest, region_interest,
    mention_count, upvote_velocity, hot_product_rank, sales_volume, price)
    so new sources never require a schema migration.

    observed_at is when the datapoint refers to; collected_at is when we
    fetched it. Keeping both is what makes backtesting honest.
    """

    keyword: str
    source: str
    metric: str
    value: float
    region: str | None              # ISO 3166 ("US", "GB", "US-CA"); None = global
    observed_at: datetime
    collected_at: datetime = field(default_factory=utcnow)
    meta: dict[str, Any] = field(default_factory=dict)
    raw_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["observed_at"] = self.observed_at.isoformat()
        d["collected_at"] = self.collected_at.isoformat()
        return d
