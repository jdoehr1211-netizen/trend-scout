"""Reddit collector — OAuth app-only, monitors product subreddits.

Unauthenticated www.reddit.com/*.json access is dead; this uses a
registered script app with the client_credentials grant (read-only public
data needs no user login). Stays well under the free tier's 100 QPM.

Emits per (keyword, subreddit, listing):
  mention_count    posts in the listing matching the keyword
  upvote_velocity  sum of score/age_hours over matching posts
"""
from __future__ import annotations

import os
import time

import requests

from ..models import RawResponse, TrendDatapoint
from ..normalize import keyword_in_text, slugify
from ..ratelimit import RateLimiter, http_get_json
from .base import BaseCollector, CollectorError

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"


class RedditCollector(BaseCollector):
    source = "reddit"

    def __init__(self, source_cfg):
        super().__init__(source_cfg)
        self.limiter = RateLimiter(source_cfg.get("rate_limit_per_min", 60))
        self.session = requests.Session()
        self._token: str | None = None

    def _user_agent(self) -> str:
        return self.require_env(os.environ.get("REDDIT_USER_AGENT"), "REDDIT_USER_AGENT")

    def _auth(self) -> str:
        if self._token:
            return self._token
        client_id = self.require_env(os.environ.get("REDDIT_CLIENT_ID"), "REDDIT_CLIENT_ID")
        secret = self.require_env(os.environ.get("REDDIT_CLIENT_SECRET"), "REDDIT_CLIENT_SECRET")
        resp = self.session.post(
            TOKEN_URL,
            auth=(client_id, secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": self._user_agent()},
            timeout=30,
        )
        if resp.status_code != 200:
            raise CollectorError(f"reddit token request failed: HTTP {resp.status_code}")
        self._token = resp.json()["access_token"]
        return self._token

    def plan(self, keywords, regions):
        subs = self.cfg.get("subreddits", [])
        listings = self.cfg.get("listings", ["rising"])
        return [
            f"reddit: {len(subs) * len(listings)} listing fetches "
            f"({len(subs)} subreddits x {listings}), matched against "
            f"{len(keywords)} keywords"
        ]

    def collect(self, keywords, regions):
        token = self._auth()
        headers = {"Authorization": f"bearer {token}", "User-Agent": self._user_agent()}
        limit = self.cfg.get("posts_per_listing", 100)
        raws = []
        for sub in self.cfg.get("subreddits", []):
            for listing in self.cfg.get("listings", ["rising"]):
                params = {"limit": limit}
                if listing == "top":
                    params["t"] = "day"
                payload = http_get_json(
                    self.session,
                    f"{API_BASE}/r/{sub}/{listing}",
                    params=params,
                    headers=headers,
                    limiter=self.limiter,
                    max_retries=self.cfg.get("max_retries", 4),
                )
                raws.append(
                    RawResponse(
                        source=self.source,
                        endpoint=f"r/{sub}/{listing}",
                        params={"subreddit": sub, "listing": listing, **params},
                        payload=payload,
                        # keywords aren't part of the API call; stash them for normalize()
                    )
                )
                self.log.info("fetched r/%s/%s", sub, listing)
        self._keywords = keywords
        return raws

    def normalize(self, raw):
        keywords = getattr(self, "_keywords", [])
        posts = [
            c["data"]
            for c in (raw.payload.get("data") or {}).get("children", [])
            if c.get("kind") == "t3"
        ]
        now = time.time()
        points = []
        for kw in keywords:
            matching = [
                p
                for p in posts
                if keyword_in_text(kw, f"{p.get('title', '')} {p.get('selftext', '')}")
            ]
            if not matching:
                continue
            velocity = sum(
                p.get("score", 0) / max((now - p.get("created_utc", now)) / 3600, 0.5)
                for p in matching
            )
            common = dict(
                keyword=slugify(kw),
                source=self.source,
                region=None,
                observed_at=raw.fetched_at,
                meta={
                    "subreddit": raw.params["subreddit"],
                    "listing": raw.params["listing"],
                    "post_ids": [p.get("id") for p in matching][:25],
                },
                raw_ref=raw.cache_ref,
            )
            points.append(
                TrendDatapoint(metric="mention_count", value=float(len(matching)), **common)
            )
            points.append(
                TrendDatapoint(metric="upvote_velocity", value=round(velocity, 2), **common)
            )
        return points
