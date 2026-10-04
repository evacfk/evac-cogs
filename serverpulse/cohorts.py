"""New-member retention: one record per join ("stint"), pure logic only.

A record follows one person from the moment they join until they leave (or
WATCH_DAYS pass), and answers: where did they come from, did they say
anything, did they come back, and how fast did they leave?

Record (compact keys, stored as JSON):
    u   user id                          j   join timestamp
    s   source label ("Disboard", ...)   c   invite code used (None if unknown)
    a   account creation timestamp (from the user id)
    l   leave timestamp (None = still here, 0 = left at an unknown time)
    ob  finished Discord onboarding when they left (None = unknown)
    r   number of roles held when they left (None = unknown)
    fm  first message timestamp (None = never / unknown)
    n   messages in the first WATCH_DAYS days
    d   sorted day offsets (local dates since the join day) with any message,
        or None when message data for that period no longer exists
    o   origin: "live" (tracked as it happened), "log" (rebuilt from the
        join/leave log channel), "member" (a current member found by join date)
    rj  Discord's "did rejoin" flag (None = unknown)

Day offsets use America/Los_Angeles calendar dates, like everything else.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from .constants import TIMEZONE

WATCH_DAYS = 45  # message activity is tracked for this long after a join
DISCORD_EPOCH_MS = 1420070400000
MATCH_TOLERANCE = 900  # seconds: a log join and a member's joined_at this close are the same join

JOIN_WORDS = ("joined", "welcome", "arrived", "has joined", "just joined", "hopped", "landed", "joining")
LEAVE_WORDS = ("left", "leave", "leaving", "goodbye", "bye", "departed", "has gone", "was kicked", "was banned")

_MENTION_RE = re.compile(r"<@!?(\d{15,21})>")
_ID_RE = re.compile(r"\bid\b\W{0,3}(\d{17,21})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def snowflake_ts(snowflake: int) -> float:
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000


def local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, TIMEZONE).date()


def record_key(rec: dict) -> str:
    return f"{rec['u']}-{int(rec['j'])}"


def month_key(ts: float) -> str:
    return local_date(ts).strftime("%Y-%m")


def new_record(uid: int, join_ts: float, *, source: str | None = None, code: str | None = None,
               origin: str = "live", rejoin: bool | None = None) -> dict:
    return {
        "u": int(uid), "j": float(join_ts), "s": source, "c": code, "a": snowflake_ts(uid),
        "l": None, "ob": None, "r": None, "fm": None, "n": 0, "d": [], "o": origin, "rj": rejoin,
    }


def day_offset(rec: dict, ts: float) -> int:
    return (local_date(ts) - local_date(rec["j"])).days


def watching(rec: dict, now_ts: float) -> bool:
    """Still collecting message activity for this record?"""
    return rec["l"] is None and now_ts - rec["j"] <= (WATCH_DAYS + 1) * 86400


def record_message(rec: dict, ts: float) -> bool:
    """Count one message. Returns True if the record changed."""
    if ts < rec["j"] or (rec["l"] not in (None, 0) and ts > rec["l"]):
        return False
    off = day_offset(rec, ts)
    if off > WATCH_DAYS:
        return False
    rec["n"] = int(rec.get("n") or 0) + 1
    if rec.get("fm") is None or ts < rec["fm"]:
        rec["fm"] = ts
    days = rec.get("d")
    if days is None:
        days = rec["d"] = []
    if off not in days:
        days.append(off)
        days.sort()
    return True


def record_leave(rec: dict, ts: float, *, onboarding: bool | None, roles: int | None) -> None:
    rec["l"] = float(ts)
    rec["ob"] = onboarding
    rec["r"] = roles


# ---------------------------------------------------------------------------
# invites
# ---------------------------------------------------------------------------

def invite_diff(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Codes whose use count went up (a code new since the last snapshot counts if used)."""
    return sorted(code for code, uses in after.items() if uses > before.get(code, 0))


def source_label(code: str | None, labels: dict[str, str], inviter: str | None = None) -> str:
    if code is None:
        return "Unknown"
    if code == "vanity":
        return "Vanity URL"
    if code in labels:
        return labels[code]
    return f"{inviter}'s invite" if inviter else f"Invite {code}"


# ---------------------------------------------------------------------------
# join/leave log parsing (for rebuilding history before live tracking)
# ---------------------------------------------------------------------------

def ids_in_text(texts: Iterable[str]) -> list[int]:
    out: list[int] = []
    for text in texts:
        for m in _MENTION_RE.finditer(text or ""):
            uid = int(m.group(1))
            if uid not in out:
                out.append(uid)
    if not out:
        for text in texts:
            for m in _ID_RE.finditer(text or ""):
                uid = int(m.group(1))
                if uid not in out:
                    out.append(uid)
    return out


