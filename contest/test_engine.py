"""Pure-logic tests for contest.engine."""
import io
from datetime import datetime

import pytest

from contest import constants as C
from contest import engine as E

LA = C.HOME_TZ


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LA).timestamp()


S = dict(C.DEFAULT_SCHEDULE)


# ---- schedule ---------------------------------------------------------------

def test_default_schedule_is_the_agreed_one():
    assert S["open_ts"] == ts(2026, 10, 26)
    assert S["entries_close_ts"] == ts(2026, 10, 31, 23, 0)
    assert S["voting_close_ts"] == ts(2026, 11, 1)
    assert S["reveal_ts"] == ts(2026, 11, 1, 12)
    assert S["purge_ts"] == ts(2026, 11, 10, 12)
    assert E.validate_schedule(S) == ""


def test_voting_days_are_oct_26_to_oct_31():
    days = E.voting_days(S)
    assert days[0] == "2026-10-26" and days[-1] == "2026-10-31" and len(days) == 6


@pytest.mark.parametrize("change,msg", [
    ({"voting_close_ts": ts(2026, 10, 25)}, "close after it opens"),
    ({"entries_close_ts": ts(2026, 11, 1, 1)}, "Entries cannot close after voting"),
    ({"reveal_ts": ts(2026, 10, 31)}, "before voting closes"),
    ({"purge_ts": ts(2026, 11, 1, 6)}, "after the reveal"),
])
def test_incoherent_schedules_are_refused(change, msg):
    assert msg in E.validate_schedule({**S, **change})


def test_parse_and_format_round_trip():
    t = E.parse_pt("2026-10-31 23:00")
    assert t == S["entries_close_ts"]
    assert E.fmt_pt(t) == "Sat Oct 31, 11:00 PM PT"
    with pytest.raises(ValueError):
        E.parse_pt("Oct 31 11pm")


def test_phases():
    kw = dict(launched=True, revealed=False, purged=False)
    assert E.phase(ts(2026, 10, 16), S, launched=False, revealed=False, purged=False) == "setup"
    assert E.phase(ts(2026, 10, 25, 23, 59), S, **kw) == "entries"
    assert E.phase(ts(2026, 10, 26), S, **kw) == "voting"
    assert E.phase(ts(2026, 10, 31, 23, 59), S, **kw) == "voting"
    assert E.phase(ts(2026, 11, 1), S, **kw) == "picks"
    assert E.phase(ts(2026, 11, 1, 13), S, launched=True, revealed=True, purged=False) == "revealed"
    assert E.phase(ts(2026, 11, 11), S, launched=True, revealed=True, purged=True) == "purged"


def test_entries_and_voting_windows():
    assert E.entries_open(ts(2026, 10, 31, 22, 59), S, launched=True)
    assert not E.entries_open(ts(2026, 10, 31, 23, 0), S, launched=True)
    assert not E.entries_open(ts(2026, 10, 20), S, launched=False)
    assert not E.voting_open(ts(2026, 10, 25, 23, 59), S, launched=True)
    assert E.voting_open(ts(2026, 10, 26), S, launched=True)
    assert not E.voting_open(ts(2026, 11, 1), S, launched=True)


# ---- votes ------------------------------------------------------------------

OWNERS = {1: 101, 2: 102, 3: 103, 4: 104}


def test_cannot_vote_for_your_own_costume():
    assert "own" in E.vote_problem({}, 101, 1, "2026-10-26", OWNERS)


def test_cannot_vote_for_unknown_costume():
    assert "not in the contest" in E.vote_problem({}, 9, 99, "2026-10-26", OWNERS)


def test_never_the_same_costume_on_two_days():
    votes = E.cast_vote({}, 9, 2, "2026-10-26")
    assert "another day" in E.vote_problem(votes, 9, 2, "2026-10-27", OWNERS)
    assert E.vote_problem(votes, 9, 3, "2026-10-27", OWNERS) == ""


def test_switching_todays_vote_is_allowed_and_frees_the_old_pick():
    votes = E.cast_vote({}, 9, 2, "2026-10-26")
    assert E.vote_problem(votes, 9, 2, "2026-10-26", OWNERS) == ""   # same day, same costume: just re-confirms
    votes = E.cast_vote(votes, 9, 3, "2026-10-26")
    assert votes["9"] == {"2026-10-26": 3}                          # one vote per day
    assert E.vote_problem(votes, 9, 2, "2026-10-27", OWNERS) == ""   # #2 is free again


def test_cast_vote_does_not_mutate_its_input():
    before = {"9": {"2026-10-26": 2}}
    E.cast_vote(before, 9, 3, "2026-10-27")
    assert before == {"9": {"2026-10-26": 2}}


