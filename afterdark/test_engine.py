import random

import pytest

from afterdark import engine
from afterdark.constants import DAY

ADULT = 100
AGE = {201, 202}
UNDER = {300}


def elig(roles, *, excluded=False, level=5, min_level=3, check_level=True):
    return engine.eligibility(
        role_ids=roles,
        excluded=excluded,
        underage_ids=UNDER,
        age_ids=AGE,
        adult_role_id=ADULT,
        level=level,
        min_level=min_level,
        check_level=check_level,
    )


# ------------------------------------------------------------- eligibility

def test_eligible_member_passes():
    assert elig({ADULT, 201}) == engine.OK


def test_excluded_beats_everything():
    assert elig({ADULT, 201}, excluded=True) == engine.EXCLUDED


def test_underage_role_beats_adult_age_role():
    # Someone holding both (should be impossible, but must fail closed).
    assert elig({ADULT, 201, 300}) == engine.UNDERAGE


def test_no_age_role_fails_closed():
    assert elig({ADULT}) == engine.NO_AGE_ROLE


def test_missing_adult_chat():
    assert elig({201}) == engine.NO_ADULT_CHAT


def test_level_below_minimum():
    assert elig({ADULT, 202}, level=2) == engine.LEVEL_LOW


def test_level_at_minimum_passes():
    assert elig({ADULT, 202}, level=3) == engine.OK


def test_unreadable_level_fails_closed_for_admission():
    assert elig({ADULT, 201}, level=None) == engine.LEVEL_UNKNOWN


def test_retention_ignores_level_even_when_unreadable():
    assert elig({ADULT, 201}, level=None, check_level=False) == engine.OK
    assert elig({ADULT, 201}, level=0, check_level=False) == engine.OK


def test_retention_still_enforces_roles():
    assert elig({201}, check_level=False) == engine.NO_ADULT_CHAT
    assert elig({ADULT, 201, 300}, check_level=False) == engine.UNDERAGE


# -------------------------------------------------------------- invitations

def test_la_date_uses_pacific_not_utc():
    # 2026-10-08 03:00 UTC is still Oct 7 in Los Angeles (UTC-7).
    ts = 1791428400  # 2026-10-08T03:00:00Z
    assert engine.la_date(ts) == "2026-10-07"
    assert engine.la_date(ts + 8 * 3600) == "2026-10-08"


def test_invite_count_within_bounds():
    rng = random.Random(1)
    counts = {engine.invite_count(rng, 3, 4) for _ in range(200)}
    assert counts == {3, 4}


def test_invite_count_handles_swapped_or_negative_bounds():
    rng = random.Random(1)
    assert engine.invite_count(rng, 5, 2) == 5
    assert engine.invite_count(rng, -3, 0) == 0


def test_shuffled_candidates_is_seeded_and_unique():
    a = engine.shuffled_candidates([5, 3, 3, 9, 1], random.Random(7))
    b = engine.shuffled_candidates([9, 1, 5, 3], random.Random(7))
    assert a == b  # input order irrelevant, duplicates removed
    assert sorted(a) == [1, 3, 5, 9]


def test_expired_invites_boundary():
    now = 1_000_000_000.0
    invites = {
        "1": {"ts": now - 30 * DAY},       # exactly 30 days: expired
        "2": {"ts": now - 30 * DAY + 1},   # one second short: still valid
        "3": {"ts": now - 31 * DAY},
    }
    assert sorted(engine.expired_invites(invites, now, 30)) == ["1", "3"]


# --------------------------------------------------------------- inactivity

NOW = 1_000_000_000.0


def entry(idle_days, *, warned_days_ago=None, since_days=None):
    last = NOW - idle_days * DAY
    since = NOW - (since_days if since_days is not None else idle_days) * DAY
    warned = NOW - warned_days_ago * DAY if warned_days_ago is not None else 0.0
    return {"since": since, "last": last, "warned": warned}


def test_status_ok_before_warning():
    assert engine.inactivity_status(entry(6.9), NOW, 7, 14) == engine.STATUS_OK


def test_status_warn_at_seven_days():
    assert engine.inactivity_status(entry(7), NOW, 7, 14) == engine.STATUS_WARN


