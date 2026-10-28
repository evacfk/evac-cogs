"""Template + pure helpers for the Pulse dashboard page. No discord/redbot imports.

Read-only by design and COUNTS ONLY: no message content and no member names or ids
ever reach the page data (the per-user top-chatters board stays in Discord). Every
value reaches the template as a plain variable and goes through `|e`; the only
values placed inside a `style=` attribute are numbers and `rgb(r,g,b)` strings that
this module builds itself.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import engine, models
from .constants import ANOMALY_BASELINE_WEEKS, DEFAULT_WINDOW_DAYS, WEEKDAY_SHORT

WINDOW_CHOICES = (7, 14, 28, 56)
DEFAULT_WINDOW = DEFAULT_WINDOW_DAYS if DEFAULT_WINDOW_DAYS in WINDOW_CHOICES else 28
ANOMALY_DAYS = 14
# the page always needs this many days on disk: the anomaly baseline reaches back
# ANOMALY_BASELINE_WEEKS before the first anomaly day
LOAD_DAYS = ANOMALY_DAYS + 7 * ANOMALY_BASELINE_WEEKS
CHANNEL_ROWS = 8

# viridis-like ramp (same as the Discord heatmap): low -> high
RAMP = [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)]


# ---------------------------------------------------------------- small helpers

def parse_window(value) -> int:
    """`?days=` value (str / int / list / None) -> an allowed window, else the default."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    return n if n in WINDOW_CHOICES else DEFAULT_WINDOW


def window_from_kwargs(days=None, **kwargs) -> int:
    """The dashboard may hand the query value over as the named `days` argument or inside `extra_kwargs`."""
    if days is None:
        extra = kwargs.get("extra_kwargs")
        if isinstance(extra, dict):
            days = extra.get("days")
    return parse_window(days)


def hour_label(h: int) -> str:
    return f"{12 if h % 12 == 0 else h % 12}{'a' if h < 12 else 'p'}"


def hour_range(start_hour: int, width: int) -> str:
    return f"{hour_label(start_hour % 24)}–{hour_label((start_hour + width) % 24)}"


def fmt_value(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.1f}k"
    if v >= 10:
        return f"{v:.0f}"
    return f"{v:.1f}"


def fmt_int(n) -> str:
    return f"{int(n):,}"


def format_age(seconds) -> str:
    if seconds is None:
        return "no messages seen yet"
    seconds = max(int(seconds), 0)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def short_date(d: date) -> str:
    return f"{WEEKDAY_SHORT[d.weekday()]} {d.strftime('%b')} {d.day}"


def ramp_rgb(t: float) -> tuple[int, int, int]:
    t = min(1.0, max(0.0, t))
    pos = t * (len(RAMP) - 1)
    i = min(int(pos), len(RAMP) - 2)
    frac = pos - i
    a, b = RAMP[i], RAMP[i + 1]
    return tuple(int(round(a[k] + (b[k] - a[k]) * frac)) for k in range(3))  # type: ignore[return-value]


def _luma(rgb) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _pct(value: float, top: float) -> str:
    return f"{(value / top * 100) if top > 0 else 0:.1f}"


# ---------------------------------------------------------------- "right now" strip

def _pace_text(ratio) -> str:
    if ratio is None:
        return "no baseline yet"
    return f"{ratio:.1f}× usual"


def _upcoming_text(w) -> str:
    if w is None:
        return "not enough data yet"
    start = models.local_dt(w.start_ts)
    return f"{WEEKDAY_SHORT[start.weekday()]} {hour_range(start.hour, w.width)} (usually ~{fmt_value(w.avg_msgs)} msgs/hr)"


def build_now(live) -> list:
    """Ordered (label, value) strings from an embeds.LiveView-shaped object (duck-typed)."""
    today_line = f"{fmt_int(live.today_msgs)} messages"
    if live.today_chatters is not None:
        today_line += f" from {fmt_int(live.today_chatters)} chatters"
    tv = live.today_vs_usual
    if tv and tv[2] is not None:
        today_line += f" ({_pace_text(tv[2])} by this time)"
    return [
        ("Last message", format_age(live.last_msg_age_s)),
        ("Chatting in the last 5 min", fmt_int(live.concurrency)),
        ("This hour so far", f"{fmt_int(live.hour_msgs)} messages from {fmt_int(live.hour_chatters)} chatters ({_pace_text(live.pace_ratio)})"),
        ("Today so far", today_line),
        ("Joins / leaves today", f"{fmt_int(live.joins_today)} / {fmt_int(live.leaves_today)}"),
        ("Next busy window", _upcoming_text(live.next_best)),
        ("Next quiet window", _upcoming_text(live.next_quiet)),
    ]


