"""Raw-response cache.

Every API response is written to disk *before* normalization so a parsing
bug can never lose collected data, and every TrendDatapoint carries a
raw_ref pointing back to the file it came from.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .models import RawResponse

log = logging.getLogger(__name__)

RAW_DIR = Path("data") / "raw"


def save(raw: RawResponse) -> str:
    """Persist a raw response; returns its cache ref (relative path)."""
    day = raw.fetched_at.strftime("%Y-%m-%d")
    envelope = {
        "source": raw.source,
        "endpoint": raw.endpoint,
        "params": raw.params,
        "fetched_at": raw.fetched_at.isoformat(),
        "payload": raw.payload,
    }
    body = json.dumps(envelope, ensure_ascii=False, default=str)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    path = RAW_DIR / raw.source / day / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    raw.cache_ref = str(path.as_posix())
    log.debug("cached %s response -> %s", raw.source, raw.cache_ref)
    return raw.cache_ref
