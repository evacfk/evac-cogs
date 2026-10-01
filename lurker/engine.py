"""Pure logic for the lurker cog -- no discord.py / redbot imports, so it is
fully unit-testable without a bot. The Cog class in lurker.py owns all I/O.
"""
import csv
import io
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

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
