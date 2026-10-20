"""Pure-logic tests for The Knave's Trial (trial.py): no discord, no redbot.

The central proof: every generated case has exactly one valid answer, checked against an INDEPENDENT brute force that
shares no code with trial.solve. Everything a member is told (testimony, hints) is also checked against the truth."""
import itertools
import re

import pytest

from secrethunt import engine, trial
from secrethunt.constants import (
    HINT_FIRST_HOURS, HINT_GAP_HOURS, HINT_MAX, TEST_DELAY_SECONDS, TRIAL_LOCK_R1_HOURS, TRIAL_LOCK_R2_HOURS,
)

HOUR = 3600
ROOMS = [f"<#{100 + i}>" for i in range(5)]


def brute(statements, n_liars, with_hour):
    """A second, deliberately naive solver: try every thief, hour and set of liars, and test every statement."""
    out = []
    for thief in range(5):
        for hour in (range(1, 13) if with_hour else (0,)):
            for liars in itertools.combinations(range(5), n_liars):
                truths = [i not in liars for i in range(5)]
                if all(trial.statement_holds(statements[i], thief, hour, truths) == truths[i] for i in range(5)):
                    out.append((thief, hour, liars))
    return out


def case(seed, rnd):
    return trial.generate(seed, rnd)


# ---------------------------------------------------------------- generated cases

@pytest.mark.parametrize("rnd,n", [(1, 150), (2, 25)])
def test_every_generated_case_has_exactly_one_valid_answer(rnd, n):
    for i in range(n):
        statements, thief, hour, liars = case(f"unique:{i}", rnd)
        assert len(liars) == trial.LIARS[rnd]
        assert brute(statements, trial.LIARS[rnd], rnd == 2) == [(thief, hour, liars)]


def test_the_optimised_solver_agrees_with_plain_brute_force():
    import random
    rng = random.Random(7)
    for rnd in (1, 2):
        for _ in range(150):
            sts = [tuple(trial._random_part(rng, i, rnd == 2) for _ in range(rng.choice((1, 2)) if rnd == 2 else 1))
                   for i in range(5)]
            assert sorted(trial.solve(sts, trial.LIARS[rnd], rnd == 2)) == sorted(brute(sts, trial.LIARS[rnd], rnd == 2))


def test_cases_are_deterministic_and_differ_between_members():
    assert case("same", 1) == case("same", 1)
    seeds = [trial.case_seed("salt", "knaves_trial", uid, 1, 0) for uid in range(60)]
    distinct = {case(s, 1)[0] for s in seeds}
    assert len(distinct) >= 55  # sharing an answer with a friend is useless: their testimony is different
    assert case(trial.case_seed("salt", "p", 1, 1, 0), 1) != case(trial.case_seed("salt", "p", 1, 1, 1), 1)  # a re-roll differs


def test_a_case_is_never_a_one_liner():
    for i in range(20):
        statements, *_ = case(f"hard:{i}", 2)
        assert trial._links(statements) >= 2 and trial._hour_statements(statements) >= 2
        statements, *_ = case(f"hard:{i}", 1)
        assert trial._links(statements) >= 2


def test_round_one_never_mentions_the_hour_and_round_two_has_one():
    for i in range(20):
        statements, _t, hour, _l = case(f"hours:{i}", 1)
        assert hour == 0 and not any(p[0] in ("before", "after", "at") for s in statements for p in s)
        _s, _t, hour, _l = case(f"hours:{i}", 2)
        assert 1 <= hour <= 12


def test_testimony_reads_cleanly_and_names_rooms_by_mention():
    for i in range(30):
        for rnd in (1, 2):
            for stmt in case(f"text:{i}", rnd)[0]:
                text = trial.testimony(stmt, ROOMS)
                assert text and "{" not in text and "(" not in text and text.endswith(".")
                for part in stmt:
                    if part[0] in ("liar", "honest"):
                        assert ROOMS[part[1]] in text