def classify_text(text: str) -> str | None:
    """'join' / 'leave' / None from wording, for a channel that logs both."""
    low = (text or "").lower()
    has_join = any(w in low for w in JOIN_WORDS)
    has_leave = any(re.search(rf"\b{re.escape(w)}\b", low) for w in LEAVE_WORDS)
    if has_join and not has_leave:
        return "join"
    if has_leave and not has_join:
        return "leave"
    return None


def pair_stints(events: list[tuple[str, float]], member_joined_ts: float | None) -> list[tuple[float, float | None]]:
    """One person's join/leave events -> [(join_ts, leave)] with leave None (still here),
    0 (left, time unknown) or a timestamp."""
    stints: list[list] = []
    current: list | None = None
    for kind, ts in sorted(events, key=lambda e: e[1]):
        if kind == "join":
            if current is not None:
                current[1] = 0  # joined again without a logged leave
                stints.append(current)
            current = [ts, None]
        elif current is not None:
            current[1] = ts
            stints.append(current)
            current = None
    if current is not None:
        if member_joined_ts is not None and abs(member_joined_ts - current[0]) <= MATCH_TOLERANCE:
            current[1] = None  # that's their current membership
        else:
            current[1] = 0  # gone, or rejoined later: this stint ended at some point
        stints.append(current)
    return [(j, l) for j, l in stints]


def activity_from_days(rec: dict, day_counts: dict[str, dict[str, int]], today: date, data_from: date | None) -> None:
    """Fill d/n/fm-day from per-day per-user counts (day precision). d=None when the data is gone."""
    jd = local_date(rec["j"])
    if data_from is None or jd < data_from:
        rec["d"], rec["n"] = None, 0
        return
    end = today
    if rec["l"]:
        end = min(end, local_date(rec["l"]))
    end = min(end, jd + timedelta(days=WATCH_DAYS))
    uid = str(rec["u"])
    days, total = [], 0
    d = jd
    while d <= end:
        n = int((day_counts.get(d.isoformat()) or {}).get(uid, 0))
        if n:
            days.append((d - jd).days)
            total += n
        d += timedelta(days=1)
    rec["d"], rec["n"] = days, total


def build_log_records(
    events: list[tuple[int, str, float]],
    members_now: dict[int, float],
    *,
    start_ts: float,
    end_ts: float,
    day_counts: dict[str, dict[str, int]],
    today: date,
    data_from: date | None,
) -> list[dict]:
    """Rebuild join records for [start_ts, end_ts) from log events + current members."""
    by_user: dict[int, list] = defaultdict(list)
    for uid, kind, ts in events:
        by_user[uid].append((kind, ts))
    out: list[dict] = []
    for uid, evs in by_user.items():
        for j, l in pair_stints(evs, members_now.get(uid)):
            if not start_ts <= j < end_ts:
                continue
            rec = new_record(uid, j, origin="log")
            rec["l"] = l
            activity_from_days(rec, day_counts, today, data_from)
            out.append(rec)
    for uid, joined in members_now.items():
        if not start_ts <= joined < end_ts:
            continue
        if any(r["u"] == uid and abs(r["j"] - joined) <= MATCH_TOLERANCE for r in out if r["l"] is None):
            continue
        rec = new_record(uid, joined, origin="member")
        activity_from_days(rec, day_counts, today, data_from)
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def _pct(n: int, d: int) -> float | None:
    return (100.0 * n / d) if d else None


def spoke(rec: dict) -> bool | None:
    if rec.get("d") is None:
        return None
    return bool(rec["d"]) or rec.get("fm") is not None


def left_within(rec: dict, seconds: float) -> bool:
    return bool(rec["l"]) and rec["l"] - rec["j"] <= seconds


def active_between(rec: dict, lo: int, hi: int) -> bool | None:
    if rec.get("d") is None:
        return None
    return any(lo <= off <= hi for off in rec["d"])


def _ratio(recs: list[dict], test) -> tuple[int, int]:
    """(hits, known) over records where test() is not None."""
    hits = known = 0
    for r in recs:
        v = test(r)
        if v is None:
            continue
        known += 1
        hits += 1 if v else 0
    return hits, known


