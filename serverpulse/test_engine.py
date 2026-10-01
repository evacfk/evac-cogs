from datetime import date, datetime, timedelta

import pytest

from serverpulse import engine, models
from serverpulse._testdata import flat_day, hour_rec, local_ts, traffic
from serverpulse.constants import TIMEZONE
from serverpulse.models import new_day_doc

D = date(2026, 9, 14)  # a Monday
NOW = local_ts(2026, 9, 30, 12)


def slot(date_key="2026-09-14", hour=0, **kw):
    info = models.day_slots(date_key)[hour]
    return engine.Slot(date_key, info.key, info.hour, info.start_ts, info.end_ts, info.weekday, **kw)


def run_days(n, per_hour_for, start=D):
    """n consecutive day docs; per_hour_for(date) -> int | dict."""
    return {(start + timedelta(days=i)).isoformat(): flat_day(start + timedelta(days=i), per_hour_for(start + timedelta(days=i))) for i in range(n)}


class TestBuildSlots:
    def test_zero_fills_hours_with_no_record(self):
        days = {"2026-09-14": flat_day(D, {20: 7})}
        slots = engine.build_slots(days, D, D, NOW, local_ts(2026, 9, 14))
        assert len(slots) == 24
        assert [s.msgs for s in slots if s.hour == 20] == [7]
        assert sum(s.msgs for s in slots) == 7

    def test_hours_before_coverage_are_skipped_not_zero(self):
        cov = local_ts(2026, 9, 14, 18)
        slots = engine.build_slots({}, D, D, NOW, cov)
        assert [s.hour for s in slots] == list(range(18, 24))

    def test_no_coverage_means_no_slots(self):
        assert engine.build_slots({}, D, D, NOW, None) == []

    def test_current_hour_is_partial_and_excluded_by_default(self):
        now = local_ts(2026, 9, 14, 10, 30)
        cov = local_ts(2026, 9, 14)
        done = engine.build_slots({}, D, D, now, cov)
        assert [s.hour for s in done] == list(range(0, 10))
        with_open = engine.build_slots({}, D, D, now, cov, include_open=True)
        assert with_open[-1].hour == 10 and with_open[-1].partial

    def test_future_hours_never_appear(self):
        now = local_ts(2026, 9, 14, 10, 30)
        slots = engine.build_slots({}, D, D + timedelta(days=3), now, local_ts(2026, 9, 14), include_open=True)
        assert all(s.start_ts < now for s in slots)

    def test_dst_days_have_real_slot_counts(self):
        fall, spring = date(2026, 11, 1), date(2026, 3, 8)
        now = local_ts(2026, 12, 1)
        assert len(engine.build_slots({}, fall, fall, now, local_ts(2026, 3, 1))) == 25
        assert len(engine.build_slots({}, spring, spring, now, local_ts(2026, 3, 1))) == 23

    def test_channel_filter_uses_only_those_channels(self):
        doc = new_day_doc()
        doc["h"]["20"] = hour_rec(10, channels={"111": [6, 3], "222": [4, 2]})
        slots = engine.build_slots({"2026-09-14": doc}, D, D, NOW, local_ts(2026, 9, 14), channels={"222"})
        s = next(s for s in slots if s.hour == 20)
        assert s.msgs == 4 and s.users == 2 and s.approx


class TestHourTable:
    def test_averages_and_ranks(self):
        days = run_days(7, lambda d: {20: 100, 21: 50, 5: 2})
        slots = engine.build_slots(days, D, D + timedelta(days=6), NOW, local_ts(2026, 9, 14))
        rows = engine.hour_table(slots)
        assert rows[20].avg_msgs == 100 and rows[20].rank == 1
        assert rows[21].rank == 2 and rows[5].rank == 3
        assert rows[20].n == 7 and rows[20].active_pct == 1.0
        assert rows[3].avg_msgs == 0 and rows[3].active_pct == 0

    def test_weekday_filter(self):
        days = run_days(14, lambda d: {20: 100 if d.weekday() == 4 else 10})
        slots = engine.build_slots(days, D, D + timedelta(days=13), NOW, local_ts(2026, 9, 14))
        assert engine.hour_table(slots, weekday=4)[20].avg_msgs == 100
        assert engine.hour_table(slots, weekday=0)[20].avg_msgs == 10

    def test_dst_fall_back_hour_averages_per_real_hour(self):
        fall = date(2026, 11, 1)
        doc = new_day_doc()
        doc["h"]["01"] = hour_rec(10)
        doc["h"]["01b"] = hour_rec(30)
        slots = engine.build_slots({"2026-11-01": doc}, fall, fall, local_ts(2026, 12, 1), local_ts(2026, 11, 1))
        assert engine.hour_table(slots)[1].avg_msgs == 20  # (10+30)/2 real hours, not 40

    def test_partial_hour_is_not_averaged_in(self):
        now = local_ts(2026, 9, 14, 10, 30)
        doc = new_day_doc()
        doc["h"]["10"] = hour_rec(5)
        slots = engine.build_slots({"2026-09-14": doc}, D, D, now, local_ts(2026, 9, 14), include_open=True)
        assert engine.hour_table(slots)[10].n == 0


