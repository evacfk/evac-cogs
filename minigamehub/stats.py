"""Shared per-user game-stats helpers backing the unified leaderboard.

One `user_stats`-equivalent table (Config's per-member `games` dict) across
every game type instead of four separate leaderboards living in four
separate cogs (design doc, Config schema section).
"""
from typing import Optional

from redbot.core import Config


async def record_result(config: Config, member, game_key: str, good: bool) -> int:
    """Update a member's good/bad/streak counters for `game_key`. Returns the
    member's new streak count (0 if this result broke/wasn't a streak)."""
    async with config.member(member).games() as games:
        entry = games.setdefault(game_key, {"good": 0, "bad": 0, "streak": 0, "highest_streak": 0})
        if good:
            entry["good"] += 1
            entry["streak"] += 1
            entry["highest_streak"] = max(entry["highest_streak"], entry["streak"])
        else:
            entry["bad"] += 1
            entry["streak"] = 0
        return entry["streak"]


async def add_boss_damage(config: Config, member, amount: int) -> None:
    if amount <= 0:
        return
    current = await config.member(member).boss_damage()
    await config.member(member).boss_damage.set(current + amount)


async def get_member_summary(config: Config, member) -> dict:
    games = await config.member(member).games()
    boss_damage = await config.member(member).boss_damage()
    return {"games": games, "boss_damage": boss_damage}
