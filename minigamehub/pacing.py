"""Per-user daily payout pacing (design doc locked decision #9, #14).

Per-user daily cap, not a guild-wide pool -- a shared pool would let one
very-active member exhaust it and lock out everyone else for the day, which
is the opposite of what evac wants. Each member has their own cap, tracked
across every game type combined. As an individual approaches their own cap,
rewards taper down rather than hard-cutting off; other members are completely
unaffected by any one member's cap status.

Reset boundary is America/Los_Angeles midnight (corrected from an initial
UTC-default mistake caught in design review), computed via zoneinfo.
"""
from datetime import datetime
from typing import Tuple

from redbot.core import Config

from .constants import RESET_TIMEZONE


def _today_str() -> str:
    return datetime.now(RESET_TIMEZONE).strftime("%Y-%m-%d")


async def _get_today_payout(config: Config, member) -> int:
    """Return this member's running payout total for *today*, resetting
    lazily if the stored date doesn't match today (America/Los_Angeles)."""
    day = await config.member(member).payout_day()
    if day != _today_str():
        await config.member(member).payout_day.set(_today_str())
        await config.member(member).payout_today.set(0)
        return 0
    return await config.member(member).payout_today()


def _taper_multiplier(today_total: int, daily_cap: int, taper_start_pct: float, taper_floor_pct: float) -> float:
    if daily_cap <= 0:
        return 1.0
    pct_of_cap = (today_total / daily_cap) * 100
    if pct_of_cap <= taper_start_pct:
        return 1.0
    if pct_of_cap >= 100:
        return taper_floor_pct / 100
    # Linear interpolation from 1.0 at taper_start_pct down to taper_floor_pct at 100%.
    span = 100 - taper_start_pct
    progressed = pct_of_cap - taper_start_pct
    frac = progressed / span if span > 0 else 1.0
    multiplier = 1.0 - frac * (1.0 - taper_floor_pct / 100)
    return max(multiplier, taper_floor_pct / 100)


async def apply_pacing(config: Config, member, base_reward: int) -> Tuple[int, float]:
    """Compute the actual reward for `member` after per-user daily-cap taper.

    Returns (actual_reward, multiplier_applied). Does NOT record the payout --
    call record_payout() separately once the reward is actually deposited, so
    a game that rolls a "bad" outcome (no payout) never touches the ledger.
    """
    pacing_conf = await config.guild(member.guild).payout_pacing()
    today_total = await _get_today_payout(config, member)
    multiplier = _taper_multiplier(
        today_total,
        pacing_conf["daily_cap_per_user"],
        pacing_conf["taper_start_pct"],
        pacing_conf["taper_floor_pct"],
    )
    actual = max(1, round(base_reward * multiplier)) if base_reward > 0 else 0
    return actual, multiplier


async def record_payout(config: Config, member, amount: int) -> None:
    """Add `amount` to member's running today-total. Call this after the
    reward has actually been deposited via bank.deposit_credits."""
    if amount <= 0:
        return
    day = await config.member(member).payout_day()
    if day != _today_str():
        await config.member(member).payout_day.set(_today_str())
        await config.member(member).payout_today.set(amount)
        return
    current = await config.member(member).payout_today()
    await config.member(member).payout_today.set(current + amount)


async def payout_status(config: Config, member) -> Tuple[int, int]:
    """Return (today_total, daily_cap) for display in stats/leaderboard."""
    pacing_conf = await config.guild(member.guild).payout_pacing()
    today_total = await _get_today_payout(config, member)
    return today_total, pacing_conf["daily_cap_per_user"]