def summarize(records: list[dict], now_ts: float) -> dict:
    """Funnel numbers for a set of join records (see `.pulse retention`)."""
    recs = list(records)
    total = len(recs)
    leavers = [r for r in recs if r["l"] is not None]
    timed = [r for r in recs if r["l"]]
    quick = [r for r in timed if r["l"] - r["j"] <= 3600]
    old_enough = lambda days: [r for r in recs if now_ts - r["j"] >= days * 86400]  # noqa: E731

    def acct_new(r, days=30):
        return r["j"] - r["a"] < days * 86400

    minutes_to_leave = [(r["l"] - r["j"]) / 60 for r in timed if r["l"] - r["j"] <= 86400]
    spoke_hits, spoke_known = _ratio(recs, spoke)
    silent_leavers = sum(1 for r in leavers if spoke(r) is False)
    leaver_known = sum(1 for r in leavers if spoke(r) is not None)
    came_back = _ratio(old_enough(2), lambda r: None if r.get("d") is None else any(o >= 1 for o in r["d"]))
    wk2 = _ratio(old_enough(14), lambda r: active_between(r, 7, 13))
    d30 = _ratio(old_enough(35), lambda r: active_between(r, 28, 34))
    onboarding_known = [r for r in leavers if r.get("ob") is not None]
    quick_ob = [r for r in quick if r.get("ob") is not None]
    quick_roles = [r for r in quick if r.get("r") is not None]
    return {
        "joined": total,
        "still_here": sum(1 for r in recs if r["l"] is None),
        "left": len(leavers),
        "left_unknown_time": sum(1 for r in recs if r["l"] == 0),
        "left_10m": sum(1 for r in timed if r["l"] - r["j"] <= 600),
        "left_1h": len(quick),
        "left_24h": sum(1 for r in timed if r["l"] - r["j"] <= 86400),
        "left_7d": sum(1 for r in timed if r["l"] - r["j"] <= 7 * 86400),
        "median_minutes_to_leave_24h": statistics.median(minutes_to_leave) if minutes_to_leave else None,
        "spoke": spoke_hits,
        "spoke_known": spoke_known,
        "silent_leavers": silent_leavers,
        "leavers_known": leaver_known,
        "came_back": came_back,
        "active_week2": wk2,
        "active_day30": d30,
        "new_accounts_7d": sum(1 for r in recs if acct_new(r, 7)),
        "new_accounts_30d": sum(1 for r in recs if acct_new(r, 30)),
        "onboarding_done_leavers": (sum(1 for r in onboarding_known if r["ob"]), len(onboarding_known)),
        "quick": {
            "count": len(quick),
            "new_account_30d": sum(1 for r in quick if acct_new(r, 30)),
            "spoke": _ratio(quick, spoke),
            "onboarding_done": (sum(1 for r in quick_ob if r["ob"]), len(quick_ob)),
            "picked_roles": (sum(1 for r in quick_roles if r["r"]), len(quick_roles)),
        },
    }


def by_source(records: list[dict], now_ts: float) -> list[tuple[str, dict]]:
    groups: dict[str, list] = defaultdict(list)
    for r in records:
        if r.get("o") != "live":
            continue
        groups[r.get("s") or "Unknown"].append(r)
    rows = [(name, summarize(recs, now_ts)) for name, recs in groups.items()]
    rows.sort(key=lambda kv: -kv[1]["joined"])
    return rows


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly_cohorts(records: list[dict], now_ts: float) -> list[dict]:
    weeks: dict[date, list] = defaultdict(list)
    for r in records:
        weeks[week_start(local_date(r["j"]))].append(r)
    out = []
    for wk in sorted(weeks):
        recs = weeks[wk]
        s = summarize(recs, now_ts)
        out.append({
            "week": wk.isoformat(),
            "joined": s["joined"],
            "left_1h": s["left_1h"],
            "spoke": s["spoke"], "spoke_known": s["spoke_known"],
            "active_week2": s["active_week2"],
            "still_here": s["still_here"],
        })
    return out


def anonymized(records: list[dict]) -> list[dict]:
    """Per-join rows safe to share: no user ids, times relative to the join."""
    rows = []
    for r in sorted(records, key=lambda r: r["j"]):
        rows.append({
            "joined": local_date(r["j"]).isoformat(),
            "join_hour_local": datetime.fromtimestamp(r["j"], TIMEZONE).hour,
            "source": r.get("s"),
            "origin": r.get("o"),
            "account_age_days": round((r["j"] - r["a"]) / 86400, 1),
            "left_after_minutes": None if r["l"] is None else (None if r["l"] == 0 else round((r["l"] - r["j"]) / 60, 1)),
            "still_here": r["l"] is None,
            "first_message_after_minutes": None if r.get("fm") is None else round((r["fm"] - r["j"]) / 60, 1),
            "messages_first_45d": r.get("n"),
            "active_day_offsets": r.get("d"),
            "finished_onboarding": r.get("ob"),
            "roles_at_leave": r.get("r"),
            "rejoin": r.get("rj"),
        })
    return rows
