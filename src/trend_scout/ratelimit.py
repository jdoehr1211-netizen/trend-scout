"""Per-source rate limiting and retry policy."""
from __future__ import annotations

import logging
import threading
import time

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)


class RateLimiter:
    """Simple thread-safe minimum-interval limiter (rate_per_min calls)."""

    def __init__(self, rate_per_min: float):
        self.interval = 60.0 / max(rate_per_min, 0.001)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class RetryableHTTPError(Exception):
    """HTTP status worth retrying (429 or 5xx)."""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(
        exc, (RetryableHTTPError, requests.ConnectionError, requests.Timeout)
    )


def http_get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    limiter: RateLimiter,
    max_retries: int = 4,
    timeout: int = 30,
):
    """GET with rate limiting, backoff-with-jitter retries, and JSON decode.

    Raises requests.HTTPError for non-retryable HTTP failures so collectors
    fail loudly (the runner catches per-collector).
    """

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential_jitter(initial=2, max=60),
        stop=stop_after_attempt(max_retries),
        reraise=True,
        before_sleep=lambda rs: log.warning(
            "retrying %s (attempt %d): %s", url, rs.attempt_number, rs.outcome.exception()
        ),
    )
    def _do():
        limiter.wait()
        resp = session.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableHTTPError(f"HTTP {resp.status_code} from {url}")
        resp.raise_for_status()
        return resp.json()

    return _do()
