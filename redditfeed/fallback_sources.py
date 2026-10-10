"""Backup Reddit data sources, plus the wrapper that fails over between them.

Arctic Shift is the primary source. When it is down (it returned HTTP 522 for
hours on 2026-10-09) FallbackSource tries Reddit's own public listing and then
PullPush instead. All three return Reddit's post JSON shape, so nothing
downstream changes; dedup by post id absorbs any overlap between sources.

Neither backup is as good as Arctic Shift: Reddit's unauthenticated listing is
rate-limited and often refused from datacenter IPs, and PullPush is a community
archive that can lag or be down itself. They exist so a primary outage degrades
the feed instead of stopping it. Discovery (subreddit search) stays primary-only.

aiohttp is imported inside methods so this module stays importable under the
dev test stub.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from . import constants
from .arctic_shift import RedditSource, RedditSourceError, describe_error, format_http_error

log = logging.getLogger("red.redditfeed.fallback")


async def _http_get_json(url: str, headers: Optional[dict] = None):
    import aiohttp  # local import -- not available under the dev test stub

    timeout = aiohttp.ClientTimeout(total=constants.FALLBACK_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers or {}) as session:
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise RedditSourceError(format_http_error(resp.status, await resp.text()))
            return await resp.json(content_type=None)


class _JsonSource(RedditSource):
    """Shared retry loop. `request_fn(url) -> parsed JSON` is injectable for tests."""

    name = "source"

    def __init__(self, request_fn: Optional[Callable[[str], Awaitable]] = None,
                 max_retries: int = constants.FALLBACK_MAX_RETRIES):
        self._request_fn = request_fn or self._default_request
        self._max_retries = max_retries

    async def _default_request(self, url: str):
        return await _http_get_json(url, {"User-Agent": constants.FALLBACK_USER_AGENT})

    async def _get(self, url: str, what: str):
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._request_fn(url)
            except Exception as exc:  # noqa: BLE001 -- any failure is a retry candidate
                last_error = exc
                log.warning("%s fetch failed for %s (attempt %d/%d): %s",
                            self.name, what, attempt + 1, self._max_retries + 1, describe_error(exc))
                if attempt < self._max_retries:
                    await asyncio.sleep(constants.FALLBACK_RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise RedditSourceError(f"{self.name} fetch failed for {what}: {describe_error(last_error)}")

    async def search_subreddits(self, prefix: str, min_subscribers: int, limit: int) -> list[dict]:
        raise RedditSourceError(f"{self.name} does not support subreddit search")


def _newer_than(posts: list[dict], after_ts: Optional[float]) -> list[dict]:
    if after_ts is None:
        return posts
    return [p for p in posts if (p.get("created_utc") or 0) > after_ts]


class RedditJsonSource(_JsonSource):
    """Reddit's public `/r/<sub>/new.json` listing. Unauthenticated Reddit allows
    roughly 10 requests a minute, so requests are spaced by a minimum gap."""

    name = "reddit"

    def __init__(self, request_fn=None, max_retries: int = constants.FALLBACK_MAX_RETRIES,
                 min_gap_seconds: float = constants.REDDIT_JSON_MIN_GAP_SECONDS):
        super().__init__(request_fn, max_retries)
        self._min_gap = min_gap_seconds
        self._last_request = 0.0
        self._gap_lock = asyncio.Lock()

    def _build_url(self, subreddit: str, limit: int) -> str:
        return (f"{constants.REDDIT_JSON_BASE_URL}/r/{subreddit}/new.json"
                f"?limit={max(1, min(int(limit), 100))}&raw_json=1")

    async def fetch_new_posts(self, subreddit: str, after_ts: Optional[float], limit: int) -> list[dict]:
        async with self._gap_lock:
            wait = self._last_request + self._min_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        payload = await self._get(self._build_url(subreddit, limit), f"r/{subreddit}")
        return _newer_than(parse_reddit_listing(payload), after_ts)


class PullPushSource(_JsonSource):
    """PullPush.io, a community Reddit archive with an Arctic-Shift-like API."""

    name = "pullpush"

    def _build_url(self, subreddit: str, after_ts: Optional[float], limit: int) -> str:
        params = [f"subreddit={subreddit}", f"size={max(1, min(int(limit), 100))}", "sort=desc", "sort_type=created_utc"]
        if after_ts is not None:
            params.append(f"after={int(after_ts)}")
        return f"{constants.PULLPUSH_BASE_URL}?{'&'.join(params)}"

    async def fetch_new_posts(self, subreddit: str, after_ts: Optional[float], limit: int) -> list[dict]:
        payload = await self._get(self._build_url(subreddit, after_ts, limit), f"r/{subreddit}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if data is None:
            raise RedditSourceError("pullpush returned no data")
        return _newer_than(data, after_ts)


def parse_reddit_listing(payload) -> list[dict]:
    """`{"data": {"children": [{"data": {...post...}}]}}` -> list of post dicts."""
    try:
        children = payload["data"]["children"]
    except (KeyError, TypeError):
        raise RedditSourceError("reddit listing had an unexpected shape") from None
    return [c["data"] for c in children if isinstance(c, dict) and isinstance(c.get("data"), dict)]


class FallbackSource(RedditSource):
    """Tries `sources` in order and returns the first that works.

    After `primary_failures_to_skip` consecutive primary failures the primary is
    skipped for `primary_cooldown` seconds, so an outage doesn't cost a full
    retry cycle on every subreddit. One subreddit's failure can't trip it: any
    primary success resets the count.
    """

    def __init__(self, sources: list, names: Optional[list] = None,
                 primary_failures_to_skip: int = 2,
                 primary_cooldown: float = constants.PRIMARY_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if not sources:
            raise ValueError("FallbackSource needs at least one source")
        self._sources = list(sources)
        self._names = list(names) if names else [getattr(s, "name", f"source{i}") for i, s in enumerate(sources)]
        self._failures_to_skip = primary_failures_to_skip
        self._cooldown = primary_cooldown
        self._clock = clock
        self._primary_failures = 0
        self._skip_primary_until = 0.0
        self.active_name: Optional[str] = None      # which source served the most recent fetch
        self.last_errors: dict = {}                 # name -> last error text (cleared on that source's success)

    @property
    def primary_name(self) -> str:
        return self._names[0]

    @property
    def primary_skipped(self) -> bool:
        return self._clock() < self._skip_primary_until

    async def fetch_new_posts(self, subreddit: str, after_ts: Optional[float], limit: int) -> list[dict]:
        errors = []
        for index, source in enumerate(self._sources):
            name = self._names[index]
            if index == 0 and len(self._sources) > 1 and self.primary_skipped:
                continue
            try:
                posts = await source.fetch_new_posts(subreddit, after_ts, limit)
            except RedditSourceError as exc:
                self.last_errors[name] = str(exc)
                errors.append(f"{name}: {exc}")
                if index == 0:
                    self._primary_failures += 1
                    if self._primary_failures >= self._failures_to_skip:
                        self._skip_primary_until = self._clock() + self._cooldown
                continue
            self.last_errors.pop(name, None)
            if index == 0:
                self._primary_failures = 0
                self._skip_primary_until = 0.0
            self.active_name = name
            return posts
        raise RedditSourceError("all sources failed -- " + "; ".join(errors))

    async def search_subreddits(self, prefix: str, min_subscribers: int, limit: int) -> list[dict]:
        return await self._sources[0].search_subreddits(prefix, min_subscribers, limit)
