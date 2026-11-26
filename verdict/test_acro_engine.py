"""Pure rules for Acrophobia: letters, entry validation, hour picking, voting, payout, stats."""
import random
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from verdict import acro_engine as ae

LA = ZoneInfo("America/Los_Angeles")


# ---- letters and themes

def test_letters_are_3_to_5_distinct_and_never_q_x_z():
    rng = random.Random(7)
    sizes = set()
    for _ in range(2000):
        letters = ae.pick_letters(rng)
        sizes.add(len(letters))
        assert len(set(letters)) == len(letters) and letters.isupper()
        assert not set(letters) & set("QXZ")
    assert sizes == {3, 4, 5}


def test_letters_lean_toward_common_starters():
    rng = random.Random(1)
    seen = {}
    for _ in range(6000):
        for c in ae.pick_letters(rng, 3):
            seen[c] = seen.get(c, 0) + 1
    assert seen["T"] > seen["B"] > seen["K"]
    assert seen["T"] > 4 * seen["K"]


def test_theme_is_optional_and_comes_from_the_list():
    rng = random.Random(3)
    picks = [ae.pick_theme(["a", "b", "c"], rng) for _ in range(500)]
    assert None in picks and {"a", "b", "c"} <= set(picks)
    assert 0.55 < sum(p is not None for p in picks) / 500 < 0.85
    assert ae.pick_theme([], rng) is None and ae.pick_theme(["  "], rng) is None


def test_parse_themes_one_per_line_dedupes_and_limits():
    assert ae.parse_themes("a pickup line\n\n  A Pickup   Line \nsecond one\n") == ["a pickup line", "second one"]
    assert ae.parse_themes("") == []
    with pytest.raises(ValueError):
        ae.parse_themes("x" * 81)
    with pytest.raises(ValueError):
        ae.parse_themes("\n".join(str(i) for i in range(61)))


# ---- entries

def test_valid_entry_is_one_word_per_letter_in_order():
    assert ae.validate_entry("Big tall kittens", "BTK") == ("Big tall kittens", None)
    assert ae.validate_entry("  big   TALL  kittens ", "BTK")[0] == "big TALL kittens"   # tidy spacing, keeps case
    assert ae.validate_entry("\"Big\" (tall) kittens!", "BTK")[1] is None                # leading punctuation is fine


@pytest.mark.parametrize("text,fragment", [
    ("", "Type 3 words"),
    ("big tall", "exactly 3 words"),
    ("big tall kittens too", "You wrote 4"),
    ("big kittens tall", "Word 2"),
    ("big tall tiger", "Word 3"),
    ("big 123 kittens", "Word 2"),
    ("big tall <@123>", "mentions"),
    ("big tall @everyone", "mentions"),
    ("big tall https://x.co", "mentions"),
    ("big tall www.kit.com", "mentions"),
    ("big tall discord.gg/abc", "mentions"),
    ("big " + "t" * 130 + " k", "characters"),
])
def test_invalid_entries_say_why(text, fragment):
    clean, err = ae.validate_entry(text, "BTK")
    assert clean is None and fragment in err


def test_wrong_letter_error_names_the_word_and_letter():
    _, err = ae.validate_entry("big kittens tall", "BTK")
    assert '"kittens"' in err and "**T**" in err


def test_escape_stops_markdown_tricks():
    assert ae.escape("**big** [x](y) ||spoil||") == "\\*\\*big\\*\\* \\[x\\](y) \\|\\|spoil\\|\\|"


# ---- scheduling

def test_pick_hours_takes_the_busiest_with_a_gap_inside_the_window():
    avg = {10: 5, 11: 90, 12: 80, 15: 70, 17: 60, 21: 999, 3: 999}
    assert ae.pick_hours(avg, 2, 9, 18, 3) == [11, 15]       # 12 is too close to 11; 21 and 3 are outside the window


def test_pick_hours_respects_taken_hours_and_runs_out_of_room():
    avg = {11: 90, 12: 80, 15: 70}
    assert ae.pick_hours(avg, 2, 9, 18, 3, taken=[11]) == [15, 18]   # 11 already ran; 12 is too close to it
    assert ae.pick_hours({}, 4, 9, 11, 3) == [9]               # only one hour fits a 3-hour window
    assert ae.pick_hours(avg, 0, 9, 18, 3) == []


def test_pick_hours_ties_go_to_the_earlier_hour():
    assert ae.pick_hours({}, 1, 9, 18, 3) == [9]


def test_plan_slots_land_inside_the_chosen_hours_and_off_the_hour():
    slots = ae.plan_slots(date(2026, 11, 26), [11, 15], random.Random(2))
    assert len(slots) == 2
    for ts, h in zip(slots, [11, 15]):
        t = datetime.fromtimestamp(ts, LA)
        assert t.hour == h and 5 <= t.minute <= 40 and t.date() == date(2026, 11, 26)


