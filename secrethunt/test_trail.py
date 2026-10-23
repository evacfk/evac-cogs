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
    from secrethunt.constants import TRAIL_MIN_ROOMS
    for uid in range(200):
        for n in (TRAIL_MIN_ROOMS, 12):
            p = trail.plan(trail.seed("s", "p", uid), n)
            assert len(set(p["chain"])) == 3 and all(0 <= r < n for r in p["chain"])
            assert len(set(p["courtesy_rooms"])) == 4 and all(0 <= r < n for r in p["courtesy_rooms"])


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


def test_test_mode_shortens_the_failed_test_rest():
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


# ---------------------------------------------------------------- stages 5 and 6, hints, funnel (pure)

def full_plan(uid=7, n=12, salt="s", pid="treacle_well"):
    return trail.plan(trail.seed(salt, pid, uid), n)


def at_stage(step, uid=7, now=1000.0):
    state = trail.start(0, now)
    state["step"] = step
    state["log"][str(step)] = {"opened": now, "solved": 0.0}
    return state


def test_the_plan_for_stages_five_and_six_is_well_formed_for_every_member():
    for uid in range(2000):
        p = full_plan(uid)
        assert len(set(p["courtesy_rooms"])) == 4 and sorted(p["courtesy_buttons"]) == sorted(trail.BUTTONS)
        assert sorted(p["tale"]) == list(range(6))
        words = p["tokens"] + p["extra_tokens"]
        assert len(set(words)) == 6
        assert p["answer"] == "".join(words[i][0] for i in p["tale"]) and len(p["answer"]) == 6


def test_the_new_draws_were_appended_so_earlier_choices_never_moved():
    for uid in range(300):
        p = full_plan(uid)
        rng = __import__("random").Random(trail.seed("s", "treacle_well", uid))
        assert p["chain"] == rng.sample(range(12), 3)
        assert p["numbers"] == [rng.randint(*trail.NUMBER_RANGE) for _ in range(3)]


def test_stage_five_in_order_passes_and_each_step_reports():
    p = full_plan()
    state = at_stage(5)
    for k in range(3):
        assert trail.courtesy(state, p["courtesy_rooms"][k], p["courtesy_buttons"][k], p, 1000.0) == ("ok", (k, p["courtesy_buttons"][k]))
    assert trail.courtesy(state, p["courtesy_rooms"][3], p["courtesy_buttons"][3], p, 1000.0) == ("passed", None)
    assert state["step"] == 6 and state["log"]["5"]["solved"] == 1000.0 and state["log"]["6"]["opened"] == 1000.0


def test_a_wrong_press_loses_progress_and_the_right_button_in_the_wrong_room_is_wrong_too():
    p = full_plan()
    state = at_stage(5)
    trail.courtesy(state, p["courtesy_rooms"][0], p["courtesy_buttons"][0], p, 1000.0)
    other = next(r for r in range(12) if r != p["courtesy_rooms"][1])
    assert trail.courtesy(state, other, p["courtesy_buttons"][1], p, 1000.0) == ("wrong", True)
    assert state["courtesy"] == 0
    assert trail.courtesy(state, p["courtesy_rooms"][0], "bow" if p["courtesy_buttons"][0] != "bow" else "look", p, 1000.0)[0] == "wrong"
    assert trail.courtesy(state, p["courtesy_rooms"][0], p["courtesy_buttons"][0], p, 1000.0)[0] == "ok"  # starts again fine


def test_five_slips_in_a_day_close_the_tables_for_two_hours_then_they_reopen():
    p = full_plan()
    state = at_stage(5)
    wrong_room = next(r for r in range(12) if r not in p["courtesy_rooms"] and r not in p["chain"])
    for i in range(4):
        assert trail.courtesy(state, wrong_room, "pour", p, 1000.0 + i)[0] == "wrong"
    action, until = trail.courtesy(state, wrong_room, "pour", p, 1005.0)
    assert action == "rest_start" and until == 1005.0 + 120 * 60
    assert trail.courtesy(state, p["courtesy_rooms"][0], p["courtesy_buttons"][0], p, 1006.0) == ("rest", until)
    assert trail.courtesy(state, p["courtesy_rooms"][0], p["courtesy_buttons"][0], p, until + 1)[0] == "ok"
    assert state["slips"] == 0


