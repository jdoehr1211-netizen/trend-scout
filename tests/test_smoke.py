"""Offline smoke tests: normalization + quota guard, no network, no keys.

Run: python -m pytest tests/ -q   (or just: python tests/test_smoke.py)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_scout.collectors.aliexpress import AliExpressCollector
from trend_scout.collectors.google_trends import GoogleTrendsCollector
from trend_scout.collectors.reddit import RedditCollector
from trend_scout.models import RawResponse
from trend_scout.normalize import region_to_iso
from trend_scout.quota import QuotaExceeded, QuotaGuard


class FakeStore:
    def __init__(self):
        self.used = 0

    def get_used(self, source):
        return self.used

    def add(self, source, n):
        self.used += n
        return self.used


def test_quota_hard_stop():
    guard = QuotaGuard("serpapi", monthly_budget=100, reserve=10, store=FakeStore())
    guard.check(90)          # exactly fits
    guard.record(85)
    guard.check(5)           # still fits
    try:
        guard.check(6)       # 85 + 6 > 90
        raise AssertionError("expected QuotaExceeded")
    except QuotaExceeded:
        pass


def test_google_trends_normalize():
    cfg = {"monthly_budget": 100, "budget_reserve": 10}
    c = GoogleTrendsCollector.__new__(GoogleTrendsCollector)
    c.cfg = cfg
    import logging

    c.log = logging.getLogger("test")

    ts_raw = RawResponse(
        source="google_trends",
        endpoint="timeseries 'neck fan'",
        params={"q": "neck fan", "data_type": "TIMESERIES", "date": "today 3-m"},
        payload={
            "interest_over_time": {
                "timeline_data": [
                    {"timestamp": "1751328000", "values": [{"extracted_value": 42}]},
                    {"timestamp": "1751932800", "values": [{"extracted_value": 58}]},
                ]
            }
        },
    )
    pts = c.normalize(ts_raw)
    assert len(pts) == 2
    assert pts[0].keyword == "neck-fan"
    assert pts[0].metric == "search_interest"
    assert pts[1].value == 58.0

    geo_raw = RawResponse(
        source="google_trends",
        endpoint="regions 'neck fan' worldwide",
        params={"q": "neck fan", "data_type": "GEO_MAP_0"},
        payload={
            "interest_by_region": [
                {"location": "United States", "extracted_value": 100},
                {"location": "Philippines", "extracted_value": 76},
                {"location": "Atlantis", "extracted_value": 1},  # unmappable
            ]
        },
    )
    pts = c.normalize(geo_raw)
    assert len(pts) == 3
    assert pts[0].region == "US"
    assert pts[1].region == "PH"
    assert pts[2].region is None
    assert pts[2].meta["location_name"] == "Atlantis"


def test_region_to_iso_us_states():
    assert region_to_iso("California", parent_geo="US") == "US-CA"
    assert region_to_iso("Texas", parent_geo="US") == "US-TX"


def test_reddit_normalize():
    c = RedditCollector({"subreddits": [], "listings": []})
    c._keywords = ["neck fan", "posture corrector"]
    now = time.time()
    raw = RawResponse(
        source="reddit",
        endpoint="r/gadgets/rising",
        params={"subreddit": "gadgets", "listing": "rising", "limit": 100},
        payload={
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "abc",
                            "title": "This neck fan saved my summer",
                            "selftext": "",
                            "score": 120,
                            "created_utc": now - 7200,
                        },
                    },
                    {
                        "kind": "t3",
                        "data": {
                            "id": "def",
                            "title": "Random unrelated post",
                            "selftext": "nothing here",
                            "score": 5,
                            "created_utc": now - 3600,
                        },
                    },
                ]
            }
        },
    )
    pts = c.normalize(raw)
    # one keyword matched -> mention_count + upvote_velocity
    assert len(pts) == 2
    by_metric = {p.metric: p for p in pts}
    assert by_metric["mention_count"].value == 1.0
    assert by_metric["mention_count"].keyword == "neck-fan"
    assert by_metric["upvote_velocity"].value > 0
    assert by_metric["mention_count"].meta["subreddit"] == "gadgets"


def test_aliexpress_normalize():
    c = AliExpressCollector({"target_currency": "USD"})
    raw = RawResponse(
        source="aliexpress",
        endpoint="affiliate.hotproduct.query",
        params={"keywords": "neck fan", "page_size": 50},
        payload={
            "current_record_count": 2,
            "products": [
                {
                    "product_id": 1,
                    "product_title": "Portable Neck Fan 4000mAh",
                    "target_sale_price": "12.99",
                    "lastest_volume": 5400,
                    "first_level_category_name": "Home Appliances",
                },
                {
                    "product_id": 2,
                    "product_title": "Neck Fan Pro",
                    "target_sale_price": None,  # missing price -> no price point
                    "lastest_volume": None,
                    "first_level_category_name": "Home Appliances",
                },
            ],
        },
    )
    pts = c.normalize(raw)
    metrics = [p.metric for p in pts]
    assert metrics.count("hot_product_rank") == 2
    assert metrics.count("price") == 1
    assert metrics.count("sales_volume") == 1
    price = next(p for p in pts if p.metric == "price")
    assert price.value == 12.99
    assert price.meta["currency"] == "USD"


def _series(days_values):
    """Build a series ending today from a list of daily values."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    n = len(days_values)
    return [(now - timedelta(days=n - 1 - i), float(v)) for i, v in enumerate(days_values)]