def test_a_witness_never_talks_about_themselves():
    for i in range(40):
        for rnd in (1, 2):
            for w, stmt in enumerate(case(f"self:{i}", rnd)[0]):
                assert all(p[1] != w for p in stmt if p[0] in ("liar", "honest"))


# ---------------------------------------------------------------- reading answers

@pytest.mark.parametrize("text,rnd,expected", [
    ("Cook", 1, ("ok", (4, 0))),
    ("it was the Hare!", 1, ("ok", (1, 0))),
    ("March Hare", 1, ("ok", (1, 0))),
    ("the mad hatter", 1, ("ok", (0, 0))),
    ("  DORMOUSE ", 1, ("ok", (2, 0))),
    ("duchess 3", 1, ("ok", (3, 0))),            # round 1 ignores an hour that nobody asked for
    ("cook 4", 2, ("ok", (4, 4))),
    ("Cook, 4 o'clock", 2, ("ok", (4, 4))),
    ("cook4", 2, ("ok", (4, 4))),
    ("4 cook", 2, ("ok", (4, 4))),
    ("the Duchess at eleven", 2, ("ok", (3, 11))),
    ("hatter 9:00", 2, ("ok", (0, 9))),
    ("hare 12", 2, ("ok", (1, 12))),
    ("cook", 2, ("malformed", "hour")),
    ("cook 0", 2, ("malformed", "hour")),
    ("cook 13", 2, ("malformed", "hour")),
    ("cook 4 or 5", 2, ("malformed", "hour")),
    ("hatter hare", 1, ("malformed", "one_suspect")),
    ("hatter or cook 4", 2, ("malformed", "one_suspect")),
    ("walrus", 1, ("none", None)),               # an Easy answer, not for the trial
    ("hello", 2, ("none", None)),
    ("", 1, ("none", None)),
])
def test_parse_answer(text, rnd, expected):
    assert trial.parse_answer(text, rnd) == expected


# ---------------------------------------------------------------- one member's progress

def fresh(now=1000.0):
    return trial.ensure(engine.new_state(0, now))


def answer_for(case_, rnd):
    _s, thief, hour, _l = case_
    return trial.SUSPECTS[thief] + (f" {hour}" if rnd == 2 else "")


def wrong_for(case_, rnd):
    _s, thief, hour, _l = case_
    other = (thief + 1) % 5
    return trial.SUSPECTS[other] + (f" {hour}" if rnd == 2 else "")


def test_a_correct_round_one_answer_adjourns_the_court_until_morning():
    st, c = fresh(), case("s1", 1)
    assert trial.check_answer(st, c, answer_for(c, 1), 5000.0) == "correct"
    assert st["stage"] == engine.STAGE_FOUND and st["due_ts"] >= 5000.0 + 4 * HOUR and st["found_ts"] == 5000.0
    assert trial.round_of(st) == 0  # nothing more to answer until round 2 opens


def test_a_wrong_round_one_answer_locks_for_four_hours_and_rerolls():
    st, c = fresh(), case("s2", 1)
    assert trial.check_answer(st, c, wrong_for(c, 1), 5000.0) == "wrong"
    assert st["roll"] == 1 and st["wrongs"] == 1 and st["locked_until"] == 5000.0 + TRIAL_LOCK_R1_HOURS * HOUR
    assert TRIAL_LOCK_R1_HOURS == 4
    # during the lockout even the right answer is refused (and it is not a second wrong answer)
    assert trial.check_answer(st, c, answer_for(c, 1), 5000.0 + HOUR) == "recess"
    assert st["roll"] == 1 and st["stage"] == engine.STAGE_RIDDLE
    # afterwards the testimony is a new roll, so the OLD answer is no longer reliable
    c2 = case("s2-roll1", 1)
    assert trial.check_answer(st, c2, answer_for(c2, 1), 5000.0 + 13 * HOUR) == "correct"