def test_due_slot_runs_a_fresh_slot_and_writes_off_stale_ones():
    plan = {"slots": [1000.0, 5000.0, 9000.0], "done": [5000.0]}
    assert ae.due_slot(plan, 999.0, 600) == (None, [])                    # not yet
    assert ae.due_slot(plan, 1100.0, 600) == (1000.0, [])
    assert ae.due_slot(plan, 1700.0, 600) == (None, [1000.0])             # too late: skipped, not run
    assert ae.due_slot(plan, 9100.0, 600) == (9000.0, [1000.0])


# ---- voting and prize

def test_tally_ignores_votes_for_removed_or_unknown_entries():
    entries = {"1": "a", "2": "b", "3": "c"}
    votes = {"10": "1", "11": "1", "12": "2", "13": "3", "14": "999"}
    assert ae.tally(votes, entries, removed=["3"]) == {"1": 2, "2": 1}


def test_winners_share_a_tie_and_nobody_wins_without_votes():
    assert ae.winners({"5": 2, "3": 2, "9": 1}) == ["3", "5"]
    assert ae.winners({"5": 3, "3": 2}) == ["5"]
    assert ae.winners({"5": 0, "3": 0}) == [] and ae.winners({}) == []


def test_ranked_entries_sort_by_votes_then_ballot_order():
    assert ae.ranked_entries({"a": 1, "b": 3, "c": 1}, ["c", "a", "b"]) == ["b", "c", "a"]


@pytest.mark.parametrize("prize,paid,cap,n,expected", [
    (2000, 0, 4000, 1, (2000, 2000)),
    (2000, 0, 4000, 2, (1000, 2000)),          # a tie splits the prize
    (2000, 0, 4000, 3, (666, 1998)),           # remainder is not minted
    (2000, 3000, 4000, 1, (1000, 1000)),       # only 1,000 left under the cap
    (2000, 4000, 4000, 1, (0, 0)),             # cap used up
    (2000, 5000, 4000, 1, (0, 0)),
    (0, 0, 4000, 1, (0, 0)),
    (2000, 0, 4000, 0, (0, 0)),
])
def test_payout_splits_and_respects_the_daily_cap(prize, paid, cap, n, expected):
    assert ae.payout(prize, paid, cap, n) == expected


# ---- stats and review

def _round(entries, *, cancelled=False, aborted=False, ts=100.0, votes=0):
    return ae.history_entry({"seq": 1, "letters": "ABC"}, cancelled=cancelled, entries=entries, votes=votes, win=[],
                            paid=0, ts=ts, aborted=aborted)


def test_history_entry_keeps_counts_only():
    e = _round(5, votes=9)
    assert set(e) == {"seq", "ts", "letters", "entries", "votes", "cancelled", "winners", "paid", "manual", "aborted"}


def test_averages_count_cancelled_rounds_but_not_mod_aborted_ones():
    hist = [_round(6, votes=10), _round(2, cancelled=True), _round(0, cancelled=True, aborted=True), _round(4, votes=8)]
    a = ae.averages(hist)
    assert a["rounds"] == 3 and a["cancelled"] == 1
    assert a["avg_entries"] == pytest.approx(4.0) and a["avg_votes"] == pytest.approx(9.0)
    assert ae.averages([])["avg_entries"] == 0.0


def test_review_waits_three_weeks_then_flags_thin_turnout():
    day = 86400
    hist = [_round(3, ts=10 * day), _round(4, ts=11 * day)]
    assert ae.review_state(0, 99 * day, hist, 21, 4.0)["days_left"] is None
    early = ae.review_state(0.5, 5 * day, hist, 21, 4.0)
    assert not early["due"] and early["days_left"] == 17 and not early["retire"]
    thin = ae.review_state(1.0, 22 * day, hist, 21, 4.0)
    assert thin["due"] and thin["retire"] and thin["avg"] == pytest.approx(3.5)
    healthy = ae.review_state(1.0, 22 * day, hist + [_round(9, ts=12 * day)], 21, 4.0)
    assert healthy["due"] and not healthy["retire"]
    old = ae.review_state(11 * day, 40 * day, hist, 21, 4.0)     # rounds before launch don't count
    assert old["avg"] == pytest.approx(4.0)


def test_merge_top_keeps_mind_reader_order_and_adds_acro_only_winners():
    rows = [("1", 5, 6), ("2", 3, 6)]
    out = ae.merge_top(rows, {"2": 2, "9": 3, "8": 3})
    assert out == [("1", 5, 6, 0), ("2", 3, 6, 2), ("8", None, None, 3), ("9", None, None, 3)]
    assert len(ae.merge_top(rows, {str(i): 1 for i in range(100, 130)}, limit=10)) == 10
    assert ae.month_wins({"2026-11": {"a": 2, "b": 0}}, "2026-11") == {"a": 2}
