"""Read-only readers for each hosted game's stored numbers. No discord imports.

Each reader returns a plain dict (or None when that game's cog is not loaded) and is wrapped by
`gather` so one broken or renamed cog can never take down the whole command.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("red.evac-cogs.mystats")


async def _minigames(bot, member) -> Optional[dict]:
    cog = bot.get_cog("MinigameHub")
    if cog is None:
        return None
    data = await cog.config.member(member).all()
    return {"games": data.get("games") or {}, "boss_damage": data.get("boss_damage", 0)}


async def _duel(bot, member) -> Optional[dict]:
    cog = bot.get_cog("Duel")
    return None if cog is None else await cog.config.member(member).all()


async def _heist(bot, member) -> Optional[dict]:
    cog = bot.get_cog("Heist")
    return None if cog is None else await cog.config.user(member).all()


async def _casino(bot, member) -> Optional[dict]:
    cog = bot.get_cog("Casino")
    if cog is None:
        return None
    group = cog.config.user(member) if await cog.casino_is_global() else cog.config.member(member)
    return await group.all()


async def _cards(bot, member) -> Optional[dict]:
    cog = bot.get_cog("CardCollect")
    if cog is None:
        return None
    state = await cog._member_state(member)
    return {"owned": len(state.collection), "daily_streak": state.daily_streak}


async def _puzzle(bot, member) -> Optional[dict]:
    cog = bot.get_cog("Puzzle")
    if cog is None:
        return None
    stats = await cog.config.guild(member.guild).lifetime_stats()
    return stats.get(str(member.id)) or {}


async def _verdict(bot, member) -> Optional[dict]:
    cog = bot.get_cog("Verdict")
    return None if cog is None else await cog.config.member(member).all()


async def _pet(bot, member) -> Optional[dict]:
    cog = bot.get_cog("WonderPet")
    if cog is None:
        return None
    weeks = await cog.config.guild(member.guild).week_carers()
    return {"care_recent": sum(int((bucket or {}).get(str(member.id), 0)) for bucket in weeks.values())}


async def _bumps(bot, member) -> Optional[dict]:
    cog = bot.get_cog("BumpReward")
    if cog is None:
        return None
    return {"total_bumps": await cog.config.member(member).total_bumps()}


async def _lottery(bot, member) -> Optional[dict]:
    cog = bot.get_cog("Lottery")
    if cog is None:
        return None
    return {"tickets": await cog.config.user(member).tickets()}


READERS: dict[str, Callable[[Any, Any], Awaitable[Optional[dict]]]] = {
    "minigames": _minigames,
    "duel": _duel,
    "heist": _heist,
    "casino": _casino,
    "cards": _cards,
    "puzzle": _puzzle,
    "verdict": _verdict,
    "pet": _pet,
    "bumps": _bumps,
    "lottery": _lottery,
}


async def gather(bot, member) -> dict[str, Optional[dict]]:
    """Run every reader; a reader that raises is logged and treated as 'no data'."""
    out: dict[str, Optional[dict]] = {}
    for name, reader in READERS.items():
        try:
            out[name] = await reader(bot, member)
        except Exception:
            log.exception("mystats: reader %r failed", name)
            out[name] = None
    return out
