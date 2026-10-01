"""Pure statistics for serverpulse -- slots, hour-of-day tables, heatmap matrices,
best-window finder, anomalies, period reports, digest schedule maths.

No discord/redbot imports. Every function takes `now_ts` explicitly so tests
never depend on the wall clock. Only *complete* clock hours (and hours at or
after `coverage_start_ts`) feed averages; the in-progress hour is available
separately and flagged `partial`.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .constants import (
    ANOMALY_BASELINE_WEEKS,
    ANOMALY_MIN_SAMPLES,
    ANOMALY_REPORT_LIMIT,
    DAY_HIGH_RATIO,
    DAY_LOW_RATIO,
    DAY_MIN_DELTA,
    DIGEST_HOUR,
    HOUR_HIGH_MIN_DELTA,
    HOUR_HIGH_RATIO,
    HOUR_LOW_MIN_EXPECTED,
    HOUR_LOW_RATIO,
    SCORE_WEIGHT_CHATTERS,
    SCORE_WEIGHT_MESSAGES,
    SCORE_WEIGHT_PEAK,
    WEEKDAY_SHORT,
)
from .models import (
    HOUR_SECONDS,
    SlotInfo,
    date_range,
    day_end_ts,
    day_slots,
    day_start_ts,
    local_dt,
    month_bounds,
    parse_date_key,
    parse_day_arg,
    week_bounds,
)

# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    date: str
    key: str
    hour: int
    start_ts: int
    end_ts: int
    weekday: int
    msgs: int = 0
    users: int = 0
    peak: int = 0
    conc_sum: int = 0
    inner_gap: int = 0
    gap_start: int = 0
    first: int | None = None
    last: int | None = None
    channels: dict = field(default_factory=dict, compare=False, hash=False)
    partial: bool = False
    approx: bool = False  # channel-filtered: `users` is summed across channels

    @property
    def longest_silence(self) -> int:
        if not self.msgs or self.first is None or self.last is None:
            return HOUR_SECONDS
        return max(self.inner_gap, self.first, HOUR_SECONDS - self.last)

    @property
    def avg_concurrency(self) -> float:
        return self.conc_sum / self.msgs if self.msgs else 0.0


def _slot_from(info: SlotInfo, rec: dict | None, partial: bool, channels: set | None) -> Slot:
    base = dict(
        date=info.date, key=info.key, hour=info.hour, start_ts=info.start_ts, end_ts=info.end_ts,
        weekday=info.weekday, partial=partial,
    )
    if not rec:
        return Slot(**base)
    if channels is not None:
        picked = {c: v for c, v in (rec.get("c") or {}).items() if c in channels}
        return Slot(
            **base,
            msgs=sum(v[0] for v in picked.values()),
            users=sum(v[1] for v in picked.values()),
            channels=picked,
            approx=True,
        )
    return Slot(
        **base,
        msgs=int(rec.get("m", 0)),
        users=int(rec.get("u", 0)),
        peak=int(rec.get("pk", 0)),
        conc_sum=int(rec.get("cs", 0)),
        inner_gap=int(rec.get("gi", 0)),
        gap_start=int(rec.get("gs", 0)),
        first=int(rec.get("f", 0)),
        last=int(rec.get("l", 0)),
        channels=dict(rec.get("c") or {}),
    )


def build_slots(
    days: dict,
    start: date,
    end: date,
    now_ts: float,
    coverage_start_ts: float | None,
    *,
    include_open: bool = False,
    channels: set | None = None,
) -> list[Slot]:
    """Zero-filled, chronological slots for start..end inclusive.

    Skips hours before coverage began (we don't know what happened) and hours
    still in the future; the in-progress hour is included only if `include_open`.
    """
    if coverage_start_ts is None:
        return []
    out: list[Slot] = []
    for d in date_range(start, end):
        dk = d.isoformat()
        hours = (days.get(dk) or {}).get("h", {})
        for info in day_slots(dk):
            if info.start_ts < coverage_start_ts or info.start_ts >= now_ts:
                continue
            partial = info.end_ts > now_ts
            if partial and not include_open:
                continue
            out.append(_slot_from(info, hours.get(info.key), partial, channels))
    return out


def _complete(slots: list[Slot]) -> list[Slot]:
    return [s for s in slots if not s.partial]


# ---------------------------------------------------------------------------
# Hour-of-day tables, matrices, scores
# ---------------------------------------------------------------------------


@dataclass
class HourRow:
    hour: int
    n: int  # hour-instances averaged
    avg_msgs: float
    avg_users: float
    avg_peak: float
    active_pct: float  # share of those hours with at least one message
    rank: int  # 1 = busiest by avg_msgs, 0 = no data


def hour_table(slots: list[Slot], weekday: int | None = None) -> list[HourRow]:
    buckets: dict[int, list[Slot]] = defaultdict(list)
    for s in _complete(slots):
        if weekday is None or s.weekday == weekday:
            buckets[s.hour].append(s)
    rows = []
    for h in range(24):
        group = buckets.get(h, [])
        n = len(group)
        if n:
            rows.append(
                HourRow(h, n, sum(s.msgs for s in group) / n, sum(s.users for s in group) / n,
                        sum(s.peak for s in group) / n, sum(1 for s in group if s.msgs) / n, 0)
            )
        else:
            rows.append(HourRow(h, 0, 0.0, 0.0, 0.0, 0.0, 0))
    ranked = sorted((r for r in rows if r.n), key=lambda r: (-r.avg_msgs, r.hour))
    for i, r in enumerate(ranked, start=1):
        r.rank = i
    return rows


METRICS = {"msgs": lambda s: s.msgs, "users": lambda s: s.users, "peak": lambda s: s.peak}


def weekday_hour_matrix(slots: list[Slot], metric: str = "msgs"):
    """(7x24 averages or None, 7x24 sample counts); Monday first."""
    fn = METRICS[metric]
    sums = [[0.0] * 24 for _ in range(7)]
    counts = [[0] * 24 for _ in range(7)]
    for s in _complete(slots):
        sums[s.weekday][s.hour] += fn(s)
        counts[s.weekday][s.hour] += 1
    avgs = [[(sums[w][h] / counts[w][h]) if counts[w][h] else None for h in range(24)] for w in range(7)]
    return avgs, counts


def cell_scores(slots: list[Slot]):
    """7x24 'how alive is this hour' scores in 0..1 (None = no data).

    Each metric (peak concurrent chatters, distinct chatters, messages) is
    normalised by its own maximum, then blended with the configured weights.
    Metrics that are all zero (e.g. peak for channel-filtered data) drop out.
    """
    msgs, _ = weekday_hour_matrix(slots, "msgs")
    users, _ = weekday_hour_matrix(slots, "users")
    peak, _ = weekday_hour_matrix(slots, "peak")
    terms = []
    for weight, mat in ((SCORE_WEIGHT_PEAK, peak), (SCORE_WEIGHT_CHATTERS, users), (SCORE_WEIGHT_MESSAGES, msgs)):
        top = max((v for row in mat for v in row if v is not None), default=0)
        if top > 0:
            terms.append((weight, mat, top))
    total_w = sum(w for w, _, _ in terms)
    scores = [[None] * 24 for _ in range(7)]
    for w in range(7):
        for h in range(24):
            if msgs[w][h] is None:
                continue
            scores[w][h] = sum(wt * (m[w][h] / top) for wt, m, top in terms) / total_w if total_w else 0.0
    return scores, msgs, users, peak


@dataclass
class Window:
    weekday: int
    start_hour: int
    width: int
    score: float
    avg_msgs: float
    avg_users: float
    avg_peak: float


def find_windows(
    slots: list[Slot], width: int = 2, top: int = 3, kind: str = "best", weekday: int | None = None
) -> list[Window]:
    """Top non-overlapping `width`-hour windows of the (circular) week."""
    scores, msgs, users, peak = cell_scores(slots)
    cands = []
    for i in range(168):
        cells = [((i + k) % 168) for k in range(width)]
        vals = [scores[c // 24][c % 24] for c in cells]
        if any(v is None for v in vals):
            continue
        wd, hr = divmod(i, 24)
        if weekday is not None and wd != weekday:
            continue
        mean = lambda mat: sum(mat[c // 24][c % 24] for c in cells) / width  # noqa: E731
        cands.append((sum(vals) / width, i, Window(wd, hr, width, sum(vals) / width, mean(msgs), mean(users), mean(peak))))
    cands.sort(key=lambda t: (-t[0], t[1]) if kind == "best" else (t[0], t[1]))
    chosen, used = [], set()
    for _score, i, win in cands:
        cells = {((i + k) % 168) for k in range(width)}
        if cells & used:
            continue
        chosen.append(win)
        used |= cells
        if len(chosen) >= top:
            break
    return chosen


@dataclass
class UpcomingWindow:
    start_ts: int
    width: int
    score: float
    avg_msgs: float


def upcoming_window(
    slots: list[Slot], now_ts: float, width: int = 2, horizon_hours: int = 24, kind: str = "best"
) -> UpcomingWindow | None:
    """Best (or quietest) `width`-hour window starting within the next `horizon_hours`."""
    scores, msgs, _u, _p = cell_scores(slots)
    first = (int(now_ts) // HOUR_SECONDS + 1) * HOUR_SECONDS
    best = None
    for off in range(horizon_hours):
        start = first + off * HOUR_SECONDS
        vals, ms = [], []
        for j in range(width):
            dt = local_dt(start + j * HOUR_SECONDS)
            v = scores[dt.weekday()][dt.hour]
            if v is None:
                vals = None
                break
            vals.append(v)
            ms.append(msgs[dt.weekday()][dt.hour])
        if vals is None:
            continue
        score = sum(vals) / width
        better = best is None or (score > best.score if kind == "best" else score < best.score)
        if better:
            best = UpcomingWindow(start, width, score, sum(ms) / width)
    return best


@dataclass
class WeekdayHourFocus:
    weekday: int
    n: int
    avg_msgs: float
    rank: int  # rank of this hour within that weekday's 24 hours (1 = busiest)
    pct_of_busiest: float


@dataclass
class HourFocus:
    hour: int
    overall: HourRow
    busiest: HourRow | None
    gap_to_busiest_pct: float | None  # how far below the busiest hour, in %
    per_weekday: list[WeekdayHourFocus]


def hour_focus(slots: list[Slot], hour: int) -> HourFocus:
    overall_rows = hour_table(slots)
    overall = overall_rows[hour]
    ranked = [r for r in overall_rows if r.n]
    busiest = max(ranked, key=lambda r: (r.avg_msgs, -r.hour)) if ranked else None
    gap = None
    if busiest and busiest.avg_msgs > 0:
        gap = (1 - overall.avg_msgs / busiest.avg_msgs) * 100
    per = []
    for wd in range(7):
        rows = hour_table(slots, weekday=wd)
        row = rows[hour]
        top = max((r.avg_msgs for r in rows if r.n), default=0.0)
        per.append(WeekdayHourFocus(wd, row.n, row.avg_msgs, row.rank, (row.avg_msgs / top * 100) if top else 0.0))
    return HourFocus(hour, overall, busiest, gap, per)


def usual_msgs(slots: list[Slot], weekday: int, hour: int) -> tuple[float | None, int]:
    """(average messages, samples) for one weekday+hour cell."""
    group = [s.msgs for s in _complete(slots) if s.weekday == weekday and s.hour == hour]
    return (sum(group) / len(group), len(group)) if group else (None, 0)


# ---------------------------------------------------------------------------
# Silence
# ---------------------------------------------------------------------------


def longest_dead_stretch(slots: list[Slot]) -> tuple[int, int] | None:
    """(seconds, start_ts) of the longest stretch with no messages, across hour borders.

    Exact to the second because every hour stores when its first/last message
    landed and its longest inner gap. Returns None if there is nothing to measure
    (no slots, or channel-filtered data that lacks timing).
    """
    chain = _complete(slots)
    if not chain or any(s.msgs and s.first is None for s in chain):
        return None
    best_len, best_start = 0, None
    run, run_start = 0, None

    def cand(length, start):
        nonlocal best_len, best_start
        if length > best_len:
            best_len, best_start = length, start

    for s in chain:
        if not s.msgs:
            if run == 0:
                run_start = s.start_ts
            run += HOUR_SECONDS
            continue
        cand(run + s.first, run_start if run else s.start_ts)
        cand(s.inner_gap, s.start_ts + s.gap_start)
        run = HOUR_SECONDS - s.last
        run_start = s.start_ts + s.last if run else None
    cand(run, run_start)
    return (best_len, best_start) if best_start is not None else None


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


@dataclass
class Anomaly:
    scope: str  # "day" | "hour"
    kind: str  # "busy" | "quiet"
    date: str
    hour: int | None
    weekday: int
    actual: float
    expected: float
    ratio: float


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values)


def find_anomalies(slots: list[Slot], start: date, end: date, limit: int = ANOMALY_REPORT_LIMIT) -> list[Anomaly]:
    """Days/hours in start..end that are far from the same weekday over the prior weeks.

    `slots` should span at least ANOMALY_BASELINE_WEEKS before `start`.
    """
    chain = _complete(slots)
    by_date: dict[str, list[Slot]] = defaultdict(list)
    for s in chain:
        by_date[s.date].append(s)
    day_totals = {
        d: sum(s.msgs for s in group)
        for d, group in by_date.items()
        if len(group) == len(day_slots(d))  # only fully covered days are comparable
    }
    cell_hist: dict[tuple[int, int], list[tuple[str, int]]] = defaultdict(list)
    for s in chain:
        cell_hist[(s.weekday, s.hour)].append((s.date, s.msgs))

    found: list[Anomaly] = []
    lookback = timedelta(weeks=ANOMALY_BASELINE_WEEKS)
    for d in date_range(start, end):
        dk = d.isoformat()
        if dk in day_totals:
            base = [
                day_totals[(d - timedelta(weeks=k)).isoformat()]
                for k in range(1, ANOMALY_BASELINE_WEEKS + 1)
                if (d - timedelta(weeks=k)).isoformat() in day_totals
            ]
            if len(base) >= ANOMALY_MIN_SAMPLES:
                exp, act = _mean(base), day_totals[dk]
                if act >= exp * DAY_HIGH_RATIO and act - exp >= DAY_MIN_DELTA:
                    found.append(Anomaly("day", "busy", dk, None, d.weekday(), act, exp, act / max(exp, 1.0)))
                elif act <= exp * DAY_LOW_RATIO and exp - act >= DAY_MIN_DELTA:
                    found.append(Anomaly("day", "quiet", dk, None, d.weekday(), act, exp, act / max(exp, 1.0)))
        for s in by_date.get(dk, []):
            base = [m for (bd, m) in cell_hist[(s.weekday, s.hour)] if d - lookback <= parse_date_key(bd) < d]
            if len(base) < ANOMALY_MIN_SAMPLES:
                continue
            exp = _mean(base)
            if s.msgs >= exp * HOUR_HIGH_RATIO and s.msgs - exp >= HOUR_HIGH_MIN_DELTA:
                found.append(Anomaly("hour", "busy", dk, s.hour, s.weekday, s.msgs, exp, s.msgs / max(exp, 1.0)))
            elif exp >= HOUR_LOW_MIN_EXPECTED and s.msgs <= exp * HOUR_LOW_RATIO:
                found.append(Anomaly("hour", "quiet", dk, s.hour, s.weekday, s.msgs, exp, s.msgs / max(exp, 1.0)))

    def weight(a: Anomaly) -> tuple:
        swing = abs(math.log(max(a.ratio, 0.01)))
        return (0 if a.scope == "day" else 1, -swing)

    found.sort(key=weight)
    return found[:limit]


def today_pace(slots: list[Slot], today: date) -> tuple[int, float, float | None, int] | None:
    """Today's messages in completed hours vs the same weekday's first-k hours.

    Returns (actual, expected, ratio|None, baseline_days) or None without a baseline.
    """
    chain = _complete(slots)
    tk = today.isoformat()
    todays = [s for s in chain if s.date == tk]
    if not todays:
        return None
    k = len(todays)
    actual = sum(s.msgs for s in todays)
    by_date: dict[str, list[Slot]] = defaultdict(list)
    for s in chain:
        by_date[s.date].append(s)
    base = []
    for w in range(1, ANOMALY_BASELINE_WEEKS + 1):
        dk = (today - timedelta(weeks=w)).isoformat()
        group = by_date.get(dk)
        if group and len(group) == len(day_slots(dk)):
            base.append(sum(s.msgs for s in group[:k]))
    if len(base) < ANOMALY_MIN_SAMPLES:
        return None
    exp = _mean(base)
    return actual, exp, (actual / exp if exp > 0 else None), len(base)


# ---------------------------------------------------------------------------
# Raw-document aggregates
# ---------------------------------------------------------------------------


def channel_ranking(days: dict, start: date, end: date, ignored: set | None = None) -> list[tuple[str, int, float]]:
    """[(channel_id, messages, share 0..1)] busiest first."""
    totals: dict[str, int] = defaultdict(int)
    for d in date_range(start, end):
        for rec in ((days.get(d.isoformat()) or {}).get("h") or {}).values():
            for cid, pair in (rec.get("c") or {}).items():
                if ignored and cid in ignored:
                    continue
                totals[cid] += int(pair[0])
    grand = sum(totals.values())
    return [(c, m, (m / grand if grand else 0.0)) for c, m in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]


def user_counts(doc: dict) -> dict[str, int]:
    merged: dict[str, int] = defaultdict(int)
    for src in (doc.get("u") or {}, doc.get("ub") or {}):
        for uid, n in src.items():
            merged[uid] += int(n)
    return merged


def top_chatters(days: dict, start: date, end: date, n: int = 10) -> list[tuple[int, int]]:
    totals: dict[str, int] = defaultdict(int)
    for d in date_range(start, end):
        for uid, c in user_counts(days.get(d.isoformat()) or {}).items():
            totals[uid] += c
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [(int(uid), c) for uid, c in ranked]


def unique_chatters(days: dict, start: date, end: date) -> int | None:
    """Distinct chatters across the period, or None if per-user data was purged for part of it."""
    seen: set[str] = set()
    for d in date_range(start, end):
        doc = days.get(d.isoformat())
        if not doc:
            continue
        users = user_counts(doc)
        has_msgs = any(int(r.get("m", 0)) for r in (doc.get("h") or {}).values())
        if has_msgs and not users:
            return None
        seen.update(users)
    return len(seen)


def joins_leaves(days: dict, start: date, end: date) -> tuple[int, int]:
    joins = leaves = 0
    for d in date_range(start, end):
        doc = days.get(d.isoformat()) or {}
        joins += int(doc.get("joins", 0))
        leaves += int(doc.get("leaves", 0))
    return joins, leaves


# ---------------------------------------------------------------------------
# Period reports (day / week / month)
# ---------------------------------------------------------------------------


@dataclass
class DayRow:
    date: str
    weekday: int
    msgs: int
    users: int | None
    peak: int
    partial: bool
    complete: bool  # every clock hour of the day is covered and finished


@dataclass
class PeriodReport:
    kind: str
    label: str
    start: date
    end: date
    over: bool  # the period has fully elapsed
    slots_n: int
    msgs: int
    unique_chatters: int | None
    joins: int
    leaves: int
    hours: list[HourRow]
    busiest_hours: list[HourRow]
    quietest_hours: list[HourRow]
    daily: list[DayRow]
    busiest_day: DayRow | None
    quietest_day: DayRow | None
    peak_slot: Slot | None
    dead: tuple[int, int] | None
    channels: list[tuple[str, int, float]]
    prev_label: str
    prev_msgs: int | None
    change_pct: float | None
    matrix: list
    partial_start: bool  # data begins after the period starts
    anomalies: list[Anomaly] = field(default_factory=list)
    baseline: list = field(default_factory=list, repr=False)  # slots incl. prior weeks, for best-window maths


def _fmt_day(d: date, year: bool = False) -> str:
    # no %-d: it isn't portable to Windows dev boxes
    return f"{d.strftime('%a %b')} {d.day}" + (f", {d.year}" if year else "")


def resolve_period(kind: str, arg: str | None, today: date):
    """-> (start, end, label, prev_start, prev_end) or None if `arg` can't be parsed."""
    a = arg.strip().lower() if arg else None
    if kind == "day":
        d = parse_day_arg(a, today)
        if d is None:
            return None
        return d, d, _fmt_day(d, year=True), d - timedelta(days=1), d - timedelta(days=1)
    if kind == "week":
        if a in (None, "this", "current"):
            anchor = today
        elif a in ("last", "previous", "prev"):
            anchor = today - timedelta(days=7)
        else:
            anchor = parse_day_arg(a, today)
            if anchor is None:
                return None
        s, e = week_bounds(anchor)
        label = f"{_fmt_day(s)} – {_fmt_day(e, year=True)}"
        return s, e, label, s - timedelta(days=7), e - timedelta(days=7)
    if kind == "month":
        if a in (None, "this", "current"):
            anchor = today
        elif a in ("last", "previous", "prev"):
            anchor = today.replace(day=1) - timedelta(days=1)
        else:
            try:
                anchor = date.fromisoformat(a + "-01") if len(a) == 7 else parse_day_arg(a, today)
            except ValueError:
                anchor = None
            if anchor is None:
                return None
        s, e = month_bounds(anchor)
        ps, pe = month_bounds(s - timedelta(days=1))
        return s, e, s.strftime("%B %Y"), ps, pe
    raise ValueError(kind)


def build_period_report(
    days: dict,
    kind: str,
    start: date,
    end: date,
    label: str,
    prev_start: date,
    prev_end: date,
    now_ts: float,
    coverage_start_ts: float | None,
) -> PeriodReport:
    cur = build_slots(days, start, end, now_ts, coverage_start_ts, include_open=True)
    done = _complete(cur)
    over = now_ts >= day_end_ts(end)

    # like-for-like comparison: prior period truncated to the same elapsed time
    elapsed = min(now_ts, day_end_ts(end)) - day_start_ts(start)
    prev_now = day_start_ts(prev_start) + elapsed
    prev = build_slots(days, prev_start, prev_end, prev_now, coverage_start_ts, include_open=True)
    comparable = bool(cur) and bool(prev) and len(prev) >= 0.9 * len(cur)
    prev_msgs = sum(s.msgs for s in prev) if comparable else None
    msgs = sum(s.msgs for s in cur)
    change = ((msgs - prev_msgs) / prev_msgs * 100) if (prev_msgs and prev_msgs > 0) else None

    by_date: dict[str, list[Slot]] = defaultdict(list)
    for s in cur:
        by_date[s.date].append(s)
    daily = []
    for d in date_range(start, end):
        dk = d.isoformat()
        group = by_date.get(dk, [])
        if not group:
            continue
        uniq = user_counts(days.get(dk) or {})
        daily.append(
            DayRow(
                dk, d.weekday(), sum(s.msgs for s in group), len(uniq) if uniq else None,
                max((s.peak for s in group), default=0), any(s.partial for s in group),
                len(group) == len(day_slots(dk)) and not any(s.partial for s in group),
            )
        )
    full_days = [r for r in daily if r.complete]
    hours = hour_table(cur)
    ranked = sorted((r for r in hours if r.n), key=lambda r: (-r.avg_msgs, r.hour))
    matrix, _ = weekday_hour_matrix(cur, "msgs")
    peak_slot = max((s for s in done if s.peak), key=lambda s: (s.peak, s.msgs), default=None)
    joins, leaves = joins_leaves(days, start, end)
    cov_date = local_dt(coverage_start_ts).date() if coverage_start_ts else None

    return PeriodReport(
        kind=kind,
        label=label,
        start=start,
        end=end,
        over=over,
        slots_n=len(cur),
        msgs=msgs,
        unique_chatters=unique_chatters(days, start, end) if cur else None,
        joins=joins,
        leaves=leaves,
        hours=hours,
        busiest_hours=ranked[:3],
        quietest_hours=list(reversed(ranked[-3:])) if len(ranked) > 3 else [],
        daily=daily,
        busiest_day=max(full_days, key=lambda r: r.msgs, default=None) if len(full_days) > 1 else None,
        quietest_day=min(full_days, key=lambda r: r.msgs, default=None) if len(full_days) > 1 else None,
        peak_slot=peak_slot,
        dead=longest_dead_stretch(cur),
        channels=channel_ranking(days, start, end)[:5],
        prev_label=f"{prev_start.isoformat()}" if kind == "day" else f"{prev_start.isoformat()}…{prev_end.isoformat()}",
        prev_msgs=prev_msgs,
        change_pct=change,
        matrix=matrix,
        partial_start=bool(cov_date and cov_date > start),
    )


# ---------------------------------------------------------------------------
# Digest schedule
# ---------------------------------------------------------------------------


def weekly_due(now_local: datetime) -> tuple[str, date, date]:
    """(key, start, end) of the Mon-Sun week whose digest is currently due (Mon 9am local)."""
    d = now_local.date()
    monday = d - timedelta(days=d.weekday())
    due_moment = datetime.combine(monday, time(DIGEST_HOUR), tzinfo=now_local.tzinfo)
    this_monday = monday if now_local >= due_moment else monday - timedelta(days=7)
    start = this_monday - timedelta(days=7)
    return start.isoformat(), start, this_monday - timedelta(days=1)


def monthly_due(now_local: datetime) -> tuple[str, date, date]:
    """(key 'YYYY-MM', start, end) of the month whose digest is currently due (1st, 9am local)."""
    d = now_local.date()
    first = d.replace(day=1)
    due_moment = datetime.combine(first, time(DIGEST_HOUR), tzinfo=now_local.tzinfo)
    anchor = first if now_local >= due_moment else (first - timedelta(days=1)).replace(day=1)
    prev_end = anchor - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    return prev_start.strftime("%Y-%m"), prev_start, prev_end
