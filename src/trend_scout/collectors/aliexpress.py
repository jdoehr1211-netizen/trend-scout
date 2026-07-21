"""AliExpress collector — official Open Platform Affiliate API.

Uses the hot-products query per seed keyword. Doubles as the supplier-cost
signal for Phase 3 pricing analysis. Wrapped via python-aliexpress-api,
which handles the Open Platform request signing.
"""
from __future__ import annotations

import os

from ..models import RawResponse, TrendDatapoint
from ..normalize import slugify
from ..ratelimit import RateLimiter
from .base import BaseCollector, CollectorError


class AliExpressCollector(BaseCollector):
    source = "aliexpress"

    def __init__(self, source_cfg):
        super().__init__(source_cfg)
        self.limiter = RateLimiter(source_cfg.get("rate_limit_per_min", 30))
        self._api = None

    def _client(self):
        if self._api is None:
            try:
                from aliexpress_api import AliexpressApi, models
            except ImportError as e:
                raise CollectorError(f"python-aliexpress-api not installed: {e}") from e
            key = self.require_env(os.environ.get("ALIEXPRESS_APP_KEY"), "ALIEXPRESS_APP_KEY")
            secret = self.require_env(
                os.environ.get("ALIEXPRESS_APP_SECRET"), "ALIEXPRESS_APP_SECRET"
            )
            tracking = self.require_env(
                os.environ.get("ALIEXPRESS_TRACKING_ID"), "ALIEXPRESS_TRACKING_ID"
            )
            lang = getattr(models.Language, self.cfg.get("target_language", "EN"))
            curr = getattr(models.Currency, self.cfg.get("target_currency", "USD"))
            self._api = AliexpressApi(key, secret, lang, curr, tracking)
        return self._api

    def plan(self, keywords, regions):
        return [f"aliexpress: {len(keywords)} hot-product queries (1 per keyword)"]

    def collect(self, keywords, regions):
        api = self._client()
        raws = []
        for kw in keywords:
            self.limiter.wait()
            try:
                result = api.get_hotproducts(
                    keywords=kw, page_size=self.cfg.get("page_size", 50)
                )
            except Exception as e:  # wrapper raises assorted exception types
                self.log.error("hot-products query failed for %r: %s", kw, e)
                continue
            products = [
                {
                    "product_id": getattr(p, "product_id", None),
                    "product_title": getattr(p, "product_title", None),
                    "target_sale_price": getattr(p, "target_sale_price", None),
                    "target_original_price": getattr(p, "target_original_price", None),
                    "lastest_volume": getattr(p, "lastest_volume", None),  # sic, API field
                    "evaluate_rate": getattr(p, "evaluate_rate", None),
                    "first_level_category_name": getattr(p, "first_level_category_name", None),
                }
                for p in (getattr(result, "products", None) or [])
            ]
            raws.append(
                RawResponse(
                    source=self.source,
                    endpoint="affiliate.hotproduct.query",
                    params={"keywords": kw, "page_size": self.cfg.get("page_size", 50)},
                    payload={
                        "current_record_count": getattr(result, "current_record_count", None),
                        "products": products,
                    },
                )
            )
            self.log.info("fetched %d hot products for %r", len(products), kw)
        return raws

    def normalize(self, raw):
        kw = slugify(raw.params["keywords"])
        points = []
        products = raw.payload.get("products", [])
        for rank, p in enumerate(products, start=1):
            common = dict(
                keyword=kw,
                source=self.source,
                region=None,
                observed_at=raw.fetched_at,
                meta={
                    "product_id": p.get("product_id"),
                    "title": p.get("product_title"),
                    "category": p.get("first_level_category_name"),
                    "currency": self.cfg.get("target_currency", "USD"),
                },
                raw_ref=raw.cache_ref,
            )
            points.append(TrendDatapoint(metric="hot_product_rank", value=float(rank), **common))
            try:
                price = float(p.get("target_sale_price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price:
                points.append(TrendDatapoint(metric="price", value=price, **common))
            volume = p.get("lastest_volume")
            if volume:
                points.append(TrendDatapoint(metric="sales_volume", value=float(volume), **common))
        return points