class TestMatrixAndWindows:
    def evening_slots(self):
        days = run_days(14, lambda d: {20: 80, 21: 100, 22: 60, 8: 5})
        return engine.build_slots(days, D, D + timedelta(days=13), NOW, local_ts(2026, 9, 14))

    def test_matrix_is_monday_first_and_averaged(self):
        avgs, counts = engine.weekday_hour_matrix(self.evening_slots(), "msgs")
        assert avgs[0][21] == 100 and counts[0][21] == 2
        assert avgs[6][8] == 5

    def test_best_window_is_the_busiest_consecutive_hours(self):
        best = engine.find_windows(self.evening_slots(), width=2, top=1, kind="best")[0]
        assert best.start_hour in (20, 21)
        assert best.avg_msgs >= 80

    def test_quiet_window_is_a_dead_stretch(self):
        quiet = engine.find_windows(self.evening_slots(), width=2, top=1, kind="quiet")[0]
        assert quiet.avg_msgs <= 5

    def test_windows_do_not_overlap(self):
        wins = engine.find_windows(self.evening_slots(), width=2, top=5, kind="best")
        cells = [((w.weekday * 24 + w.start_hour + k) % 168) for w in wins for k in range(2)]
        assert len(cells) == len(set(cells))

    def test_weekday_filter_restricts_start_day(self):
        wins = engine.find_windows(self.evening_slots(), width=2, top=3, weekday=2)
        assert wins and all(w.weekday == 2 for w in wins)

    def test_windows_empty_without_data(self):
        assert engine.find_windows([], 2, 3) == []

    def test_upcoming_window_finds_tonights_peak(self):
        slots = self.evening_slots()
        now = local_ts(2026, 9, 30, 12, 15)  # Wednesday noon
        best = engine.upcoming_window(slots, now, 2, 24, "best")
        start = models.local_dt(best.start_ts)
        assert start.hour in (20, 21) and start.date() == date(2026, 9, 30)
        quiet = engine.upcoming_window(slots, now, 2, 24, "quiet")
        assert quiet.avg_msgs < best.avg_msgs

    def test_upcoming_window_never_starts_in_the_past(self):
        slots = self.evening_slots()
        now = local_ts(2026, 9, 30, 12, 15)
        assert engine.upcoming_window(slots, now, 2, 24, "best").start_ts > now


class TestHourFocus:
    def test_rank_gap_and_per_weekday(self):
        def per_hour(d):
            return {21: 100, 20: 40 if d.weekday() == 1 else 80}

        days = run_days(14, per_hour)
        slots = engine.build_slots(days, D, D + timedelta(days=13), NOW, local_ts(2026, 9, 14))
        f = engine.hour_focus(slots, 20)
        assert f.overall.rank == 2
        assert f.busiest.hour == 21
        assert round(f.gap_to_busiest_pct) == round((1 - ((6 * 80 + 40) / 7) / 100) * 100)
        tue = f.per_weekday[1]
        assert tue.avg_msgs == 40 and tue.pct_of_busiest == 40 and tue.rank == 2
        assert f.per_weekday[0].pct_of_busiest == 80


