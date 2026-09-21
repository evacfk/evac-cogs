from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from photodrop import models
from photodrop.constants import STATUS_FULL, STATUS_NO_SHOW, STATUS_PTO, STATUS_TARDY


def fresh_member():
    return {"streak": 0, "strikes": [], "history": {}}


class TestRecordDrop:
    def test_full_quota_increments_streak(self):
        member = fresh_member()
        member["streak"] = 5
        updated, outcome, strike_added = models.record_drop(member, "2026-09-21", 3, 3, ["a", "b", "c"])
        assert outcome == STATUS_FULL
        assert updated["streak"] == 6
        assert strike_added is False
        assert updated["history"]["2026-09-21"]["status"] == STATUS_FULL
        assert updated["history"]["2026-09-21"]["photo_paths"] == ["a", "b", "c"]

    def test_over_quota_still_full(self):
        member = fresh_member()
        updated, outcome, _ = models.record_drop(member, "2026-09-21", 5, 3, ["a"] * 5)
        assert outcome == STATUS_FULL
        assert updated["streak"] == 1

    def test_partial_resets_streak_and_adds_strike(self):
        member = fresh_member()
        member["streak"] = 10
        updated, outcome, strike_added = models.record_drop(member, "2026-09-21", 2, 3, ["a", "b"])
        assert outcome == STATUS_TARDY
        assert updated["streak"] == 0
        assert strike_added is True
        assert len(updated["strikes"]) == 1

    def test_zero_photos_is_no_show(self):
        member = fresh_member()
        member["streak"] = 4
        updated, outcome, strike_added = models.record_drop(member, "2026-09-21", 0, 3, [])
        assert outcome == STATUS_NO_SHOW
        assert updated["streak"] == 0
        assert strike_added is True


class TestPtoAndNoShow:
    def test_pto_does_not_touch_streak_or_strikes(self):
        member = fresh_member()
        member["streak"] = 7
        updated = models.record_pto(member, "2026-09-21")
        assert updated["streak"] == 7
        assert updated["strikes"] == []
        assert updated["history"]["2026-09-21"]["status"] == STATUS_PTO

    def test_no_show_resets_streak_and_adds_strike(self):
        member = fresh_member()
        member["streak"] = 3
        updated = models.record_no_show(member, "2026-09-21")
        assert updated["streak"] == 0
        assert len(updated["strikes"]) == 1
        assert updated["history"]["2026-09-21"]["status"] == STATUS_NO_SHOW

    def test_needs_no_show_check_true_when_missing(self):
        member = fresh_member()
        assert models.needs_no_show_check(member, "2026-09-21") is True

    def test_needs_no_show_check_false_after_pto(self):
        member = models.record_pto(fresh_member(), "2026-09-21")
        assert models.needs_no_show_check(member, "2026-09-21") is False

    def test_needs_no_show_check_false_after_drop(self):
        member, _, _ = models.record_drop(fresh_member(), "2026-09-21", 3, 3, ["a"])
        assert models.needs_no_show_check(member, "2026-09-21") is False


class TestStrikeWindow:
    def _ts(self, days_ago):
        return (datetime.now(tz=ZoneInfo("UTC")) - timedelta(days=days_ago)).isoformat()

    def test_old_strikes_pruned(self):
        strikes = [self._ts(40), self._ts(5), self._ts(1)]
        kept, count = models.prune_and_count_strikes(strikes, window_days=30)
        assert count == 2

    def test_should_lose_role_at_threshold(self):
        member = {"strikes": [self._ts(1), self._ts(2), self._ts(3)]}
        assert models.should_lose_role(member, threshold=3, window_days=30) is True

    def test_should_not_lose_role_below_threshold(self):
        member = {"strikes": [self._ts(1), self._ts(2)]}
        assert models.should_lose_role(member, threshold=3, window_days=30) is False

    def test_strikes_outside_window_dont_count_toward_threshold(self):
        member = {"strikes": [self._ts(40), self._ts(1), self._ts(2)]}
        assert models.should_lose_role(member, threshold=3, window_days=30) is False

    def test_single_strike_revokes_role_at_shipped_default_threshold(self):
        # threshold=1 is the product default: any one Tardy or no-show costs the role.
        member = {"strikes": [self._ts(1)]}
        assert models.should_lose_role(member, threshold=1, window_days=30) is True

    def test_no_strikes_never_loses_role(self):
        member = {"strikes": []}
        assert models.should_lose_role(member, threshold=1, window_days=30) is False


class TestManualOverrides:
    def test_restore_streak(self):
        updated = models.restore_streak(fresh_member(), 14)
        assert updated["streak"] == 14

    def test_restore_streak_floors_at_zero(self):
        updated = models.restore_streak(fresh_member(), -5)
        assert updated["streak"] == 0

    def test_clear_strikes(self):
        member = fresh_member()
        member["strikes"] = ["x", "y"]
        updated = models.clear_strikes(member)
        assert updated["strikes"] == []


class TestDayKeys:
    def test_day_key_format(self):
        dt = datetime(2026, 9, 21, 14, 30)
        assert models.day_key(dt) == "2026-09-21"

    def test_parse_day_key_roundtrip(self):
        dt = models.parse_day_key("2026-09-21")
        assert dt.year == 2026 and dt.month == 9 and dt.day == 21

    def test_parse_day_key_rejects_bad_format(self):
        with pytest.raises(ValueError):
            models.parse_day_key("not-a-date")

    def test_week_dates_returns_monday_through_sunday(self):
        # 2026-09-21 is a Monday
        dates = models.week_dates(datetime(2026, 9, 23))
        assert models.day_key(dates[0]) == "2026-09-21"
        assert models.day_key(dates[6]) == "2026-09-27"
        assert len(dates) == 7


class TestMissedDays:
    def test_normal_one_day_gap_is_just_yesterday(self):
        # last ran "yesterday", running again "today" -> exactly one missed day
        assert models.missed_days("2026-09-20", "2026-09-21") == ["2026-09-20"]

    def test_multi_day_outage_catches_every_missed_day(self):
        # bot was down for several rollovers -> every day in between, oldest first,
        # including last_rollover's own date (it ran that day but only checked the
        # day before it, since that day itself wasn't over yet)
        assert models.missed_days("2026-09-18", "2026-09-22") == [
            "2026-09-18",
            "2026-09-19",
            "2026-09-20",
            "2026-09-21",
        ]

    def test_same_day_has_no_missed_days(self):
        assert models.missed_days("2026-09-21", "2026-09-21") == []


class TestCalendarStatus:
    def test_returns_none_for_missing_day(self):
        assert models.calendar_status(fresh_member(), "2026-09-21") is None

    def test_returns_status_for_logged_day(self):
        member, _, _ = models.record_drop(fresh_member(), "2026-09-21", 3, 3, ["a"])
        assert models.calendar_status(member, "2026-09-21") == STATUS_FULL
