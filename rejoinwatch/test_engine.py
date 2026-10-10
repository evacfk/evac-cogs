"""Pure-logic tests for rejoinwatch.engine (no discord / redbot needed)."""
from rejoinwatch import engine

NOW = 1_800_000_000.0
DAY = engine.DAY
RET = 180


# ---------------------------------------------------------------- records

def test_window_drops_expired_and_sorts():
    stamps = [NOW - 10 * DAY, NOW - 200 * DAY, NOW - 100 * DAY]
    assert engine.window(stamps, NOW, RET) == [NOW - 100 * DAY, NOW - 10 * DAY]


def test_window_boundary_is_inclusive():
    assert engine.window([NOW - RET * DAY], NOW, RET) == [NOW - RET * DAY]
    assert engine.window([NOW - RET * DAY - 1], NOW, RET) == []


def test_history_and_count_use_string_keys():
    leaves = {"42": [NOW - DAY, NOW - 2 * DAY]}
    assert engine.count_leaves(leaves, 42, NOW, RET) == 2
    assert engine.count_leaves(leaves, 43, NOW, RET) == 0
    assert engine.history(leaves, 42, NOW, RET) == [NOW - 2 * DAY, NOW - DAY]


def test_record_leave_appends_in_place_and_returns_window():
    leaves = {}
    stamps = engine.record_leave(leaves, 7, NOW, NOW, RET)
    assert stamps == [NOW]
    assert leaves == {"7": [NOW]}
    stamps = engine.record_leave(leaves, 7, NOW + 5, NOW + 5, RET)
    assert stamps == [NOW, NOW + 5]
    assert len(stamps) == 2


def test_record_leave_expires_old_leaves_for_that_user():
    leaves = {"7": [NOW - 300 * DAY]}
    stamps = engine.record_leave(leaves, 7, NOW, NOW, RET)
    assert stamps == [NOW]  # the 300-day-old leave no longer counts


def test_prune_removes_expired_and_empty_users():
    leaves = {"1": [NOW - 300 * DAY], "2": [NOW - 300 * DAY, NOW - DAY], "3": [NOW - DAY]}
    kept, changed = engine.prune(leaves, NOW, RET)
    assert kept == {"2": [NOW - DAY], "3": [NOW - DAY]}
    assert changed is True
    assert "1" in leaves  # the input is not mutated


def test_prune_reports_no_change():
    leaves = {"3": [NOW - DAY]}
    kept, changed = engine.prune(leaves, NOW, RET)
    assert kept == leaves and changed is False
    assert engine.prune({}, NOW, RET) == ({}, False)


# -------------------------------------------------------------- decisions

def test_leave_action_only_alerts_from_second_leave():
    assert engine.leave_action(1) == "silent"
    assert engine.leave_action(2) == "alert"
    assert engine.leave_action(5) == "alert"


def test_rejoin_action_warns_once_then_escalates():
    assert engine.rejoin_action(0) == "none"
    assert engine.rejoin_action(1) == "warn"
    assert engine.rejoin_action(2) == "escalate"
    assert engine.rejoin_action(9) == "escalate"


def test_is_recent_is_symmetric():
    assert engine.is_recent(NOW, NOW - 10, 30)
    assert engine.is_recent(NOW, NOW + 10, 30)  # clock skew
    assert not engine.is_recent(NOW, NOW - 31, 30)


def test_can_resolve():
    assert engine.can_resolve([5, 6], 6, False, False)       # mod role
    assert engine.can_resolve([], 6, True, False)            # Ban Members
    assert engine.can_resolve([], None, False, True)         # Administrator
    assert not engine.can_resolve([5], 6, False, False)
    assert not engine.can_resolve([5], None, False, False)


# ------------------------------------------------------------------- text

def test_phrases():
    assert engine.count_phrase(1) == "1 time"
    assert engine.count_phrase(3) == "3 times"
    assert [engine.times_phrase(n) for n in (1, 2, 3)] == ["once", "twice", "3 times"]


def test_render_warning_tolerates_braces():
    assert engine.render_warning("Hi, {server}!", "Wonderland") == "Hi, Wonderland!"
    assert engine.render_warning("odd {braces} {server}", "W") == "odd {braces} W"


def test_delivery_phrase():
    assert "DM" in engine.delivery_phrase("dm")
    assert "<#9>" in engine.delivery_phrase("channel", "<#9>")
    assert "manually" in engine.delivery_phrase("failed")


# ------------------------------------------------------------ button ids

def test_custom_id_round_trip():
    assert engine.parse_custom_id(engine.make_custom_id("ban", 123)) == ("ban", 123)
    assert engine.parse_custom_id(engine.make_custom_id("keep", 9)) == ("keep", 9)


def test_parse_custom_id_rejects_foreign_or_malformed():
    for bad in (None, "", "other:ban:1", "rejoinwatch:ban", "rejoinwatch:nuke:1",
                "rejoinwatch:ban:abc", "rejoinwatch:ban:1:2"):
        assert engine.parse_custom_id(bad) is None