class TestDeadStretch:
    def test_longest_stretch_spans_empty_hours_and_head_gap(self):
        slots = [
            slot(hour=0),
            slot(hour=1),
            slot(hour=2, msgs=3, first=1200, last=3000, inner_gap=100, gap_start=1500),
            slot(hour=3),
            slot(hour=4, msgs=2, first=300, last=400, inner_gap=100, gap_start=310),
        ]
        secs, start = engine.longest_dead_stretch(slots)
        assert secs == 2 * 3600 + 1200
        assert start == slot(hour=0).start_ts

    def test_stretch_counts_tail_of_one_hour_plus_head_of_next(self):
        slots = [slot(hour=5, msgs=1, first=10, last=100), slot(hour=6, msgs=1, first=3000, last=3100)]
        secs, start = engine.longest_dead_stretch(slots)
        assert secs == (3600 - 100) + 3000
        assert start == slot(hour=5).start_ts + 100

    def test_inner_gap_can_be_the_longest(self):
        slots = [slot(hour=5, msgs=3, first=0, last=3599, inner_gap=2000, gap_start=500)]
        secs, start = engine.longest_dead_stretch(slots)
        assert secs == 2000 and start == slot(hour=5).start_ts + 500

    def test_trailing_silence_counts(self):
        slots = [slot(hour=5, msgs=1, first=0, last=0), slot(hour=6), slot(hour=7)]
        secs, start = engine.longest_dead_stretch(slots)
        assert secs == 3600 * 3 - 0 and start == slot(hour=5).start_ts

    def test_none_without_slots_or_timing(self):
        assert engine.longest_dead_stretch([]) is None
        assert engine.longest_dead_stretch([engine.Slot("2026-09-14", "05", 5, 0, 3600, 0, msgs=3, approx=True)]) is None


class TestAnomalies:
    def weeks(self, target_total, target_day=date(2026, 9, 30), base_per_hour=4):
        days = {}
        for k in range(1, 9):
            d = target_day - timedelta(weeks=k)
            days[d.isoformat()] = flat_day(d, base_per_hour)
        days[target_day.isoformat()] = flat_day(target_day, target_total)
        return days

    def slots_for(self, days, target=date(2026, 9, 30), coverage=None):
        return engine.build_slots(days, target - timedelta(weeks=9), target, local_ts(2026, 10, 1, 1), coverage or local_ts(2026, 7, 1))

    def test_busy_day_is_flagged(self):
        target = date(2026, 9, 30)
        found = engine.find_anomalies(self.slots_for(self.weeks(12)), target, target)
        days = [a for a in found if a.scope == "day"]
        assert len(days) == 1 and days[0].kind == "busy"
        assert days[0].expected == 96 and days[0].actual == 288 and round(days[0].ratio, 1) == 3.0

    def test_dead_day_is_flagged(self):
        target = date(2026, 9, 30)
        found = engine.find_anomalies(self.slots_for(self.weeks(0, base_per_hour=8)), target, target)
        assert any(a.scope == "day" and a.kind == "quiet" for a in found)

    def test_normal_day_is_not_flagged(self):
        target = date(2026, 9, 30)
        assert engine.find_anomalies(self.slots_for(self.weeks(4)), target, target) == []

    def test_small_absolute_swings_are_ignored(self):
        # 2 msgs/hr vs 1 msg/hr is 2x but only 24 messages: noise, not an anomaly
        target = date(2026, 9, 30)
        assert engine.find_anomalies(self.slots_for(self.weeks(2, base_per_hour=1)), target, target) == []

    def test_needs_enough_history(self):
        target = date(2026, 9, 30)
        days = {target.isoformat(): flat_day(target, 50), (target - timedelta(weeks=1)).isoformat(): flat_day(target, 1)}
        # coverage began a week ago, so there is only ONE comparable prior same-weekday
        slots = self.slots_for(days, coverage=local_ts(2026, 9, 22))
        assert engine.find_anomalies(slots, target, target) == []

    def test_hour_spike_is_flagged(self):
        target = date(2026, 9, 30)
        days = self.weeks(4)
        days[target.isoformat()] = flat_day(target, {**{h: 4 for h in range(24)}, 21: 80})
        found = engine.find_anomalies(self.slots_for(days), target, target)
        assert any(a.scope == "hour" and a.kind == "busy" and a.hour == 21 for a in found)

    def test_dead_hour_in_a_normally_busy_slot_is_flagged(self):
        target = date(2026, 9, 30)
        days = {}
        for k in range(1, 9):
            d = target - timedelta(weeks=k)
            days[d.isoformat()] = flat_day(d, {h: 4 for h in range(24)} | {21: 60})
        days[target.isoformat()] = flat_day(target, {h: 4 for h in range(24)} | {21: 3})
        found = engine.find_anomalies(self.slots_for(days), target, target)
        assert any(a.scope == "hour" and a.kind == "quiet" and a.hour == 21 for a in found)

    def test_limit_and_day_level_first(self):
        target = date(2026, 9, 30)
        days = self.weeks(40)  # busy enough that individual hours spike too
        everything = engine.find_anomalies(self.slots_for(days), target, target, limit=50)
        assert any(a.scope == "hour" for a in everything)
        found = engine.find_anomalies(self.slots_for(days), target, target, limit=2)
        assert len(found) == 2 and found[0].scope == "day"