# ---------------------------------------------------------------- the page data

def _anomaly_text(a) -> str:
    d = date.fromisoformat(a.date)
    when = short_date(d) if a.hour is None else f"{short_date(d)} {hour_label(a.hour)}"
    what = ("Busy" if a.kind == "busy" else "Quiet") + (" day" if a.scope == "day" else " hour")
    return f"{what}: {when} — {fmt_int(round(a.actual))} messages vs ~{fmt_int(round(a.expected))} usual ({a.ratio:.1f}×)"


def build_page_data(
    *,
    days: dict,
    today: date,
    now_ts: float,
    coverage_start,
    ignored,
    window_days: int,
    channel_name,
) -> dict:
    """Everything the template needs, as plain strings/numbers. `days` must cover LOAD_DAYS ending today.

    `ignored` is the set of ignored channel ids (ints); `channel_name(channel_id_str)` returns a
    display name or None.
    """
    start = today - timedelta(days=window_days - 1)
    done = engine.build_slots(days, start, today, now_ts, coverage_start)                     # finished hours
    with_open = engine.build_slots(days, start, today, now_ts, coverage_start, include_open=True)
    data: dict = {
        "window_days": window_days,
        "windows": [{"days": n, "active": n == window_days} for n in WINDOW_CHOICES],
        "has_data": bool(done),
        "since": None,
    }
    if coverage_start:
        cov = models.local_dt(coverage_start).date()
        data["since"] = f"{short_date(cov)} ({(today - cov).days + 1} days of data)"
    if not done:
        return data

    # --- summary cards
    by_date: dict[str, list] = defaultdict(list)
    for s in with_open:
        by_date[s.date].append(s)
    daily = []
    for d in models.date_range(start, today):
        dk = d.isoformat()
        group = by_date.get(dk, [])
        if not group:
            continue
        full = len(group) == len(models.day_slots(dk)) and not any(s.partial for s in group)
        daily.append({"date": d, "msgs": sum(s.msgs for s in group), "partial": not full})
    full_days = [r for r in daily if not r["partial"]]
    total = sum(r["msgs"] for r in daily)
    uniq = engine.unique_chatters(days, start, today)
    joins, leaves = engine.joins_leaves(days, start, today)
    cards = [
        ("Messages", fmt_int(total), f"last {window_days} days"),
        ("Average per full day", fmt_int(round(sum(r["msgs"] for r in full_days) / len(full_days))) if full_days else "n/a", f"{len(full_days)} full days"),
        ("Unique chatters", fmt_int(uniq) if uniq is not None else "n/a", "distinct members who posted"),
        ("Joins / leaves", f"{fmt_int(joins)} / {fmt_int(leaves)}", "daily totals"),
    ]
    if len(full_days) > 1:
        top = max(full_days, key=lambda r: r["msgs"])
        low = min(full_days, key=lambda r: r["msgs"])
        cards.append(("Busiest full day", short_date(top["date"]), f"{fmt_int(top['msgs'])} messages"))
        cards.append(("Quietest full day", short_date(low["date"]), f"{fmt_int(low['msgs'])} messages"))
    data["cards"] = [{"label": a, "value": b, "sub": c} for a, b, c in cards]

    # --- weekday x hour heatmap (average messages per hour)
    avgs, counts = engine.weekday_hour_matrix(done, "msgs")
    top_v = max((v for row in avgs for v in row if v is not None), default=0.0)
    heat_rows = []
    for w in range(7):
        cells = []
        for h in range(24):
            v = avgs[w][h]
            if v is None:
                cells.append({"text": "", "bg": "transparent", "fg": "inherit", "title": f"{WEEKDAY_SHORT[w]} {hour_label(h)}: no data"})
                continue
            rgb = ramp_rgb(v / top_v if top_v else 0.0)
            cells.append({
                "text": fmt_value(v),
                "bg": f"rgb({rgb[0]},{rgb[1]},{rgb[2]})",
                "fg": "#111" if _luma(rgb) > 140 else "#fff",
                "title": f"{WEEKDAY_SHORT[w]} {hour_label(h)}: {v:.1f} msgs/hr on average over {counts[w][h]} such hours",
            })
        heat_rows.append({"day": WEEKDAY_SHORT[w], "cells": cells})
    data["heat_hours"] = [hour_label(h) for h in range(24)]
    data["heat_rows"] = heat_rows

    # --- hour-of-day bars
    table = engine.hour_table(done)
    top_h = max((r.avg_msgs for r in table), default=0.0)
    data["hour_bars"] = [
        {"label": hour_label(r.hour), "value": fmt_value(r.avg_msgs), "pct": _pct(r.avg_msgs, top_h), "top": 0 < r.rank <= 3,
         "title": f"{hour_label(r.hour)}: {r.avg_msgs:.1f} msgs/hr, {r.avg_users:.1f} chatters, active in {r.active_pct * 100:.0f}% of such hours"}
        for r in table
    ]

    # --- daily totals (newest first)
    top_d = max((r["msgs"] for r in daily), default=0)
    data["daily"] = [
        {"label": short_date(r["date"]), "value": fmt_int(r["msgs"]), "pct": _pct(r["msgs"], top_d), "partial": r["partial"]}
        for r in reversed(daily)
    ]

    # --- channels
    ranking = engine.channel_ranking(days, start, today, {str(c) for c in (ignored or ())})[:CHANNEL_ROWS]
    top_c = ranking[0][1] if ranking else 0
    data["channels"] = [
        {"name": channel_name(cid) or "deleted or unknown channel", "value": fmt_int(m), "share": f"{share * 100:.0f}%", "pct": _pct(m, top_c)}
        for cid, m, share in ranking
    ]

    # --- anomalies (last ANOMALY_DAYS days against the prior weeks)
    a_start = today - timedelta(days=ANOMALY_DAYS - 1)
    base_start = a_start - timedelta(weeks=ANOMALY_BASELINE_WEEKS)
    baseline = engine.build_slots(days, base_start, today, now_ts, coverage_start)
    data["anomalies"] = [_anomaly_text(a) for a in engine.find_anomalies(baseline, a_start, today)]
    data["anomaly_days"] = ANOMALY_DAYS
    return data


