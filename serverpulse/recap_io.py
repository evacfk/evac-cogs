"""Discord history and Claude API I/O for the weekly recap. Thin on purpose: all decisions live in recap.py.

Nothing here stores message text. Candidates exist in memory for the length of one
draft; only the handful of picked highlights are kept (in the pending draft) until
a mod approves or rejects it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from . import recap
from .constants import RECAP_API_TIMEOUT, RECAP_API_URL, RECAP_API_VERSION, RECAP_MAX_SCAN
from .tracker import should_count

log = logging.getLogger("red.serverpulse")


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


async def collect_candidates(channel, start_ts: float, end_ts: float, *, min_reactions: int, prefixes, exclude_commands: bool,
                             excluded_user_ids=(), max_scan: int = RECAP_MAX_SCAN):
    """Read one channel between two timestamps and keep only well-reacted human messages.

    Returns (candidates, scanned, truncated). "Human message" is decided by the same
    `should_count` the live tracker uses, so highlights and activity numbers agree.
    """
    excluded = {int(u) for u in excluded_user_ids}
    channel_key = getattr(channel, "parent_id", None) or channel.id
    out: list[recap.Highlight] = []
    scanned = 0
    truncated = False
    async for m in channel.history(limit=None, after=_utc(start_ts), before=_utc(end_ts), oldest_first=True):
        scanned += 1
        if scanned > max_scan:
            truncated = True
            scanned = max_scan
            break
        content = m.content or ""
        if not should_count(
            is_bot=bool(m.author.bot), webhook_id=getattr(m, "webhook_id", None), type_name=m.type.name, content=content,
            channel_key=channel_key, ignored=(), exclude_commands=exclude_commands, prefixes=prefixes,
        ):
            continue
        if m.author.id in excluded:
            continue
        reactions = sum(int(r.count) for r in (m.reactions or ()))
        if reactions < min_reactions:
            continue
        out.append(recap.Highlight(
            m.id, m.author.id, getattr(m.author, "display_name", None) or str(m.author), content[:2000],
            reactions, m.jump_url, bool(m.attachments),
        ))
    return out, scanned, truncated


def _status_message(status: int, payload) -> str:
    detail = ""
    if isinstance(payload, dict):
        detail = str(((payload.get("error") or {}).get("message")) or "")[:200]
    if status == 401:
        return "The API key was rejected. Set it again with `.set api anthropic api_key,<key>`."
    if status == 403:
        return "That API key isn't allowed to do this (check the key's permissions and the model name)."
    if status == 404:
        return "The API doesn't know that model name. Change it with `.pulse digest recap model <name>`."
    if status == 429:
        return "The API is rate-limiting or out of credit. Try again later, and check the Anthropic console."
    if status in (500, 502, 503, 504, 529):
        return "The Claude API is busy right now. Try again in a minute."
    return f"The API refused the request ({status}){': ' + detail if detail else ''}."


async def call_claude(api_key: str, body: dict, *, timeout: int = RECAP_API_TIMEOUT) -> dict:
    """POST one Messages API request. Raises RecapError with a mod-readable message on any failure."""
    import aiohttp  # lazy: keeps the pure modules importable without it

    headers = {"x-api-key": api_key, "anthropic-version": RECAP_API_VERSION, "content-type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(RECAP_API_URL, json=body, headers=headers) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = None
                if resp.status == 200:
                    return payload
                raise recap.RecapError(_status_message(resp.status, payload))
    except asyncio.TimeoutError:
        raise recap.RecapError("Claude took too long to answer. Try again.") from None
    except aiohttp.ClientError:
        raise recap.RecapError("Couldn't reach the Claude API. Try again in a minute.") from None