def test_a_wrong_round_two_answer_is_only_a_four_hour_cooldown():
    st = fresh()
    st["stage"], st["final_ts"] = engine.STAGE_FINAL, 9000.0
    c = case("s3", 2)
    assert trial.check_answer(st, c, wrong_for(c, 2), 9500.0) == "wrong"
    assert st["locked_until"] == 9500.0 + TRIAL_LOCK_R2_HOURS * HOUR and TRIAL_LOCK_R2_HOURS == 4
    assert st["stage"] == engine.STAGE_FINAL and st["roll"] == 1  # round 1 stays solved; only round 2 re-rolls
    assert trial.check_answer(st, c, answer_for(c, 2), 9500.0 + 5 * HOUR) == "correct"
    assert st["stage"] == engine.STAGE_SOLVED and st["solved_ts"] > 0


def test_a_right_thief_with_the_wrong_hour_is_wrong():
    st = fresh()
    st["stage"], st["final_ts"] = engine.STAGE_FINAL, 9000.0
    c = case("s4", 2)
    _s, thief, hour, _l = c
    assert trial.check_answer(st, c, f"{trial.SUSPECTS[thief]} {hour % 12 + 1}", 9500.0) == "wrong"


def test_malformed_answers_cost_nothing_and_non_answers_are_ignored():
    st = fresh()
    st["stage"], st["final_ts"] = engine.STAGE_FINAL, 9000.0
    c = case("s5", 2)
    assert trial.check_answer(st, c, "cook", 9500.0) == "malformed_hour"
    assert trial.check_answer(st, c, "cook hatter 4", 9500.0) == "malformed_suspect"
    assert trial.check_answer(st, c, "walrus", 9500.0) == "ignore"
    assert st["roll"] == 0 and st["wrongs"] == 0 and not st["locked_until"]


def test_nothing_is_accepted_while_waiting_or_after_solving():
    c = case("s6", 1)
    for stage in (engine.STAGE_FOUND, engine.STAGE_SOLVED):
        st = fresh()
        st["stage"] = stage
        assert trial.check_answer(st, c, answer_for(c, 1), 5000.0) == "ignore"


def test_test_mode_shortens_the_recess():
    st, c = fresh(), case("s7", 1)
    assert trial.check_answer(st, c, wrong_for(c, 1), 5000.0, test_mode=True) == "wrong"
    assert st["locked_until"] == 5000.0 + TEST_DELAY_SECONDS


def test_the_court_reconvening_resets_the_round_state():
    st = fresh()
    st.update(roll=3, round_hints=2, last_hint_ts=77.0, locked_until=88.0, hints=2, wrongs=3)
    trial.start_round2(st)
    assert (st["roll"], st["round_hints"], st["last_hint_ts"], st["locked_until"]) == (0, 0, 0.0, 0.0)
    assert st["hints"] == 2 and st["wrongs"] == 3  # the Hall total and the history carry over


# ---------------------------------------------------------------- hints

def test_hint_timing_is_48_hours_then_24_then_24_then_no_more():
    assert (HINT_FIRST_HOURS, HINT_GAP_HOURS, HINT_MAX) == (48, 24, 3)
    st = fresh(1000.0)
    assert trial.request_hint(st, 1000.0 + 47 * HOUR) == ("early", 1000.0 + 48 * HOUR)
    assert trial.request_hint(st, 1000.0 + 48 * HOUR) == ("ok", 1)
    t1 = 1000.0 + 48 * HOUR
    assert trial.request_hint(st, t1 + 23 * HOUR) == ("early", t1 + 24 * HOUR)
    assert trial.request_hint(st, t1 + 24 * HOUR) == ("ok", 2)
    assert trial.request_hint(st, t1 + 48 * HOUR) == ("ok", 3)
    assert trial.request_hint(st, t1 + 99 * HOUR) == ("max", None)
    assert st["hints"] == 3 and st["round_hints"] == 3


