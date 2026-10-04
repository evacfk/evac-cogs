"""Pure logic for the lurker cog -- no discord.py / redbot imports, so it is
fully unit-testable without a bot. The Cog class in lurker.py owns all I/O.
"""
import csv
import io
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

DAY = 86400

# Sources whose events are only *counted* in the weekly report, never listed
# individually (a 7,000-member backfill must not bloat Config or the report).
BULK_SOURCES = frozenset({"backfill", "undo"})

# Hard cap on individually stored report events between two reports.
MAX_EVENTS = 2000


# ------------------------------------------------------------ activity logic

def cutoff_ts(now: float, threshold_days: int) -> float:
    """Unix timestamp before which a member counts as inactive."""
    return now - threshold_days * DAY


# A member's stored timestamp is only refreshed when it is at least this old.
# The inactivity threshold is measured in days, so hour-level precision buys
# nothing -- but skipping the no-op refreshes keeps the activity map from being
# marked dirty (and the whole Config file rewritten) on every single message.
TOUCH_RESOLUTION = 6 * 3600


def should_record(existing: Optional[float], now: float,
                  resolution: float = TOUCH_RESOLUTION) -> bool:
    """Whether a new activity event should overwrite the stored timestamp."""
    if existing is None:
        return True
    if now < existing:  # clock stepped backwards: never keep a future timestamp
        return True
    return (now - existing) >= resolution


def resolve_last_active(cached: Optional[float], joined: Optional[float]) -> float:
    """Best-known last-activity time: tracked activity, else join date, else 0."""
    if cached is not None:
        return cached
    return joined if joined is not None else 0.0


def is_inactive(cached: Optional[float], joined: Optional[float], cutoff: float) -> bool:
    # No data at all (never seen AND unknown join date, e.g. a partial member
    # object) means "can't tell" -- never flag on missing information.
    if cached is None and joined is None:
        return False
    return resolve_last_active(cached, joined) < cutoff


def merge_last_active(existing: Dict[int, float], scanned: Dict[int, float]) -> Dict[int, float]:
    """Union of two {user_id: ts} maps, keeping the newest timestamp per user.

    Never lowers an existing timestamp, so seeding from a history scan can only
    ever make the data more accurate.
    """
    merged = dict(existing)
    for uid, ts in scanned.items():
        if ts > merged.get(uid, 0.0):
            merged[uid] = ts
    return merged


# ----------------------------------------------------------- report schedule

def report_due(now: float, last_report_ts: float, interval_days: int) -> bool:
    return (now - last_report_ts) >= interval_days * DAY


def trim_events(events: List[dict], cap: int = MAX_EVENTS) -> Tuple[List[dict], int]:
    """Keep the newest `cap` events. Returns (kept, number_dropped)."""
    if len(events) <= cap:
        return events, 0
    dropped = len(events) - cap
    return events[dropped:], dropped


# ------------------------------------------------------------- report render

def summarize(events: Iterable[dict], bulk: Dict[str, int]) -> dict:
    """Bucket report events. Event shape:
    {"uid": int, "name": str, "ts": float, "kind": "flag"|"unflag", "src": str}
    """
    events = list(events)
    flagged = [e for e in events if e["kind"] == "flag"]
    restored = [e for e in events if e["kind"] == "unflag"]
    return {
        "sweep": [e for e in flagged if e["src"] == "sweep"],
        "manual": [e for e in flagged if e["src"] not in ("sweep",)],
        "restored_self": sum(1 for e in restored if e["src"] == "post"),
        "restored_mod": sum(1 for e in restored if e["src"] != "post"),
        "bulk_flagged": int(bulk.get("backfill", 0)),
        "bulk_restored": int(bulk.get("undo", 0)),
        "dropped": int(bulk.get("dropped", 0)),
    }


def format_name_list(events: List[dict], max_chars: int = 900) -> str:
    """Bullet list that is guaranteed to fit an embed field (1024 chars)."""
    if not events:
        return "none"
    lines: List[str] = []
    used = 0
    for i, e in enumerate(events):
        line = f"• {e['name']} (`{e['uid']}`)"
        remaining = len(events) - i
        # reserve room for a "+N more" tail
        if used + len(line) + 1 > max_chars - 20:
            lines.append(f"…and {remaining} more")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def events_to_csv(events: Iterable[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["time_utc", "action", "source", "user_id", "name"])
    for e in events:
        when = datetime.fromtimestamp(e["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([when, e["kind"], e["src"], e["uid"], e["name"]])
    return buf.getvalue()


def candidates_to_csv(rows: Iterable[Tuple[int, str, Optional[float], str]]) -> str:
    """rows: (user_id, name, last_active_ts_or_None, basis)"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "name", "last_active_utc", "basis"])
    for uid, name, ts, basis in rows:
        when = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if ts else "never seen"
        )
        writer.writerow([uid, name, when, basis])
    return buf.getvalue()


# ------------------------------------------------------------- year club
#
# "N year club!" roles: a member holds exactly ONE club role, the one for the
# number of full years since their (current) join date. The anniversary is the
# same calendar date in America/Los_Angeles; a Feb 29 join counts on Mar 1 in
# non-leap years. Anyone past the highest configured role keeps the highest.

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# auto-pass circuit breaker: the hourly upkeep only ever has a handful of real
# anniversaries to apply; more than this means a config change, which must go
# through the explicit `.yearclub sync` instead.
YEARCLUB_AUTO_MAX = 50

_CLUB_NAME_RE = re.compile(r"^\W*(\d{1,2})\s*-?\s*(?:years?|yrs?)\s*club", re.IGNORECASE)


def years_completed(joined_ts: float, now_ts: float, tz=LOCAL_TZ) -> int:
    """Full years between the join and now, by local calendar date."""
    j = datetime.fromtimestamp(joined_ts, tz).date()
    n = datetime.fromtimestamp(now_ts, tz).date()
    years = n.year - j.year - ((n.month, n.day) < (j.month, j.day))
    return max(0, years)


def parse_club_role_name(name: str) -> Optional[int]:
    """'7 year club!' -> 7, '1 Year Club' -> 1, anything else -> None."""
    m = _CLUB_NAME_RE.match(name or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 1 else None


def club_target(years: int, mapping: Dict[int, int]) -> Optional[int]:
    """Role id for `years` (largest configured N <= years), or None under one year."""
    eligible = [n for n in mapping if n <= years]
    return mapping[max(eligible)] if eligible else None


def club_plan(current_ids: Set[int], target: Optional[int], club_ids: Set[int]) -> Tuple[Optional[int], Set[int]]:
    """(role to add or None, club roles to remove) so the member ends with only `target`."""
    remove = {rid for rid in current_ids if rid in club_ids and rid != target}
    add = target if target is not None and target not in current_ids else None
    return add, remove


def club_mapping(raw: Dict[str, int]) -> Dict[int, int]:
    """Config stores {"7": role_id}; normalise to {7: role_id}."""
    out: Dict[int, int] = {}
    for k, v in (raw or {}).items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out
