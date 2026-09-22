"""Pure game-logic functions: streak/strike/PTO bookkeeping and day-key helpers.

Deliberately has zero redbot/discord dependency so it can be pytest'd standalone,
same pattern as engine.py/table.py in the blackjacktable cog.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .constants import (
    DEFAULT_TIMEZONE,
    STATUS_FULL,
    STATUS_NO_SHOW,
    STATUS_PTO,
    STATUS_TARDY,
    VALID_RATINGS,
)


def get_zone(tz_name: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    return ZoneInfo(tz_name)


def now_local(tz_name: str = DEFAULT_TIMEZONE) -> datetime:
    return datetime.now(get_zone(tz_name))


def day_key(dt: datetime) -> str:
    """Render a datetime as its local calendar-day key, YYYY-MM-DD."""
    return dt.strftime("%Y-%m-%d")


def today_key(tz_name: str = DEFAULT_TIMEZONE) -> str:
    return day_key(now_local(tz_name))


def yesterday_key(tz_name: str = DEFAULT_TIMEZONE) -> str:
    return day_key(now_local(tz_name) - timedelta(days=1))


def parse_day_key(key: str) -> datetime:
    """Parse a YYYY-MM-DD key back into a naive datetime (date-only, midnight)."""
    return datetime.strptime(key, "%Y-%m-%d")


def missed_days(last_rollover_key: str, today_key_: str) -> list[str]:
    """Every fully-elapsed day from `last_rollover_key` up to (but not
    including) `today_key_`, oldest first -- i.e. every day that's completed
    since the rollover check last ran but hasn't been evaluated for a
    no-show yet.

    `last_rollover_key` is inclusive: it's the date the check last *ran on*,
    not the last date it *checked* -- when it ran that day, it only checked
    the day before (since that day itself wasn't over yet), so
    `last_rollover_key`'s own date is still unchecked and belongs in this
    range. In the normal case (run once a day, no outage) this returns
    exactly yesterday, same as before. If the bot was offline across more
    than one rollover, this catches every missed day in between instead of
    silently skipping all but the most recent one.
    """
    current = parse_day_key(last_rollover_key)
    end = parse_day_key(today_key_)
    days = []
    while current < end:
        days.append(day_key(current))
        current += timedelta(days=1)
    return days


def _outcome_for(photo_count: int, quota: int) -> str:
    if photo_count <= 0:
        return STATUS_NO_SHOW
    if photo_count < quota:
        return STATUS_TARDY
    return STATUS_FULL


def record_drop(member: dict, date_key: str, photo_count: int, quota: int, photo_paths: list[str]) -> tuple[dict, str, bool]:
    """Apply a `.pp drop` submission to a member's Config dict.

    Returns (updated_member, outcome_status, strike_added).
    A zero-photo drop is rejected by the caller before this is reached (see the
    daily-flow note in the design doc: a no-show is detected at rollover, not
    via this command) — but this function still handles it defensively.
    """
    outcome = _outcome_for(photo_count, quota)
    member = dict(member)
    history = dict(member.get("history", {}))
    history[date_key] = {"status": outcome, "photo_paths": list(photo_paths)}
    member["history"] = history

    strike_added = False
    if outcome == STATUS_FULL:
        member["streak"] = member.get("streak", 0) + 1
    else:
        member["streak"] = 0
        strike_added = True
        member["strikes"] = list(member.get("strikes", [])) + [datetime.now(tz=ZoneInfo("UTC")).isoformat()]

    return member, outcome, strike_added


def record_pto(member: dict, date_key: str) -> dict:
    """Excuse a day entirely: no strike, no streak change, logged as PTO."""
    member = dict(member)
    history = dict(member.get("history", {}))
    history[date_key] = {"status": STATUS_PTO, "photo_paths": []}
    member["history"] = history
    return member


def record_no_show(member: dict, date_key: str) -> dict:
    """Apply an automatic no-show at day-rollover for a day with no drop and no PTO."""
    member = dict(member)
    history = dict(member.get("history", {}))
    history[date_key] = {"status": STATUS_NO_SHOW, "photo_paths": []}
    member["history"] = history
    member["streak"] = 0
    member["strikes"] = list(member.get("strikes", [])) + [datetime.now(tz=ZoneInfo("UTC")).isoformat()]
    return member


def needs_no_show_check(member: dict, date_key: str) -> bool:
    """True if `date_key` has no history entry at all yet (never dropped, never PTO'd)."""
    return date_key not in member.get("history", {})


def prune_and_count_strikes(strikes: list[str], window_days: int, now: datetime | None = None) -> tuple[list[str], int]:
    """Return (strikes still inside the rolling window, count of those strikes)."""
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    cutoff = now - timedelta(days=window_days)
    kept = []
    for raw in strikes:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZoneInfo("UTC"))
        if ts >= cutoff:
            kept.append(raw)
    return kept, len(kept)


def strike_count(member: dict, window_days: int, now: datetime | None = None) -> int:
    _, count = prune_and_count_strikes(member.get("strikes", []), window_days, now)
    return count


def should_lose_role(member: dict, threshold: int, window_days: int, now: datetime | None = None) -> bool:
    return strike_count(member, window_days, now) >= threshold


def restore_streak(member: dict, value: int) -> dict:
    member = dict(member)
    member["streak"] = max(0, value)
    return member


def clear_strikes(member: dict) -> dict:
    member = dict(member)
    member["strikes"] = []
    return member


def set_rating(member: dict, date_key: str, photo_index: int, rating: str) -> dict:
    """Record an owner rating for one specific photo within a day's history
    entry. No-ops (returns `member` unchanged) if that day has no history
    entry at all -- the caller (the rating-button handler) is expected to
    have already confirmed the prompt is still valid via its own pending-
    ratings bookkeeping; this is just a defensive guard against stale state,
    not the source of truth for whether a rating is allowed.
    """
    if rating not in VALID_RATINGS:
        raise ValueError(f"Unknown rating: {rating!r}")

    member = dict(member)
    history = dict(member.get("history", {}))
    entry = history.get(date_key)
    if entry is None:
        return member

    entry = dict(entry)
    ratings = dict(entry.get("ratings", {}))
    ratings[str(photo_index)] = rating
    entry["ratings"] = ratings
    history[date_key] = entry
    member["history"] = history
    return member


def calendar_status(member: dict, date_key: str) -> str | None:
    entry = member.get("history", {}).get(date_key)
    if entry is None:
        return None
    return entry.get("status")


def week_dates(reference: datetime) -> list[datetime]:
    """Monday..Sunday for the week containing `reference` (local, naive-date arithmetic)."""
    monday = reference - timedelta(days=reference.weekday())
    return [monday + timedelta(days=i) for i in range(7)]
