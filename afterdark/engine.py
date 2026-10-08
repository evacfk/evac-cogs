"""Pure logic for afterdark -- no discord.py / redbot imports, so it is fully
unit-testable without a bot. The Cog class in afterdark.py owns all I/O.
"""
import random
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set
from zoneinfo import ZoneInfo

from .constants import DAY, LA_TZ, TOUCH_RESOLUTION

# ------------------------------------------------------------- eligibility

OK = "ok"
EXCLUDED = "excluded"
UNDERAGE = "underage"
NO_AGE_ROLE = "no_age_role"
NO_ADULT_CHAT = "no_adult_chat"
LEVEL_LOW = "level_low"
LEVEL_UNKNOWN = "level_unknown"

# Order matters: the first failing rule wins, and "excluded" / "underage" are
# checked before anything that could be satisfied by a role.
ELIGIBILITY_CODES = (OK, EXCLUDED, UNDERAGE, NO_AGE_ROLE, NO_ADULT_CHAT, LEVEL_LOW, LEVEL_UNKNOWN)


def eligibility(
    *,
    role_ids: Iterable[int],
    excluded: bool,
    underage_ids: Iterable[int],
    age_ids: Iterable[int],
    adult_role_id: int,
    level: Optional[int],
    min_level: int,
    check_level: bool = True,
) -> str:
    """Can this member hold Rabbit Hole access?

    `check_level=False` is used for *retention* (sweeps, mod grants): level is
    only required to be admitted, never to keep access, so a later level drop
    (or a LevelUp read failure) must not strip someone.
    An unreadable level (None) fails closed when it is checked.
    """
    roles = set(role_ids)
    if excluded:
        return EXCLUDED
    if roles & set(underage_ids):
        return UNDERAGE
    if not roles & set(age_ids):
        return NO_AGE_ROLE
    if adult_role_id not in roles:
        return NO_ADULT_CHAT
    if check_level:
        if level is None:
            return LEVEL_UNKNOWN
        if level < min_level:
            return LEVEL_LOW
    return OK


# ---------------------------------------------------------------- invitations

def la_date(now: float) -> str:
    """America/Los_Angeles calendar date (YYYY-MM-DD) for a unix timestamp."""
    return datetime.fromtimestamp(now, ZoneInfo(LA_TZ)).strftime("%Y-%m-%d")


def invite_count(rng: random.Random, lo: int, hi: int) -> int:
    lo = max(0, int(lo))
    hi = max(lo, int(hi))
    return rng.randint(lo, hi)


def shuffled_candidates(candidates: Iterable[int], rng: random.Random) -> List[int]:
    """Deterministic base order, then shuffled, so a seeded rng is testable.
    The caller walks this list until it has enough successful DMs."""
    pool = sorted(set(candidates))
    rng.shuffle(pool)
    return pool


def rank_by_level(levels: Dict[int, int], snoozed: Dict[str, float], now: float,
                  skip: Iterable[int] = ()) -> List[int]:
    """Who to suggest next: highest level first, ties broken by the older account
    (smaller id). Anyone snoozed ("not now") and not yet due, or in `skip`, is left out."""
    skipped = {int(u) for u in skip}
    ready = [uid for uid in levels
             if uid not in skipped and float(snoozed.get(str(uid), 0)) <= now]
    return sorted(ready, key=lambda uid: (-int(levels[uid]), uid))


def snooze_until(now: float, days: float) -> float:
    return now + max(0.0, days) * DAY


def prune_snoozed(snoozed: Dict[str, float], now: float) -> Dict[str, float]:
    return {uid: until for uid, until in snoozed.items() if float(until) > now}


def new_round(date: str, target: int) -> dict:
    return {"date": date, "target": int(target), "sent": 0, "done": False, "prompt": {}}


def round_needs_prompt(rnd: dict) -> bool:
    """True when a round is open (not finished) and nothing is waiting on a mod."""
    return bool(rnd) and not rnd.get("done") and rnd.get("sent", 0) < rnd.get("target", 0) and not rnd.get("prompt")


def expired_invites(invites: Dict[str, dict], now: float, ttl_days: float) -> List[str]:
    ttl = ttl_days * DAY
    return [uid for uid, inv in invites.items() if now - float(inv.get("ts", 0)) >= ttl]


# ---------------------------------------------------------------- inactivity

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_REMOVE = "remove"


def should_record(existing: Optional[float], now: float, resolution: float = TOUCH_RESOLUTION) -> bool:
    """Whether a new activity event should overwrite the stored timestamp."""
    if existing is None:
        return True
    if now < existing:  # clock stepped backwards: never keep a future timestamp
        return True
    return (now - existing) >= resolution


