"""Pure leave/rejoin bookkeeping for rejoinwatch. No discord.py or redbot imports."""
from typing import Dict, Iterable, List, Optional, Tuple

DAY = 86400

# Leave count at which a rejoin gets the one-time user-facing warning.
WARN_AT = 1
# From this many leaves on, nothing more is said to the user: it is a moderator decision.
ESCALATE_AT = 2

PREFIX = "rejoinwatch"
ACTIONS = ("ban", "keep")

Leaves = Dict[str, List[float]]


# ---------------------------------------------------------------- records

def window(stamps: Iterable[float], now: float, retention_days: float) -> List[float]:
    """Timestamps still inside the retention window, oldest first."""
    floor = now - retention_days * DAY
    return sorted(t for t in stamps if t >= floor)


def history(leaves: Leaves, user_id: int, now: float, retention_days: float) -> List[float]:
    return window(leaves.get(str(user_id), []), now, retention_days)


def count_leaves(leaves: Leaves, user_id: int, now: float, retention_days: float) -> int:
    return len(history(leaves, user_id, now, retention_days))


def record_leave(leaves: Leaves, user_id: int, ts: float, now: float, retention_days: float) -> List[float]:
    """Add one leave to `leaves` in place (expired ones for that user are dropped).

    Returns the user's in-window timestamps including this one, so len() is the count.
    """
    stamps = sorted(window(leaves.get(str(user_id), []), now, retention_days) + [ts])
    leaves[str(user_id)] = stamps
    return stamps


def prune(leaves: Leaves, now: float, retention_days: float) -> Tuple[Leaves, bool]:
    """A copy of `leaves` with expired leaves (and then-empty users) removed, plus whether anything changed."""
    kept: Leaves = {}
    for key, stamps in leaves.items():
        live = window(stamps, now, retention_days)
        if live:
            kept[key] = live
    return kept, kept != leaves


# --------------------------------------------------------------- decisions

def leave_action(count: int) -> str:
    """What a counted leave triggers: 'alert' (ping mods, with ban buttons) or 'silent'."""
    return "alert" if count >= ESCALATE_AT else "silent"


def rejoin_action(count: int) -> str:
    """What a join triggers given prior leaves: 'none', 'warn' (warn the user) or 'escalate' (mods only)."""
    if count >= ESCALATE_AT:
        return "escalate"
    if count >= WARN_AT:
        return "warn"
    return "none"


def is_recent(now: float, created_ts: float, window_seconds: float) -> bool:
    """True when an audit-log entry is close enough to now (either direction, to tolerate clock skew)."""
    return abs(now - created_ts) <= window_seconds


def can_resolve(role_ids: Iterable[int], mod_role_id: Optional[int], can_ban: bool, is_admin: bool) -> bool:
    """Who may press the ban / dismiss buttons."""
    return bool(is_admin or can_ban or (mod_role_id is not None and mod_role_id in set(role_ids)))


# -------------------------------------------------------------------- text

def count_phrase(n: int) -> str:
    return "1 time" if n == 1 else f"{n} times"


def times_phrase(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def render_warning(template: str, server: str) -> str:
    # str.replace, not str.format: staff-written text may contain stray braces.
    return template.replace("{server}", server)


def delivery_phrase(delivery: str, channel_mention: Optional[str] = None) -> str:
    if delivery == "dm":
        return "They have been warned via DM."
    if delivery == "channel":
        where = channel_mention or "the fallback channel"
        return f"Their DMs are closed, so they have been warned in {where}."
    return "I could not warn them (DMs closed and no usable fallback channel), so please warn them manually."


# ------------------------------------------------------------- button ids

def make_custom_id(action: str, user_id: int) -> str:
    return f"{PREFIX}:{action}:{user_id}"


def parse_custom_id(custom_id: Optional[str]) -> Optional[Tuple[str, int]]:
    """(action, user_id) for one of our buttons, else None."""
    parts = (custom_id or "").split(":")
    if len(parts) != 3 or parts[0] != PREFIX or parts[1] not in ACTIONS:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None
