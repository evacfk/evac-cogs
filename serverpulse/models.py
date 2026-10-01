"""Time handling, slot geometry, and the on-disk day-document schema.

Zero discord/redbot imports -- everything here is pure and unit-testable.

A *slot* is one real clock hour in America/Los_Angeles. Normal days have 24
slots, the spring-forward day 23, and the fall-back day 25 (the repeated 1am
hour is keyed "01" and "01b"). Keying by real hours -- not by hour-of-day
numbers -- is what keeps DST from corrupting averages.

Day document (one JSON file per local date):
    {
      "h": {"<HH or HHb>": {"m","u","pk","cs","gi","gs","f","l","c": {channel_id: [msgs, chatters]}}},
      "u":  {"<user_id>": msgs}      # live per-user counts (top chatters), 90-day retention
      "ub": {"<user_id>": msgs}      # same, but written by backfill
      "joins": int, "leaves": int
    }
Hour record fields: m=messages, u=distinct chatters, pk=peak concurrent chatters
(distinct authors in a rolling 5 min), cs=sum of concurrency samples (avg
concurrency = cs/m), gi=longest silent gap *between* messages inside the hour
(seconds), gs=offset of that gap's start, f/l=offset of first/last message from
the hour start. f/l/gi let us reconstruct the longest silent stretch across hours
exactly. A slot with no record had zero messages.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import NamedTuple

from .constants import TIMEZONE, WEEKDAY_ALIASES

HOUR_SECONDS = 3600
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SlotInfo(NamedTuple):
    date: str
    key: str  # "HH" or "HHb"
    hour: int  # local hour 0-23
    start_ts: int
    end_ts: int
    weekday: int  # 0=Monday of the local date


def new_day_doc() -> dict:
    return {"h": {}, "u": {}, "ub": {}, "joins": 0, "leaves": 0}


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TIMEZONE)


def date_key_for_ts(ts: float) -> str:
    return local_dt(ts).date().isoformat()


def parse_date_key(value: str) -> date:
    if not DATE_RE.match(value):
        raise ValueError(f"bad date key: {value!r}")
    return date.fromisoformat(value)


def day_start_ts(d: date) -> int:
    """Epoch seconds of local midnight at the start of `d`."""
    return int(datetime(d.year, d.month, d.day, tzinfo=TIMEZONE).timestamp())


def day_end_ts(d: date) -> int:
    """Epoch seconds of local midnight at the *end* of `d` (start of the next day)."""
    return day_start_ts(d + timedelta(days=1))


def floor_hour(ts: float) -> int:
    # LA's UTC offset is always a whole number of hours, so UTC hour boundaries
    # are local hour boundaries.
    return int(ts) - int(ts) % HOUR_SECONDS


def ceil_hour(ts: float) -> int:
    return math.ceil(ts / HOUR_SECONDS) * HOUR_SECONDS


def slot_key(hour: int, fold: int) -> str:
    return f"{hour:02d}{'b' if fold else ''}"


def slot_for_ts(ts: float) -> SlotInfo:
    start = floor_hour(ts)
    dt = local_dt(start)
    return SlotInfo(dt.date().isoformat(), slot_key(dt.hour, dt.fold), dt.hour, start, start + HOUR_SECONDS, dt.weekday())


@lru_cache(maxsize=4096)
def day_slots(date_key: str) -> tuple[SlotInfo, ...]:
    """Every real clock hour of local date `date_key`, in order (23/24/25 slots)."""
    d = parse_date_key(date_key)
    start, end = day_start_ts(d), day_end_ts(d)
    return tuple(slot_for_ts(t) for t in range(start, end, HOUR_SECONDS))


def date_range(start: date, end: date):
    """Inclusive range of dates."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def week_bounds(d: date) -> tuple[date, date]:
    """Monday..Sunday week containing `d`."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


# -- user-input parsing -------------------------------------------------------

def parse_weekday(token: str) -> int | None:
    return WEEKDAY_ALIASES.get(token.strip().lower())


_HOUR_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$", re.I)


def parse_hour(text: str) -> int | None:
    """'8pm' / '8 pm' / '20' / '20:00' / '12am' / 'noon' / 'midnight' -> 0..23, else None."""
    t = text.strip().lower().replace(" ", "")
    if t == "noon":
        return 12
    if t == "midnight":
        return 0
    m = _HOUR_RE.match(t)
    if not m:
        return None
    hour = int(m.group(1))
    suffix = (m.group(3) or "")[:1]
    if suffix:
        if not 1 <= hour <= 12:
            return None
        if suffix == "a":
            return 0 if hour == 12 else hour
        return 12 if hour == 12 else hour + 12
    return hour if 0 <= hour <= 23 else None


def parse_day_arg(token: str | None, today: date) -> date | None:
    """None/'today' -> today, 'yesterday', or YYYY-MM-DD; anything else -> None."""
    if token is None or token.lower() == "today":
        return today
    if token.lower() == "yesterday":
        return today - timedelta(days=1)
    try:
        return parse_date_key(token)
    except ValueError:
        return None
