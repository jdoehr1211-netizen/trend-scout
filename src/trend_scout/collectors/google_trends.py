"""Google Trends via SerpApi.

Why SerpApi and not pytrends: pytrends was archived (read-only) in April
2025 and breaks silently when Google changes internal endpoints. Regional
interest is the core signal of this whole system, so it runs on the paid,
maintained path. Every call passes through QuotaGuard so a scheduling
mistake can never burn the 100-search/month free tier.

Modes (scheduled separately to fit the budget — see config/settings.yaml):
  timeseries  weekly   1 call/keyword   interest over time, worldwide
  regions     monthly  1 call/keyword   interest_by_region, by country
              (+1 call/keyword for US states if us_state_breakdown: true)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

from ..models import RawResponse, TrendDatapoint
from ..normalize import region_to_iso, slugify
from ..quota import QuotaGuard
from ..ratelimit import RateLimiter, http_get_json
from .base import BaseCollector, CollectorError

SERPAPI_URL = "https://serpapi.com/search.json"


class GoogleTrendsCollector(BaseCollector):
    source = "google_trends"

    def __init__(self, source_cfg, settings, mode: str = "timeseries"):
        super().__init__(source_cfg)
        if mode not in ("timeseries", "regions", "both"):
            raise CollectorError(f"unknown google_trends mode: {mode}")
        self.mode = mode
        self.settings = settings
        self.limiter = RateLimiter(source_cfg.get("rate_limit_per_min", 20))
        self.guard = QuotaGuard(
            "serpapi",
            monthly_budget=source_cfg.get("monthly_budget", 100),
            reserve=source_cfg.get("budget_reserve", 10),
        )
        self.session = requests.Session()

    # -- planning ----------------------------------------------------------

    def _planned_calls(self, keywords: list[str]) -> list[tuple[str, dict]]:
        """(description, params) for every SerpApi call this run would make."""
        calls: list[tuple[str, dict]] = []
        window = self.cfg.get("timeseries_window", "today 3-m")
        if self.mode in ("timeseries", "both"):
            for kw in keywords:
                calls.append(
                    (f"timeseries '{kw}'", {"q": kw, "data_type": "TIMESERIES", "date": window})
                )
        if self.mode in ("regions", "both"):
            for kw in keywords:
                calls.append((f"regions '{kw}' worldwide", {"q": kw, "data_type": "GEO_MAP_0"}))
                if self.settings.get("us_state_breakdown"):
                    calls.append(
                        (
                            f"regions '{kw}' US states",
                            {"q": kw, "data_type": "GEO_MAP_0", "geo": "US", "region": "REGION"},
                        )
                    )
        return calls

    def plan(self, keywords, regions):
        calls = self._planned_calls(keywords)
        return [
            f"google_trends[{self.mode}]: {len(calls)} SerpApi calls "
            f"(quota: {self.guard.used}/{self.guard.budget} used, "
            f"{self.guard.remaining} usable)"
        ] + [f"  - {desc}" for desc, _ in calls]

    # -- collection --------------------------------------------------------

    def collect(self, keywords, regions):
        api_key = self.require_env(os.environ.get("SERPAPI_KEY"), "SERPAPI_KEY")
        calls = self._planned_calls(keywords)

        # Hard-stop up front: refuse the whole batch rather than dying
        # mid-cycle with half the keyword list collected.
        self.guard.check(len(calls))

        raws: list[RawResponse] = []
        for desc, params in calls:
            full = {"engine": "google_trends", "api_key": api_key, **params}
            payload = http_get_json(
                self.session,
                SERPAPI_URL,
                params=full,
                limiter=self.limiter,
                max_retries=self.cfg.get("max_retries", 4),
            )
            self.guard.record(1)
            if payload.get("error"):
                raise CollectorError(f"SerpApi error on {desc}: {payload['error']}")
            safe_params = {k: v for k, v in full.items() if k != "api_key"}
            raws.append(
                RawResponse(source=self.source, endpoint=desc, params=safe_params, payload=payload)
            )
            self.log.info("fetched %s", desc)
        return raws

    # -- normalization -----------------------------------------------------

    def normalize(self, raw):
        params = raw.params
        keyword = slugify(params["q"])
        if params.get("data_type") == "TIMESERIES":
            return self._normalize_timeseries(raw, keyword)
        return self._normalize_regions(raw, keyword, parent_geo=params.get("geo"))

    def _normalize_timeseries(self, raw, keyword):
        points = []
        timeline = (raw.payload.get("interest_over_time") or {}).get("timeline_data", [])
        for entry in timeline:
            values = entry.get("values", [])
            if not values:
                continue
            ts = entry.get("timestamp")
            if ts is None:
                continue
            points.append(
                TrendDatapoint(
                    keyword=keyword,
                    source=self.source,
                    metric="search_interest",
                    value=float(values[0].get("extracted_value") or 0),
                    region=None,
                    observed_at=datetime.fromtimestamp(int(ts), tz=timezone.utc),
                    meta={"query": raw.params["q"], "window": raw.params.get("date")},
                    raw_ref=raw.cache_ref,
                )
            )
        if not timeline:
            self.log.warning("no timeline data for %r", keyword)
        return points

    def _normalize_regions(self, raw, keyword, parent_geo=None):
        # SerpApi has shipped this list under a couple of key names.
        entries = (
            raw.payload.get("interest_by_region")
            or raw.payload.get("compared_breakdown_by_region")
            or []
        )
        points = []
        for entry in entries:
            name = entry.get("location", "")
            iso = region_to_iso(name, parent_geo=parent_geo)
            points.append(
                TrendDatapoint(
                    keyword=keyword,
                    source=self.source,
                    metric="region_interest",
                    value=float(entry.get("extracted_value") or 0),
                    region=iso,
                    observed_at=raw.fetched_at,
                    meta={"query": raw.params["q"], "location_name": name, "geo": parent_geo},
                    raw_ref=raw.cache_ref,
                )
            )
        if not entries:
            self.log.warning("no region data for %r", keyword)
        return points