def test_slips_start_again_on_a_new_pacific_day():
    p = full_plan()
    state = at_stage(5)
    wrong_room = next(r for r in range(12) if r not in p["courtesy_rooms"] and r not in p["chain"])
    for i in range(4):
        trail.courtesy(state, wrong_room, "pour", p, 1000.0 + i)
    assert trail.courtesy(state, wrong_room, "pour", p, 1000.0 + 86400 * 2)[0] == "wrong"  # not the fifth: a new day


def test_looking_in_a_sisters_room_during_stage_five_is_free_unless_it_is_the_next_courtesy():
    p = full_plan()
    state = at_stage(5)
    sister_room = p["chain"][1]
    expected = (sister_room, "look") == (p["courtesy_rooms"][0], p["courtesy_buttons"][0])
    action, detail = trail.courtesy(state, sister_room, "look", p, 1000.0)
    assert (action == "ok") == expected
    if not expected:
        assert (action, detail) == ("sister", 1) and state["slips"] == 0 and state["courtesy"] == 0
    # any other button in that room is just a wrong press
    assert trail.courtesy(at_stage(5), sister_room, "pour", p, 1000.0)[0] in ("wrong", "ok")


def test_stage_five_does_nothing_for_members_on_other_stages():
    p = full_plan()
    for step in (2, 3, 4, 6):
        assert trail.courtesy(at_stage(step), 0, "look", p, 1000.0) == ("idle", None)
    assert trail.courtesy(None, 0, "look", p, 1000.0) == ("idle", None)


def test_test_mode_shortens_the_courtesy_rest():
    p = full_plan()
    state = at_stage(5)
    wrong_room = next(r for r in range(12) if r not in p["courtesy_rooms"] and r not in p["chain"])
    for i in range(5):
        out = trail.courtesy(state, wrong_room, "pour", p, 1000.0 + i, True)
    assert out == ("rest_start", 1004.0 + 30)


def test_the_answer_is_the_first_letters_in_tale_order_and_forgiving_about_case_and_spaces():
    p = full_plan()
    for typed in (p["answer"], p["answer"].upper(), " ".join(p["answer"]), p["answer"][:3] + "-" + p["answer"][3:]):
        state = at_stage(6)
        assert trail.answer(state, typed, p, 2000.0) == ("solved", None)
        assert state["stage"] == engine.STAGE_SOLVED and state["solved_ts"] == 2000.0 and state["log"]["6"]["solved"] == 2000.0


def test_a_wrong_six_letter_answer_costs_an_hour_but_other_messages_cost_nothing():
    p = full_plan()
    state = at_stage(6)
    for text in ("hello", "dormouse", "seven77", "12345 6", "", "what is it"):
        assert trail.answer(state, text, p, 2000.0) == ("ignore", None), text
    assert state["answer_wrongs"] == 0
    wrong = "zzzzzz" if p["answer"] != "zzzzzz" else "yyyyyy"
    action, until = trail.answer(state, wrong, p, 2000.0)
    assert action == "wrong" and until == 2000.0 + 3600 and state["answer_wrongs"] == 1
    assert trail.answer(state, p["answer"], p, 2100.0) == ("rest", until)  # even the right one waits out the rest
    assert trail.answer(state, p["answer"], p, until + 1)[0] == "solved"


def test_answers_only_count_on_stage_six():
    p = full_plan()
    for step in (2, 3, 4, 5):
        assert trail.answer(at_stage(step), p["answer"], p, 2000.0) == ("ignore", None)


