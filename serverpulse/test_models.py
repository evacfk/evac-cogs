from datetime import date, datetime

import pytest

from serverpulse import models
from serverpulse._testdata import local_ts
from serverpulse.constants import TIMEZONE


class TestSlots:
    def test_normal_day_has_24_slots(self):
        assert len(models.day_slots("2026-09-30")) == 24

    def test_spring_forward_day_has_23_slots_and_no_2am(self):
        slots = models.day_slots("2026-03-08")
        assert len(slots) == 23
        assert 2 not in [s.hour for s in slots]

    def test_fall_back_day_has_25_slots_with_distinct_keys_for_repeated_1am(self):
        slots = models.day_slots("2026-11-01")
        assert len(slots) == 25
        keys = [s.key for s in slots]
        assert keys.count("01") == 1 and keys.count("01b") == 1
        assert len(set(keys)) == 25

    def test_slot_for_ts_marks_second_1am_as_b(self):
        first = models.day_slots("2026-11-01")
        one_b = next(s for s in first if s.key == "01b")
        assert models.slot_for_ts(one_b.start_ts + 90).key == "01b"
        assert models.slot_for_ts(one_b.start_ts - 90).key == "01"

    def test_slots_are_contiguous(self):
        for dk in ("2026-03-08", "2026-11-01", "2026-06-15"):
            slots = models.day_slots(dk)
            for a, b in zip(slots, slots[1:]):
                assert a.end_ts == b.start_ts

    def test_weekday_of_slot_is_local(self):
        # 2026-09-28 is a Monday; 11pm local is already Tuesday in UTC
        s = models.slot_for_ts(local_ts(2026, 9, 28, 23, 30))
        assert s.weekday == 0 and s.date == "2026-09-28" and s.hour == 23


class TestHourMath:
    def test_floor_and_ceil_hour(self):
        t = local_ts(2026, 9, 30, 14, 20, 5)
        assert models.floor_hour(t) == int(local_ts(2026, 9, 30, 14))
        assert models.ceil_hour(t) == int(local_ts(2026, 9, 30, 15))
        exact = local_ts(2026, 9, 30, 14)
        assert models.ceil_hour(exact) == int(exact)

    def test_day_bounds_across_dst(self):
        assert models.day_end_ts(date(2026, 3, 8)) - models.day_start_ts(date(2026, 3, 8)) == 23 * 3600


class TestParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [("8pm", 20), ("8 pm", 20), ("8PM", 20), ("12am", 0), ("12pm", 12), ("12", 12), ("20", 20), ("20:00", 20),
         ("8:30pm", 20), ("noon", 12), ("midnight", 0), ("0", 0), ("23", 23), ("7a", 7), ("11p", 23)],
    )
    def test_parse_hour_valid(self, text, expected):
        assert models.parse_hour(text) == expected

    @pytest.mark.parametrize("text", ["24", "25", "13pm", "0pm", "abc", "", "8:5x"])
    def test_parse_hour_invalid(self, text):
        assert models.parse_hour(text) is None

    def test_parse_weekday(self):
        assert models.parse_weekday("Fri") == 4
        assert models.parse_weekday("sunday") == 6
        assert models.parse_weekday("nope") is None

    def test_parse_day_arg(self):
        today = date(2026, 9, 30)
        assert models.parse_day_arg(None, today) == today
        assert models.parse_day_arg("yesterday", today) == date(2026, 9, 29)
        assert models.parse_day_arg("2026-09-01", today) == date(2026, 9, 1)
        assert models.parse_day_arg("garbage", today) is None

    def test_date_key_validation_blocks_path_tricks(self):
        with pytest.raises(ValueError):
            models.parse_date_key("../../etc/passwd")

    def test_week_and_month_bounds(self):
        assert models.week_bounds(date(2026, 9, 30)) == (date(2026, 9, 28), date(2026, 10, 4))
        assert models.month_bounds(date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 2, 28))
        assert models.month_bounds(date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))
