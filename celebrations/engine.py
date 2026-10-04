"""Pure date logic for celebrations: no discord.py / redbot imports, fully unit-testable."""
from __future__ import annotations

import calendar
import random
import re
from datetime import date, datetime, timedelta
from typing import NamedTuple, Optional

from .constants import MIN_AGE, RECENT_CELEBRATION_DAYS, TZ

MONTHS = {
    name.lower(): i
    for i in range(1, 13)
    for name in (calendar.month_name[i], calendar.month_abbr[i])
}
MONTHS["sept"] = 9


class Birthday(NamedTuple):
    month: int
    day: int
    year: Optional[int]


class BirthdayError(ValueError):
    pass


def local_today(now_ts: float) -> date:
    return datetime.fromtimestamp(now_ts, TZ).date()


def local_hour(now_ts: float) -> int:
    return datetime.fromtimestamp(now_ts, TZ).hour


def _valid_md(month: int, day: int) -> bool:
    if not 1 <= month <= 12:
        return False
    return 1 <= day <= calendar.monthrange(2000, month)[1]  # 2000 is a leap year: Feb 29 allowed


def _check_year(year: Optional[int], month: int, day: int, today: date) -> Optional[int]:
    if year is None:
        return None
    if year < 100:
        raise BirthdayError("Use a four-digit year, like `1998`.")
    if not 1900 <= year <= today.year:
        raise BirthdayError("That year doesn't look right.")
    if month == 2 and day == 29 and not calendar.isleap(year):
        raise BirthdayError(f"{year} wasn't a leap year, so there was no Feb 29.")
    if age_on(year, month, day, today) < MIN_AGE:
        raise BirthdayError("That year doesn't look right.")
    return year


def parse_birthday(text: str, today: date) -> Birthday:
    """Accepts `March 14`, `14 March`, `Mar 14 1998`, `1998-03-14`, `14/3`, `3/14/1998`, `14.03.1998`.

    Purely numeric dates are only accepted when they can't be misread: `03/04`
    could be March 4 (US) or 3 April (Europe), so that raises and asks for the
    month name instead.
    """
    raw = (text or "").strip().lower().replace(",", " ")
    if not raw:
        raise BirthdayError("Give me a date, like `March 14` or `March 14 1998`.")
    words = re.findall(r"[a-z]+|\d+", raw)
    month_words = [w for w in words if w.isalpha()]
    nums = [int(w) for w in words if w.isdigit()]
    noise = [w for w in month_words if w not in MONTHS and w not in ("st", "nd", "rd", "th", "of")]
    if noise:
        raise BirthdayError("I couldn't read that. Try `March 14` or `March 14 1998`.")
    month_names = [w for w in month_words if w in MONTHS]

    if month_names:
        if len(month_names) != 1 or not 1 <= len(nums) <= 2:
            raise BirthdayError("I couldn't read that. Try `March 14` or `March 14 1998`.")
        month = MONTHS[month_names[0]]
        day = nums[0]
        year = nums[1] if len(nums) == 2 else None
    else:
        if len(nums) == 3 and nums[0] >= 1000:  # 1998-03-14
            year, month, day = nums
        elif len(nums) in (2, 3):
            a, b = nums[0], nums[1]
            year = nums[2] if len(nums) == 3 else None
            if a > 12 and b <= 12:
                day, month = a, b
            elif b > 12 and a <= 12:
                month, day = a, b
            elif a == b:
                month = day = a
            else:
                raise BirthdayError(
                    f"`{text.strip()}` could be read two ways (US vs European order). "
                    "Write the month as a word, like `March 14`."
                )
        else:
            raise BirthdayError("I couldn't read that. Try `March 14` or `March 14 1998`.")

    if not _valid_md(month, day):
        raise BirthdayError("That date doesn't exist.")
    return Birthday(month, day, _check_year(year, month, day, today))


def celebration_date(month: int, day: int, year: int) -> date:
    """The date a month/day is celebrated in `year` (Feb 29 -> Feb 28 in non-leap years)."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def is_today(month: int, day: int, today: date) -> bool:
    return celebration_date(month, day, today.year) == today


def next_occurrence(month: int, day: int, today: date) -> date:
    this_year = celebration_date(month, day, today.year)
    return this_year if this_year >= today else celebration_date(month, day, today.year + 1)


def age_on(year: int, month: int, day: int, today: date) -> int:
    return today.year - year - ((today.month, today.day) < (month, day))


def turning_age(year: Optional[int], month: int, day: int, today: date) -> Optional[int]:
    """Age reached on today's celebration (handles Feb 29 celebrated on Feb 28)."""
    if year is None:
        return None
    return today.year - year


def anniversary_years(joined: date, today: date) -> int:
    """Years completed *today* if today is the join anniversary, else 0."""
    if today.year <= joined.year:
        return 0
    if celebration_date(joined.month, joined.day, today.year) != today:
        return 0
    return today.year - joined.year


def recently_celebrated(last_iso: Optional[str], today: date, window: int = RECENT_CELEBRATION_DAYS) -> bool:
    if not last_iso:
        return False
    try:
        last = date.fromisoformat(last_iso)
    except ValueError:
        return False
    return timedelta(0) <= today - last < timedelta(days=window)


def pick_gift(gift_min: int, gift_max: int, rng: random.Random | None = None) -> int:
    lo, hi = sorted((max(0, int(gift_min)), max(0, int(gift_max))))
    if hi <= 0:
        return 0
    return (rng or random).randint(lo, hi)


def fmt_birthday(month: int, day: int, year: Optional[int] = None) -> str:
    out = f"{calendar.month_name[month]} {day}"
    return f"{out}, {year}" if year else out


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
