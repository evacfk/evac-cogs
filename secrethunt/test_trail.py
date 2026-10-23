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
    for field in ("rest", "rest_start", "quiet", "idle", "already", "solved", "probe_tired", "test_malformed", "pour_wrong",
                  "pour_full", "toast_wrong", "toast_empty", "toast_miscount"):
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


# ---------------------------------------------------------------- the plan never changes under a member

def test_the_first_draws_of_the_plan_are_pinned_so_later_stages_never_move_a_members_rooms():
    golden = {
        1: ([8, 2, 1], [13, 2, 6], ["biscuit", "spoon", "pocket"]),
        2: ([7, 11, 5], [5, 8, 5], ["spoon", "violet", "candle"]),
        99: ([7, 4, 1], [8, 3, 3], ["sugar", "violet", "candle"]),
        12345: ([0, 11, 7], [9, 11, 12], ["pocket", "biscuit", "clover"]),
    }
    for uid, (chain, numbers, tokens) in golden.items():
        p = trail.plan(trail.seed("fixed-test-salt", "treacle_well", uid), 12)
        assert (p["chain"], p["numbers"], p["tokens"]) == (chain, numbers, tokens), uid


def test_the_later_stage_plan_is_valid_for_every_seed():
    for uid in range(2000):
        p = trail.plan(trail.seed("salt", "treacle_well", uid), 12)
        assert p["rule"] in trail.RULES and p["pour_room"] != p["toast_room"]
        assert 0 <= p["pour_room"] < 12 and 0 <= p["toast_room"] < 12
        assert len(set(p["extra_tokens"])) == 3 and not set(p["extra_tokens"]) & set(p["tokens"])
        assert p["pour_total"] == sum(p["numbers"]) and p["seed"] == trail.seed("salt", "treacle_well", uid)


# ---------------------------------------------------------------- stage 3: the Muchness

ORACLE = {  # an independent statement of each rule (regex or plain python), so `holds` is not graded by itself
    "has": lambda w, c: c in w, "lacks": lambda w, c: c not in w, "starts": lambda w, c: bool(re.match(c, w)),
    "ends": lambda w, c: bool(re.search(c + "$", w)),
}


def oracle(rule, word):
    import re as _re
    kind, args = rule[0], rule[1:]
    if kind in ORACLE:
        return ORACLE[kind](word, *args)
    return {
        "len_even": lambda: len(word) % 2 == 0, "len_odd": lambda: len(word) % 2 == 1,
        "len_ge": lambda: len(word) >= args[0], "len_le": lambda: len(word) <= args[0],
        "double": lambda: bool(_re.search(r"(.)\1", word)), "vowels_ge": lambda: len(_re.findall("[aeiou]", word)) >= args[0],
        "vowels_le": lambda: len(_re.findall("[aeiou]", word)) <= args[0],
        "pair_vowels": lambda: bool(_re.search("[aeiou][aeiou]", word)),
        "starts_vowel": lambda: bool(_re.match("[aeiou]", word)), "ends_vowel": lambda: bool(_re.search("[aeiou]$", word)),
        "starts_consonant_ends_vowel": lambda: bool(_re.match("[^aeiou]", word)) and bool(_re.search("[aeiou]$", word)),
        "first_half": lambda: word[0] in "abcdefghijklm", "last_after_first": lambda: ord(word[-1]) > ord(word[0]),
        "both": lambda: args[0] in word and args[1] in word,
    }[kind]()


import re  # noqa: E402  (used by the oracle above)


@pytest.mark.parametrize("rule", trail.RULES)
def test_every_rule_agrees_with_an_independent_check_and_has_enough_words_both_ways(rule):
    for word in trail.WORDS:
        assert trail.holds(rule, word) == oracle(rule, word), (rule, word)
    assert trail.viable(rule), rule
    assert trail.rule_text(rule)


def test_the_rule_library_is_unique_and_the_word_bank_is_clean():
    assert len(set(trail.RULES)) == len(trail.RULES) and len(trail.RULES) >= 40
    assert all(w.isalpha() and w == w.lower() and 2 <= len(w) <= 9 for w in trail.WORDS)
    assert len(set(trail.WORDS)) == len(trail.WORDS) and len(trail.WORDS) >= 250


def test_the_rules_are_pinned_by_position():
    assert trail.RULES[0] == ("has", "e") and trail.RULES[9] == ("starts", "s") and trail.RULES[-1] == ("both", "s", "e")


def test_a_test_has_eight_distinct_words_three_to_five_of_them_drawable():
    for rule in trail.RULES:
        for roll in range(40):
            words, yes = trail.make_test(f"seed{roll}", roll, rule)
            assert len(words) == 8 == len(set(words)) and 3 <= len(yes) <= 5
            assert yes == sorted(w for w in words if oracle(rule, w))


