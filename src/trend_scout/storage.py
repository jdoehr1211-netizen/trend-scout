"""Datapoint storage.

Primary store (Phase 2+): Supabase trend_datapoints, upserted in batches so
re-collecting an overlapping Google Trends window never duplicates rows.
JSONL under data/normalized/ is still written on every run as a local audit
trail and as the fallback when Supabase credentials are absent.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .models import TrendDatapoint

log = logging.getLogger(__name__)

NORMALIZED_DIR = Path("data") / "normalized"
BATCH = 500


def _supabase_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not (url and key):
        return None
    from supabase import create_client

    return create_client(url, key)


def _to_row(p: TrendDatapoint) -> dict:
    d = p.to_dict()
    d["region"] = d["region"] or ""     # '' = global; NULL breaks the unique constraint
    return d


def write_jsonl(points: list[TrendDatapoint]) -> Path | None:
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


def write_supabase(points: list[TrendDatapoint], client=None) -> int:
    """Upsert datapoints; returns rows sent. No-op (0) without credentials."""
    if not points:
        return 0
    client = client or _supabase_client()
    if client is None:
        log.info("supabase credentials absent; JSONL only")
        return 0
    rows = [_to_row(p) for p in points]
    sent = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        client.table("trend_datapoints").upsert(
            chunk, on_conflict="keyword,source,metric,region,observed_at"
        ).execute()
        sent += len(chunk)
    log.info("upserted %d datapoints -> supabase", sent)
    return sent


def write(points: list[TrendDatapoint]) -> None:
    write_jsonl(points)
    write_supabase(points)


def import_jsonl(paths: list[Path]) -> int:
    """Backfill: import existing normalized JSONL files into Supabase."""
    client = _supabase_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY required for import")
    total = 0
    for path in paths:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            d["region"] = d.get("region") or ""
            d.setdefault("meta", {})
            d.setdefault("raw_ref", "")
            rows.append(d)
        for i in range(0, len(rows), BATCH):
            client.table("trend_datapoints").upsert(
                rows[i : i + BATCH], on_conflict="keyword,source,metric,region,observed_at"
            ).execute()
        log.info("imported %d rows from %s", len(rows), path)
        total += len(rows)
    return total


def load_series(
    metric: str = "search_interest",
    source: str = "google_trends",
    keywords: list[str] | None = None,
) -> dict[str, list[tuple[datetime, float]]]:
    """Load per-keyword time series from Supabase (falls back to local JSONL).

    Returns {keyword: [(observed_at, value), ...]} sorted by time.
    """
    client = _supabase_client()
    series: dict[str, dict[datetime, float]] = {}

    if client is not None:
        page, page_size = 0, 1000
        while True:
            q = (
                client.table("trend_datapoints")
                .select("keyword,value,observed_at")
                .eq("metric", metric)
                .eq("source", source)
                .order("observed_at")
                .range(page * page_size, (page + 1) * page_size - 1)
            )
            if keywords:
                q = q.in_("keyword", keywords)
            rows = q.execute().data
            for r in rows:
                ts = datetime.fromisoformat(r["observed_at"])
                series.setdefault(r["keyword"], {})[ts] = float(r["value"])
            if len(rows) < page_size:
                break
            page += 1
    else:
        for path in sorted(NORMALIZED_DIR.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                d = json.loads(line)
                if d["metric"] != metric or d["source"] != source:
                    continue
                if keywords and d["keyword"] not in keywords:
                    continue
                ts = datetime.fromisoformat(d["observed_at"])
                series.setdefault(d["keyword"], {})[ts] = float(d["value"])

    return {
        kw: sorted(points.items())
        for kw, points in series.items()
    }


def load_region_interest(keywords: list[str] | None = None) -> dict[str, dict[str, float]]:
    """{keyword: {region_code: latest interest value}} from region_interest rows."""
    client = _supabase_client()
    out: dict[str, dict[str, float]] = {}
    if client is None:
        for path in sorted(NORMALIZED_DIR.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                d = json.loads(line)
                if d["metric"] != "region_interest" or not d.get("region"):
                    continue
                if keywords and d["keyword"] not in keywords:
                    continue
                out.setdefault(d["keyword"], {})[d["region"]] = float(d["value"])
        return out
    q = (
        client.table("trend_datapoints")
        .select("keyword,region,value,observed_at")
        .eq("metric", "region_interest")
        .neq("region", "")
        .order("observed_at")
    )
    if keywords:
        q = q.in_("keyword", keywords)
    for r in q.execute().data:      # later rows overwrite: latest wins
        out.setdefault(r["keyword"], {})[r["region"]] = float(r["value"])
    return out
