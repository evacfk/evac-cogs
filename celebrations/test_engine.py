from datetime import date

import pytest

from celebrations import engine
from celebrations.engine import BirthdayError, parse_birthday

TODAY = date(2026, 10, 3)


@pytest.mark.parametrize("text,expected", [
    ("March 14", (3, 14, None)),
    ("14 March", (3, 14, None)),
    ("mar 14", (3, 14, None)),
    ("Mar 14, 1998", (3, 14, 1998)),
    ("14th of March 1998", (3, 14, 1998)),
    ("1998-03-14", (3, 14, 1998)),
    ("14/3", (3, 14, None)),
    ("3/14", (3, 14, None)),
    ("3/14/1998", (3, 14, 1998)),
    ("14.03.1998", (3, 14, 1998)),
    ("5/5", (5, 5, None)),
    ("Feb 29", (2, 29, None)),
    ("Feb 29 2000", (2, 29, 2000)),
    ("sept 9", (9, 9, None)),
])
def test_parse_birthday_formats(text, expected):
    assert tuple(parse_birthday(text, TODAY)) == expected


@pytest.mark.parametrize("text", [
    "03/04",          # March 4 or 3 April: ambiguous, must ask for the month name
    "3/4/1998",
    "Feb 30",
    "13/13",
    "March",
    "",
    "hello 14",
    "March 14 98",    # two-digit year
    "Feb 29 2001",    # not a leap year
    "March 14 2020",  # implies under 13
    "March 14 1850",
])
def test_parse_birthday_rejects(text):
    with pytest.raises(BirthdayError):
        parse_birthday(text, TODAY)


def test_feb29_is_celebrated_feb28_in_non_leap_years():
    assert engine.is_today(2, 29, date(2027, 2, 28))
    assert not engine.is_today(2, 29, date(2027, 3, 1))
    assert engine.is_today(2, 29, date(2028, 2, 29))
    assert not engine.is_today(2, 29, date(2028, 2, 28))


def test_next_occurrence_and_age():
    assert engine.next_occurrence(10, 3, TODAY) == TODAY
    assert engine.next_occurrence(10, 2, TODAY) == date(2027, 10, 2)
    assert engine.turning_age(1998, 10, 3, TODAY) == 28
    assert engine.turning_age(None, 10, 3, TODAY) is None
    assert engine.age_on(2000, 10, 4, TODAY) == 25


def test_anniversary_years():
    assert engine.anniversary_years(date(2023, 10, 3), TODAY) == 3
    assert engine.anniversary_years(date(2026, 10, 3), TODAY) == 0  # joined today
    assert engine.anniversary_years(date(2023, 10, 4), TODAY) == 0
    assert engine.anniversary_years(date(2024, 2, 29), date(2027, 2, 28)) == 3


def test_recently_celebrated_window():
    assert engine.recently_celebrated("2026-10-03", TODAY)
    assert engine.recently_celebrated("2026-01-01", TODAY)
    assert not engine.recently_celebrated("2025-10-03", TODAY)
    assert not engine.recently_celebrated(None, TODAY)
    assert not engine.recently_celebrated("garbage", TODAY)


def test_pick_gift():
    import random
    rng = random.Random(1)
    for _ in range(50):
        assert 20000 <= engine.pick_gift(20000, 30000, rng) <= 30000
    assert engine.pick_gift(25000, 25000) == 25000
    assert engine.pick_gift(0, 0) == 0
    assert engine.pick_gift(30000, 20000, rng) in range(20000, 30001)  # swapped args tolerated


def test_formatting():
    assert engine.fmt_birthday(3, 14) == "March 14"
    assert engine.fmt_birthday(3, 14, 1998) == "March 14, 1998"
    assert [engine.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st"]
