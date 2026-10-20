"""Pure-logic tests for The Treacle Well (trail.py): the poem reactions, the personal plan and the tea table state machine."""
from collections import deque

import pytest

from secrethunt import engine, trail
from secrethunt.constants import TEST_DELAY_SECONDS, TRAIL_REACTS_PER_HOUR, TRAIL_REST_MINUTES, TRAIL_WRONG_LOOKS
from secrethunt.puzzles import PUZZLES

WELL = PUZZLES["treacle_well"]
T0 = 1_800_000_000.0
TEA, HONEY, SLEEP = "\N{HOT BEVERAGE}", "\N{HONEY POT}", "\N{SLEEPING FACE}"


def hist(*pairs):
    return deque((T0 + dt, e) for dt, e in pairs)


# ---------------------------------------------------------------- stage 1

def test_the_three_images_in_order_wake_the_dormouse():
    assert trail.poem_solved(hist((0, TEA), (5, HONEY), (9, SLEEP)))


@pytest.mark.parametrize("first", ["\N{HOT BEVERAGE}", "\N{TEACUP WITHOUT HANDLE}", "\N{TEAPOT}"])
@pytest.mark.parametrize("last", ["\N{SLEEPING FACE}", "\N{SLEEPING SYMBOL}", "\N{BED}"])
def test_look_alike_emoji_count(first, last):
    assert trail.poem_solved(hist((0, first), (1, HONEY), (2, last)))


def test_variation_selectors_do_not_matter():
    assert trail.poem_solved(hist((0, "\N{HOT BEVERAGE}\N{VARIATION SELECTOR-16}"), (1, HONEY), (2, SLEEP)))


@pytest.mark.parametrize("order", [(HONEY, TEA, SLEEP), (SLEEP, HONEY, TEA), (TEA, SLEEP, HONEY), (TEA, HONEY, TEA)])
def test_the_wrong_order_does_not(order):
    assert not trail.poem_solved(hist(*[(i, e) for i, e in enumerate(order)]))


def test_two_of_three_is_not_enough_and_noise_before_is_fine():
    assert not trail.poem_solved(hist((0, TEA), (1, HONEY)))
    assert trail.poem_solved(hist((0, "\N{FIRE}"), (1, "\N{CAT}"), (2, TEA), (3, HONEY), (4, SLEEP)))


def test_noise_in_the_middle_breaks_it():
    assert not trail.poem_solved(hist((0, TEA), (1, "\N{FIRE}"), (2, HONEY), (3, SLEEP)))


def test_the_three_must_come_within_the_window():
    assert not trail.poem_solved(hist((0, TEA), (500, HONEY), (700, SLEEP)))
    assert trail.poem_solved(hist((0, TEA), (500, HONEY), (599, SLEEP)))


def test_reactions_past_the_hourly_cap_are_ignored():
    h = deque()
    for i in range(TRAIL_REACTS_PER_HOUR):
        assert trail.record_reaction(h, "\N{FIRE}", T0 + i)
    assert not trail.record_reaction(h, TEA, T0 + 40) and len(h) == TRAIL_REACTS_PER_HOUR
    assert trail.record_reaction(h, TEA, T0 + 3700)  # an hour later the old ones have aged out


# ---------------------------------------------------------------- the plan

def test_the_plan_is_deterministic_and_valid_for_every_seed():
    seen = set()
    for uid in range(2000):
        p = trail.plan(trail.seed("salt", "treacle_well", uid), 12)
        assert p == trail.plan(trail.seed("salt", "treacle_well", uid), 12)
        assert len(set(p["chain"])) == 3 and all(0 <= r < 12 for r in p["chain"])
        assert all(trail.NUMBER_RANGE[0] <= n <= trail.NUMBER_RANGE[1] for n in p["numbers"])
        assert len(set(p["tokens"])) == 3 and all(t in trail.TOKEN_WORDS for t in p["tokens"])
        assert 6 <= sum(p["numbers"]) <= 39  # stage 4's count will be this sum
        seen.add(tuple(p["chain"]))
    assert len(seen) > 500  # 1,320 ordered chains exist; members do not share one


def test_a_different_salt_gives_different_plans():
    a = [trail.plan(trail.seed("one", "treacle_well", u), 12)["chain"] for u in range(50)]
    b = [trail.plan(trail.seed("two", "treacle_well", u), 12)["chain"] for u in range(50)]
    assert a != b


def test_the_plan_works_with_the_smallest_trail():
    for uid in range(200):
        assert sorted(trail.plan(trail.seed("s", "p", uid), 3)["chain"]) == [0, 1, 2]


def test_every_emoji_slot_is_a_single_distinct_group():
    flat = [engine.normalize_emoji(e) for group in trail.EMOJI_SLOTS for e in group]
    assert len(flat) == len(set(flat)) and len(trail.EMOJI_SLOTS) == 3


# ---------------------------------------------------------------- the tea table

