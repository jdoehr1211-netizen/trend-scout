"""Shared normalization helpers: keyword slugs and region-code mapping."""
from __future__ import annotations

import functools
import logging
import re

import pycountry

log = logging.getLogger(__name__)


def slugify(keyword: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")


def keyword_in_text(keyword: str, text: str) -> bool:
    """Whole-phrase, word-boundary match, case-insensitive."""
    return re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE) is not None


@functools.lru_cache(maxsize=2048)
def region_to_iso(name: str, parent_geo: str | None = None) -> str | None:
    """Map a display name from Google Trends to an ISO 3166 code.

    'United States' -> 'US'; with parent_geo='US', 'California' -> 'US-CA'.
    Returns None when unmappable (caller keeps the raw name in meta).
    """
    name = name.strip()
    if parent_geo:
        for sub in pycountry.subdivisions.get(country_code=parent_geo) or []:
            if sub.name.lower() == name.lower():
                return sub.code
        return None
    try:
        matches = pycountry.countries.search_fuzzy(name)
    except LookupError:
        log.debug("unmappable region name: %r", name)
        return None
    return matches[0].alpha_2 if matches else None
