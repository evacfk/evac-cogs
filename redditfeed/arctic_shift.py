"""Reddit data-source layer. Built behind a swappable interface because Arctic
Shift (the current implementation) itself depends on the official Reddit API
Reddit is retiring in stages through March 2027 -- Arctic Shift's own long-term
viability is uncertain. If it goes away, write a new RedditSource subclass and
swap it in redditfeed.py's __init__; nothing else in the cog needs to change.

aiohttp is only imported inside methods (not at module scope) so this module
stays importable under the dev test stub, which doesn't provide aiohttp.
"""
from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlencode

from . import constants

log = logging.getLogger("red.redditfeed.arctic_shift")


class RedditSourceError(Exception):
    """Raised when a source fails after exhausting its own retries."""


class RedditSource(ABC):
    """Swap implementations here, not in redditfeed.py, if the backing API changes."""

    @abstractmethod
    async def fetch_new_posts(
        self, subreddit: str, after_ts: Optional[float], limit: int
    ) -> list[dict]:
        """Return a list of raw post JSON dicts for `subreddit`, newest activity
        included, created after `after_ts` (epoch seconds) if given. Raises
        RedditSourceError if the fetch ultimately fails.
        """
        raise NotImplementedError

    @abstractmethod
    async def search_subreddits(
        self, prefix: str, min_subscribers: int, limit: int
    ) -> list[dict]:
        """Return raw subreddit JSON dicts for NSFW subreddits whose name starts
        with `prefix`, biggest first. Raises RedditSourceError on failure.
        """
        raise NotImplementedError


class ArcticShiftSource(RedditSource):
    """Free, keyless community-run Reddit archive API. No auth, no official
    rate-limit contract -- self-throttled client-side via `request_fn` retries.
    """

    def __init__(self, request_fn=None, max_retries: int = constants.ARCTIC_SHIFT_MAX_RETRIES):
        """`request_fn` is `async def request_fn(url: str) -> dict`, injected so
        tests can exercise retry/parsing logic without a real network call. When
        omitted, a real aiohttp-based fetch is used.
        """
        self._request_fn = request_fn or self._default_request
        self._max_retries = max_retries

    async def _default_request(self, url: str) -> dict:
        import aiohttp  # local import -- not available under the dev test stub

        timeout = aiohttp.ClientTimeout(total=constants.ARCTIC_SHIFT_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    # Not raise_for_status(): that discards the body, and Arctic
                    # Shift puts its real reason there (see format_http_error).
                    raise RedditSourceError(format_http_error(resp.status, await resp.text()))
                return await resp.json()

    def _build_url(self, subreddit: str, after_ts: Optional[float], limit: int) -> str:
        params = [f"subreddit={subreddit}", f"limit={limit}", "sort=desc"]
        if after_ts is not None:
            params.append(f"after={int(after_ts)}")
        return f"{constants.ARCTIC_SHIFT_BASE_URL}?{'&'.join(params)}"

    async def _get_data(self, url: str, what: str) -> list[dict]:
        """GET `url` and return the parsed `data` list, retrying with backoff.
        `what` names the request in logs/errors (e.g. "r/feet")."""
        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                payload = await self._request_fn(url)
                return parse_arctic_shift_response(payload)
            except Exception as exc:  # noqa: BLE001 -- any failure is a retry candidate
                last_error = exc
                log.warning(
                    "Arctic Shift fetch failed for %s (attempt %d/%d): %s",
                    what, attempt + 1, self._max_retries + 1, describe_error(exc),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(constants.ARCTIC_SHIFT_RETRY_BACKOFF_SECONDS * (attempt + 1))

        raise RedditSourceError(f"Arctic Shift fetch failed for {what}: {describe_error(last_error)}")

    async def fetch_new_posts(
        self, subreddit: str, after_ts: Optional[float], limit: int
    ) -> list[dict]:
        url = self._build_url(subreddit, after_ts, limit)
        return await self._get_data(url, f"r/{subreddit}")

    def _build_subreddit_search_url(self, prefix: str, min_subscribers: int, limit: int) -> str:
        query = urlencode(
            {
                "subreddit_prefix": prefix,
                "over18": "true",
                "min_subscribers": int(min_subscribers),
                "limit": int(limit),
                "sort": "desc",
                "sort_type": "subscribers",
            }
        )
        return f"{constants.ARCTIC_SHIFT_SUBREDDIT_SEARCH_URL}?{query}"

    async def search_subreddits(
        self, prefix: str, min_subscribers: int, limit: int
    ) -> list[dict]:
        url = self._build_subreddit_search_url(prefix, min_subscribers, limit)
        return await self._get_data(url, f"subreddit search '{prefix}'")


def describe_error(exc: Optional[BaseException]) -> str:
    """Readable text for any exception. A bare `str(exc)` is empty for timeouts
    (asyncio.TimeoutError), which is why outages used to log a blank reason."""
    if exc is None:
        return "unknown error"
    text = str(exc).strip()
    if isinstance(exc, RedditSourceError) and text:
        return text
    name = type(exc).__name__
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and not text:
        return f"{name} (no response within {constants.ARCTIC_SHIFT_TIMEOUT_SECONDS}s)"
    return f"{name}: {text}" if text else name


def format_http_error(status: int, body: str) -> str:
    """Arctic Shift reports its own query timeouts as HTTP 422 with a JSON body
    like {"data": null, "error": "Timeout. Maybe slow down a bit"}. Pull the
    "error" field out so the log says why, instead of a bare "Unprocessable Entity".
    """
    detail = (body or "").strip()
    try:
        parsed = json.loads(detail)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        detail = str(parsed["error"])
    detail = detail[:200]
    return f"HTTP {status}: {detail}" if detail else f"HTTP {status}"


def parse_arctic_shift_response(payload: dict) -> list[dict]:
    """Arctic Shift returns {"data": [...posts...]} on success, or
    {"data": null, "error": "..."} on a transient failure (e.g. its own timeout).
    Raise so the retry loop in fetch_new_posts treats it like any other failure.
    """
    if payload.get("error"):
        raise RedditSourceError(payload["error"])
    data = payload.get("data")
    if data is None:
        raise RedditSourceError("Arctic Shift returned no data and no error")
    return data