def test_tests_are_deterministic_and_a_new_roll_gives_new_words():
    rule = ("has", "e")
    assert trail.make_test("s", 0, rule) == trail.make_test("s", 0, rule)
    assert trail.make_test("s", 0, rule)[0] != trail.make_test("s", 1, rule)[0]
    assert trail.make_test("s", 0, rule)[0] != trail.make_test("t", 0, rule)[0]


def on_stage3(rule=("has", "e")):
    state, p = started()
    for k, room in enumerate(p["chain"]):
        trail.look(state, room, p, T0)
    p = {**p, "rule": rule}
    return state, p


def test_the_third_sister_opens_stage_three():
    state, _ = on_stage3()
    assert state["step"] == 3 and state["log"]["3"]["opened"] == T0


def test_a_word_gets_a_true_verdict():
    state, p = on_stage3(("has", "e"))
    assert trail.muchness(state, p, "apple", T0) == ("probe", ("apple", True))
    assert trail.muchness(state, p, "  Cloud!  ", T0) == ("probe", ("cloud", False))
    assert state["probes"] == 2


def test_only_stage_three_members_are_answered():
    state, p = started()
    assert trail.muchness(state, p, "apple", T0) == ("not_here", None)
    assert trail.muchness(None, p, "apple", T0) == ("not_here", None)
    state, p = on_stage3()
    state["step"] = 4
    assert trail.muchness(state, p, "apple", T0) == ("not_here", None)


def test_ready_opens_one_test_and_asking_again_shows_the_same_one():
    state, p = on_stage3()
    action, words = trail.muchness(state, p, "ready", T0)
    assert action == "ready" and len(words) == 8 and state["test_words"] == words
    assert trail.muchness(state, p, "Ready.", T0 + 5) == ("test_open", words)


def test_a_word_in_the_open_test_is_not_judged():
    state, p = on_stage3()
    _a, words = trail.muchness(state, p, "ready", T0)
    assert trail.muchness(state, p, words[0], T0) == ("saving", words[0])
    outside = next(w for w in trail.WORDS if w not in words)
    assert trail.muchness(state, p, outside, T0)[0] == "probe"


def test_the_right_answer_passes_in_any_order_with_any_punctuation():
    state, p = on_stage3(("has", "e"))
    _a, words = trail.muchness(state, p, "ready", T0)
    _w, yes = trail.make_test(p["seed"], 0, ("has", "e"))
    answer = ", ".join(reversed(yes)).upper() + "!"
    assert trail.muchness(state, p, answer, T0 + 9) == ("passed", None)
    assert state["step"] == 4 and state["test_words"] == [] and state["log"]["3"]["solved"] == T0 + 9
    assert state["log"]["4"]["opened"] == T0 + 9


def test_a_wrong_answer_rests_then_gives_fresh_words():
    state, p = on_stage3(("has", "e"))
    _a, first = trail.muchness(state, p, "ready", T0)
    _w, yes = trail.make_test(p["seed"], 0, ("has", "e"))
    wrong = " ".join(yes[:-1])
    action, until = trail.muchness(state, p, wrong, T0)
    assert action == "failed" and until == T0 + 15 * 60 and state["test_roll"] == 1 and state["test_words"] == []
    assert trail.muchness(state, p, "ready", T0 + 60) == ("resting", until)
    action, second = trail.muchness(state, p, "ready", until + 1)
    assert action == "ready" and second != first


def test_extra_words_or_none_fail_and_a_message_off_the_list_costs_nothing():
    state, p = on_stage3(("has", "e"))
    _a, words = trail.muchness(state, p, "ready", T0)
    assert trail.muchness(state, p, "none", T0)[0] == "failed"
    trail.muchness(state, p, "ready", T0 + 15 * 60 + 1)  # a fresh test
    _a, words = state["test_words"], None
    assert trail.muchness(state, p, "hello there friend", T0 + 15 * 60 + 2) == ("malformed", None)
    assert state["test_words"] and state["test_roll"] == 1  # nothing was spent


def test_a_single_word_is_always_a_question_never_an_answer():
    state, p = on_stage3()
    trail.muchness(state, p, "ready", T0)
    _w, yes = trail.make_test(p["seed"], 0, ("has", "e"))
    assert trail.muchness(state, p, yes[0], T0)[0] == "saving"
    assert state["test_roll"] == 0  # not graded