def test_the_hint_clock_runs_from_the_round_start_and_survives_a_reroll():
    st, c = fresh(1000.0), case("h1", 1)
    trial.check_answer(st, c, wrong_for(c, 1), 1000.0 + 10 * HOUR)  # re-roll in the middle of the wait
    assert trial.request_hint(st, 1000.0 + 49 * HOUR) == ("ok", 1)  # not restarted by the wrong answer


def test_no_hint_during_a_recess_or_with_no_round_open():
    st, c = fresh(1000.0), case("h2", 1)
    trial.check_answer(st, c, wrong_for(c, 1), 1000.0 + 60 * HOUR)
    assert trial.request_hint(st, 1000.0 + 61 * HOUR)[0] == "recess"
    st["stage"] = engine.STAGE_FOUND
    assert trial.request_hint(st, 9e9) == ("none", None)


def test_round_two_has_its_own_hint_ladder_but_one_hall_total():
    st = fresh(1000.0)
    assert trial.request_hint(st, 1000.0 + 48 * HOUR) == ("ok", 1)
    st["stage"], st["final_ts"] = engine.STAGE_FINAL, 500000.0
    trial.start_round2(st)
    assert trial.request_hint(st, 500000.0 + 47 * HOUR)[0] == "early"
    assert trial.request_hint(st, 500000.0 + 48 * HOUR) == ("ok", 1)  # first hint OF THIS ROUND
    assert st["hints"] == 2  # ...and the second one in total


def test_test_mode_shortens_the_hint_clock():
    st = fresh(1000.0)
    assert trial.request_hint(st, 1000.0 + TEST_DELAY_SECONDS - 1, test_mode=True)[0] == "early"
    assert trial.request_hint(st, 1000.0 + TEST_DELAY_SECONDS, test_mode=True) == ("ok", 1)
    assert trial.request_hint(st, 1000.0 + 2 * TEST_DELAY_SECONDS, test_mode=True) == ("ok", 2)


@pytest.mark.parametrize("rnd,n", [(1, 80), (2, 15)])
def test_hints_are_always_true(rnd, n):
    for i in range(n):
        c = case(f"truth:{i}", rnd)
        _s, thief, hour, liars = c
        seed = f"truth:{i}"
        honest_room = trial.hint_text(c, rnd, 1, ROOMS, seed)
        liar_room = trial.hint_text(c, rnd, 2, ROOMS, seed)
        room = lambda text: ROOMS.index(re.search(r"<#\d+>", text).group())
        assert "telling the truth" in honest_room and room(honest_room) not in liars
        assert "is lying" in liar_room and room(liar_room) in liars
        third = trial.hint_text(c, rnd, 3, ROOMS, seed)
        if rnd == 1:
            innocent = next(s for s, name in enumerate(trial.SUSPECTS) if f"the {name}" in third)
            assert innocent != thief
        else:
            low, high = map(int, re.search(r"between (\d+) and (\d+)", third).groups())
            assert low <= hour <= high and 1 <= low and high <= 12 and high - low == 3
        for n_ in (1, 2, 3):  # a hint never states the answer outright
            assert "stole" not in trial.hint_text(c, rnd, n_, ROOMS, seed)


def test_hint_labels_for_the_hall():
    assert [trial.hint_label(n) for n in (0, 1, 2, 3)] == ["no hints", "1 hint", "2 hints", "3 hints"]


def test_no_statement_contradicts_itself_or_hands_over_the_whole_answer():
    for i in range(25):
        for rnd in (1, 2):
            for stmt in case(f"alone:{i}", rnd)[0]:
                fits = trial._fits(stmt)
                assert fits > 1  # not empty (it would expose a liar for free) and not a single (thief, hour)
                sentences = [trial.part_text(p, ROOMS) for p in stmt]
                assert len(set(sentences)) == len(sentences)  # and never the same sentence twice