PAGE_TEMPLATE = """\
<style>
.pulse-page .pulse-card{border:1px solid rgba(128,128,128,.3);border-radius:.5rem;padding:.6rem .9rem;height:100%}
.pulse-page .pulse-card .v{font-size:1.35rem;font-weight:600;line-height:1.2}
.pulse-page .pulse-card .s{font-size:.75rem;opacity:.7}
.pulse-page table.pulse-heat{border-collapse:separate;border-spacing:2px;font-size:.68rem;width:100%;min-width:720px}
.pulse-page table.pulse-heat th{font-weight:600;text-align:center;padding:0 1px;white-space:nowrap}
.pulse-page table.pulse-heat td{text-align:center;padding:.3rem 0;border-radius:3px;min-width:24px}
.pulse-page .pulse-bar{display:flex;align-items:center;gap:.5rem;font-size:.8rem;margin:2px 0}
.pulse-page .pulse-bar .l{width:5.5rem;flex:none;text-align:right;white-space:nowrap}
.pulse-page .pulse-bar .t{flex:1;background:rgba(128,128,128,.15);border-radius:3px;height:.9rem}
.pulse-page .pulse-bar .f{height:100%;border-radius:3px;background:#5e72e4}
.pulse-page .pulse-bar .f.top{background:#2dce89}
.pulse-page .pulse-bar .f.partial{opacity:.5}
.pulse-page .pulse-bar .n{width:4.5rem;flex:none;font-variant-numeric:tabular-nums}
.pulse-page .pulse-scroll{max-height:26rem;overflow-y:auto}
.pulse-page h5{margin-top:1.4rem}
</style>
<div class="pulse-page">
  <h3>Pulse</h3>
  <p class="text-muted">Server activity for {{ guild_name|e }}. Counts only: no message text and no member names are shown. Use <code>.pulse</code> in Discord for the member-level views.</p>
  {% if denied %}
  <p>You need the mod role, Administrator, or to be the bot owner to view this page.</p>
  {% elif disabled %}
  <p>Pulse is switched off for this server.</p>
  {% elif not page.has_data %}
  <p>Pulse is still collecting data{% if page.since %} (since {{ page.since|e }}){% endif %}. Check back after a few full hours, or run <code>.pulse backfill 30</code> in Discord to import history.</p>
  {% else %}
  <p>
    Window:
    {% for w in page.windows %}
      {% if w.active %}<strong>{{ w.days|e }} days</strong>{% else %}<a href="?days={{ w.days|e }}">{{ w.days|e }} days</a>{% endif %}{% if not loop.last %} &middot; {% endif %}
    {% endfor %}
    {% if page.since %}<span class="text-muted"> &mdash; data since {{ page.since|e }}</span>{% endif %}
  </p>

  <div class="row g-2">
    {% for c in page.cards %}
    <div class="col-6 col-md-4 col-xl-2"><div class="pulse-card"><div class="s">{{ c.label|e }}</div><div class="v">{{ c.value|e }}</div><div class="s">{{ c.sub|e }}</div></div></div>
    {% endfor %}
  </div>

  {% if now %}
  <h5>Right now</h5>
  <div class="table-responsive">
    <table class="table table-sm align-middle"><tbody>
      {% for label, value in now %}<tr><th scope="row">{{ label|e }}</th><td>{{ value|e }}</td></tr>{% endfor %}
    </tbody></table>
  </div>
  {% endif %}

  <h5>When it is busy</h5>
  <p class="text-muted">Average messages per hour by weekday and hour (America/Los_Angeles). Brighter is busier; hover a cell for details.</p>
  <div class="table-responsive">
    <table class="pulse-heat">
      <thead><tr><th></th>{% for h in page.heat_hours %}<th>{{ h|e }}</th>{% endfor %}</tr></thead>
      <tbody>
      {% for row in page.heat_rows %}
        <tr><th>{{ row.day|e }}</th>{% for c in row.cells %}<td title="{{ c.title|e }}" style="background:{{ c.bg|e }};color:{{ c.fg|e }}">{{ c.text|e }}</td>{% endfor %}</tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <h5>Average by hour of day</h5>
  <p class="text-muted">Green marks the three busiest hours.</p>
  {% for b in page.hour_bars %}
  <div class="pulse-bar" title="{{ b.title|e }}"><span class="l">{{ b.label|e }}</span><span class="t"><span class="f{% if b.top %} top{% endif %}" style="display:block;width:{{ b.pct|e }}%"></span></span><span class="n">{{ b.value|e }}</span></div>
  {% endfor %}

  <h5>Messages per day</h5>
  <p class="text-muted">Newest first. Faded bars are days that are not finished (or not fully covered by data yet).</p>
  <div class="pulse-scroll">
  {% for b in page.daily %}
  <div class="pulse-bar"><span class="l">{{ b.label|e }}</span><span class="t"><span class="f{% if b.partial %} partial{% endif %}" style="display:block;width:{{ b.pct|e }}%"></span></span><span class="n">{{ b.value|e }}</span></div>
  {% endfor %}
  </div>

  <h5>Busiest channels</h5>
  {% if page.channels %}
  {% for b in page.channels %}
  <div class="pulse-bar"><span class="l" style="width:10rem">{{ b.name|e }}</span><span class="t"><span class="f" style="display:block;width:{{ b.pct|e }}%"></span></span><span class="n">{{ b.value|e }} ({{ b.share|e }})</span></div>
  {% endfor %}
  {% else %}
  <p class="text-muted">No channel data in this window.</p>
  {% endif %}

  <h5>Unusual days and hours</h5>
  <p class="text-muted">Last {{ page.anomaly_days|e }} days compared with the same weekday and hour over the prior weeks.</p>
  {% if page.anomalies %}
  <ul>{% for a in page.anomalies %}<li>{{ a|e }}</li>{% endfor %}</ul>
  {% else %}
  <p>Nothing unusual.</p>
  {% endif %}
  {% endif %}
</div>
"""