def started():
    state = trail.start(0, T0)
    return state, trail.plan("seed", 12)


def test_a_started_trail_is_on_stage_two_and_a_riddle_state():
    state, _ = started()
    assert state["stage"] == engine.STAGE_RIDDLE and state["step"] == 2 and state["found"] == 0
    assert state["log"]["2"]["opened"] == T0


def test_looking_before_waking_the_dormouse_does_nothing():
    assert trail.look(None, 0, {"chain": [0, 1, 2]}, T0) == ("idle", None)


def test_the_chain_is_followed_in_order():
    state, p = started()
    for k, room in enumerate(p["chain"]):
        assert trail.look(state, room, p, T0 + k) == ("sister", k)
    assert state["found"] == 3 and state["step"] == 3 and state["log"]["2"]["solved"] == T0 + 2


def test_a_sister_out_of_order_is_just_a_tea_table():
    state, p = started()
    assert trail.look(state, p["chain"][1], p, T0)[0] == "wrong"
    assert state["found"] == 0


def test_a_found_sister_can_be_heard_again_without_penalty():
    state, p = started()
    trail.look(state, p["chain"][0], p, T0)
    for _ in range(10):
        assert trail.look(state, p["chain"][0], p, T0) == ("replay", 0)
    assert state["wrong_looks"] == 0 and state["found"] == 1


def test_three_wrong_tables_in_a_row_rest_the_tables():
    state, p = started()
    wrong = next(r for r in range(12) if r not in p["chain"])
    for _ in range(TRAIL_WRONG_LOOKS - 1):
        assert trail.look(state, wrong, p, T0) == ("wrong", None)
    action, until = trail.look(state, wrong, p, T0)
    assert action == "rest_start" and until == T0 + TRAIL_REST_MINUTES * 60
    assert trail.look(state, p["chain"][0], p, T0 + 60) == ("rest", until)  # even the right table is quiet
    assert state["found"] == 0


def test_the_rest_ends_and_the_count_starts_again():
    state, p = started()
    wrong = next(r for r in range(12) if r not in p["chain"])
    for _ in range(TRAIL_WRONG_LOOKS):
        trail.look(state, wrong, p, T0)
    later = T0 + TRAIL_REST_MINUTES * 60 + 1
    assert trail.look(state, wrong, p, later) == ("wrong", None) and state["wrong_looks"] == 1
    assert trail.look(state, p["chain"][0], p, later) == ("sister", 0)


def test_a_right_table_clears_the_wrong_streak():
    state, p = started()
    wrong = next(r for r in range(12) if r not in p["chain"])
    trail.look(state, wrong, p, T0)
    trail.look(state, wrong, p, T0)
    trail.look(state, p["chain"][0], p, T0)
    assert state["wrong_looks"] == 0
    assert trail.look(state, wrong, p, T0)[0] == "wrong"


def test_test_mode_shortens_the_rest():
    state, p = started()
    wrong = next(r for r in range(12) if r not in p["chain"])
    out = [trail.look(state, wrong, p, T0, test_mode=True) for _ in range(TRAIL_WRONG_LOOKS)]
    assert out[-1] == ("rest_start", T0 + TEST_DELAY_SECONDS)


def test_a_solved_or_other_stage_member_gets_the_idle_answer():
    state, p = started()
    state["stage"] = engine.STAGE_SOLVED
    assert trail.look(state, p["chain"][0], p, T0) == ("idle", None)


# ---------------------------------------------------------------- the words

def test_every_trail_text_formats_cleanly():
    p = {"numbers": [3, 4, 5], "tokens": ["a", "b", "c"], "chain": [0, 1, 2]}
    assert "{" not in trail.first_clue(WELL, "PTR")
    for k in range(3):
        line = trail.sister_line(WELL, k, p, "PTR")
        assert "{" not in line and str(p["numbers"][k]) in line and p["tokens"][k] in line
    assert "PTR" in trail.sister_line(WELL, 0, p, "PTR") and "PTR" in trail.sister_line(WELL, 1, p, "PTR")
    keys = dict(until="<t:1:R>", rank=" #1")
    for field in ("rest", "rest_start", "quiet", "idle", "already", "solved", "sisters_done_dm"):
        assert "{" not in WELL[field].format_map(keys), field
    assert all("{" not in t for t in WELL["wrong_look"])


def test_the_poem_never_shows_an_emoji_or_names_the_images_outright():
    body = " ".join(WELL["poem"]) + WELL["poem_title"] + WELL["nudge"]
    assert all(ord(ch) < 0x2000 for ch in body)  # no emoji characters at all
    for word in ("honey", "teapot", "sleep", "emoji", "react", "reaction"):
        assert word not in body.lower(), word


def test_the_pointer_keywords_are_not_substring_traps():
    # a room is matched by `keyword in lowercase title`; these short words sit inside many ordinary titles
    for risky in ("pet", "art", "car", "tea", "sea", "pic"):
        assert risky not in WELL["pointers"], risky