def test_questions_are_capped_per_day_and_reset_the_next_day():
    state, p = on_stage3()
    words = list(trail.WORDS)
    for i in range(trail.TRAIL_PROBES_PER_DAY):
        assert trail.muchness(state, p, words[i % len(words)], T0)[0] == "probe"
    assert trail.muchness(state, p, "apple", T0) == ("tired", None)
    assert trail.muchness(state, p, "apple", T0 + 86400 + 1)[0] == "probe"


def test_nonsense_is_ignored():
    state, p = on_stage3()
    for text in ("", "!!!", "x", "a" * 40, "two words"):
        assert trail.muchness(state, p, text, T0)[0] == "ignore", text


def test_test_mode_shortens_the_rest():
    state, p = on_stage3(("has", "e"))
    trail.muchness(state, p, "ready", T0)
    assert trail.muchness(state, p, "none", T0, test_mode=True) == ("failed", T0 + TEST_DELAY_SECONDS)


def test_every_rule_can_be_found_by_asking_about_the_bank():
    """Asking about enough words always singles out the rule among the library (so a player can win by experiment)."""
    for rule in trail.RULES:
        verdicts = tuple(trail.holds(rule, w) for w in trail.WORDS)
        twins = [r for r in trail.RULES if tuple(trail.holds(r, w) for w in trail.WORDS) == verdicts]
        assert twins == [rule], (rule, twins)


# ---------------------------------------------------------------- stage 4: the Long Pour

def on_stage4():
    state, p = on_stage3()
    state["step"] = 4
    p = {**p, "pour_room": 3, "toast_room": 5, "pour_total": 7}
    return state, p


def test_pouring_counts_only_in_the_pour_room_and_only_on_stage_four():
    state, p = on_stage3()
    assert trail.pour(state, 3, {**p, "pour_room": 3}, T0) == ("idle", None)
    state, p = on_stage4()
    assert trail.pour(state, 4, p, T0) == ("wrong", None) and state["pours"] == 0
    assert [trail.pour(state, 3, p, T0 + i)[1] for i in range(3)] == [1, 2, 3]


def test_the_exact_count_passes_the_toast():
    state, p = on_stage4()
    for _ in range(7):
        trail.pour(state, 3, p, T0)
    assert trail.toast(state, 5, p, T0 + 5) == ("passed", None)
    assert state["step"] == 5 and state["pours"] == 0 and state["log"]["4"]["solved"] == T0 + 5


def test_toasting_in_the_wrong_room_or_with_an_empty_cup_does_nothing():
    state, p = on_stage4()
    assert trail.toast(state, 5, p, T0) == ("empty", None)
    trail.pour(state, 3, p, T0)
    assert trail.toast(state, 4, p, T0) == ("wrong", None) and state["pours"] == 1  # nothing spent


def test_a_wrong_count_empties_the_cup_and_three_in_a_row_rest_the_cups():
    state, p = on_stage4()
    for attempt in range(trail.TRAIL_POUR_WRONGS):
        for _ in range(6):
            trail.pour(state, 3, p, T0)
        action, detail = trail.toast(state, 5, p, T0)
        assert state["pours"] == 0
    assert action == "miscount_rest" and detail == T0 + 60 * 60
    assert trail.pour(state, 3, p, T0 + 60)[0] == "rest" and trail.toast(state, 5, p, T0 + 60)[0] == "rest"
    assert trail.pour(state, 3, p, detail + 1) == ("poured", 1)  # the rest is over and the count starts clean


def test_an_overshoot_is_a_miscount_and_the_cup_cannot_hold_more_than_the_cap():
    state, p = on_stage4()
    for _ in range(8):
        trail.pour(state, 3, p, T0)
    assert trail.toast(state, 5, p, T0) == ("miscount", 8)
    for _ in range(trail.TRAIL_POUR_CAP):
        trail.pour(state, 3, p, T0)
    assert trail.pour(state, 3, p, T0) == ("full", trail.TRAIL_POUR_CAP)


def test_test_mode_shortens_the_cup_washing():
    state, p = on_stage4()
    out = None
    for _ in range(trail.TRAIL_POUR_WRONGS):
        trail.pour(state, 3, p, T0)
        out = trail.toast(state, 5, p, T0, test_mode=True)
    assert out == ("miscount_rest", T0 + TEST_DELAY_SECONDS)


def test_looking_after_stage_two_costs_nothing():
    state, p = on_stage3()
    wrong = next(r for r in range(12) if r not in p["chain"])
    for _ in range(10):
        assert trail.look(state, wrong, p, T0) == ("wrong", None)
    assert state["wrong_looks"] == 0 and state["rest_until"] == 0.0
    assert trail.look(state, p["chain"][0], p, T0) == ("replay", 0)
