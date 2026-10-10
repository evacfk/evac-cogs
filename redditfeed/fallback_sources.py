"""Backup Reddit data sources, plus the wrapper that fails over between them.

Arctic Shift is the primary source. When it is down (it returned HTTP 522 for
hours on 2026-10-09) FallbackSource tries Reddit's public RSS feed instead. It is
mapped to Reddit's post JSON shape, so nothing downstream changes; dedup by post
id absorbs any overlap between sources.

Why RSS and not Reddit's .json listing: from the evacOVH datacenter IP the
.json endpoint answers 403 while the .rss feed answers 200 (checked 2026-10-09).

RSS is thinner than Arctic Shift: no score, no gallery image list, no NSFW flag,
and Reddit rate-limits it hard from datacenter IPs (HTTP 429). PullPush was tried
and dropped: it sits behind a Cloudflare challenge that answers 403 to this host.
The backup exists so a primary outage degrades the feed instead of stopping it.
Discovery (subreddit search) stays primary-only.

aiohttp is imported inside methods so this module stays importable under the
dev test stub.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from . import constants
from .arctic_shift import RedditSource, RedditSourceError, describe_error, format_http_error

log = logging.getLogger("red.redditfeed.fallback")


class SourceRateLimited(RedditSourceError):
    """HTTP 429. Carries how long the server asked us to stay away."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value) -> float:
    """Retry-After header (seconds) -> a sane wait, defaulting when absent/odd."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return constants.RATE_LIMIT_DEFAULT_WAIT_SECONDS
    return min(max(seconds, 10.0), constants.RATE_LIMIT_MAX_WAIT_SECONDS)


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


class RedditRssSource(_JsonSource):
    """Reddit's public `/r/<sub>/new.rss` Atom feed.

    Reddit rate-limits anonymous datacenter traffic hard, so requests are spaced
    by a minimum gap, and a 429 puts the whole source on hold (no retries, no
    network calls) until the server's Retry-After passes. Hammering a 429 only
    extends it.
    """

    name = "reddit-rss"

    def __init__(self, request_fn=None, max_retries: int = constants.FALLBACK_MAX_RETRIES,
                 min_gap_seconds: float = constants.REDDIT_RSS_MIN_GAP_SECONDS,
                 clock=time.monotonic):
        super().__init__(request_fn, max_retries)
        self._min_gap = min_gap_seconds
        self._clock = clock
        self._last_request = 0.0
        self._blocked_until = 0.0
        self._gap_lock = asyncio.Lock()

    @property
    def blocked_for(self) -> float:
        """Seconds left on a rate-limit hold (0 when clear)."""
        return max(0.0, self._blocked_until - self._clock())

    async def _default_request(self, url: str):
        import aiohttp  # local import -- not available under the dev test stub

        timeout = aiohttp.ClientTimeout(total=constants.FALLBACK_TIMEOUT_SECONDS)
        headers = {"User-Agent": constants.FALLBACK_USER_AGENT}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 429:
                    raise SourceRateLimited("HTTP 429 (rate limited)", parse_retry_after(resp.headers.get("Retry-After")))
                if resp.status >= 400:
                    raise RedditSourceError(format_http_error(resp.status, await resp.text()))
                return await resp.text()

    def _build_url(self, subreddit: str, limit: int) -> str:
        return f"{constants.REDDIT_RSS_BASE_URL}/r/{subreddit}/new.rss?limit={max(1, min(int(limit), 100))}"

    async def fetch_new_posts(self, subreddit: str, after_ts: Optional[float], limit: int) -> list[dict]:
        if self.blocked_for > 0:
            raise RedditSourceError(f"reddit-rss is rate limited, holding off {int(self.blocked_for)}s more")
        async with self._gap_lock:
            wait = self._last_request + self._min_gap - self._clock()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = self._clock()
        url = self._build_url(subreddit, limit)
        try:
            body = await self._request_fn(url)
        except SourceRateLimited as exc:
            self._blocked_until = self._clock() + exc.retry_after
            log.warning("reddit-rss rate limited (429); holding off %ds", int(exc.retry_after))
            raise RedditSourceError(f"reddit-rss fetch failed for r/{subreddit}: HTTP 429, holding off {int(exc.retry_after)}s") from None
        except Exception as exc:  # noqa: BLE001 -- one retry for transient errors
            log.warning("reddit-rss fetch failed for r/%s: %s", subreddit, describe_error(exc))
            if self._max_retries < 1:
                raise RedditSourceError(f"reddit-rss fetch failed for r/{subreddit}: {describe_error(exc)}") from None
            await asyncio.sleep(constants.FALLBACK_RETRY_BACKOFF_SECONDS)
            self._last_request = self._clock()
            try:
                body = await self._request_fn(url)
            except SourceRateLimited as exc2:
                self._blocked_until = self._clock() + exc2.retry_after
                raise RedditSourceError(f"reddit-rss fetch failed for r/{subreddit}: HTTP 429, holding off {int(exc2.retry_after)}s") from None
            except Exception as exc2:  # noqa: BLE001
                raise RedditSourceError(f"reddit-rss fetch failed for r/{subreddit}: {describe_error(exc2)}") from None
        return _newer_than(parse_reddit_rss(body), after_ts)


_ATOM = "{http://www.w3.org/2005/Atom}"
_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')


def _parse_iso(text: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat((text or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_reddit_rss(body) -> list[dict]:
    """Reddit Atom feed -> post dicts in the shape Arctic Shift returns.

    What RSS can't tell us is left out rather than invented: no `score` key (the
    queue's score gate skips unknown scores), no gallery image list, no NSFW flag.
    Galleries and Reddit-hosted video can't be resolved to files from RSS, so they
    are marked `is_video` and post as a plain permalink instead of being dropped.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RedditSourceError(f"reddit RSS was not valid XML ({exc})") from None
    posts = []
    for entry in root.iter(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        post_id = raw_id[3:] if raw_id.startswith("t3_") else raw_id
        link_el = entry.find(f"{_ATOM}link")
        permalink_url = link_el.get("href", "") if link_el is not None else ""
        created = _parse_iso(entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated") or "")
        if not post_id or created is None:
            continue
        content = entry.findtext(f"{_ATOM}content") or ""
        match = _LINK_RE.search(content)
        url = html.unescape(match.group(1)) if match else permalink_url
        permalink = re.sub(r"^https?://[^/]+", "", permalink_url)
        lowered = url.lower()
        post = {
            "id": post_id,
            "title": (entry.findtext(f"{_ATOM}title") or "").strip(),
            "selftext": "",
            "url": url,
            "permalink": permalink,
            "created_utc": created,
        }
        if "reddit.com/gallery/" in lowered or "v.redd.it/" in lowered:
            post["is_video"] = True
        posts.append(post)
    return posts


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