def new_membership(now: float) -> dict:
    return {"since": now, "last": now, "warned": 0.0}


def anchor(entry: dict) -> float:
    """The moment the idle clock counts from: join or last activity, whichever is later."""
    return max(float(entry.get("since", 0)), float(entry.get("last", 0)))


def idle_days(entry: dict, now: float) -> float:
    return max(0.0, (now - anchor(entry)) / DAY)


def inactivity_status(entry: dict, now: float, warn_days: float, remove_days: float) -> str:
    """OK / WARN / REMOVE for one interest membership.

    A warning only counts if it was sent *after* the member's latest activity
    (activity after a warning resets the cycle), and each cycle warns once.
    """
    idle = idle_days(entry, now)
    if idle >= remove_days:
        return STATUS_REMOVE
    if idle >= warn_days:
        warned = float(entry.get("warned", 0))
        if warned and warned >= anchor(entry):
            return STATUS_OK
        return STATUS_WARN
    return STATUS_OK


def days_left(entry: dict, now: float, remove_days: float) -> float:
    return max(0.0, remove_days - idle_days(entry, now))


# ------------------------------------------------------------------ lapsed

def lapsed_has(lapsed: Dict[str, List[int]], key: str, uid: int) -> bool:
    return int(uid) in {int(u) for u in lapsed.get(key, [])}


def lapsed_add(lapsed: Dict[str, List[int]], key: str, uid: int) -> Dict[str, List[int]]:
    out = {k: list(v) for k, v in lapsed.items()}
    members = out.setdefault(key, [])
    if int(uid) not in {int(u) for u in members}:
        members.append(int(uid))
    return out


def lapsed_remove(lapsed: Dict[str, List[int]], key: str, uid: int) -> Dict[str, List[int]]:
    out = {k: [int(u) for u in v if int(u) != int(uid)] for k, v in lapsed.items()}
    return {k: v for k, v in out.items() if v}


# ------------------------------------------------------------------- helpers

_KEY_RE = re.compile(r"[^a-z0-9-]+")


def normalize_key(text: str) -> str:
    """Interest keys: lowercase, letters/digits/dashes only, max 32 chars."""
    key = _KEY_RE.sub("-", text.strip().lower()).strip("-")
    return key[:32]


_CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")


def valid_emoji(token: str) -> bool:
    """Would Discord accept this as a button emoji?

    discord.py passes any string through as a "unicode emoji" and Discord then
    rejects it with 'Invalid emoji'. Real emoji are non-ASCII and contain no
    letters/digits; a word ("Feet"), a shortcode (":foot:") or a typo is not.
    Custom emoji must be in the <:name:id> form.
    """
    token = (token or "").strip()
    if not token:
        return False
    if _CUSTOM_EMOJI_RE.match(token):
        return True
    return (not token.isascii()) and not any(ch.isalnum() for ch in token)


def split_emoji_and_name(emoji: str, name: str) -> tuple:
    """If the 'emoji' argument is really the first word of the name, move it.
    Returns (emoji, name) with an emoji that is either valid or empty."""
    emoji = (emoji or "").strip()
    name = (name or "").strip()
    if emoji and not valid_emoji(emoji):
        name = f"{emoji} {name}".strip()
        emoji = ""
    return emoji, name


def parse_toggle(text: str) -> Optional[bool]:
    value = text.strip().lower()
    if value in ("on", "true", "yes", "enable", "enabled", "1"):
        return True
    if value in ("off", "false", "no", "disable", "disabled", "0"):
        return False
    return None


def circuit_open(action_count: int, limit: int) -> bool:
    """A live sweep that would act on more than `limit` members is aborted."""
    return action_count > limit


def cap_lines(lines: Sequence[str], cap: int) -> List[str]:
    if len(lines) <= cap:
        return list(lines)
    return list(lines[:cap]) + [f"...and {len(lines) - cap} more"]


def humanize_days(days: float) -> str:
    if days < 1:
        hours = max(1, int(round(days * 24)))
        return f"{hours} hour{'s' if hours != 1 else ''}"
    whole = int(round(days))
    return f"{whole} day{'s' if whole != 1 else ''}"


def validate_warn_remove(warn_days: float, remove_days: float) -> Optional[str]:
    """Returns an error message, or None if the pair is acceptable."""
    if warn_days <= 0 or remove_days <= 0:
        return "Both values must be greater than zero."
    if warn_days >= remove_days:
        return "The warning must come before removal (warn days < remove days)."
    return None
