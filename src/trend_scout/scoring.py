"""Scoring engine: turns time series into a 0-100 opportunity score.

The composite formula (weights tunable in config/settings.yaml -> scoring):

    composite = w_momentum   * momentum_score          (0-100)
              + w_regional   * regional_score          (0-100)
              + w_freshness  * (100 - saturation_score)(0-100)

    …then clamped to [0, 100]. Weights are renormalized over the components
    that could actually be computed, so a missing regional breakdown doesn't
    silently deflate every score.

Component definitions
---------------------
momentum      Growth of the 7-day average vs windows 7/30/90 days back.
              Each growth percentage is squashed with tanh(pct / squash)
              so a 10,000% spike on a tiny base can't dominate, then the
              windows are combined 50/30/20 (renormalized over available
              windows) and mapped from [-1, 1] to [0, 100].
seasonality   Annual-recurrence detector. Needs >= 400 days of history to
              compare the current window against the same window last year;
              with less it reports 'insufficient_history' and never affects
              the composite (flag only, per the design: a seasonal spike is
              context, not automatically bad).
regional      Share of worldwide region_interest mass that falls inside the
              configured target_geos, 0-100. High = the trend is strong
              where the user can actually sell/advertise.
saturation    How far past its peak the trend is: ratio of current 7-day avg
              to the historical peak 7-day avg, weighted by how long ago the
              peak was (a trend 60+ days past peak at 30% of peak is
              saturated/declining; one at its peak scores 0).
confidence    'low' when the series is short (< min_days) or mostly zeros
              (> max_zero_ratio); low-confidence composites are multiplied
              by low_confidence_factor rather than hidden, so sparse-but-
              interesting keywords stay visible without outranking solid data.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS = {
    "weights": {"momentum": 0.5, "regional": 0.3, "freshness": 0.2},
    "momentum_windows": {"7": 0.5, "30": 0.3, "90": 0.2},
    "momentum_squash": 50.0,          # tanh scale, in percent growth
    "saturation_stale_days": 45.0,    # days-past-peak at which staleness saturates
    "seasonality_min_days": 400,
    "seasonality_ratio": 2.0,         # same-window-last-year spike ratio to flag
    "min_days": 30,
    "max_zero_ratio": 0.4,
    "low_confidence_factor": 0.6,
}


@dataclass
class Score:
    keyword: str
    as_of: datetime
    composite: float
    confidence: str
    components: dict[str, Any] = field(default_factory=dict)

    def to_row(self, backtest: bool = False) -> dict:
        return {
            "keyword": self.keyword,
            "as_of": self.as_of.isoformat(),
            "composite": round(self.composite, 2),
            "components": self.components,
            "confidence": self.confidence,
            "backtest": backtest,
        }


def _avg(vals):
    return sum(vals) / len(vals) if vals else None


def _window_avg(series, end: datetime, days: int = 7):
    """Average of values in the 7-day window ending `end` (inclusive)."""
    lo = end - timedelta(days=days)
    vals = [v for t, v in series if lo < t <= end]
    return _avg(vals)


def momentum(series, as_of, cfg) -> dict:
    now_avg = _window_avg(series, as_of)
    if now_avg is None:
        return {"score": None, "reason": "no recent data"}
    growths, weights = [], []
    for win, w in cfg["momentum_windows"].items():
        base = _window_avg(series, as_of - timedelta(days=int(win)))
        if base is None or base == 0:
            continue
        pct = (now_avg - base) / base * 100
        growths.append((int(win), pct, math.tanh(pct / cfg["momentum_squash"])))
        weights.append(w)
    if not growths:
        return {"score": None, "reason": "no baseline windows"}
    total_w = sum(weights)
    squashed = sum(g[2] * w for g, w in zip(growths, weights)) / total_w
    return {
        "score": round((squashed + 1) / 2 * 100, 2),
        "growth_pct": {f"{g[0]}d": round(g[1], 1) for g in growths},
        "current_avg": round(now_avg, 2),
    }


def saturation(series, as_of, cfg) -> dict:
    now_avg = _window_avg(series, as_of)
    if now_avg is None:
        return {"score": None}
    # rolling 7-day averages over the whole series to find the peak
    peak_val, peak_at = 0.0, None
    t0 = series[0][0]
    t = t0 + timedelta(days=7)
    while t <= as_of:
        v = _window_avg(series, t)
        if v is not None and v > peak_val:
            peak_val, peak_at = v, t
        t += timedelta(days=1)
    if not peak_val or peak_at is None:
        return {"score": None}
    ratio = min(now_avg / peak_val, 1.0)
    stale = min((as_of - peak_at).days / cfg["saturation_stale_days"], 1.0)
    return {
        "score": round((1 - ratio) * stale * 100, 2),
        "pct_of_peak": round(ratio * 100, 1),
        "days_since_peak": (as_of - peak_at).days,
    }


def seasonality(series, as_of, cfg) -> dict:
    span = (as_of - series[0][0]).days if series else 0
    if span < cfg["seasonality_min_days"]:
        return {"flag": "insufficient_history", "span_days": span}
    now_avg = _window_avg(series, as_of)
    year_ago = _window_avg(series, as_of - timedelta(days=365), days=21)
    year_ago_prior = _window_avg(series, as_of - timedelta(days=365 + 45), days=21)
    if not (now_avg and year_ago and year_ago_prior):
        return {"flag": "insufficient_history", "span_days": span}
    spiked_last_year = year_ago / max(year_ago_prior, 0.01) >= cfg["seasonality_ratio"]
    return {
        "flag": "seasonal_spike" if spiked_last_year else "no_annual_pattern",
        "span_days": span,
        "last_year_ratio": round(year_ago / max(year_ago_prior, 0.01), 2),
    }


def regional(region_interest: dict[str, float] | None, target_geos: list[str]) -> dict:
    if not region_interest:
        return {"score": None, "reason": "no regional data"}
    total = sum(region_interest.values())
    if total <= 0:
        return {"score": None, "reason": "empty regional data"}
    targets = {g.upper() for g in target_geos}
    target_mass = sum(
        v for code, v in region_interest.items()
        if code and (code.upper() in targets or code.split("-")[0].upper() in targets)
    )
    top = sorted(region_interest.items(), key=lambda kv: -kv[1])[:5]
    return {
        "score": round(target_mass / total * 100, 2),
        "top_regions": [{"region": c, "value": v} for c, v in top],
        "n_regions": len(region_interest),
    }


def confidence(series, as_of, cfg) -> str:
    recent = [(t, v) for t, v in series if t <= as_of]
    if not recent:
        return "low"
    span = (as_of - recent[0][0]).days
    zero_ratio = sum(1 for _, v in recent if v == 0) / len(recent)
    return "low" if span < cfg["min_days"] or zero_ratio > cfg["max_zero_ratio"] else "normal"


def score_keyword(
    keyword: str,
    series: list[tuple[datetime, float]],
    region_interest: dict[str, float] | None,
    target_geos: list[str],
    as_of: datetime,
    scoring_cfg: dict | None = None,
) -> Score:
    cfg = {**DEFAULTS, **(scoring_cfg or {})}
    m = momentum(series, as_of, cfg)
    sat = saturation(series, as_of, cfg)
    sea = seasonality(series, as_of, cfg)
    reg = regional(region_interest, target_geos)
    conf = confidence(series, as_of, cfg)

    parts, w = [], cfg["weights"]
    if m["score"] is not None:
        parts.append((w["momentum"], m["score"]))
    if reg["score"] is not None:
        parts.append((w["regional"], reg["score"]))
    if sat["score"] is not None:
        parts.append((w["freshness"], 100 - sat["score"]))

    if parts:
        total_w = sum(p[0] for p in parts)
        composite = sum(wt * s for wt, s in parts) / total_w
    else:
        composite = 0.0
    if conf == "low":
        composite *= cfg["low_confidence_factor"]

    return Score(
        keyword=keyword,
        as_of=as_of,
        composite=max(0.0, min(100.0, composite)),
        confidence=conf,
        components={
            "momentum": m,
            "saturation": sat,
            "seasonality": sea,
            "regional": reg,
            "weights_used": w,
        },
    )