class TestTodayPace:
    def test_compares_with_same_weekday_up_to_the_same_hour(self):
        today = date(2026, 9, 30)
        days = {}
        for k in range(1, 5):
            d = today - timedelta(weeks=k)
            days[d.isoformat()] = flat_day(d, 10)
        days[today.isoformat()] = flat_day(today, {h: 20 for h in range(12)})
        now = local_ts(2026, 9, 30, 12, 20)
        slots = engine.build_slots(days, today - timedelta(weeks=4), today, now, local_ts(2026, 8, 1))
        actual, exp, ratio, n = engine.today_pace(slots, today)
        assert (actual, exp, n) == (240, 120, 4) and ratio == 2.0

    def test_none_without_baseline(self):
        today = date(2026, 9, 30)
        slots = engine.build_slots({today.isoformat(): flat_day(today, 5)}, today, today, local_ts(2026, 9, 30, 6), local_ts(2026, 9, 30))
        assert engine.today_pace(slots, today) is None


class TestAggregates:
    def docs(self):
        a, b = new_day_doc(), new_day_doc()
        a["h"]["20"] = hour_rec(10, channels={"111": [7, 3], "222": [3, 2]})
        b["h"]["20"] = hour_rec(10, channels={"111": [4, 2], "333": [6, 2]})
        a["u"], a["ub"] = {"1": 5, "2": 5}, {"3": 2}
        b["u"] = {"1": 8, "4": 2}
        a["joins"], a["leaves"], b["joins"] = 4, 1, 2
        return {"2026-09-14": a, "2026-09-15": b}

    def test_channel_ranking_sums_and_shares(self):
        r = engine.channel_ranking(self.docs(), date(2026, 9, 14), date(2026, 9, 15))
        assert [c for c, _, _ in r] == ["111", "333", "222"]
        assert r[0][1] == 11 and round(r[0][2], 2) == 0.55

    def test_channel_ranking_honours_ignored(self):
        r = engine.channel_ranking(self.docs(), date(2026, 9, 14), date(2026, 9, 15), ignored={"111"})
        assert "111" not in [c for c, _, _ in r]

    def test_top_chatters_merges_live_and_backfilled_counts(self):
        top = engine.top_chatters(self.docs(), date(2026, 9, 14), date(2026, 9, 15), 3)
        assert top == [(1, 13), (2, 5), (4, 2)] or top[0] == (1, 13)

    def test_unique_chatters_is_a_union(self):
        assert engine.unique_chatters(self.docs(), date(2026, 9, 14), date(2026, 9, 15)) == 4

    def test_unique_chatters_is_none_once_user_data_was_purged(self):
        d = self.docs()
        d["2026-09-14"]["u"], d["2026-09-14"]["ub"] = {}, {}
        assert engine.unique_chatters(d, date(2026, 9, 14), date(2026, 9, 15)) is None

    def test_joins_and_leaves(self):
        assert engine.joins_leaves(self.docs(), date(2026, 9, 14), date(2026, 9, 15)) == (6, 1)


