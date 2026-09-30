"""Pure logic for bumpreward. No discord/redbot imports, so it is unit-testable anywhere."""
from __future__ import annotations

import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")

DISBOARD_ID = 302050872383242240
BUMP_COOLDOWN_SECONDS = 7200

_SUCCESS_RE = re.compile(r"bump\s+done", re.I)
_COOLDOWN_RE = re.compile(r"wait\s+another\s+(\d+)\s+(minute|minutes|hour|hours)", re.I)


def is_success_text(text: str) -> bool:
    """True if Disboard's reply text is the 'Bump done!' confirmation."""
    return bool(text and _SUCCESS_RE.search(text))


def parse_cooldown_seconds(text: str) -> int | None:
    """Parse 'Please wait another 37 minutes until the server can be bumped' -> seconds.

    Returns None when the text isn't a cooldown notice. Rounds up a little: Disboard
    only reports whole minutes, so the real remaining time is (n-1, n] minutes.
    """
    if not text:
        return None
    m = _COOLDOWN_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * (3600 if unit.startswith("hour") else 60)


def _la(ts: float | datetime | None) -> datetime:
    if ts is None:
        return datetime.now(TZ)
    if isinstance(ts, datetime):
        return ts.astimezone(TZ)
    return datetime.fromtimestamp(ts, TZ)


def day_key(ts: float | datetime | None = None) -> str:
    """ISO date of `ts` in America/Los_Angeles (daily board rollover)."""
    return _la(ts).date().isoformat()


def week_key(ts: float | datetime | None = None) -> str:
    """ISO week (Mon-Sun) in America/Los_Angeles, e.g. '2026-W40'."""
    y, w, _ = _la(ts).isocalendar()
    return f"{y}-W{w:02d}"


def month_key(ts: float | datetime | None = None) -> str:
    """Calendar month in America/Los_Angeles, e.g. '2026-09'."""
    return _la(ts).strftime("%Y-%m")


def update_run(last_bumper: int, run: int, bumper: int) -> int:
    """Consecutive bumps in a row by the same member.

    Same bumper as last time extends the run; anyone else (or no previous bumper)
    starts a fresh run of 1.
    """
    if last_bumper and last_bumper == bumper:
        return max(run, 1) + 1
    return 1


def streak_bonus_pct(run: int, per_step_pct: int, max_steps: int) -> int:
    """Bonus % for a run. First bump = 0%; each further bump in a row adds `per_step_pct`, capped at `max_steps` steps."""
    steps = min(max(run - 1, 0), max(max_steps, 0))
    return steps * max(per_step_pct, 0)


def bump_period(counts_store: dict | None, key: str) -> dict[str, int]:
    """Counts for the current period. A stored bucket whose key is stale counts as empty."""
    if not counts_store or counts_store.get("key") != key:
        return {}
    return dict(counts_store.get("counts", {}))


def add_bump(counts_store: dict | None, key: str, member_id: int) -> dict:
    """Return a new bucket for `key` with one more bump for `member_id` (stale bucket is reset first)."""
    counts = bump_period(counts_store, key)
    counts[str(member_id)] = counts.get(str(member_id), 0) + 1
    return {"key": key, "counts": counts}


def roll_reward(min_reward: int, max_reward: int, rng: random.Random | None = None) -> int:
    lo, hi = sorted((int(min_reward), int(max_reward)))
    return (rng or random).randint(lo, hi)


def final_reward(base: int, bonus_pct: int) -> int:
    return base + (base * bonus_pct) // 100


def format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def rank_counts(counts: dict[str, int], limit: int = 10) -> list[tuple[str, int]]:
    """Highest count first; ties broken by id so ordering is stable."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
