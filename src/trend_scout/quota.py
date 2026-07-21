"""Monthly API quota tracking with a hard stop.

Free-tier cap + scheduled cron is exactly the combo that burns a month's
quota in three days if a config mistake polls too often, so the guard
refuses to *start* a call once (used + reserve + n) would exceed budget —
it never fails mid-cycle with half a keyword list collected.

Backend order: Supabase if SUPABASE_URL is set (required for GitHub
Actions, where the local filesystem resets every run), else a local JSON
file (fine for laptop runs).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

QUOTA_FILE = Path("data") / "quota.json"


class QuotaExceeded(Exception):
    pass


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class FileQuotaStore:
    def get_used(self, source: str) -> int:
        if not QUOTA_FILE.exists():
            return 0
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        return int(data.get(source, {}).get(_month(), 0))

    def add(self, source: str, n: int) -> int:
        data = {}
        if QUOTA_FILE.exists():
            data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        month = _month()
        data.setdefault(source, {})[month] = data.get(source, {}).get(month, 0) + n
        QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUOTA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data[source][month]


class SupabaseQuotaStore:
    def __init__(self, url: str, key: str):
        from supabase import create_client

        self.client = create_client(url, key)

    def get_used(self, source: str) -> int:
        rows = (
            self.client.table("api_quota")
            .select("used")
            .eq("source", source)
            .eq("month", _month())
            .execute()
            .data
        )
        return int(rows[0]["used"]) if rows else 0

    def add(self, source: str, n: int) -> int:
        # increment_quota (sql/001_quota.sql) is an atomic upsert-increment.
        result = self.client.rpc(
            "increment_quota", {"p_source": source, "p_month": _month(), "p_n": n}
        ).execute()
        return int(result.data)


def make_store():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if url and key:
        log.info("quota store: supabase")
        return SupabaseQuotaStore(url, key)
    if os.environ.get("GITHUB_ACTIONS"):
        raise RuntimeError(
            "Running in GitHub Actions without SUPABASE_URL/SUPABASE_SERVICE_KEY: "
            "the local quota file resets every run, which would silently bypass "
            "the SerpApi budget cap. Set the Supabase secrets."
        )
    log.info("quota store: local file (%s)", QUOTA_FILE)
    return FileQuotaStore()


class QuotaGuard:
    def __init__(self, source: str, monthly_budget: int, reserve: int = 0, store=None):
        self.source = source
        self.budget = monthly_budget
        self.reserve = reserve
        self.store = store or make_store()

    @property
    def used(self) -> int:
        return self.store.get_used(self.source)

    @property
    def remaining(self) -> int:
        return self.budget - self.reserve - self.used

    def check(self, n: int = 1) -> None:
        """Raise QuotaExceeded unless n more calls fit under (budget - reserve)."""
        used = self.used
        if used + n > self.budget - self.reserve:
            raise QuotaExceeded(
                f"{self.source}: {used}/{self.budget} used this month "
                f"(reserve {self.reserve}); refusing {n} more call(s)"
            )

    def record(self, n: int = 1) -> None:
        total = self.store.add(self.source, n)
        log.info("%s quota: %d/%d used this month", self.source, total, self.budget)
