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
import logging
from abc import ABC, abstractmethod
from typing import Optional

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
                resp.raise_for_status()
                return await resp.json()

    def _build_url(self, subreddit: str, after_ts: Optional[float], limit: int) -> str:
        params = [f"subreddit={subreddit}", f"limit={limit}", "sort=desc"]
        if after_ts is not None:
            params.append(f"after={int(after_ts)}")
        return f"{constants.ARCTIC_SHIFT_BASE_URL}?{'&'.join(params)}"

    async def fetch_new_posts(
        self, subreddit: str, after_ts: Optional[float], limit: int
    ) -> list[dict]:
        url = self._build_url(subreddit, after_ts, limit)
        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                payload = await self._request_fn(url)
                return parse_arctic_shift_response(payload)
            except Exception as exc:  # noqa: BLE001 -- any failure is a retry candidate
                last_error = exc
                log.warning(
                    "Arctic Shift fetch failed for r/%s (attempt %d/%d): %s",
                    subreddit, attempt + 1, self._max_retries + 1, exc,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(constants.ARCTIC_SHIFT_RETRY_BACKOFF_SECONDS * (attempt + 1))

        raise RedditSourceError(f"Arctic Shift fetch failed for r/{subreddit}: {last_error}")


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
