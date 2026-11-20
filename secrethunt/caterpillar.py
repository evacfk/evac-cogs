"""Pure logic for The Caterpillar's Question (kind "profile"): no discord or redbot imports, so all of it is testable.

A profile puzzle asks the member something only they can look up: the month and year they joined this server. Pressing the
pinned Caterpillar (in any pool room) starts the puzzle and asks the question; the member answers by DM. A right answer
sends the Caterpillar to sleep until the next 9:00 Pacific (the same wait as the other Easy puzzles), and the final clue is
the member's word slid forward by the month they told him, so every member's clue differs even when two share an answer.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import engine
from .constants import PROFILE_WRONG_PER_DAY

MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december")
# Every spelling a member might fairly use: the full name, the three-letter form, and "sept".
MONTH_WORDS = {**{name: i + 1 for i, name in enumerate(MONTH_NAMES)}, **{name[:3]: i + 1 for i, name in enumerate(MONTH_NAMES)},
               "sept": 9}
# Discord shows a join date in the viewer's own time zone, so the month a member reads off their profile can be one day
# either side of the UTC month. Every offset on Earth is accepted so a fair reading is never marked wrong.
OFFSET_HOURS = range(-12, 15)


def parse_when(text: str):
    """What the member wrote: ("ok", month, year), ("partial", None, None) when it names only a month or only a year (or
    a month with no year), and ("none", None, None) when it looks like neither. Accepts "March 2023", "mar 2023",
    "2023 March", "3/2023", "03-2023" and "2023-03"."""
    low = (text or "").lower()
    year_match = re.search(r"(?<!\d)(20\d\d)(?!\d)", low)
    year = int(year_match.group(1)) if year_match else None
    month = next((MONTH_WORDS[w] for w in re.findall(r"[a-z]+", low) if w in MONTH_WORDS), None)
    if month is None and year is not None:  # numeric month next to the year: 3/2023, 03-2023, 2023-03
        rest = low.replace(year_match.group(1), " ", 1)
        number = re.search(r"(?<!\d)(\d{1,2})(?!\d)", rest)
        if number and 1 <= int(number.group(1)) <= 12:
            month = int(number.group(1))
    if month is not None and year is not None:
        return "ok", month, year
    if month is not None or year is not None:
        return "partial", None, None
    return "none", None, None


def valid_months(joined_at) -> set:
    """Every (year, month) a member could fairly read off their own join date, across all time zones."""
    if joined_at is None:
        return set()
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=timezone.utc)
    return {(d.year, d.month) for d in (joined_at.astimezone(timezone(timedelta(hours=h))) for h in OFFSET_HOURS)}


def check(state: dict, text: str, joined_at, now: float, test_mode: bool = False) -> str:
    """The member DM'd something while their Caterpillar question is open. Returns ignore (not an attempt: leave the DM for
    other handlers) / partial (no penalty) / locked / wrong / correct. A right answer moves the member to the waiting stage
    (due at the next 9:00 Pacific) and remembers the month they gave, which is the shift of the final clue."""
    if not state or state.get("stage") != engine.STAGE_RIDDLE:
        return "ignore"
    kind, month, year = parse_when(text)
    if kind == "none":
        return "ignore"
    if kind == "partial":
        return "partial"
    today = engine.pacific_date(now)
    if state["wrong_date"] != today:
        state["wrong_date"], state["wrong_count"] = today, 0
    if state["wrong_count"] >= PROFILE_WRONG_PER_DAY:
        return "locked"
    if (year, month) in valid_months(joined_at):
        state["stage"], state["found_ts"] = engine.STAGE_FOUND, now
        state["due_ts"] = engine.next_unlock(now, test_mode)
        state["shift"] = month
        return "correct"
    state["wrong_count"] += 1
    return "wrong"


def slide(word: str, shift: int) -> str:
    """The word with every letter moved `shift` places on (a to b for 1), wrapping z to a. Non-letters stay as they are."""
    return "".join(chr((ord(c) - 97 + shift) % 26 + 97) if "a" <= c <= "z" else c for c in word.lower())
