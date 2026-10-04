from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from wonderevents import engine
from wonderevents.engine import WhenError, parse_when

LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 3, 20, 0, tzinfo=LA)  # Saturday 8pm Pacific


def when(text, tz=LA):
    return parse_when(text, NOW, tz)


def test_weekday_and_time_in_named_timezone():
    t = when("Sat 9:00 PM EST")  # the screenshot's style; Oct is really EDT, zoneinfo handles it
    assert t == datetime(2026, 10, 10, 21, 0, tzinfo=NY)  # 9pm ET Sat Oct 3 = 6pm PT, already passed -> next Sat
    assert when("sat 9pm") == datetime(2026, 10, 3, 21, 0, tzinfo=LA)  # later today
    assert when("sun 7pm") == datetime(2026, 10, 4, 19, 0, tzinfo=LA)


def test_dates_times_and_keywords():
    assert when("Oct 17 9:30pm") == datetime(2026, 10, 17, 21, 30, tzinfo=LA)
    assert when("17 october at 21:00") == datetime(2026, 10, 17, 21, 0, tzinfo=LA)
    assert when("Sat Oct 17, 9 pm ET") == datetime(2026, 10, 17, 21, 0, tzinfo=NY)
    assert when("tomorrow 8pm") == datetime(2026, 10, 4, 20, 0, tzinfo=LA)
    assert when("tonight 11pm") == datetime(2026, 10, 3, 23, 0, tzinfo=LA)
    assert when("2026-10-17 21:00 Europe/London") == datetime(2026, 10, 17, 21, 0, tzinfo=ZoneInfo("Europe/London"))
    assert when("10/17 noon") == datetime(2026, 10, 17, 12, 0, tzinfo=LA)
    assert when("17/10 noon") == datetime(2026, 10, 17, 12, 0, tzinfo=LA)
    assert when("Jan 5 8pm") == datetime(2027, 1, 5, 20, 0, tzinfo=LA)  # rolls to next year
    assert when("9pm", tz=NY) == datetime(2026, 10, 4, 21, 0, tzinfo=NY)  # 9pm ET today already passed
    assert when("Oct 17 9pm", tz=ZoneInfo("Europe/London")).tzinfo == ZoneInfo("Europe/London")


@pytest.mark.parametrize("text", ["", "Saturday", "Oct 17", "Oct 32 9pm", "03/04 9pm", "13pm", "Sat 25:00",
                                  "Oct 3 7pm", "9pm Mars/Base", "2028-01-01 9pm"])
def test_rejections(text):
    with pytest.raises(WhenError):
        when(text)


def test_poll_hours_and_options():
    now = NOW.timestamp()
    assert engine.poll_hours(now + 26 * 3600, now) == 24
    assert engine.poll_hours(now + 1800, now) == 1
    assert engine.poll_hours(now + 30 * 86400, now) == 168
    assert engine.poll_options("Hail Mary\n- Dune 2\n\n dune 2 \nx" + "y" * 80) == ["Hail Mary", "Dune 2", "x" + "y" * 54]
    assert len(engine.poll_options("\n".join(str(i) for i in range(20)))) == 10


def test_rsvp_toggle():
    r = {}
    assert engine.toggle_rsvp(r, 1, "going") == "going"
    assert engine.toggle_rsvp(r, 1, "maybe") == "maybe"
    assert engine.toggle_rsvp(r, 1, "maybe") is None and r == {}
    engine.toggle_rsvp(r, 2, "going")
    engine.toggle_rsvp(r, 3, "no")
    assert engine.rsvp_lists(r) == {"going": [2], "maybe": [], "no": [3]}


def test_due_actions_timeline():
    start = NOW.timestamp() + 7200
    ev = {"start_ts": start, "duration": 120}
    assert engine.due_actions(ev, start - 7000, 60) == []
    assert engine.due_actions(ev, start - 3000, 60) == ["remind"]
    ev["reminded"] = True
    assert engine.due_actions(ev, start - 3000, 60) == []
    assert engine.due_actions(ev, start + 10, 60) == ["start", "sample"]
    ev.update(started=True, last_sample=start + 10)
    assert engine.due_actions(ev, start + 100, 60) == []
    assert engine.due_actions(ev, start + 310, 60) == ["sample"]
    assert engine.due_actions(ev, start + 7200, 60) == ["end"]
    ev["cancelled"] = True
    assert engine.due_actions(ev, start + 10, 60) == []


def test_bot_down_through_the_whole_event_just_ends_it():
    start = NOW.timestamp()
    assert engine.due_actions({"start_ts": start, "duration": 60}, start + 4000, 60) == ["end"]


def test_attendance_and_regulars():
    att = {}
    for present in ([1, 2], [1, 2], [1], [1, 3]):
        engine.add_sample(att, present)
    assert att == {"1": 20, "2": 10, "3": 5}
    assert engine.attendees(att) == [1]
    evs = [
        {"start_ts": 100, "attend": {"1": 30, "2": 25}},
        {"start_ts": 200, "attend": {"1": 40}},
        {"start_ts": 300, "attend": {"2": 60}, "cancelled": True},
        {"start_ts": 10, "attend": {"9": 60}},
    ]
    assert engine.regulars(evs, since_ts=50) == [(1, 2), (2, 1)]
