import random

import pytest

from cardcollect import engine
from cardcollect.constants import DEFAULT_DROP_WEIGHTS, DEFAULT_SELL_PRICES, DEFAULT_TIER_CUTOFFS
from cardcollect.models import Card


def make_pool():
    return [
        Card(1, "Common A", "Series", "common", "1.png"),
        Card(2, "Common B", "Series", "common", "2.png"),
        Card(3, "Rare A", "Series", "rare", "3.png"),
        Card(4, "Epic A", "Series", "epic", "4.png"),
        Card(5, "Legendary A", "Series", "legendary", "5.png"),
    ]


def test_bucket_tier_boundaries():
    cutoffs = DEFAULT_TIER_CUTOFFS
    assert engine.bucket_tier(cutoffs["legendary"], cutoffs) == "legendary"
    assert engine.bucket_tier(cutoffs["legendary"] - 1, cutoffs) == "epic"
    assert engine.bucket_tier(cutoffs["epic"], cutoffs) == "epic"
    assert engine.bucket_tier(0, cutoffs) == "common"


def test_roll_tier_deterministic_with_seed():
    rng = random.Random(42)
    results = {engine.roll_tier(DEFAULT_DROP_WEIGHTS, rng) for _ in range(200)}
    assert results <= {"common", "rare", "epic", "legendary"}
    assert "common" in results  # by far the heaviest weight, must show up


def test_roll_tier_rejects_all_zero_weights():
    with pytest.raises(ValueError):
        engine.roll_tier({"common": 0, "rare": 0, "epic": 0, "legendary": 0})


def test_pick_card_for_tier_only_from_that_tier():
    pool = make_pool()
    rng = random.Random(1)
    for _ in range(20):
        card = engine.pick_card_for_tier(pool, "rare", rng)
        assert card is not None
        assert card.rarity == "rare"


def test_pick_card_for_tier_empty_tier_returns_none():
    pool = [c for c in make_pool() if c.rarity != "legendary"]
    assert engine.pick_card_for_tier(pool, "legendary", random.Random(1)) is None


def test_should_drop_bounds():
    rng = random.Random(7)
    assert engine.should_drop(0.0, rng) is False
    assert engine.should_drop(1.0, rng) is True




def test_build_drop_is_test_flag_threaded_through():
    pool = make_pool()
    drop = engine.build_drop(
        pool, DEFAULT_DROP_WEIGHTS, 2, guild_id=1, channel_id=2, rng=random.Random(1), is_test=True
    )
    assert drop.is_test is True


def test_build_drop_empty_pool_returns_none():
    drop = engine.build_drop([], DEFAULT_DROP_WEIGHTS, 3, 1, 2, rng=random.Random(1))
    assert drop is None


def test_build_drop_falls_back_when_rolled_tier_is_empty():
    # only commons in the pool -- every roll into rare/epic/legendary must
    # fall back rather than fail the whole drop
    pool = [Card(1, "Only Common", "Series", "common", "1.png")]
    drop = engine.build_drop(pool, DEFAULT_DROP_WEIGHTS, 3, 1, 2, rng=random.Random(2))
    assert drop is not None
    assert all(c["card_id"] == 1 for c in drop.cards)


def test_make_sell_token_and_price():
    token = engine.make_sell_token(card_id=5, rarity="epic")
    assert token.card_id == 5
    assert token.rarity == "epic"
    assert engine.sell_price("epic", DEFAULT_SELL_PRICES) == DEFAULT_SELL_PRICES["epic"]
    assert engine.sell_price("unknown_tier", DEFAULT_SELL_PRICES) == 0


def test_claim_outcome_new_pickup_when_not_owned():
    assert engine.claim_outcome([1, 2, 3], 99, max_copies=2) == "new"


def test_claim_outcome_duplicate_when_below_cap():
    # owns exactly one copy, cap is 2 -- this claim becomes a tradeable spare
    assert engine.claim_outcome([1, 2, 3], 2, max_copies=2) == "duplicate"


