"""Datapoint storage.

Phase 1: append-only JSONL under data/normalized/ (one file per day).
Phase 2 replaces this with Supabase trend_datapoints writes; the JSONL
files remain importable so nothing collected now is wasted.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .models import TrendDatapoint

log = logging.getLogger(__name__)

NORMALIZED_DIR = Path("data") / "normalized"


def write(points: list[TrendDatapoint]) -> Path | None:
    if not points:
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = NORMALIZED_DIR / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for p in points:
            f.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
    log.info("wrote %d datapoints -> %s", len(points), path)
    return path
