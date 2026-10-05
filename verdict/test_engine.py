import random

import pytest

from verdict import engine
from verdict.seeds import SEED_QUESTIONS


def test_parse_question_ok_and_errors():
    assert engine.parse_question("Best snack? | Chips | Candy | Fruit") == ("Best snack?", ["Chips", "Candy", "Fruit"])
    assert engine.parse_question("  Q  |  a   b |  c ") == ("Q", ["a b", "c"])
    for bad in ("only a question", "Q | one option", "Q | a | b | c | d | e", "Q | same | SAME", "Q | " + "x" * 41 + " | y",
                "x" * 201 + " | a | b"):
        with pytest.raises(engine.QuestionError):
            engine.parse_question(bad)


def test_every_seed_question_is_valid():
    for s in SEED_QUESTIONS:
        engine.parse_question(" | ".join([s["question"], *s["options"]]))
    assert len({s["question"] for s in SEED_QUESTIONS}) == len(SEED_QUESTIONS)


def test_tally_winners_and_ties():
    counts = engine.tally({"1": 0, "2": 1, "3": 1, "4": 7}, 3)  # 7 is out of range and ignored
    assert counts == [1, 2, 0]
    assert engine.winners(counts) == {1}
    assert engine.winners([2, 2, 0]) == {0, 1}
    assert engine.winners([0, 0]) == set()


def test_lone_wolves_need_turnout_and_a_real_minority():
    answers = {str(i): 0 for i in range(9)} | {"w": 1}
    counts = engine.tally(answers, 2)
    assert engine.lone_wolves(answers, counts) == ["w"]
    small = {"a": 0, "b": 1}
    assert engine.lone_wolves(small, engine.tally(small, 2)) == []  # too few voters
    even = {str(i): i % 2 for i in range(10)}
    assert engine.lone_wolves(even, engine.tally(even, 2)) == []  # 50/50: nobody is a wolf


def test_participants_need_both_answer_and_guess():
    assert engine.participants({"1": 0, "2": 1}, {"2": 0, "3": 1}) == ["2"]


def test_mind_readers_include_ties():
    assert engine.mind_readers({"1": 0, "2": 1, "3": 2}, {0, 1}) == ["1", "2"]


def test_streak_rules():
    assert engine.update_streak(0, 0, 1) == 1
    assert engine.update_streak(4, 3, 5) == 4
    assert engine.update_streak(4, 3, 6) == 1
    assert engine.update_streak(5, 9, 5) == 9
    assert engine.streak_milestone(7) == 7 and engine.streak_milestone(8) is None


def test_bar_and_percent():
    assert engine.bar(0.5) == "█" * 5 + "░" * 5
    assert engine.bar(2) == "█" * 10 and engine.bar(-1) == "░" * 10
    assert engine.percent(1, 3) == 33 and engine.percent(0, 0) == 0


def test_month_helpers():
    assert engine.previous_month_key("2026-01") == "2025-12"
    assert engine.previous_month_key("2026-10") == "2026-09"
    # 2026-10-01 03:00 UTC is still Sept 30 in Pacific
    assert engine.month_key(1790823600) == "2026-09"
    assert engine.pt_date(1790823600) == "2026-09-30"


def test_monthly_winner_needs_games_and_breaks_ties_by_accuracy():
    scores = {"1": [5, 10], "2": [5, 6], "3": [9, 4], "4": [0, 9]}
    assert engine.pick_monthly_winner(scores) == "2"  # 3 has too few games; 2 beats 1 on accuracy
    assert engine.pick_monthly_winner({"1": [0, 9]}) is None
    assert engine.pick_monthly_winner({}) is None
    assert [r[0] for r in engine.ranking(scores)] == ["3", "2", "1", "4"]  # the leaderboard has no minimum


def test_pick_seed_never_repeats_until_exhausted():
    rng = random.Random(0)
    used, seen = [], []
    for _ in range(5):
        i, used = engine.pick_seed(used, 5, rng)
        seen.append(i)
    assert sorted(seen) == [0, 1, 2, 3, 4]
    i, used = engine.pick_seed(used, 5, rng)  # exhausted -> start over
    assert used == [i]


def test_uses_up_day_only_for_posts_near_or_after_the_posting_hour():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    t = lambda h: datetime(2026, 10, 4, h, 0, tzinfo=la).timestamp()  # noqa: E731
    assert not engine.uses_up_day(t(0), 10)
    assert not engine.uses_up_day(t(3), 10)
    assert engine.uses_up_day(t(4), 10)
    assert engine.uses_up_day(t(10), 10)
    assert engine.uses_up_day(t(22), 10)


def test_results_pages_split_under_the_limit_and_keep_everyone():
    entry = {"seq": 3, "question": "Q?", "options": ["a", "b"], "counts": [300, 200], "winners": [0],
             "players": [str(1000 + i) for i in range(500)], "readers": [str(1000 + i) for i in range(300)],
             "wolves": ["1001"]}
    pages = engine.results_pages(entry)
    assert len(pages) > 1 and all(len(p) <= 2000 for p in pages)
    text = "\n".join(pages)
    for i in range(500):
        assert f"<@{1000 + i}>" in text


def test_results_pages_for_a_question_nobody_answered_is_just_the_header():
    entry = {"seq": 1, "question": "Q?", "options": ["a", "b"], "counts": [0, 0], "winners": [],
             "players": [], "readers": [], "wolves": []}
    assert len(engine.results_pages(entry)) == 1