def test_claim_outcome_sell_token_when_at_cap():
    # already holds max_copies (an "original" + a spare) -- a 3rd claim
    # converts straight to a sell token instead of piling up more copies
    assert engine.claim_outcome([1, 2, 2, 3], 2, max_copies=2) == "sell_token"


def test_claim_outcome_respects_a_higher_or_lower_max_copies():
    assert engine.claim_outcome([5], 5, max_copies=1) == "sell_token"  # cap of 1: no spares allowed
    assert engine.claim_outcome([5, 5, 5], 5, max_copies=5) == "duplicate"  # generous cap: still room


def test_today_str_uses_injected_now_not_wall_clock():
    import datetime
    from zoneinfo import ZoneInfo

    moment = datetime.datetime(2026, 9, 22, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert engine.today_str("America/Los_Angeles", now=moment) == "2026-09-22"










def test_search_cards_matches_partial_words_in_any_order_ignoring_case_and_accents():
    pool = [
        Card(1, "Lucy", "Cyberpunk: Edgerunners", "rare", "1.png"),
        Card(2, "Lucy Heartfilia", "Fairy Tail", "common", "2.png"),
        Card(3, "Rébecca", "Cyberpunk: Edgerunners", "epic", "3.png"),
        Card(4, "Nami", "One Piece", "common", "4.png"),
    ]
    assert [c.card_id for c in engine.search_cards(pool, "LUCY")] == [1, 2]
    assert [c.card_id for c in engine.search_cards(pool, "heartfilia lucy")] == [2]
    assert [c.card_id for c in engine.search_cards(pool, "rebecca")] == [3]
    assert [c.card_id for c in engine.search_cards(pool, "lucy edgerunners")] == [1]
    assert engine.search_cards(pool, "   ") == []
    assert engine.search_cards(pool, "nobody") == []


def test_search_cards_ranks_name_matches_ahead_of_series_only_matches():
    pool = [
        Card(1, "Zed", "Lucy's Show", "common", "1.png"),
        Card(2, "Lucy", "Other", "common", "2.png"),
    ]
    assert [c.card_id for c in engine.search_cards(pool, "lucy")] == [2, 1]



def test_daily_streak_rules():
    assert engine.daily_streak("", 0, "2026-10-04") == (True, 1)
    assert engine.daily_streak("2026-10-03", 4, "2026-10-04") == (True, 5)
    assert engine.daily_streak("2026-10-04", 5, "2026-10-04") == (False, 5)
    assert engine.daily_streak("2026-10-01", 9, "2026-10-04") == (True, 1)
    assert engine.daily_streak("2026-09-30", 2, "2026-10-01") == (True, 3)  # across a month end


def test_bonus_day_and_upgrade():
    assert engine.is_bonus_day(7, 7) and engine.is_bonus_day(14, 7) and not engine.is_bonus_day(6, 7)
    assert engine.upgrade_tier("common") == "rare" and engine.upgrade_tier("legendary") == "legendary"


def test_roll_daily_odds_are_roughly_right():
    from cardcollect.constants import DEFAULT_DAILY_WEIGHTS
    pool = make_pool()
    rng = random.Random(1)
    counts = {}
    n = 200000
    for _ in range(n):
        c = engine.roll_daily(pool, DEFAULT_DAILY_WEIGHTS, False, rng)
        counts[c.rarity] = counts.get(c.rarity, 0) + 1
    assert abs(counts["common"] / n - 0.75) < 0.01
    assert abs(counts["rare"] / n - 0.20) < 0.01
    assert abs(counts["epic"] / n - 0.045) < 0.004
    assert abs(counts["legendary"] / n - 0.005) < 0.0015


def test_roll_daily_bonus_never_gives_common_and_falls_back_when_tier_empty():
    pool = make_pool()
    rng = random.Random(2)
    assert all(engine.roll_daily(pool, {"common": 1}, True, rng).rarity == "rare" for _ in range(50))
    only_common = [c for c in pool if c.rarity == "common"]
    assert engine.roll_daily(only_common, {"legendary": 1}, False, rng).rarity == "common"
    assert engine.roll_daily([], {"common": 1}, False, rng) is None


def test_gallery_sort_key_orders_rarity_then_name_ignoring_case_and_accents():
    cards = [
        Card(1, "Zoe", "S", "common", "1.png"),
        Card(2, "émi", "S", "epic", "2.png"),
        Card(3, "Beth", "S", "legendary", "3.png"),
        Card(4, "adam", "S", "common", "4.png"),
        Card(5, "Eve", "S", "epic", "5.png"),
        Card(6, "Anna", "S", "rare", "6.png"),
        Card(8, "Twin", "S", "rare", "8.png"),
        Card(7, "Twin", "S", "rare", "7.png"),
    ]
    ordered = [c.card_id for c in sorted(cards, key=engine.gallery_sort_key)]
    assert ordered == [3, 2, 5, 6, 7, 8, 4, 1]


def test_search_finds_a_card_by_its_english_series_title():
    pool = [
        Card(1, "Fern", "Sousou no Frieren", "rare", "1.png", series_alt="Frieren: Beyond Journey's End"),
        Card(2, "Lucy", "Cyberpunk: Edgerunners", "rare", "2.png"),
    ]
    assert [c.name for c in engine.search_cards(pool, "beyond journey")] == ["Fern"]
    assert [c.name for c in engine.search_cards(pool, "frieren")] == ["Fern"]  # the romaji title still works
    assert [c.name for c in engine.search_cards(pool, "edgerunners")] == ["Lucy"]


def test_a_series_title_match_still_ranks_below_a_name_match():
    pool = [
        Card(1, "Zed", "Frieren", "common", "1.png"),
        Card(2, "Frieren", "Sousou no Frieren", "rare", "2.png", series_alt="Frieren: Beyond Journey's End"),
    ]
    assert [c.name for c in engine.search_cards(pool, "frieren")] == ["Frieren", "Zed"]



# --- 2.0.0: the pick-your-card draw ------------------------------------------


def test_build_drop_gives_distinct_cards_in_numbered_positions():
    drop = engine.build_drop(make_pool(), DEFAULT_DROP_WEIGHTS, 3, guild_id=1, channel_id=2, rng=random.Random(11))
    assert len(drop.cards) == 3
    assert [c["position"] for c in drop.cards] == [0, 1, 2]
    assert len({c["card_id"] for c in drop.cards}) == 3
    assert all("code" not in c for c in drop.cards)
    assert drop.is_test is False and drop.picks == {}


def test_owner_tier_ranks_non_owners_then_spares_then_the_limit():
    assert engine.owner_tier([], 7, 2) == 0
    assert engine.owner_tier([7], 7, 2) == 1
    assert engine.owner_tier([7, 7], 7, 2) == 2
    assert engine.owner_tier([8, 8, 8], 7, 2) == 0


def test_pity_multiplier_grows_by_a_quarter_and_caps_at_double():
    assert engine.pity_multiplier(0) == 1.0
    assert engine.pity_multiplier(2) == 1.5
    assert engine.pity_multiplier(4) == 2.0
    assert engine.pity_multiplier(40) == 2.0
    assert engine.pity_multiplier(-3) == 1.0


CARDS = [{"card_id": 10, "position": 0}, {"card_id": 11, "position": 1}, {"card_id": 12, "position": 2}]


def test_resolve_drop_a_card_nobody_picked_is_wasted():
    results, _ = engine.resolve_drop(CARDS, {1: 0}, {1: []}, {}, 2, random.Random(1))
    by_pos = {r["position"]: r for r in results}
    assert by_pos[0]["winner"] == 1 and by_pos[0]["outcome"] == "new"
    assert by_pos[1]["winner"] is None and by_pos[1]["pickers"] == 0 and by_pos[1]["outcome"] is None


def test_resolve_drop_a_non_owner_always_beats_owners():
    for seed in range(30):
        results, _ = engine.resolve_drop(
            CARDS[:1], {1: 0, 2: 0, 3: 0}, {1: [10], 2: [], 3: [10, 10]}, {}, 2, random.Random(seed)
        )
        assert results[0]["winner"] == 2 and results[0]["outcome"] == "new"


def test_resolve_drop_owners_with_room_beat_those_at_the_limit_and_get_a_spare():
    for seed in range(30):
        results, _ = engine.resolve_drop(CARDS[:1], {1: 0, 3: 0}, {1: [10], 3: [10, 10]}, {}, 2, random.Random(seed))
        assert results[0]["winner"] == 1 and results[0]["outcome"] == "duplicate"


def test_resolve_drop_someone_at_the_limit_wins_only_when_alone_and_gets_a_sell_token():
    results, _ = engine.resolve_drop(CARDS[:1], {3: 0}, {3: [10, 10]}, {}, 2, random.Random(1))
    assert results[0]["winner"] == 3 and results[0]["outcome"] == "sell_token"


def test_resolve_drop_pity_only_grows_for_equal_rivals_and_resets_on_a_win():
    # 1 (non-owner) beats 2 (owner): 2 lost on priority, not a lottery -> no pity
    results, pity = engine.resolve_drop(CARDS[:1], {1: 0, 2: 0}, {1: [], 2: [10]}, {1: 0, 2: 0}, 2, random.Random(1))
    assert pity[2] == 0 and pity[1] == 0 and results[0]["contested"] is False
    # two equal pickers: one wins (pity 0), the loser gains 1
    results, pity = engine.resolve_drop(CARDS[:1], {1: 0, 2: 0}, {1: [], 2: []}, {1: 3, 2: 0}, 2, random.Random(5))
    winner = results[0]["winner"]
    loser = 2 if winner == 1 else 1
    assert results[0]["contested"] is True
    assert pity[winner] == 0
    assert pity[loser] == ({1: 3, 2: 0}[loser] + 1)


def test_resolve_drop_pity_makes_a_long_loser_win_more_often():
    wins = 0
    for seed in range(2000):
        results, _ = engine.resolve_drop(CARDS[:1], {1: 0, 2: 0}, {1: [], 2: []}, {1: 4, 2: 0}, 2, random.Random(seed))
        wins += results[0]["winner"] == 1
    assert 0.60 < wins / 2000 < 0.73  # 2.0 : 1.0 weight is about two thirds


def test_resolve_drop_is_deterministic_for_a_seeded_rng():
    args = (CARDS, {1: 0, 2: 0, 3: 1}, {1: [], 2: [], 3: []}, {}, 2)
    assert engine.resolve_drop(*args, random.Random(9)) == engine.resolve_drop(*args, random.Random(9))


def test_build_sets_groups_active_cards_by_exact_series_and_needs_three():
    cards = [Card(i, f"C{i}", "Frieren", "common", f"{i}.png") for i in (1, 2, 3)]
    cards += [Card(4, "X", "Two Cards", "rare", "4.png"), Card(5, "Y", "Two Cards", "rare", "5.png")]
    cards += [Card(6, "Gone", "Frieren", "common", "6.png", retired=True)]
    sets = engine.build_sets(cards)
    assert list(sets) == ["Frieren"] and len(sets["Frieren"]) == 3  # retired and too-small groups left out
    assert engine.set_progress(sets, [1, 2, 9]) == [("Frieren", 2, 3)]
    assert engine.set_progress(sets, [9]) == []
    assert engine.set_bonus(3) == 5000 + 3 * 500
