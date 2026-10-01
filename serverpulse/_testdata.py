"""Synthetic-traffic helpers shared by the tests (not used by the cog)."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

from .constants import TIMEZONE
from .models import new_day_doc
from .tracker import GuildTracker


def local_ts(y, mo, d, h=0, mi=0, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=TIMEZONE).timestamp()


def docs_from_tracker(tracker: GuildTracker, now_ts: float) -> dict[str, dict]:
    snap = tracker.snapshot(now_ts)
    docs: dict[str, dict] = {}
    for dk, key, rec, _v, _c in snap.hours:
        docs.setdefault(dk, new_day_doc())["h"][key] = rec
    for dk, users in snap.user_deltas.items():
        docs.setdefault(dk, new_day_doc())["u"] = {str(u): n for u, n in users.items()}
    return docs


def traffic(start: date, ndays: int, *, seed: int = 1, peak_hour: int = 21, scale: float = 6.0,
            users: int = 60, channels=("111", "222", "333")):
    """Evening-peaked synthetic chat. Returns (docs, coverage_start_ts, end_ts)."""
    rng = random.Random(seed)
    t0 = datetime(start.year, start.month, start.day, tzinfo=TIMEZONE).timestamp()
    end = (datetime(start.year, start.month, start.day, tzinfo=TIMEZONE) + timedelta(days=ndays)).timestamp()
    tracker = GuildTracker(floor_ts=t0)
    t = t0
    while True:
        hour = datetime.fromtimestamp(t, TIMEZONE).hour
        rate = 0.2 + scale * math.exp(-((hour - peak_hour) ** 2) / 8)  # messages per minute
        t += rng.expovariate(rate / 60)
        if t >= end:
            break
        tracker.record(t, rng.randint(1, users), rng.choice(channels))
    return docs_from_tracker(tracker, end + 1), t0, end + 1


def hour_rec(msgs: int, *, users: int | None = None, peak: int | None = None, first: int = 0, last: int = 3599,
             inner_gap: int = 0, gap_start: int = 0, channels: dict | None = None) -> dict:
    return {
        "m": msgs, "u": users if users is not None else min(msgs, 5), "pk": peak if peak is not None else min(msgs, 3),
        "cs": msgs, "gi": inner_gap, "gs": gap_start, "f": first, "l": last,
        "c": channels if channels is not None else {"111": [msgs, min(msgs, 5)]},
    }


def flat_day(d: date, per_hour: dict[int, int] | int) -> dict:
    """A day doc with fixed message counts per local hour (int = same for all 24)."""
    doc = new_day_doc()
    for h in range(24):
        n = per_hour if isinstance(per_hour, int) else per_hour.get(h, 0)
        if n:
            doc["h"][f"{h:02d}"] = hour_rec(n)
    return doc
