"""Discord-facing rendering for serverpulse: embeds and small text tables.

Everything is written to be read by a human at a glance: plain-language
sentence first, numbers second. All times are Pacific.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import discord

from . import engine
from .constants import (
    COG_BUILD,
    COG_VERSION,
    COLOR_BAD,
    COLOR_GOOD,
    COLOR_PRIMARY,
    COLOR_QUIET,
    COLOR_WARN,
    SPARK_CHARS,
    WEEKDAY_NAMES,
    WEEKDAY_SHORT,
)
from .models import local_dt

FIELD_MAX = 1024


# -- formatting helpers -----------------------------------------------------

def _color(value: int):
    return discord.Color(value)


def clip(text: str, limit: int = FIELD_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_hour(h: int) -> str:
    return f"{12 if h % 12 == 0 else h % 12}{'am' if h < 12 else 'pm'}"


def fmt_hour_range(start: int, width: int = 1) -> str:
    """8, 2 -> '8–10pm'; 23, 2 -> '11pm–1am'."""
    s, e = fmt_hour(start % 24), fmt_hour((start + width) % 24)
    if s[-2:] == e[-2:] and width < 12:
        s = s[:-2]
    return f"{s}–{e}"


def hour_blocks(hours: list[int]) -> list[str]:
    """[21, 22, 23, 5] -> ['9pm–12am', '5–6am'] (consecutive hours merged)."""
    out, run = [], []
    for h in sorted(set(hours)):
        if run and h == run[-1] + 1:
            run.append(h)
        else:
            if run:
                out.append(fmt_hour_range(run[0], len(run)))
            run = [h]
    if run:
        out.append(fmt_hour_range(run[0], len(run)))
    return out


def fmt_int(n: float) -> str:
    return f"{int(round(n)):,}"


def fmt_avg(v: float) -> str:
    return f"{v:,.0f}" if v >= 10 else f"{v:.1f}"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def fmt_clock(ts: float) -> str:
    dt = local_dt(ts)
    return f"{WEEKDAY_SHORT[dt.weekday()]} {12 if dt.hour % 12 == 0 else dt.hour % 12}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def fmt_short_date(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{WEEKDAY_SHORT[d.weekday()]} {d.strftime('%b')} {d.day}"


def arrow(pct: float | None) -> str:
    if pct is None:
        return ""
    if abs(pct) < 1:
        return "≈ flat"
    return f"{'▲' if pct > 0 else '▼'} {abs(pct):.0f}%"


def sparkline(values: list[float]) -> str:
    top = max(values) if values else 0
    if top <= 0:
        return SPARK_CHARS[0] * len(values)
    return "".join(SPARK_CHARS[min(len(SPARK_CHARS) - 1, int(v / top * (len(SPARK_CHARS) - 1) + 0.5))] for v in values)


def bar(value: float, top: float, width: int = 8) -> str:
    if top <= 0:
        return ""
    return "█" * max(0, min(width, int(round(value / top * width))))


def day_axis() -> str:
    return "12a   3a    6a    9a    12p   3p    6p    9p"


def _embed(title: str, description: str | None = None, color: int = COLOR_PRIMARY):
    return discord.Embed(title=title, description=description, color=_color(color))


def _footer(embed, text: str):
    embed.set_footer(text=f"{text} · Pacific time")
    return embed


# -- plain-language summaries ---------------------------------------------------

def busy_quiet_sentence(rows: list[engine.HourRow], scope: str = "") -> str | None:
    ranked = sorted((r for r in rows if r.n), key=lambda r: (-r.avg_msgs, r.hour))
    if len(ranked) < 6 or ranked[0].avg_msgs <= 0:
        return None
    busy = hour_blocks([r.hour for r in ranked[:3]])
    quiet = hour_blocks([r.hour for r in ranked[-3:]])
    where = f" {scope}" if scope else ""
    return f"Chat{where} is usually busiest around **{', '.join(busy)}** and quietest around **{', '.join(quiet)}**."


# -- overview -----------------------------------------------------------------

@dataclass
class LiveView:
    now_ts: float
    last_msg_age_s: float | None
    hour_msgs: int
    hour_chatters: int
    concurrency: int
    usual_hour_msgs: float | None
    pace_ratio: float | None
    today_msgs: int
    today_chatters: int | None
    today_vs_usual: tuple | None  # (actual, expected, ratio, baseline_days)
    joins_today: int
    leaves_today: int
    next_best: engine.UpcomingWindow | None
    next_quiet: engine.UpcomingWindow | None
    hours: list
    history_days: int


def _upcoming_text(w: engine.UpcomingWindow | None) -> str:
    if w is None:
        return "not enough data yet"
    start = local_dt(w.start_ts)
    return f"{WEEKDAY_SHORT[start.weekday()]} {fmt_hour_range(start.hour, w.width)} (usually ~{fmt_avg(w.avg_msgs)} msgs/hr)"


def _pace_text(ratio: float | None) -> str:
    if ratio is None:
        return "no baseline yet"
    if ratio >= 1.5:
        return f"🔥 {ratio:.1f}× usual"
    if ratio <= 0.5:
        return f"😴 {ratio:.1f}× usual"
    return f"{ratio:.1f}× usual"


def board_embed(live: LiveView):
    e = _embed("📊 Server Pulse — live", None, COLOR_PRIMARY)
    last = "no messages yet" if live.last_msg_age_s is None else (
        "just now" if live.last_msg_age_s < 60 else f"{fmt_duration(live.last_msg_age_s)} ago"
    )
    hour_line = f"**{fmt_int(live.hour_msgs)}** msgs · **{live.hour_chatters}** chatters this hour"
    if live.usual_hour_msgs is not None:
        hour_line += f"\nUsual for this hour: ~{fmt_avg(live.usual_hour_msgs)} msgs"
    if live.pace_ratio is not None:
        hour_line += f" → pace {_pace_text(live.pace_ratio)}"
    e.add_field(name="Right now", value=clip(f"Last message: {last}\nActive in the last 5 min: **{live.concurrency}**\n{hour_line}"), inline=False)

    today = f"**{fmt_int(live.today_msgs)}** msgs"
    if live.today_chatters is not None:
        today += f" · **{live.today_chatters}** chatters"
    today += f"\n+{live.joins_today} joined · −{live.leaves_today} left"
    if live.today_vs_usual:
        actual, exp, ratio, n = live.today_vs_usual
        today += f"\nvs a usual {WEEKDAY_NAMES[local_dt(live.now_ts).weekday()]} by now: {_pace_text(ratio)} (~{fmt_int(exp)})"
    e.add_field(name="Today so far", value=clip(today), inline=False)
    e.add_field(name="Next good window (24h)", value=_upcoming_text(live.next_best), inline=True)
    e.add_field(name="Next quiet stretch (24h)", value=_upcoming_text(live.next_quiet), inline=True)
    if live.hours and any(r.n for r in live.hours):
        e.add_field(name="A typical day", value=f"`{sparkline([r.avg_msgs for r in live.hours])}`\n`{day_axis()}`", inline=False)
    e.add_field(name="Updated", value=f"<t:{int(live.now_ts)}:R>", inline=False)
    return _footer(e, f"based on the last {live.history_days} days")


def overview_embed(live: LiveView, sentence: str | None, hours: list[engine.HourRow], window_days: int):
    e = _embed("📊 Server Pulse", sentence or "Still learning your server's rhythm — numbers sharpen as data builds up.")
    ranked = sorted((r for r in hours if r.n), key=lambda r: (-r.avg_msgs, r.hour))
    if len(ranked) >= 6:
        top = "\n".join(f"**{fmt_hour_range(r.hour)}** — ~{fmt_avg(r.avg_msgs)} msgs/hr" for r in ranked[:3])
        low = "\n".join(f"**{fmt_hour_range(r.hour)}** — ~{fmt_avg(r.avg_msgs)} msgs/hr" for r in reversed(ranked[-3:]))
        e.add_field(name="🔥 Busiest hours", value=top, inline=True)
        e.add_field(name="😴 Quietest hours", value=low, inline=True)
    today = f"**{fmt_int(live.today_msgs)}** msgs"
    if live.today_chatters is not None:
        today += f" · **{live.today_chatters}** chatters"
    if live.today_vs_usual:
        today += f"\n{_pace_text(live.today_vs_usual[2])} for a {WEEKDAY_NAMES[local_dt(live.now_ts).weekday()]}"
    e.add_field(name="Today so far", value=today, inline=False)
    e.add_field(name="Next good window (24h)", value=_upcoming_text(live.next_best), inline=True)
    e.add_field(name="Next quiet stretch (24h)", value=_upcoming_text(live.next_quiet), inline=True)
    if ranked:
        e.add_field(name="A typical day", value=f"`{sparkline([r.avg_msgs for r in hours])}`\n`{day_axis()}`", inline=False)
    e.add_field(name="More", value="`.pulse day` `.pulse week` `.pulse month` · `.pulse hours` `.pulse hour 8pm` · `.pulse heatmap` · `.pulse best` · `.pulse channels` · `.pulse top` · `.pulse anomalies` · `.pulse export`", inline=False)
    return _footer(e, f"last {window_days} days")


def collecting_embed(coverage_start_ts: float | None, now_ts: float):
    if coverage_start_ts is None:
        text = "Tracking hasn't started yet."
    elif now_ts < coverage_start_ts + 3600:
        text = f"Tracking started. The first full hour of data completes {fmt_clock(coverage_start_ts + 3600)}."
    else:
        text = "Not enough data to report yet."
    text += "\n\nWant instant history? Run `.pulse backfill 30` to import the last 30 days from Discord's message history."
    return _embed("📊 Server Pulse — collecting data", text, COLOR_WARN)


# -- period reports ---------------------------------------------------------------

_KIND_TITLE = {"day": "Daily", "week": "Weekly", "month": "Monthly"}
_KIND_ICON = {"day": "🗓️", "week": "📅", "month": "🗒️"}


def _week_breakdown(daily: list[engine.DayRow]) -> str:
    weeks: dict[str, int] = {}
    for r in daily:
        d = date.fromisoformat(r.date)
        monday = d.toordinal() - d.weekday()
        weeks[date.fromordinal(monday).isoformat()] = weeks.get(date.fromordinal(monday).isoformat(), 0) + r.msgs
    top = max(weeks.values(), default=0)
    return "\n".join(f"`{fmt_short_date(k):<11}` {bar(v, top, 10)} {fmt_int(v)}" for k, v in sorted(weeks.items()))


def _daily_breakdown(daily: list[engine.DayRow]) -> str:
    top = max((r.msgs for r in daily), default=0)
    lines = []
    for r in daily:
        flag = " *(so far)*" if r.partial else ""
        lines.append(f"`{WEEKDAY_SHORT[r.weekday]} {r.date[5:]}` {bar(r.msgs, top, 10)} {fmt_int(r.msgs)}{flag}")
    return "\n".join(lines)


def anomaly_line(a: engine.Anomaly) -> str:
    when = fmt_short_date(a.date) if a.hour is None else f"{fmt_short_date(a.date)} {fmt_hour_range(a.hour)}"
    word = "busier" if a.kind == "busy" else "quieter"
    mult = a.ratio if a.kind == "busy" else (1 / a.ratio if a.ratio > 0 else 0)
    unit = "day" if a.scope == "day" else "hour"
    return f"**{when}** — {mult:.1f}× {word} than a normal {WEEKDAY_NAMES[a.weekday]} {unit} ({fmt_int(a.actual)} vs ~{fmt_int(a.expected)})"


def period_embed(
    report: engine.PeriodReport,
    *,
    digest: bool = False,
    best: list[engine.Window] | None = None,
    window_days_note: str | None = None,
):
    title = f"{_KIND_ICON[report.kind]} {_KIND_TITLE[report.kind]} {'digest' if digest else 'report'} — {report.label}"
    if not report.slots_n:
        return _embed(title, "No data for this period yet.", COLOR_WARN)
    parts = []
    headline = f"**{fmt_int(report.msgs)} messages**"
    if report.unique_chatters is not None:
        headline += f" from **{fmt_int(report.unique_chatters)} chatters**"
    if report.change_pct is not None:
        sofar = "" if report.over else " at the same point"
        headline += f" — {arrow(report.change_pct)} vs previous {report.kind}{sofar}"
    if not report.over:
        headline += " *(so far)*"
    parts.append(headline)
    sentence = busy_quiet_sentence(report.hours, "")
    if sentence and report.kind != "day":
        parts.append(sentence)
    if report.partial_start:
        parts.append("*Data for this period starts partway through — numbers cover only the part we have.*")
    e = _embed(title, "\n".join(parts), COLOR_PRIMARY)

    if report.busiest_hours:
        e.add_field(
            name="🔥 Busiest hours",
            value="\n".join(f"**{fmt_hour_range(r.hour)}** — ~{fmt_avg(r.avg_msgs)}/hr · {fmt_avg(r.avg_users)} chatters" for r in report.busiest_hours),
            inline=True,
        )
    if report.quietest_hours:
        e.add_field(
            name="😴 Quietest hours",
            value="\n".join(f"**{fmt_hour_range(r.hour)}** — ~{fmt_avg(r.avg_msgs)}/hr · {fmt_avg(r.avg_users)} chatters" for r in report.quietest_hours),
            inline=True,
        )
    if report.kind == "day" and any(r.n for r in report.hours):
        e.add_field(name="Hour by hour", value=f"`{sparkline([r.avg_msgs for r in report.hours])}`\n`{day_axis()}`", inline=False)
    if report.kind == "week" and report.daily:
        e.add_field(name="By day", value=clip(_daily_breakdown(report.daily)), inline=False)
    elif report.kind == "month" and report.daily:
        e.add_field(name="By week", value=clip(_week_breakdown(report.daily)), inline=False)
    if report.busiest_day and report.quietest_day:
        b, q = report.busiest_day, report.quietest_day
        e.add_field(
            name="Busiest / quietest day",
            value=f"🔥 {fmt_short_date(b.date)} — {fmt_int(b.msgs)}\n😴 {fmt_short_date(q.date)} — {fmt_int(q.msgs)}",
            inline=True,
        )
    if report.peak_slot:
        p = report.peak_slot
        e.add_field(
            name="⚡ Peak moment",
            value=f"{fmt_short_date(p.date)}, {fmt_hour_range(p.hour)} — **{p.peak}** people chatting within 5 minutes",
            inline=True,
        )
    if report.dead:
        secs, start_ts = report.dead
        e.add_field(name="🕳️ Longest dead stretch", value=f"**{fmt_duration(secs)}** starting {fmt_clock(start_ts)}", inline=True)
    if report.joins or report.leaves:
        e.add_field(name="👥 Members", value=f"+{fmt_int(report.joins)} joined · −{fmt_int(report.leaves)} left (net {report.joins - report.leaves:+d})", inline=True)
    if report.channels:
        e.add_field(
            name="💬 Top channels",
            value=clip("\n".join(f"<#{cid}> — {fmt_int(m)} ({share * 100:.0f}%)" for cid, m, share in report.channels[:5])),
            inline=False,
        )
    if report.anomalies:
        e.add_field(name="🚨 Unusual", value=clip("\n".join(anomaly_line(a) for a in report.anomalies)), inline=False)
    if best:
        e.add_field(
            name="🎯 Best windows for events & announcements",
            value="\n".join(f"**{WEEKDAY_SHORT[w.weekday]} {fmt_hour_range(w.start_hour, w.width)}** — ~{fmt_avg(w.avg_msgs)} msgs/hr, ~{fmt_avg(w.avg_peak)} chatting at once" for w in best),
            inline=False,
        )
    return _footer(e, window_days_note or "human messages only")


# -- tables & focused views ---------------------------------------------------------

def hours_embed(rows: list[engine.HourRow], weekday: int | None, days_note: str):
    scope = f"{WEEKDAY_NAMES[weekday]}s" if weekday is not None else "every day"
    e = _embed(f"🕒 Hour-by-hour — {scope}", busy_quiet_sentence(rows) or "Not enough data for a clear pattern yet.")
    top = max((r.avg_msgs for r in rows), default=0)
    ranked = [r for r in rows if r.n]
    busy = {r.hour for r in sorted(ranked, key=lambda r: -r.avg_msgs)[:3]}
    quiet = {r.hour for r in sorted(ranked, key=lambda r: r.avg_msgs)[:3]} if len(ranked) > 6 else set()
    lines = ["Hour   msgs  chat  peak"]
    for r in rows:
        mark = "▲" if r.hour in busy else ("▽" if r.hour in quiet else " ")
        lines.append(f"{fmt_hour(r.hour):>4} {mark} {fmt_avg(r.avg_msgs):>5} {fmt_avg(r.avg_users):>5} {fmt_avg(r.avg_peak):>5}  {bar(r.avg_msgs, top, 10)}")
    e.add_field(name="Averages per hour", value="```\n" + "\n".join(lines) + "\n```", inline=False)
    e.add_field(name="Legend", value="msgs = messages · chat = distinct chatters · peak = most people chatting within 5 min · ▲ busiest · ▽ quietest", inline=False)
    return _footer(e, days_note)


def hour_focus_embed(focus: engine.HourFocus, days_note: str):
    h = fmt_hour_range(focus.hour)
    o = focus.overall
    if not o.n:
        return _embed(f"🔎 {h}", "No data for that hour yet.", COLOR_WARN)
    rank_txt = f"ranks **#{o.rank} of 24** hours"
    lines = [f"**{h}** {rank_txt}: ~**{fmt_avg(o.avg_msgs)}** messages from ~**{fmt_avg(o.avg_users)}** chatters per hour on average."]
    if focus.busiest and focus.busiest.hour != focus.hour and focus.gap_to_busiest_pct is not None:
        lines.append(
            f"That is **{focus.gap_to_busiest_pct:.0f}% below** your busiest hour ({fmt_hour_range(focus.busiest.hour)}, ~{fmt_avg(focus.busiest.avg_msgs)})."
        )
    elif focus.busiest and focus.busiest.hour == focus.hour:
        lines.append("That's your busiest hour of the day.")
    e = _embed(f"🔎 {h} — how does it compare?", "\n".join(lines), COLOR_QUIET)
    rows = ["Day   avg msgs  rank  vs best hour"]
    have = [p for p in focus.per_weekday if p.n]
    for p in focus.per_weekday:
        if p.n:
            rows.append(f"{WEEKDAY_SHORT[p.weekday]:<4} {fmt_avg(p.avg_msgs):>8}  #{p.rank:<3}  {p.pct_of_busiest:>5.0f}%")
        else:
            rows.append(f"{WEEKDAY_SHORT[p.weekday]:<4}  no data")
    e.add_field(name=f"{h} by weekday", value="```\n" + "\n".join(rows) + "\n```", inline=False)
    if len(have) >= 3:
        weakest = min(have, key=lambda p: p.pct_of_busiest)
        strongest = max(have, key=lambda p: p.pct_of_busiest)
        e.add_field(
            name="Where to push",
            value=f"Weakest: **{WEEKDAY_NAMES[weakest.weekday]}** ({weakest.pct_of_busiest:.0f}% of that day's best hour). Strongest: **{WEEKDAY_NAMES[strongest.weekday]}** ({strongest.pct_of_busiest:.0f}%).",
            inline=False,
        )
    return _footer(e, days_note)


def windows_embed(best: list[engine.Window], quiet: list[engine.Window], width: int, weekday: int | None, days_note: str):
    scope = f" on {WEEKDAY_NAMES[weekday]}s" if weekday is not None else ""
    e = _embed(f"🎯 Best {width}-hour windows{scope}", "Ranked by how many people are chatting *at once* (50%), distinct chatters (30%) and messages (20%).")
    if not best:
        e.add_field(name="Not enough data", value="Need more history — try `.pulse backfill 30`.", inline=False)
        return _footer(e, days_note)

    def fmt(w):
        return f"**{WEEKDAY_SHORT[w.weekday]} {fmt_hour_range(w.start_hour, w.width)}** — ~{fmt_avg(w.avg_msgs)} msgs/hr · ~{fmt_avg(w.avg_users)} chatters · ~{fmt_avg(w.avg_peak)} at once"

    e.add_field(name="🔥 Best for events & announcements", value="\n".join(fmt(w) for w in best), inline=False)
    if quiet:
        e.add_field(name="😴 Quietest (good for maintenance)", value="\n".join(fmt(w) for w in quiet), inline=False)
    return _footer(e, days_note)


def channels_embed(rows: list[tuple[str, int, float]], label: str, ignored_note: str | None = None):
    e = _embed(f"💬 Channel ranking — {label}", None)
    if not rows:
        e.description = "No messages in this period."
        return e
    top = rows[0][1]
    lines = [f"**{i}.** <#{cid}> {bar(m, top, 8)} {fmt_int(m)} ({share * 100:.0f}%)" for i, (cid, m, share) in enumerate(rows[:15], 1)]
    e.description = clip("\n".join(lines), 4000)
    return _footer(e, ignored_note or "ignored channels excluded")


def top_chatters_embed(rows: list[tuple[str, int]], label: str):
    e = _embed(f"🏆 Top chatters — {label}", None)
    if not rows:
        e.description = "No per-user data for this period (kept for 90 days)."
        return e
    e.description = clip("\n".join(f"**{i}.** {name} — {fmt_int(n)}" for i, (name, n) in enumerate(rows, 1)), 4000)
    return _footer(e, "mods only · counts, never message content")


def anomalies_embed(rows: list[engine.Anomaly], label: str):
    e = _embed(f"🚨 Unusual activity — {label}", None, COLOR_WARN)
    if not rows:
        e.description = "Nothing unusual — everything is within the normal range for its weekday."
        e.color = _color(COLOR_GOOD)
        return e
    e.description = clip("\n".join(anomaly_line(a) for a in rows), 4000)
    return _footer(e, "vs the same weekday over the previous 8 weeks")


def members_embed(rows: list[tuple[str, int, int]], label: str):
    e = _embed(f"👥 Joins & leaves — {label}", None)
    if not rows:
        e.description = "No join/leave data yet (it starts when tracking started and can't be backfilled)."
        return e
    lines = ["Day          +join  −leave   net"]
    for dk, j, l in rows:
        lines.append(f"{fmt_short_date(dk):<12} {j:>5}  {l:>6}  {j - l:>+4}")
    lines.append(f"{'Total':<12} {sum(r[1] for r in rows):>5}  {sum(r[2] for r in rows):>6}  {sum(r[1] - r[2] for r in rows):>+4}")
    e.description = "```\n" + "\n".join(lines[-40:]) + "\n```"
    return _footer(e, "member counts only, no names stored")


# -- admin -------------------------------------------------------------------------

def settings_embed(*, ignored: list[int], mod_channel: int | None, mod_role: int | None, exclude_commands: bool,
                   coverage_start_ts: float | None, live_since_ts: float | None, board: dict, digest_weekly: dict,
                   digest_monthly: dict, days_stored: int):
    e = _embed("⚙️ Server Pulse settings")
    e.add_field(name="Mod channel", value=f"<#{mod_channel}>" if mod_channel else "not set", inline=True)
    e.add_field(name="Mod role", value=f"<@&{mod_role}>" if mod_role else "not set", inline=True)
    e.add_field(name="Ignore bot commands", value="yes (`.cmd` messages)" if exclude_commands else "no", inline=True)
    e.add_field(name="Ignored channels", value=clip(", ".join(f"<#{c}>" for c in ignored) or "none"), inline=False)
    e.add_field(name="Weekly digest", value="on" if digest_weekly.get("enabled") else "off", inline=True)
    e.add_field(name="Monthly digest", value="on" if digest_monthly.get("enabled") else "off", inline=True)
    e.add_field(name="Live board", value=f"<#{board['channel_id']}>" if board.get("channel_id") else "off", inline=True)
    e.add_field(name="Data", value=f"{days_stored} day files · data since {fmt_clock(coverage_start_ts) if coverage_start_ts else 'n/a'}", inline=False)
    return e


def version_embed(*, days_stored: int, coverage_start_ts: float | None, live_since_ts: float | None, pending_msgs: int, backfill_status: str):
    e = _embed("serverpulse", f"v{COG_VERSION} · build {COG_BUILD}")
    e.add_field(name="Coverage starts", value=fmt_clock(coverage_start_ts) if coverage_start_ts else "n/a", inline=True)
    e.add_field(name="Live since", value=fmt_clock(live_since_ts) if live_since_ts else "n/a", inline=True)
    e.add_field(name="Day files", value=str(days_stored), inline=True)
    e.add_field(name="Unflushed messages", value=str(pending_msgs), inline=True)
    e.add_field(name="Backfill", value=backfill_status, inline=True)
    return e


def backfill_embed(state: dict, now_ts: float):
    status = state.get("status", "none")
    colors = {"running": COLOR_WARN, "done": COLOR_GOOD, "error": COLOR_BAD, "cancelled": COLOR_BAD}
    e = _embed(f"📥 Backfill — {status}", None, colors.get(status, COLOR_PRIMARY))
    if status == "none":
        e.description = "No backfill has run. Start one with `.pulse backfill 30`."
        return e
    total, done = state.get("channels_total", 0), state.get("channels_done", 0)
    e.add_field(name="Window", value=f"last {state.get('days')} days", inline=True)
    e.add_field(name="Channels", value=f"{done}/{total}", inline=True)
    e.add_field(name="Messages counted", value=fmt_int(state.get("messages", 0)), inline=True)
    if status == "running" and state.get("current"):
        e.add_field(name="Now reading", value=state["current"], inline=False)
    if state.get("skipped"):
        e.add_field(name="Skipped (no access)", value=clip(", ".join(state["skipped"])), inline=False)
    if state.get("error"):
        e.add_field(name="Error", value=clip(str(state["error"])), inline=False)
    if status in ("cancelled", "error"):
        e.add_field(name="Resume", value="`.pulse backfill resume` continues where it stopped.", inline=False)
    if state.get("started"):
        e.add_field(name="Started", value=f"<t:{int(state['started'])}:R>", inline=True)
    return e
