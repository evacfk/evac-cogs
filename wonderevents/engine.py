"""Pure logic for wonderevents: time parsing, RSVPs, attendance, scheduling decisions.

No discord.py / redbot imports, so it is fully unit-testable.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import ATTEND_MIN_MINUTES, POLL_CLOSE_BEFORE_HOURS, POLL_MAX_HOURS, SAMPLE_MINUTES

TZ_ALIASES = {
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pacific": "America/Los_Angeles",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver", "mountain": "America/Denver",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago", "central": "America/Chicago",
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "eastern": "America/New_York",
    "uk": "Europe/London", "gmt": "Europe/London", "bst": "Europe/London", "london": "Europe/London",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin", "eet": "Europe/Athens", "eest": "Europe/Athens",
    "utc": "UTC",
}
MONTHS = {n.lower(): i for i in range(1, 13) for n in (calendar.month_name[i], calendar.month_abbr[i])}
MONTHS["sept"] = 9
WEEKDAYS = {n.lower(): i for i in range(7) for n in (calendar.day_name[i], calendar.day_abbr[i])}
WEEKDAYS.update({"tues": 1, "weds": 2, "thur": 3, "thurs": 3})

_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$")


class WhenError(ValueError):
    pass


def resolve_tz(name: str | None):
    if not name:
        return None
    key = name.strip().lower()
    if key in TZ_ALIASES:
        return ZoneInfo(TZ_ALIASES[key])
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _parse_time(tokens: list[str]) -> tuple[int, int] | None:
    joined = " ".join(tokens).replace(" ", "")
    if joined == "noon":
        return 12, 0
    if joined == "midnight":
        return 0, 0
    m = _TIME_RE.match(joined)
    if not m:
        return None
    hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if minute > 59:
        return None
    if mer:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if mer.startswith("p") else 0)
    elif hour > 23:
        return None
    return hour, minute


def parse_when(text: str, now: datetime, default_tz) -> datetime:
    """'Sat 9pm ET', 'Oct 17 9:30pm', 'tomorrow 8pm', '2026-10-17 21:00 Europe/London' -> aware datetime.

    A missing timezone means the staff member's own (default_tz). A date without a
    year is the next time it comes round. Must be in the future.
    """
    raw = " " + (text or "").strip().replace(",", " ") + " "
    raw = re.sub(r"\s(?:at|@)\s", " ", raw, flags=re.IGNORECASE).strip()
    if not raw:
        raise WhenError("When is it? e.g. `Sat 9pm ET` or `Oct 17 9:30pm`.")
    tz = None
    rest = []
    for tok in raw.split():
        low = tok.lower()
        is_tz = low in TZ_ALIASES or ("/" in tok and tok[0].isalpha())
        cand = resolve_tz(tok) if is_tz else None
        if cand is not None and tz is None:
            tz = cand
        elif is_tz and cand is None and "/" in tok:
            raise WhenError(f"I don't know the timezone `{tok}`.")
        else:
            rest.append(low)
    tz = tz or default_tz
    local_now = now.astimezone(tz)

    # pull out a time (last 1-2 tokens), the rest is the date
    clock = None
    for take in (2, 1):
        if len(rest) >= take:
            clock = _parse_time(rest[-take:])
            if clock is not None:
                date_tokens = rest[:-take]
                break
    if clock is None:
        raise WhenError("I need a time too, e.g. `9pm`, `9:30pm` or `21:00`.")
    day = _parse_day(date_tokens, local_now, clock)
    start = datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=tz)
    if start <= now + timedelta(minutes=5):
        raise WhenError("That time is in the past (or too soon).")
    if start > now + timedelta(days=366):
        raise WhenError("That's more than a year away.")
    return start


def _parse_day(tokens: list[str], local_now: datetime, clock: tuple[int, int]) -> date:
    today = local_now.date()
    later_today = (clock[0], clock[1]) > (local_now.hour, local_now.minute)
    if not tokens:
        return today if later_today else today + timedelta(days=1)
    if tokens == ["today"] or tokens == ["tonight"]:
        return today
    if tokens == ["tomorrow"]:
        return today + timedelta(days=1)
    words = [t for t in tokens if t not in ("this", "next", "on", "the", "of")]
    if len(words) == 1 and words[0] in WEEKDAYS:
        ahead = (WEEKDAYS[words[0]] - today.weekday()) % 7
        if ahead == 0 and not later_today:
            ahead = 7
        return today + timedelta(days=ahead)  # "next sat" = the coming Saturday, same as "sat"
    words = [w for w in words if w not in WEEKDAYS]  # "sat oct 17" -> the weekday is decoration
    nums = [int(re.sub(r"(st|nd|rd|th)$", "", w)) for w in words if re.fullmatch(r"\d+(st|nd|rd|th)?", w)]
    months = [MONTHS[w] for w in words if w in MONTHS]
    year = None
    if len(words) == 1 and re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", words[0]):
        year, month, dd = (int(x) for x in words[0].split("-"))
    elif len(words) == 1 and re.fullmatch(r"\d{1,2}/\d{1,2}", words[0]):
        a, b = (int(x) for x in words[0].split("/"))
        if a > 12 >= b:
            dd, month = a, b
        elif b > 12 >= a:
            month, dd = a, b
        elif a == b:
            month = dd = a
        else:
            raise WhenError(f"`{words[0]}` could be read two ways. Write the month as a word, like `Oct 17`.")
    elif len(months) == 1 and 1 <= len(nums) <= 2:
        month = months[0]
        days_ = [n for n in nums if n <= 31]
        if len(days_) != 1:
            raise WhenError("I couldn't read the date. Try `Sat`, `Oct 17`, `tomorrow` or `2026-10-17`.")
        dd = days_[0]
        year = next((n for n in nums if n >= 1000), None)
    else:
        raise WhenError("I couldn't read the date. Try `Sat`, `Oct 17`, `tomorrow` or `2026-10-17`.")
    try:
        candidate = date(year or today.year, month, dd)
    except ValueError:
        raise WhenError("That date doesn't exist.") from None
    if year is None and candidate < today:  # today-but-already-passed stays today, so it errors as "past"
        candidate = date(today.year + 1, month, dd)
    return candidate


def poll_hours(start_ts: float, now_ts: float) -> int:
    """Native polls run whole hours: close a little before the event, at least 1h, at most a week."""
    hours = int((start_ts - now_ts) // 3600) - POLL_CLOSE_BEFORE_HOURS
    return max(1, min(POLL_MAX_HOURS, hours))


def poll_options(text: str, limit: int = 10, max_len: int = 55) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip(" -•*\t")
        if line and line.lower() not in (o.lower() for o in out):
            out.append(line[:max_len])
    return out[:limit]


def toggle_rsvp(rsvp: dict, uid: int, choice: str) -> str | None:
    """Click a button: set that choice, or clear it if it was already set. Returns the new state."""
    key = str(uid)
    if rsvp.get(key) == choice:
        rsvp.pop(key, None)
        return None
    rsvp[key] = choice
    return choice


def rsvp_lists(rsvp: dict) -> dict[str, list[int]]:
    out = {"going": [], "maybe": [], "no": []}
    for uid, choice in rsvp.items():
        if choice in out:
            out[choice].append(int(uid))
    return out


def due_actions(ev: dict, now_ts: float, remind_minutes: int) -> list[str]:
    """What the scheduler should do for one event right now, in order."""
    if ev.get("cancelled") or ev.get("ended"):
        return []
    start = ev["start_ts"]
    end = start + ev.get("duration", 180) * 60
    actions = []
    if now_ts >= end:
        return ["end"]
    if not ev.get("reminded") and remind_minutes and start - remind_minutes * 60 <= now_ts < start:
        actions.append("remind")
    if now_ts >= start and not ev.get("started"):
        actions.append("start")
    if now_ts >= start and now_ts - (ev.get("last_sample") or 0) >= SAMPLE_MINUTES * 60 - 5:
        actions.append("sample")
    return actions


def add_sample(attend: dict, present_ids, minutes: int = SAMPLE_MINUTES) -> None:
    for uid in present_ids:
        attend[str(uid)] = int(attend.get(str(uid), 0)) + minutes


def attendees(attend: dict, threshold: int = ATTEND_MIN_MINUTES) -> list[int]:
    return sorted(int(uid) for uid, mins in attend.items() if int(mins) >= threshold)


def regulars(events: list[dict], since_ts: float) -> list[tuple[int, int]]:
    counts: dict[int, int] = {}
    for ev in events:
        if ev.get("cancelled") or ev["start_ts"] < since_ts:
            continue
        for uid in attendees(ev.get("attend") or {}):
            counts[uid] = counts.get(uid, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