def test_tally_and_drop():
    votes = {"1": {"d1": 2, "d2": 3}, "2": {"d1": 2}, "3": {"d1": 4}}
    assert E.tally(votes, [1, 2, 3, 4]) == {1: 0, 2: 2, 3: 1, 4: 1}
    dropped = E.drop_entry_votes(votes, 2)
    assert dropped == {"1": {"d2": 3}, "3": {"d1": 4}}
    assert E.voters_for(votes, 2) == 2


# ---- placements -------------------------------------------------------------

def test_places_simple():
    assert E.places_from_counts({1: 5, 2: 3, 3: 1, 4: 0}) == {1: 1, 2: 2, 3: 3}


def test_ties_share_a_place_and_skip_the_next():
    assert E.places_from_counts({1: 5, 2: 5, 3: 2, 4: 1}) == {1: 1, 2: 1, 3: 3}


def test_zero_votes_never_place_and_four_way_tie_for_third_all_place():
    assert E.places_from_counts({1: 0, 2: 0}) == {}
    assert E.places_from_counts({1: 9, 2: 8, 3: 7, 4: 7, 5: 7, 6: 1}) == {1: 1, 2: 2, 3: 3, 4: 3, 5: 3}


# ---- mods' choice -----------------------------------------------------------

APPROVED = {1: 101, 2: 102, 3: 103, 4: 104, 5: 105}


def test_eligible_excludes_mod_entrants_and_the_peoples_choice_podium():
    assert E.eligible_for_picks(APPROVED, [2], {1: 1, 3: 2}) == [4, 5]


def test_picks_problems():
    assert "twice" in E.picks_problem([4, 4, None], [4, 5])
    assert "cannot be picked" in E.picks_problem([2, None, None], [4, 5])
    assert E.picks_problem([5, 4, None], [4, 5]) == ""


def test_picks_complete_allows_blank_slots_when_there_are_too_few_candidates():
    assert not E.picks_complete([None, None, None], [4, 5])
    assert E.picks_complete([4, 5, None], [4, 5])
    assert E.picks_complete([None, None, None], [])
    assert not E.picks_complete([4, None, None], [4, 5, 6])


def test_parse_pick():
    assert E.parse_pick(" #7 ") == 7 and E.parse_pick("") is None
    with pytest.raises(ValueError):
        E.parse_pick("seven")


# ---- tiers ------------------------------------------------------------------

def test_tiers_use_the_best_placement_only():
    approved = {1: 101, 2: 102, 3: 103, 4: 104, 5: 105, 6: 106}
    tiers = E.assign_tiers(approved, {1: 1, 2: 2, 3: 3}, {4: 1, 5: 2})
    assert tiers[101]["tier"] == C.TIER_GOLD
    assert tiers[104]["tier"] == C.TIER_GOLD
    assert tiers[102]["tier"] == C.TIER_SECOND and tiers[105]["tier"] == C.TIER_SECOND
    assert tiers[103]["tier"] == C.TIER_THIRD
    assert tiers[106]["tier"] == C.TIER_PARTICIPANT


def test_a_person_in_both_awards_gets_one_tier():
    tiers = E.assign_tiers({1: 101}, {1: 3}, {1: 1})
    assert tiers == {101: {"tier": C.TIER_GOLD, "number": 1, "why": ["Mods' Choice 1st", "People's Choice 3rd"]}}


def test_coins_and_roles_by_tier():
    coins = {"participant": 10, "third": 30, "second": 50, "gold": 100}
    assert [E.coins_for(t, coins) for t in (1, 2, 3, 4)] == [100, 50, 30, 10]
    assert E.role_kind(C.TIER_GOLD) == "gold"
    assert all(E.role_kind(t) == "bronze" for t in (2, 3, 4))


def test_next_number_reuses_only_when_the_highest_was_removed():
    assert E.next_number([]) == 1
    assert E.next_number([1, 2, 3]) == 4
    assert E.next_number([1, 3]) == 4


def test_exclusion_and_staff_checks():
    assert E.is_excluded([5, 9], [9, 10])
    assert not E.is_excluded([5], [9, 10])
    assert E.has_any([1], C.STAFF_ROLE_IDS) is False


def test_assign_tiers_keeps_the_best_tier_if_a_user_ever_holds_two_entries():
    tiers = E.assign_tiers({1: 101, 2: 101}, {1: 3, 2: 1}, {})
    assert tiers[101]["tier"] == C.TIER_GOLD and tiers[101]["number"] == 2
