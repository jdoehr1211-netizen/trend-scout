"""Shared collector interface.

Adding a new source = subclass BaseCollector, implement collect() and
normalize(), register it in collectors/__init__.py. Nothing else changes.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..models import RawResponse, TrendDatapoint


class CollectorError(Exception):
    """Raised for unrecoverable collector failures (bad credentials, quota).

    The runner catches this per-collector so one source failing never kills
    the whole pipeline run.
    """


class BaseCollector(ABC):
    source: str

    def __init__(self, source_cfg: dict[str, Any]):
        self.cfg = source_cfg
        self.log = logging.getLogger(f"collector.{self.source}")

    @abstractmethod
    def collect(self, keywords: list[str], regions: list[str]) -> list[RawResponse]:
        """Fetch raw API responses. Must NOT parse — parsing lives in normalize()."""

    @abstractmethod
    def normalize(self, raw: RawResponse) -> list[TrendDatapoint]:
        """Reduce one raw response to normalized datapoints."""

    def plan(self, keywords: list[str], regions: list[str]) -> list[str]:
        """Human-readable list of API calls collect() would make (for --dry-run)."""
        return [f"{self.source}: collect for {len(keywords)} keywords"]

    @staticmethod
    def require_env(value: str | None, name: str) -> str:
        if not value:
            raise CollectorError(f"missing required env var {name} (see .env.example)")
        return value
