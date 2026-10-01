"""JSON export for sharing with Claude (or any analysis tool).

Stable, self-describing schema (`serverpulse.export.v1`). Deliberately free of
message content; per-user data is opt-in and by user id only.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from . import engine
from .constants import COG_VERSION, TIMEZONE_NAME, WEEKDAY_NAMES
from .models import date_range, day_slots, local_dt

SCHEMA = "serverpulse.export.v1"

DEFINITIONS = {
    "messages": "Human messages only: bots, webhooks, system messages, ignored channels and (by default) bot commands are excluded. Thread messages count toward their parent channel.",
    "chatters": "Distinct users who posted in that hour (per-channel numbers are distinct users in that channel; they can overlap).",
    "peak_concurrent": "Highest number of distinct users who posted within any rolling 5-minute window during the hour.",
    "avg_concurrency": "Mean of the rolling-5-minute distinct-poster count sampled at every message.",
    "longest_silence_s": "Longest stretch with no messages inside the hour, in seconds (3600 for an empty hour).",
    "hour_key": "'HH' local hour; the repeated 1am on the DST fall-back day is '01b'. The spring-forward day has no '02'.",
    "missing_hours": "Within coverage, a (date, hour) not listed under 'hourly' had zero messages. Outside coverage it is unknown.",
    "weekday_hour_matrix": "7 rows (Monday..Sunday) x 24 columns (local hour); each cell is the average over the complete hours observed (null = never observed).",
    "timezone": TIMEZONE_NAME,
}


def _iso(ts: float | None) -> str | None:
    return local_dt(ts).isoformat() if ts is not None else None


def _round(v, n=2):
    return None if v is None else round(v, n)


def _window_dict(w: engine.Window) -> dict:
    return {
        "weekday": WEEKDAY_NAMES[w.weekday], "start_hour": w.start_hour, "width_hours": w.width,
        "score": _round(w.score, 3), "avg_messages_per_hour": _round(w.avg_msgs),
        "avg_chatters_per_hour": _round(w.avg_users), "avg_peak_concurrent": _round(w.avg_peak),
    }


def build_export(
    *,
    guild_id: int,
    guild_name: str,
    now_ts: float,
    coverage_start_ts: float | None,
    days: dict,
    start: date,
    end: date,
    raw_start: date,
    channel_names: dict[str, str] | None = None,
    settings: dict | None = None,
    user_names: dict[int, str] | None = None,
    include_users: bool = False,
) -> dict:
    """`days` must cover at least `start..end` (plus baseline weeks if anomalies are wanted)."""
    channel_names = channel_names or {}
    slots = engine.build_slots(days, start, end, now_ts, coverage_start_ts)
    hours = engine.hour_table(slots)
    avg_msgs, samples = engine.weekday_hour_matrix(slots, "msgs")
    avg_users, _ = engine.weekday_hour_matrix(slots, "users")
    avg_peak, _ = engine.weekday_hour_matrix(slots, "peak")

    by_date: dict[str, list] = defaultdict(list)
    for s in slots:
        by_date[s.date].append(s)
    daily = []
    for d in date_range(start, end):
        dk = d.isoformat()
        group = by_date.get(dk)
        if not group:
            continue
        doc = days.get(dk) or {}
        users = engine.user_counts(doc)
        daily.append(
            {
                "date": dk, "weekday": WEEKDAY_NAMES[d.weekday()],
                "messages": sum(s.msgs for s in group),
                "unique_chatters": len(users) if users else None,
                "peak_concurrent": max(s.peak for s in group),
                "active_hours": sum(1 for s in group if s.msgs),
                "joins": int(doc.get("joins", 0)), "leaves": int(doc.get("leaves", 0)),
                "complete_day": len(group) == len(day_slots(dk)),
            }
        )

    def rollup(key_fn) -> list[dict]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in daily:
            groups[key_fn(date.fromisoformat(row["date"]))].append(row)
        out = []
        for key in sorted(groups):
            rows = groups[key]
            out.append(
                {
                    "period": key, "days_covered": len(rows),
                    "complete_days": sum(1 for r in rows if r["complete_day"]),
                    "messages": sum(r["messages"] for r in rows),
                    "avg_messages_per_day": _round(sum(r["messages"] for r in rows) / len(rows)),
                    "joins": sum(r["joins"] for r in rows), "leaves": sum(r["leaves"] for r in rows),
                }
            )
        return out

    ranking = engine.channel_ranking(days, start, end)
    hourly = []
    for s in slots:
        if not s.msgs:
            continue
        if s.date < raw_start.isoformat():
            continue
        hourly.append(
            {
                "date": s.date, "hour": s.hour, "hour_key": s.key, "weekday": WEEKDAY_NAMES[s.weekday],
                "messages": s.msgs, "chatters": s.users, "peak_concurrent": s.peak,
                "avg_concurrency": _round(s.avg_concurrency), "longest_silence_s": s.longest_silence,
                "channels": {cid: {"messages": v[0], "chatters": v[1]} for cid, v in s.channels.items()},
            }
        )

    anomalies = engine.find_anomalies(
        engine.build_slots(days, start - timedelta(weeks=8), end, now_ts, coverage_start_ts),
        max(start, end - timedelta(days=29)), end, limit=20,
    )
    dead = engine.longest_dead_stretch(slots)

    out = {
        "schema": SCHEMA,
        "generated_at": _iso(now_ts),
        "cog_version": COG_VERSION,
        "guild": {"id": str(guild_id), "name": guild_name},
        "range": {"start": start.isoformat(), "end": end.isoformat(), "hourly_detail_from": raw_start.isoformat()},
        "coverage": {
            "starts_at": _iso(coverage_start_ts),
            "complete_hours_in_range": len(slots),
            "days_in_range_with_data": len(daily),
        },
        "settings": settings or {},
        "definitions": DEFINITIONS,
        "hour_of_day": [
            {
                "hour": r.hour, "samples": r.n, "avg_messages": _round(r.avg_msgs), "avg_chatters": _round(r.avg_users),
                "avg_peak_concurrent": _round(r.avg_peak), "pct_hours_with_activity": _round(r.active_pct * 100, 1),
                "rank_by_messages": r.rank or None,
            }
            for r in hours
        ],
        "weekday_hour_matrix": {
            "weekdays": WEEKDAY_NAMES,
            "avg_messages": [[_round(v) for v in row] for row in avg_msgs],
            "avg_chatters": [[_round(v) for v in row] for row in avg_users],
            "avg_peak_concurrent": [[_round(v) for v in row] for row in avg_peak],
            "samples": samples,
        },
        "best_windows_2h": [_window_dict(w) for w in engine.find_windows(slots, 2, 5, "best")],
        "quietest_windows_2h": [_window_dict(w) for w in engine.find_windows(slots, 2, 5, "quiet")],
        "longest_dead_stretch": (
            {"seconds": dead[0], "starts_at": _iso(dead[1])} if dead else None
        ),
        "anomalies_last_30_days": [
            {
                "scope": a.scope, "kind": a.kind, "date": a.date, "hour": a.hour,
                "weekday": WEEKDAY_NAMES[a.weekday], "actual": _round(a.actual),
                "expected": _round(a.expected), "ratio": _round(a.ratio),
            }
            for a in anomalies
        ],
        "daily": daily,
        "weekly": rollup(lambda d: (d - timedelta(days=d.weekday())).isoformat()),
        "monthly": rollup(lambda d: d.strftime("%Y-%m")),
        "channels": [
            {"channel_id": cid, "name": channel_names.get(cid), "messages": m, "share": _round(share, 4)}
            for cid, m, share in ranking
        ],
        "hourly": hourly,
    }
    if include_users:
        names = user_names or {}
        out["top_chatters"] = [
            {"user_id": str(uid), "name": names.get(uid), "messages": n}
            for uid, n in engine.top_chatters(days, start, end, 25)
        ]
    return out