class TestPeriods:
    def test_resolve_week_and_month(self):
        today = date(2026, 9, 30)
        s, e, label, ps, pe = engine.resolve_period("week", None, today)
        assert (s, e) == (date(2026, 9, 28), date(2026, 10, 4)) and (ps, pe) == (date(2026, 9, 21), date(2026, 9, 27))
        s, e, *_ = engine.resolve_period("week", "last", today)
        assert (s, e) == (date(2026, 9, 21), date(2026, 9, 27))
        s, e, label, ps, pe = engine.resolve_period("month", "last", today)
        assert (s, e) == (date(2026, 8, 1), date(2026, 8, 31)) and label == "August 2026"
        s, e, *_ = engine.resolve_period("month", "2026-02", today)
        assert (s, e) == (date(2026, 2, 1), date(2026, 2, 28))
        assert engine.resolve_period("day", "yesterday", today)[0] == date(2026, 9, 29)
        assert engine.resolve_period("week", "banana", today) is None
        assert engine.resolve_period("month", "13-2026", today) is None

    def test_weekly_digest_due_boundary(self):
        before = datetime(2026, 9, 28, 8, 59, tzinfo=TIMEZONE)  # Monday, 1 minute early
        at = datetime(2026, 9, 28, 9, 0, tzinfo=TIMEZONE)
        k1, s1, e1 = engine.weekly_due(before)
        k2, s2, e2 = engine.weekly_due(at)
        assert (s1, e1) == (date(2026, 9, 14), date(2026, 9, 20)) and k1 == "2026-09-14"
        assert (s2, e2) == (date(2026, 9, 21), date(2026, 9, 27)) and k2 == "2026-09-21"
        # a Friday: the most recently due digest is still the one that went out Monday (week of 9/21)
        assert engine.weekly_due(datetime(2026, 10, 2, 15, 0, tzinfo=TIMEZONE))[0] == "2026-09-21"

    def test_monthly_digest_due_boundary(self):
        assert engine.monthly_due(datetime(2026, 10, 1, 8, 0, tzinfo=TIMEZONE))[0] == "2026-08"
        key, s, e = engine.monthly_due(datetime(2026, 10, 1, 9, 0, tzinfo=TIMEZONE))
        assert (key, s, e) == ("2026-09", date(2026, 9, 1), date(2026, 9, 30))
        assert engine.monthly_due(datetime(2027, 1, 1, 10, 0, tzinfo=TIMEZONE))[0] == "2026-12"

    def make_weeks(self, this_week, last_week):
        days = {}
        for i in range(7):
            days[(date(2026, 9, 21) + timedelta(days=i)).isoformat()] = flat_day(date(2026, 9, 21), last_week)
            days[(date(2026, 9, 28) + timedelta(days=i)).isoformat()] = flat_day(date(2026, 9, 28), this_week)
        return days

    def test_complete_week_vs_previous_week(self):
        days = self.make_weeks(this_week=6, last_week=4)
        now = local_ts(2026, 10, 6, 12)
        rep = engine.build_period_report(days, "week", date(2026, 9, 28), date(2026, 10, 4), "w", date(2026, 9, 21), date(2026, 9, 27),
                                         now, local_ts(2026, 9, 1))
        assert rep.over and rep.msgs == 6 * 24 * 7
        assert rep.prev_msgs == 4 * 24 * 7 and round(rep.change_pct) == 50
        assert rep.busiest_day is not None and rep.quietest_day is not None
        assert len(rep.daily) == 7 and all(r.complete for r in rep.daily)

    def test_in_progress_week_compares_like_for_like(self):
        days = self.make_weeks(this_week=6, last_week=4)
        for i in range(3, 7):  # nothing yet recorded for Thu-Sun this week
            days.pop((date(2026, 9, 28) + timedelta(days=i)).isoformat())
        now = local_ts(2026, 10, 1, 0)  # Thursday 12am: Mon-Wed finished
        rep = engine.build_period_report(days, "week", date(2026, 9, 28), date(2026, 10, 4), "w", date(2026, 9, 21), date(2026, 9, 27),
                                         now, local_ts(2026, 9, 1))
        assert not rep.over
        assert rep.msgs == 6 * 24 * 3
        assert rep.prev_msgs == 4 * 24 * 3  # prior week truncated to the same 3 days
        assert round(rep.change_pct) == 50

    def test_no_comparison_when_prior_period_has_no_data(self):
        days = self.make_weeks(this_week=6, last_week=4)
        now = local_ts(2026, 10, 6, 12)
        rep = engine.build_period_report(days, "week", date(2026, 9, 28), date(2026, 10, 4), "w", date(2026, 9, 21), date(2026, 9, 27),
                                         now, local_ts(2026, 9, 28))
        assert rep.prev_msgs is None and rep.change_pct is None

    def test_data_starting_mid_period_is_flagged(self):
        days = self.make_weeks(this_week=6, last_week=4)
        now = local_ts(2026, 10, 6, 12)
        rep = engine.build_period_report(days, "week", date(2026, 9, 28), date(2026, 10, 4), "w", date(2026, 9, 21), date(2026, 9, 27),
                                         now, local_ts(2026, 10, 1))
        assert rep.partial_start

    def test_empty_period(self):
        rep = engine.build_period_report({}, "day", date(2026, 9, 14), date(2026, 9, 14), "d", date(2026, 9, 13), date(2026, 9, 13),
                                         local_ts(2026, 9, 30), None)
        assert rep.slots_n == 0 and rep.msgs == 0 and rep.dead is None


class TestSyntheticTraffic:
    """End-to-end sanity: an evening-peaked server should read as one."""

    def test_peak_is_in_the_evening_and_quiet_in_the_morning(self):
        docs, cov, end = traffic(date(2026, 9, 1), 21)
        slots = engine.build_slots(docs, date(2026, 9, 1), date(2026, 9, 21), end, cov)
        rows = engine.hour_table(slots)
        ranked = sorted((r for r in rows if r.n), key=lambda r: r.rank)
        assert ranked[0].hour in (20, 21, 22)
        assert ranked[-1].hour in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        assert engine.find_windows(slots, 2, 1)[0].start_hour in (20, 21)
