"""Pure-logic tests for secrethunt: no discord, no redbot."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from secrethunt import engine
from secrethunt.constants import (
    MAX_DM_FAILS, MAX_WRONG_PER_DAY, MAX_WRONG_REACTS, NO_ROLE_TIERS, REACT_LOCK_HOURS, POOL_MAX, TIER_ORDER, TIER_ROLE_NAMES,
)
from secrethunt.puzzles import PUZZLES

LA = ZoneInfo("America/Los_Angeles")


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LA).timestamp()


def local(t):
    return datetime.fromtimestamp(t, LA)


# ---------------------------------------------------------------- puzzle data


ACROSTIC = [pid for pid, p in PUZZLES.items() if p.get("trigger_kind", "acrostic") == "acrostic"]
RIDDLE = [pid for pid, p in PUZZLES.items() if p.get("trigger_kind") == "riddle"]
FIXED = [pid for pid, p in PUZZLES.items() if p.get("kind") not in ("trial", "trail")]   # puzzles with a fixed answer list (a trial or a trail generates its own)


@pytest.mark.parametrize("pid", ACROSTIC)
def test_every_acrostic_poem_spells_its_trigger_word(pid):
    p = PUZZLES[pid]
    assert engine.acrostic(p["poem"]) == engine.normalize(p["trigger"])


@pytest.mark.parametrize("pid", RIDDLE)
def test_a_riddle_poem_hides_no_acrostic_and_never_says_its_own_answer(pid):
    p = PUZZLES[pid]
    word = engine.normalize(p["trigger"])
    assert engine.acrostic(p["poem"]) != word  # members already know to look for first letters
    assert word not in engine.normalize(" ".join(p["poem"]) + p["poem_title"])  # it is a riddle, not a giveaway
    assert word not in engine.acrostic(p["poem"]) and word not in "".join(
        engine.normalize(line)[-1:] for line in p["poem"])  # nor a last-letter trick


@pytest.mark.parametrize("pid", FIXED)
def test_answers_cover_the_largest_pool_and_stay_out_of_the_public_poem(pid):
    p = PUZZLES[pid]
    answers = [engine.normalize(a) for a in p["answers"]]
    assert len(answers) >= POOL_MAX and len(set(answers)) == len(answers)
    poem = engine.normalize(" ".join(p["poem"]))
    assert not any(a in poem for a in answers)  # nobody can read an answer off the public poem


@pytest.mark.parametrize("pid", FIXED)
def test_text_templates_format_cleanly(pid):
    p = PUZZLES[pid]
    keys = dict(hint="x", shape="1", pointer="p", object_hint="o", channel="#c", anagram="x", rebus="r", rank=" #1")
    for field in ("riddle", "riddle_no_hint", "final", "solved"):
        p[field].format_map(keys)


@pytest.mark.parametrize("pid", list(PUZZLES))
def test_every_puzzle_has_a_known_tier_and_easy_promises_no_role(pid):
    p = PUZZLES[pid]
    assert p["tier"] in TIER_ORDER
    if p["tier"] == "easy":
        assert "role" not in p["solved"].lower() and "badge" not in p["solved"].lower()


def test_easy_and_medium_never_have_a_role_and_the_ladder_starts_at_easy():
    assert TIER_ORDER[0] == "easy" and "easy" not in TIER_ROLE_NAMES and "medium" not in TIER_ROLE_NAMES
    assert set(NO_ROLE_TIERS) == {"easy", "medium"}
    assert set(TIER_ROLE_NAMES) == set(TIER_ORDER) - set(NO_ROLE_TIERS)


# ---------------------------------------------------------------- normalising and scrambling


def test_normalize_ignores_case_and_punctuation():
    assert engine.normalize("  Hat-ter! ") == "hatter"
    assert engine.normalize(None) == ""


def test_normalize_emoji_drops_the_variation_selector():
    assert engine.normalize_emoji("\N{MANTELPIECE CLOCK}️") == "\N{MANTELPIECE CLOCK}"


def test_scramble_is_a_deterministic_real_anagram_never_the_word():
    for word in ("hatter", "dormouse", "flamingo", "jabberwock", "walrus"):
        s = engine.scramble(word, "seed")
        assert s != word and sorted(s) == sorted(word)
        assert s == engine.scramble(word, "seed")
    assert engine.scramble("aaa", "seed") == "aaa"  # nothing to shuffle


def test_title_hint_scrambles_a_single_word_title_and_reports_its_length():
    hint, shape = engine.title_hint("Gaming", "seed")
    assert hint != "gaming" and sorted(hint) == sorted("gaming") and shape == "6"
    assert engine.title_hint("Gaming", "seed") == (hint, shape)  # deterministic: the riddle can be re-sent


def test_title_hint_scrambles_each_word_separately_and_keeps_tiny_words():
    hint, shape = engine.title_hint("Best pizza in the Bay?", "seed")
    words = hint.split(" ")
    assert shape == "4 + 5 + 2 + 3 + 3" and len(words) == 5
    assert words[2] == "in"  # two letters: nothing to shuffle
    for scrambled, original in zip(words, ["best", "pizza", "in", "the", "bay"]):
        assert sorted(scrambled) == sorted(original)


def test_title_hint_uses_letters_only_and_skips_tiny_titles():
    assert engine.title_hint("wonder-land_chat \U0001f4ac", "s")[1] == "6 + 4 + 4"
    assert engine.title_hint("Caf\u00e9", "s")[1] == "4"
    assert engine.title_hint("ab", "s") is None
    assert engine.title_hint("\U0001f4ac\u2502", "s") is None
    assert engine.title_hint(None, "s") is None


def test_escape_markdown_stops_names_restyling_the_hall():
    assert engine.escape_markdown("a_b*c~d|e`f>g\\h") == "a\\_b\\*c\\~d\\|e\\`f\\>g\\\\h"
    assert engine.escape_markdown("plain name") == "plain name" and engine.escape_markdown(None) == ""


def test_hall_text_numbers_solvers_by_their_original_finishing_position():
    assert engine.hall_text([(1, "ann"), (2, "bo"), (4, "cy")]) == ("1. ann\n2. bo\n4. cy", 0)  # #3 left: a gap, no renumbering
    assert engine.hall_text([]) == ("", 0)


def test_hall_text_puts_an_optional_note_after_the_name():
    entries = [(1, "ann", "no hints"), (2, "bo", "2 hints"), (4, "cy")]
    assert engine.hall_text(entries) == ("1. ann \N{MIDDLE DOT} no hints\n2. bo \N{MIDDLE DOT} 2 hints\n4. cy", 0)
    assert engine.hall_text([(1, "ann", "")]) == ("1. ann", 0)  # an empty note adds nothing


def test_hall_text_cuts_off_at_the_budget_and_says_how_many_are_hidden():
    entries = [(i, "x" * 10) for i in range(1, 101)]
    text, hidden = engine.hall_text(entries, budget=200)
    assert len(text) <= 200 and hidden == 100 - len(text.split("\n")) and hidden > 0
    assert engine.hall_text(entries)[1] == 0 and len(engine.hall_text(entries)[0]) < 4096


# ---------------------------------------------------------------- variants


def test_variant_is_stable_in_range_and_depends_on_the_salt():
    assert engine.variant_for("salt", "p", 42, 5) == engine.variant_for("salt", "p", 42, 5)
    assert all(0 <= engine.variant_for("salt", "p", u, 5) < 5 for u in range(500))
    mine = [engine.variant_for("salt-a", "p", u, 5) for u in range(500)]
    other = [engine.variant_for("salt-b", "p", u, 5) for u in range(500)]
    assert mine != other  # nobody can compute a variant without the salt


def test_variants_are_spread_roughly_evenly():
    counts = [0] * 5
    for u in range(5000):
        counts[engine.variant_for("s", "p", 10_000 + u, 5)] += 1
    assert all(800 <= c <= 1200 for c in counts), counts


def test_the_puzzle_id_reshuffles_who_gets_which_variant():
    a = [engine.variant_for("s", "one", u, 5) for u in range(300)]
    b = [engine.variant_for("s", "two", u, 5) for u in range(300)]
    assert a != b


# ---------------------------------------------------------------- triggers and objects


def test_match_trigger_only_matches_published_puzzles():
    assert engine.match_trigger(PUZZLES, ["late_rabbit"], "TICK") == "late_rabbit"
    assert engine.match_trigger(PUZZLES, [], "tick") is None
    assert engine.match_trigger(PUZZLES, ["late_rabbit"], "tock") is None
    assert engine.match_trigger(PUZZLES, ["late_rabbit"], "") is None
    assert engine.match_trigger(PUZZLES, ["gone"], "tick") is None


def test_find_object_by_message_id():
    objects = {"late_rabbit": {"101": 5, "102": 6}}
    assert engine.find_object(objects, 6) == ("late_rabbit", 102)
    assert engine.find_object(objects, 7) is None


# ---------------------------------------------------------------- the daily delay


def test_next_unlock_is_the_next_nine_am_at_least_four_hours_on():
    assert local(engine.next_unlock(ts(2026, 10, 13, 16, 6))) == datetime(2026, 10, 14, 9, 0, tzinfo=LA)
    # found at 11pm: 4h later is 3am, so the same morning's 9:00 is next
    assert local(engine.next_unlock(ts(2026, 10, 13, 23, 0))) == datetime(2026, 10, 14, 9, 0, tzinfo=LA)
    # found at 8am: 4h later is noon, 9:00 has passed, so wait for tomorrow
    assert local(engine.next_unlock(ts(2026, 10, 13, 8, 0))) == datetime(2026, 10, 14, 9, 0, tzinfo=LA)
    # found at 5am: exactly 4h later is 9:00, which counts
    assert local(engine.next_unlock(ts(2026, 10, 13, 5, 0))) == datetime(2026, 10, 13, 9, 0, tzinfo=LA)


def test_next_unlock_stays_at_nine_across_a_dst_change():
    due = local(engine.next_unlock(ts(2026, 10, 31, 21, 0)))  # clocks go back on Nov 1
    assert (due.month, due.day, due.hour) == (11, 1, 9)


def test_test_mode_skips_the_wait():
    assert engine.next_unlock(1000.0, test_mode=True) == 1030.0


# ---------------------------------------------------------------- the chain


def started(variant=2, now=1000.0):
    state, action = engine.trigger(None, variant, now)
    assert action == "start"
    return state


def test_trigger_replays_the_current_step_at_every_stage():
    st = started()
    assert engine.trigger(st, 0, 1.0)[1] == "riddle_again"
    st["stage"] = engine.STAGE_FOUND
    assert engine.trigger(st, 0, 1.0)[1] == "waiting"
    st["stage"] = engine.STAGE_FINAL
    assert engine.trigger(st, 0, 1.0)[1] == "final_again"
    st["stage"] = engine.STAGE_SOLVED
    assert engine.trigger(st, 0, 1.0)[1] == "done"


def test_trigger_never_changes_an_existing_variant():
    st = started(variant=3)
    st2, _ = engine.trigger(st, 0, 5.0)  # a different variant offered later (e.g. pool resized) is ignored
    assert st2["variant"] == 3


def test_reacting_to_your_own_object_advances_and_schedules_the_next_clue():
    st = started()
    assert engine.react(st, True, 5000.0) == "found"
    assert st["stage"] == engine.STAGE_FOUND and st["due_ts"] > 5000.0 and st["found_ts"] == 5000.0
    assert engine.react(st, True, 5001.0) == "ignore"  # already past that step


def test_two_wrong_objects_lock_the_member_out_for_a_day():
    st = started()
    assert MAX_WRONG_REACTS == 2
    assert engine.react(st, False, 1000.0) == "decoy"
    assert engine.react(st, False, 1001.0) == "lockout"
    assert st["locked_until"] == 1001.0 + REACT_LOCK_HOURS * 3600 and st["stage"] == engine.STAGE_RIDDLE
    # while locked, even their own object does nothing (and nothing counts further)
    assert engine.react(st, True, 1002.0) == "locked" and st["stage"] == engine.STAGE_RIDDLE
    assert engine.react(st, False, 1003.0) == "locked" and st["decoys"] == 2


def test_the_lock_ends_and_they_get_two_fresh_chances_on_the_same_riddle():
    st = started()
    engine.react(st, False, 1000.0)
    engine.react(st, False, 1001.0)
    after = st["locked_until"] + 1
    assert engine.react(st, False, after) == "decoy" and st["decoys"] == 1 and st["locked_until"] == 0.0
    assert engine.react(st, True, after + 1) == "found"


def test_a_correct_first_reaction_after_one_mistake_still_works():
    st = started()
    assert engine.react(st, False, 1.0) == "decoy"
    assert engine.react(st, True, 2.0) == "found"


def test_lockout_uses_the_short_wait_in_test_mode():
    st = started()
    engine.react(st, False, 100.0, test_mode=True)
    assert engine.react(st, False, 101.0, test_mode=True) == "lockout" and st["locked_until"] == 131.0


def test_states_saved_before_the_lock_existed_still_work():
    st = started()
    del st["locked_until"]
    assert engine.react(st, True, 5.0) == "found"


def test_react_without_a_started_chain_does_nothing():
    assert engine.react(None, True, 1.0) == "ignore"


def test_clue_is_due_only_after_the_unlock_time():
    st = started()
    engine.react(st, True, ts(2026, 10, 13, 16, 0))
    assert not engine.is_due(st, ts(2026, 10, 14, 8, 59))
    assert engine.is_due(st, ts(2026, 10, 14, 9, 0))
    engine.mark_final_sent(st, ts(2026, 10, 14, 9, 0))
    assert not engine.is_due(st, ts(2026, 10, 14, 9, 5))  # sent once, never again


def test_a_failed_dm_is_retried_then_given_up_and_revived_by_retriggering():
    st = started()
    engine.react(st, True, 1000.0, test_mode=True)
    engine.mark_final_sent(st, 2000.0)
    engine.dm_failed(st, 2000.0)
    assert st["stage"] == engine.STAGE_FOUND and st["due_ts"] == 2000.0 + 3600
    st["dm_fails"] = MAX_DM_FAILS - 1
    engine.dm_failed(st, 5000.0)
    assert not engine.is_due(st, 10 ** 12)
    _, action = engine.trigger(st, 0, 7000.0)
    assert action == "waiting" and engine.is_due(st, 7000.0) and st["dm_fails"] == 0


def test_state_survives_a_json_round_trip_in_every_stage():
    st = started()
    engine.react(st, True, 1.0)
    for _ in range(MAX_DM_FAILS):
        engine.mark_final_sent(st, 1.0)
        engine.dm_failed(st, 1.0)
    assert json.loads(json.dumps(st, allow_nan=False)) == st  # no inf/nan that Config would choke on


# ---------------------------------------------------------------- guessing


def final_state():
    st = started()
    st["stage"] = engine.STAGE_FINAL
    return st


def test_a_correct_guess_solves_it_whatever_the_case_or_punctuation():
    st = final_state()
    assert engine.guess(st, "Hatter", " HAT-ter ", 9.0) == "correct"
    assert st["stage"] == engine.STAGE_SOLVED and st["solved_ts"] == 9.0


def test_a_wrong_guess_is_counted_and_not_a_solve():
    st = final_state()
    assert engine.guess(st, "hatter", "walrus", 9.0) == "wrong"
    assert st["wrong_count"] == 1 and st["stage"] == engine.STAGE_FINAL


def test_guesses_are_ignored_before_the_final_stage():
    for stage in (engine.STAGE_RIDDLE, engine.STAGE_FOUND, engine.STAGE_SOLVED):
        st = started()
        st["stage"] = stage
        assert engine.guess(st, "hatter", "hatter", 9.0) == "ignore"
        assert st["stage"] == stage
    assert engine.guess(None, "hatter", "hatter", 9.0) == "ignore"


def test_too_many_wrong_guesses_lock_you_out_until_tomorrow_pacific():
    st = final_state()
    day1 = ts(2026, 10, 14, 10, 0)
    for _ in range(MAX_WRONG_PER_DAY):
        assert engine.guess(st, "hatter", "nope", day1) == "wrong"
    assert engine.guess(st, "hatter", "hatter", day1 + 60) == "locked"  # even the right word waits
    assert engine.guess(st, "hatter", "hatter", ts(2026, 10, 15, 0, 5)) == "correct"


def test_the_wait_is_at_least_four_hours_then_the_next_nine_am():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")

    def due(h, m=0):
        found = datetime(2026, 10, 14, h, m, tzinfo=la).timestamp()
        got = datetime.fromtimestamp(engine.next_unlock(found), la)
        return got.strftime("%a %H:%M"), round((got.timestamp() - found) / 3600, 1)

    assert due(4, 50) == ("Wed 09:00", 4.2)    # 4h20m... just over four hours: the same morning
    assert due(5, 30) == ("Thu 09:00", 27.5)   # four hours later is past 9:00, so the next morning
    assert due(16, 50) == ("Thu 09:00", 16.2)
    assert due(22, 0) == ("Thu 09:00", 11.0)   # late evening now wakes the next morning, not a day later


# ---------------------------------------------------------------- the button / pointer / rebus puzzle kind

BUTTON = [pid for pid, p in PUZZLES.items() if p.get("kind") == "button"]


@pytest.mark.parametrize("pid", BUTTON)
def test_a_button_puzzle_covers_every_variant_and_only_uses_real_buttons(pid):
    from secrethunt.views import BUTTON_KEYS
    p = PUZZLES[pid]
    assert len(p["buttons"]) >= POOL_MAX and set(p["buttons"]) <= set(BUTTON_KEYS)
    assert set(p["buttons"]) <= set(p["button_hints"])
    assert len(p["rebuses"]) >= POOL_MAX and len(set(p["rebuses"])) == len(p["rebuses"])
    assert all(v < POOL_MAX for v in p["letter_variants"])
    assert all(k == k.lower() for k in p["pointers"])
    assert "idle" in p and "{rebus}" in p["final"] and "{shape}" in p["final"]


def test_the_second_easy_puzzle_is_not_solved_like_the_first():
    first, second = PUZZLES["late_rabbit"], PUZZLES["looking_glass"]
    assert second["tier"] == "easy"
    assert engine.acrostic(second["poem"]) != engine.normalize(second["trigger"])  # not a first-letter acrostic
    assert first.get("kind", "reaction") == "reaction" and second["kind"] == "button"  # not the clock reaction
    assert "rebuses" in second and "{anagram}" not in second["final"]  # not the unscramble
    assert "{hint}" not in second["riddle"]  # not a jumbled room name


def test_letter_clue_names_the_length_and_last_letter():
    assert engine.letter_clue("Gaming") == "the room whose name has 6 letters and ends in **G**"
    assert engine.letter_clue("Chat to the Queen") == "the room whose name has 4 + 2 + 3 + 5 letters and ends in **N**"
    assert engine.letter_clue("A-1") is None
    assert engine.letter_clue("") is None


def test_pointer_uses_the_keyword_description_unless_the_variant_is_a_letter_variant():
    p = {"pointers": {"music": "where songs are shared"}, "letter_variants": [1]}
    assert engine.pointer_for(p, "Music Talk", 0) == "where songs are shared"
    assert engine.pointer_for(p, "Music Talk", 1) == engine.letter_clue("Music Talk")  # forced to the letter clue
    assert engine.pointer_for(p, "Artwork", 0) == engine.letter_clue("Artwork")        # no keyword fits
    assert engine.pointer_for(p, "??", 0) is None                                      # nothing usable


def test_object_hint_follows_the_variant_and_wraps():
    p = {"buttons": ["look", "drink"], "button_hints": {"look": "L", "drink": "D"}}
    assert [engine.object_hint(p, v) for v in range(3)] == ["L", "D", "L"]
