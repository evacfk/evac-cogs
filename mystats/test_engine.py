"""Pure formatting tests: what a member sees for each game's raw numbers."""
from mystats import engine


def test_pct_handles_zero_total():
    assert engine.pct(1, 0) == "-"
    assert engine.pct(3, 4) == "75%"


def test_minigames_wins_only_games_show_wins_and_best_streak():
    data = {"games": {"mathdrop": {"good": 12, "bad": 0, "highest_streak": 5},
                      "pet": {"good": 1, "bad": 0, "highest_streak": 1}}, "boss_damage": 0}
    lines = engine.minigame_lines(data)
    assert lines == ["**Pet the pupper:** 1 win", "**Quick math:** 12 wins · best streak 5"]


def test_minigames_two_sided_games_show_won_and_lost_and_boss_damage():
    data = {"games": {"hunt": {"good": 7, "bad": 2, "highest_streak": 3}}, "boss_damage": 12345}
    assert engine.minigame_lines(data) == ["**Hunt:** 7 won, 2 lost · best streak 3", "**Boss damage dealt:** 12,345"]


def test_minigames_empty_or_missing_hides_section():
    assert engine.minigame_lines(None) == []
    assert engine.minigame_lines({"games": {"hunt": {"good": 0, "bad": 0}}, "boss_damage": 0}) == []


def test_duels_net_sign_and_rate():
    assert engine.duel_lines({"wins": 3, "losses": 1, "coins_won": 5000, "coins_lost": 1000}, "coins") == [
        "3 won, 1 lost (75%) · net +4,000 coins"]
    assert engine.duel_lines({"wins": 0, "losses": 2, "coins_won": 0, "coins_lost": 800}, "coins") == [
        "0 won, 2 lost (0%) · net -800 coins"]
    assert engine.duel_lines({"wins": 0, "losses": 0}) == []


def test_heist_defaults_to_level_one_and_hides_when_never_played():
    assert engine.heist_lines({"stats": {"success": 0, "fail": 0, "caught": 0}, "level": 1}) == []
    assert engine.heist_lines({"stats": {"success": 4, "fail": 2, "caught": 1}, "level": 3}) == [
        "Level 3 · 4 pulled off, 2 failed, 1 times caught"]


def test_casino_totals_and_top_three_games():
    data = {"Played": {"Dice": 10, "Blackjack": 5, "War": 1, "Coin": 7, "Craps": 0},
            "Won": {"Dice": 6, "Blackjack": 1, "War": 0, "Coin": 3, "Craps": 0}}
    lines = engine.casino_lines(data)
    assert lines[0] == "23 games played, 10 won (43%)"
    # most played first, even though alphabetical order would put Blackjack first
    assert lines[1:] == ["Dice: 10 played, 6 won", "Coin: 7 played, 3 won", "Blackjack: 5 played, 1 won"]
    assert engine.casino_lines({"Played": {"Dice": 0}, "Won": {}}) == []


def test_cards_pluralisation_and_streak():
    assert engine.card_lines({"owned": 1, "daily_streak": 0}) == ["1 card collected"]
    assert engine.card_lines({"owned": 40, "daily_streak": 6}) == ["40 cards collected · daily pull streak 6"]
    assert engine.card_lines({"owned": 0, "daily_streak": 0}) == []


def test_puzzle_verdict_pet_bumps_lottery():
    assert engine.puzzle_lines({"puzzles_won": 2, "pieces_collected": 80}) == ["2 puzzles won · 80 pieces collected"]
    assert engine.puzzle_lines({}) == []
    assert engine.verdict_lines({"played": 10, "correct": 6, "streak": 2, "best_streak": 5}) == [
        "10 played · 6 right guesses (60%) · streak 2 (best 5)"]
    assert engine.verdict_lines({"played": 0}) == []
    assert engine.pet_lines({"care_recent": 1}) == ["Looked after the pet 1 time in the last few weeks"]
    assert engine.pet_lines({"care_recent": 0}) == []
    assert engine.bump_lines({"total_bumps": 3}) == ["3 server bumps"]
    assert engine.lottery_lines({"tickets": {"a": 2, "b": 3, "weird": {"x": 1}}}) == ["Holding 5 lottery tickets"]
    assert engine.lottery_lines({"tickets": {}}) == []


def test_garbage_numbers_never_crash():
    assert engine.duel_lines({"wins": "x", "losses": None}) == []
    assert engine.minigame_lines({"games": {"hunt": {"good": "7", "bad": None}}}) == ["**Hunt:** 7 wins"]


def test_build_sections_skips_empty_and_keeps_order():
    raw = {"duel": {"wins": 1, "losses": 0, "coins_won": 10, "coins_lost": 0}, "bumps": {"total_bumps": 2},
           "heist": None, "casino": {"Played": {}, "Won": {}}}
    titles = [t for t, _ in engine.build_sections(raw)]
    assert titles == ["\N{CROSSED SWORDS} Duels", "\N{PUBLIC ADDRESS LOUDSPEAKER} Bumps"]


def test_clip_limits_field_length():
    assert len(engine.clip("x" * 5000)) == 1024
    assert engine.clip("short") == "short"


def test_games_that_cannot_be_lost_never_show_a_lost_count():
    data = {"games": {"mathdrop": {"good": 4, "bad": 2, "highest_streak": 0}}, "boss_damage": 0}
    assert engine.minigame_lines(data) == ["**Quick math:** 4 wins"]