def test_hints_come_24_hours_after_the_stage_opens_then_12_hours_apart_and_stop_at_two():
    state = at_stage(3, now=1000.0)
    assert trail.hint(state, 1000.0 + 86400 - 1) == ("early", 1000.0 + 86400)
    assert trail.hint(state, 1000.0 + 86400) == ("ok", 1)
    assert trail.hint(state, 1000.0 + 86400 + 12 * 3600 - 1)[0] == "early"
    assert trail.hint(state, 1000.0 + 86400 + 12 * 3600) == ("ok", 2)
    assert trail.hint(state, 1000.0 + 10 * 86400) == ("max", None)
    assert state["hints"] == 2


def test_each_stage_has_its_own_hints_and_the_total_keeps_counting():
    state = at_stage(3, now=0.0)
    trail.hint(state, 86400.0)
    trail.hint(state, 86400.0 + 12 * 3600)
    state["step"] = 4
    state["log"]["4"] = {"opened": 200000.0, "solved": 0.0}
    assert trail.hint(state, 200000.0 + 100)[0] == "early"  # the new stage's clock starts at its own opening
    assert trail.hint(state, 200000.0 + 86400)[0] == "ok" and state["hints"] == 3


def test_test_mode_shrinks_hint_waits_to_thirty_seconds():
    state = at_stage(2, now=100.0)
    assert trail.hint(state, 110.0, True) == ("early", 130.0)
    assert trail.hint(state, 130.0, True) == ("ok", 1)
    assert trail.hint(state, 150.0, True)[0] == "early" and trail.hint(state, 160.0, True) == ("ok", 2)


def test_no_hint_without_a_trail_or_after_solving():
    assert trail.hint(None, 1.0) == ("idle", None)
    state = at_stage(6)
    state["stage"] = engine.STAGE_SOLVED
    assert trail.hint(state, 1e9) == ("idle", None)


ROOMS = [(f"room {c}", f"<#room-{c}>") for c in "abcdefghijkl"]


def test_every_hint_text_is_true_for_every_stage_over_many_members():
    for uid in range(400):
        p = full_plan(uid)
        state = at_stage(2)
        state["found"] = uid % 3
        name = ROOMS[p["chain"][state["found"]]][0]
        h1, h2 = (trail.hint_text(2, n, p, state, ROOMS) for n in (1, 2))
        assert f"letter {name[0].upper()}" in h1 and "2 words" in h2
        rule = tuple(p["rule"])
        words1, words2 = (trail.hint_text(3, n, p, state, ROOMS) for n in (1, 2))
        said = re.findall(r"\*\*(\w+)\*\*", words1 + words2)
        assert trail.holds(rule, said[0]) and trail.holds(rule, said[1]) and not trail.holds(rule, said[2])
        assert ("odd" in trail.hint_text(4, 2, p, state, ROOMS)) == bool(p["pour_total"] % 2)
        assert ROOMS[p["courtesy_rooms"][0]][1] in trail.hint_text(5, 1, p, state, ROOMS)
        order = trail.hint_text(5, 2, p, state, ROOMS)
        assert [order.index(ROOMS[r][1]) for r in p["courtesy_rooms"]] == sorted(order.index(ROOMS[r][1]) for r in p["courtesy_rooms"])
        assert trail.SOURCE_LABELS[p["tale"][0]] in trail.hint_text(6, 2, p, state, ROOMS)


def test_no_hint_text_gives_an_answer_outright():
    for uid in range(200):
        p = full_plan(uid)
        state = at_stage(2)
        for step in range(2, 7):
            for n in (1, 2):
                text = trail.hint_text(step, n, p, state, ROOMS)
                assert p["answer"] not in text.lower() and str(p["pour_total"]) not in text
                assert not any(w in text.lower() for w in p["tokens"] + p["extra_tokens"])


