"""Collector registry. Register new sources here."""
from __future__ import annotations

from typing import Any

from .aliexpress import AliExpressCollector
from .base import BaseCollector, CollectorError
from .google_trends import GoogleTrendsCollector
from .reddit import RedditCollector

__all__ = ["BaseCollector", "CollectorError", "build_collectors", "SOURCES"]

SOURCES = ["google_trends", "reddit", "aliexpress"]


def build_collectors(
    names: list[str], sources_cfg: dict[str, Any], settings: dict[str, Any], gt_mode: str
) -> list[BaseCollector]:
    out: list[BaseCollector] = []
    for name in names:
        cfg = sources_cfg.get(name, {})
        if not cfg.get("enabled", False):
            continue
        if name == "google_trends":
            out.append(GoogleTrendsCollector(cfg, settings, mode=gt_mode))
        elif name == "reddit":
            out.append(RedditCollector(cfg))
        elif name == "aliexpress":
            out.append(AliExpressCollector(cfg))
        else:
            raise CollectorError(f"unknown source: {name}")
    return out