def test_status_not_rewarned_in_same_cycle():
    e = entry(9, warned_days_ago=2)
    assert engine.inactivity_status(e, NOW, 7, 14) == engine.STATUS_OK


def test_status_remove_at_fourteen_days_even_if_warned():
    e = entry(14, warned_days_ago=7)
    assert engine.inactivity_status(e, NOW, 7, 14) == engine.STATUS_REMOVE


def test_activity_after_warning_resets_the_cycle():
    # Warned 10 days ago, then posted 8 days ago, idle again past 7 days: warn again.
    e = {"since": NOW - 30 * DAY, "last": NOW - 8 * DAY, "warned": NOW - 10 * DAY}
    assert engine.inactivity_status(e, NOW, 7, 14) == engine.STATUS_WARN


def test_clock_counts_from_join_when_never_active():
    # Joined the interest 5 days ago, never posted (last == since): fine.
    e = {"since": NOW - 5 * DAY, "last": NOW - 5 * DAY, "warned": 0}
    assert engine.inactivity_status(e, NOW, 7, 14) == engine.STATUS_OK


def test_recent_join_beats_old_last_activity():
    # Re-picked the interest 1 day ago; stale `last` must not trigger removal.
    e = {"since": NOW - 1 * DAY, "last": NOW - 40 * DAY, "warned": 0}
    assert engine.inactivity_status(e, NOW, 7, 14) == engine.STATUS_OK


def test_days_left_never_negative():
    assert engine.days_left(entry(20), NOW, 14) == 0.0
    assert engine.days_left(entry(7), NOW, 14) == pytest.approx(7.0)


def test_should_record_resolution_and_clock_skew():
    assert engine.should_record(None, NOW)
    assert not engine.should_record(NOW - 3600, NOW)
    assert engine.should_record(NOW - 7 * 3600, NOW)
    assert engine.should_record(NOW + 500, NOW)  # future timestamp is replaced


def test_new_membership_shape():
    m = engine.new_membership(NOW)
    assert m == {"since": NOW, "last": NOW, "warned": 0.0}


# ------------------------------------------------------------------- lapsed

def test_lapsed_add_has_remove_roundtrip():
    lapsed = {}
    lapsed = engine.lapsed_add(lapsed, "feet", 1)
    lapsed = engine.lapsed_add(lapsed, "feet", 1)  # idempotent
    assert lapsed == {"feet": [1]}
    assert engine.lapsed_has(lapsed, "feet", 1)
    assert not engine.lapsed_has(lapsed, "bdsm", 1)
    lapsed = engine.lapsed_remove(lapsed, "feet", 1)
    assert lapsed == {}  # empty keys are dropped


def test_lapsed_helpers_do_not_mutate_input():
    original = {"feet": [1, 2]}
    engine.lapsed_add(original, "feet", 3)
    engine.lapsed_remove(original, "feet", 1)
    assert original == {"feet": [1, 2]}


# ------------------------------------------------------------------ helpers

def test_normalize_key():
    assert engine.normalize_key("  Feet! ") == "feet"
    assert engine.normalize_key("B.D.S.M") == "b-d-s-m"
    assert engine.normalize_key("x" * 80) == "x" * 32


def test_parse_toggle():
    assert engine.parse_toggle("ON") is True
    assert engine.parse_toggle("off") is False
    assert engine.parse_toggle("maybe") is None


def test_circuit_open_is_strictly_greater():
    assert not engine.circuit_open(25, 25)
    assert engine.circuit_open(26, 25)


def test_cap_lines():
    assert engine.cap_lines(["a", "b"], 5) == ["a", "b"]
    out = engine.cap_lines([str(i) for i in range(30)], 25)
    assert len(out) == 26 and out[-1] == "...and 5 more"


def test_humanize_days():
    assert engine.humanize_days(0.2) == "5 hours"
    assert engine.humanize_days(1) == "1 day"
    assert engine.humanize_days(6.6) == "7 days"


def test_validate_warn_remove():
    assert engine.validate_warn_remove(7, 14) is None
    assert engine.validate_warn_remove(14, 14) is not None
    assert engine.validate_warn_remove(0, 14) is not None