def test_took_label_reads_naturally():
    day = 86400.0
    assert trail.took_label(0, day * 0.5) == "solved in under a day"
    assert trail.took_label(0, day) == "solved in 1 day"
    assert trail.took_label(0, day * 2.6) == "solved in 3 days"


def test_the_funnel_counts_members_per_stage_with_medians_and_hints():
    a, b, c = at_stage(3, now=1000.0), at_stage(6, now=1000.0), at_stage(6, now=1000.0)
    a["log"]["2"] = {"opened": 1000.0, "solved": 1600.0}
    b["log"]["2"] = {"opened": 1000.0, "solved": 2800.0}
    c["log"]["2"] = {"opened": 1000.0, "solved": 4600.0}
    c["stage"], c["hints"] = engine.STAGE_SOLVED, 2
    out = trail.funnel([a, b, c])
    assert out["at"] == {2: 0, 3: 1, 4: 0, 5: 0, 6: 1} and out["solved"] == 1 and out["hints"] == 2
    assert out["median_minutes"][2] == 30
    assert trail.funnel([]) == {"at": {2: 0, 3: 0, 4: 0, 5: 0, 6: 0}, "solved": 0, "median_minutes": {}, "hints": 0}


def test_a_synthetic_solver_finishes_the_whole_trail_for_a_few_hundred_members():
    """Drive the pure state machine with a player who knows everything, for 300 members, and check each ends solved with
    the answer built only from the words the stages hand out."""
    for uid in range(300):
        p = full_plan(uid)
        state = trail.start(0, 0.0)
        now = 100.0
        for k, room in enumerate(p["chain"]):
            assert trail.look(state, room, p, now)[0] == "sister"
        assert state["step"] == 3
        _w, yes = trail.make_test(p["seed"], 0, tuple(p["rule"]))
        trail.muchness(state, p, "ready", now)
        assert trail.muchness(state, p, ", ".join(yes) or "none", now)[0] == "passed" and state["step"] == 4
        for _ in range(p["pour_total"]):
            assert trail.pour(state, p["pour_room"], p, now)[0] == "poured"
        assert trail.toast(state, p["toast_room"], p, now)[0] == "passed" and state["step"] == 5
        for k in range(4):
            trail.courtesy(state, p["courtesy_rooms"][k], p["courtesy_buttons"][k], p, now)
        assert state["step"] == 6
        heard = p["tokens"] + p["extra_tokens"]  # the six words the member was told, by source
        typed = "".join(heard[i][0] for i in p["tale"])
        assert trail.answer(state, typed, p, now) == ("solved", None) and state["stage"] == engine.STAGE_SOLVED
        assert all(state["log"][str(s)]["solved"] for s in range(2, 7))


def test_every_stage_message_fits_discords_2000_character_limit_even_with_letter_clue_rooms():
    from secrethunt.puzzles import PUZZLES
    well = PUZZLES["treacle_well"]
    long_names = [f"{'extremely long room title ' * 3}{i}" for i in range(12)]  # no keyword: the letter clue is used
    for names in (long_names, ["Gaming"] * 12):
        for uid in range(300):
            p = trail.plan(trail.seed("s", "treacle_well", uid), 12)
            verses = []
            for k in range(4):
                pointer = trail.room_pointer(well, names[p["courtesy_rooms"][k]], 5 + k)
                verses.append(f"**{well['verse_label'][k]}**\n{well['verses'][p['courtesy_buttons'][k]]}\n"
                              + well["verse_place"].format(pointer=pointer))
            texts = [well["stage4_done"].format(token="marmalade", verses="\n\n".join(verses)),
                     well["stage5_done"].format(token="marmalade", tale=trail.tale_text(p)),
                     well["stage3_done"].format(token="marmalade", pour_pointer=trail.room_pointer(well, names[p["pour_room"]], 3),
                                                toast_pointer=trail.room_pointer(well, names[p["toast_room"]], 4))]
            assert all(len(t) < 2000 for t in texts)
