"""Pure-logic tests for secrethunt: no discord, no redbot."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from secrethunt import engine
from secrethunt.constants import MAX_DECOY_REPLIES, MAX_DM_FAILS, MAX_WRONG_PER_DAY, POOL_MAX
from secrethunt.puzzles import PUZZLES

LA = ZoneInfo("America/Los_Angeles")


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LA).timestamp()


def local(t):
    return datetime.fromtimestamp(t, LA)


# ---------------------------------------------------------------- puzzle data


@pytest.mark.parametrize("pid", list(PUZZLES))
def test_every_poem_spells_its_trigger_word(pid):
    p = PUZZLES[pid]
    assert engine.acrostic(p["poem"]) == engine.normalize(p["trigger"])


@pytest.mark.parametrize("pid", list(PUZZLES))
def test_answers_cover_the_largest_pool_and_stay_out_of_the_public_poem(pid):
    p = PUZZLES[pid]
    answers = [engine.normalize(a) for a in p["answers"]]
    assert len(answers) >= POOL_MAX and len(set(answers)) == len(answers)
    poem = engine.normalize(" ".join(p["poem"]))
    assert not any(a in poem for a in answers)  # nobody can read an answer off the public poem


@pytest.mark.parametrize("pid", list(PUZZLES))
def test_text_templates_format_cleanly(pid):
    p = PUZZLES[pid]
    p["riddle"].format(hint="x", n=1)
    p["riddle_no_hint"].format(channel="#c")
    p["final"].format(anagram="x")
    p["solved"].format(rank=" #1")
    p["hall"].format(member="m", title="t", rank=1)


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


def test_channel_hint_uses_letters_only_and_skips_tiny_names():
    hint = engine.channel_hint("wonder-land_chat 💬", "s")
    assert sorted(hint) == sorted("wonderlandchat")
    assert engine.channel_hint("ab", "s") is None
    assert engine.channel_hint("💬│", "s") is None


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


def test_next_unlock_is_the_next_nine_am_at_least_twelve_hours_on():
    assert local(engine.next_unlock(ts(2026, 10, 13, 16, 6))) == datetime(2026, 10, 14, 9, 0, tzinfo=LA)
    # found just before midnight: 12h later is 11:00, so 9:00 is already past; wait for the day after
    assert local(engine.next_unlock(ts(2026, 10, 13, 23, 0))) == datetime(2026, 10, 15, 9, 0, tzinfo=LA)
    # found at 8am: 12h later is 8pm, next 9am is tomorrow
    assert local(engine.next_unlock(ts(2026, 10, 13, 8, 0))) == datetime(2026, 10, 14, 9, 0, tzinfo=LA)
    # found at 9pm: 12h later is exactly 9:00
    assert local(engine.next_unlock(ts(2026, 10, 13, 21, 0))) == datetime(2026, 10, 14, 9, 0, tzinfo=LA)


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


def test_reacting_to_someone_elses_object_is_a_decoy_that_goes_quiet():
    st = started()
    replies = [engine.react(st, False, 1.0) for _ in range(MAX_DECOY_REPLIES + 2)]
    assert replies == ["decoy"] * MAX_DECOY_REPLIES + ["quiet_decoy"] * 2
    assert st["stage"] == engine.STAGE_RIDDLE


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
