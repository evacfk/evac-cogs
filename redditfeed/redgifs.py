"""RedGifs -> a real video file Discord can play.

Discord can't unfurl redgifs.com/watch/... links (the page has no embeddable
video), so they show up as a bare link. This resolves the page URL to the clip's
MP4 through RedGifs' public API and downloads it, so the cog can upload it as an
attachment (which Discord plays inline). Anything going wrong raises RedgifsError
and the caller falls back to posting the plain link, exactly as before.

aiohttp is imported inside methods so this module stays importable under the
dev test stub. All network I/O goes through two injectable functions for tests.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("red.redditfeed.redgifs")

API_BASE = "https://api.redgifs.com/v2"
AUTH_URL = f"{API_BASE}/auth/temporary"
GIF_URL = API_BASE + "/gifs/{id}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REFERER = "https://www.redgifs.com/"
TOKEN_TTL_SECONDS = 20 * 3600          # temporary tokens last about a day; refresh well before
API_TIMEOUT_SECONDS = 15
DOWNLOAD_TIMEOUT_SECONDS = 90
CHUNK_BYTES = 256 * 1024
HARD_MAX_BYTES = 50 * 1024 * 1024      # never hold more than this in memory
UPLOAD_MARGIN_BYTES = 512 * 1024       # leave room for the request envelope

_ID_RE = re.compile(r"^/(?:watch|ifr|gifs/detail)/([A-Za-z0-9]+)", re.IGNORECASE)


class RedgifsError(Exception):
    """Any failure to turn a RedGifs link into a file. Callers fall back to the link."""


class RedgifsTooLarge(RedgifsError):
    """The clip is bigger than the channel's upload limit."""


@dataclass
class RedgifsClip:
    gif_id: str
    video_url: str


def gif_id_from_url(url: str) -> Optional[str]:
    """`https://www.redgifs.com/watch/FantasticRoundPuma` -> `fantasticroundpuma`."""
    try:
        match = _ID_RE.match(urlparse(url).path)
    except ValueError:
        return None
    return match.group(1).lower() if match else None


def pick_video_url(payload: dict) -> Optional[str]:
    """HD if present, else SD. Never the poster/thumbnail (those are images)."""
    gif = payload.get("gif") if isinstance(payload, dict) else None
    urls = (gif or {}).get("urls") or {}
    for key in ("hd", "sd"):
        value = urls.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def upload_limit(guild_limit: Optional[int]) -> int:
    """How many bytes we may upload to this server's channels."""
    limit = int(guild_limit or 10 * 1024 * 1024)
    return max(0, min(limit, HARD_MAX_BYTES) - UPLOAD_MARGIN_BYTES)


class RedgifsResolver:
    def __init__(self, get_json=None, get_bytes=None, clock=time.time):
        self._get_json = get_json or self._default_get_json
        self._get_bytes = get_bytes or self._default_get_bytes
        self._clock = clock
        self._token: Optional[str] = None
        self._token_ts = 0.0

    # -- network (replaced in tests) ------------------------------------------------

    async def _default_get_json(self, url: str, headers: dict) -> dict:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status >= 400:
                    raise RedgifsError(f"HTTP {resp.status} from {urlparse(url).netloc}")
                return await resp.json(content_type=None)

    async def _default_get_bytes(self, url: str, headers: dict, max_bytes: int) -> bytes:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status >= 400:
                    raise RedgifsError(f"download refused (HTTP {resp.status})")
                declared = resp.content_length
                if declared is not None and declared > max_bytes:
                    raise RedgifsTooLarge(f"{declared} bytes")
                data = bytearray()
                async for chunk in resp.content.iter_chunked(CHUNK_BYTES):
                    data += chunk
                    if len(data) > max_bytes:
                        raise RedgifsTooLarge(f">{max_bytes} bytes")
                return bytes(data)

    # -- public ----------------------------------------------------------------------

    async def _auth_headers(self, force: bool = False) -> dict:
        if force or self._token is None or self._clock() - self._token_ts > TOKEN_TTL_SECONDS:
            payload = await self._get_json(AUTH_URL, {"User-Agent": USER_AGENT})
            token = payload.get("token") if isinstance(payload, dict) else None
            if not token:
                raise RedgifsError("RedGifs gave no access token")
            self._token, self._token_ts = token, self._clock()
        return {"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT}

    async def resolve(self, page_url: str) -> RedgifsClip:
        gif_id = gif_id_from_url(page_url)
        if not gif_id:
            raise RedgifsError("not a RedGifs watch link")
        try:
            payload = await self._get_json(GIF_URL.format(id=gif_id), await self._auth_headers())
        except RedgifsError:
            # The token may have been revoked early: one retry with a fresh one.
            payload = await self._get_json(GIF_URL.format(id=gif_id), await self._auth_headers(force=True))
        video = pick_video_url(payload)
        if not video:
            raise RedgifsError("RedGifs returned no video for that clip")
        return RedgifsClip(gif_id=gif_id, video_url=video)

    async def download(self, clip: RedgifsClip, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise RedgifsTooLarge("no upload room")
        data = await self._get_bytes(clip.video_url, {"User-Agent": USER_AGENT, "Referer": REFERER}, max_bytes)
        if not data:
            raise RedgifsError("empty download")
        if len(data) > max_bytes:
            raise RedgifsTooLarge(f"{len(data)} bytes")
        return data

    async def fetch(self, page_url: str, max_bytes: int) -> tuple:
        """`(filename, bytes)` for a RedGifs page URL, or RedgifsError."""
        try:
            clip = await self.resolve(page_url)
            return f"{clip.gif_id}.mp4", await self.download(clip, max_bytes)
        except RedgifsError:
            raise
        except Exception as exc:  # noqa: BLE001 -- network errors, bad JSON, timeouts
            raise RedgifsError(f"{type(exc).__name__}: {exc}") from exc