def test_scoring_rising_beats_declining():
    from datetime import datetime, timezone

    from trend_scout.scoring import score_keyword

    now = datetime.now(timezone.utc)
    rising = _series([10 + i for i in range(90)])          # steady climb to 99
    declining = _series([99 - i for i in range(90)])       # steady fall to 10
    s_up = score_keyword("up", rising, None, ["US"], now)
    s_down = score_keyword("down", declining, None, ["US"], now)
    assert s_up.composite > s_down.composite
    assert s_up.confidence == "normal"
    assert s_up.components["momentum"]["score"] > 50
    assert s_down.components["saturation"]["score"] > 50   # far past peak
    assert s_up.components["seasonality"]["flag"] == "insufficient_history"


def test_scoring_sparse_is_low_confidence():
    from datetime import datetime, timezone

    from trend_scout.scoring import score_keyword

    now = datetime.now(timezone.utc)
    sparse = _series([0, 0, 0, 5, 0, 0, 0, 0, 6, 0] * 9)
    sc = score_keyword("sparse", sparse, None, ["US"], now)
    assert sc.confidence == "low"

    solid = _series([50] * 90)
    sc2 = score_keyword("solid", solid, None, ["US"], now)
    assert sc2.confidence == "normal"


def test_scoring_regional_share():
    from datetime import datetime, timezone

    from trend_scout.scoring import regional, score_keyword

    reg = regional({"US": 60, "GB": 20, "PH": 20}, ["US", "GB"])
    assert reg["score"] == 80.0
    assert reg["top_regions"][0]["region"] == "US"

    # US state codes count toward the US target
    reg2 = regional({"US-CA": 50, "US-TX": 30, "DE": 20}, ["US"])
    assert reg2["score"] == 80.0

    now = datetime.now(timezone.utc)
    flat = _series([50] * 90)
    with_reg = score_keyword("a", flat, {"US": 100}, ["US"], now)
    no_reg = score_keyword("b", flat, None, ["US"], now)
    # missing regional data must not tank the composite (weights renormalize)
    assert with_reg.composite > no_reg.composite
    assert no_reg.components["regional"]["score"] is None
    assert no_reg.composite > 0


def test_scoring_weights_configurable():
    from datetime import datetime, timezone

    from trend_scout.scoring import score_keyword

    now = datetime.now(timezone.utc)
    rising = _series([10 + i for i in range(90)])
    heavy = score_keyword("x", rising, None, ["US"], now,
                          {"weights": {"momentum": 1.0, "regional": 0.0, "freshness": 0.0}})
    assert abs(heavy.composite - heavy.components["momentum"]["score"]) < 0.01


if __name__ == "__main__":
    for fn in [v for k, v in list(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"ok: {fn.__name__}")
    print("all smoke tests passed")
