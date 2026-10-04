import random

import pytest

from cardcollect import engine
from cardcollect import captcha
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


def test_build_drop_gives_every_card_its_own_valid_code():
    pool = make_pool()
    rng = random.Random(11)
    drop = engine.build_drop(pool, DEFAULT_DROP_WEIGHTS, 3, guild_id=1, channel_id=2, rng=rng)
    assert drop is not None
    assert len(drop.cards) == 3
    codes = [c["code"] for c in drop.cards]
    assert all(captcha.looks_like_code(code) for code in codes)
    assert len(set(codes)) == 3
    assert [c["position"] for c in drop.cards] == [0, 1, 2]
    for i, a in enumerate(codes):
        for b in codes[i + 1 :]:
            assert captcha.edit_distance(a, b) >= 2
    assert drop.is_test is False


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


def test_has_quota_remaining_unlimited_when_quota_is_zero_or_negative():
    assert engine.has_quota_remaining(999, "2026-09-22", quota=0, today="2026-09-22") is True
    assert engine.has_quota_remaining(999, "2026-09-22", quota=-1, today="2026-09-22") is True


def test_has_quota_remaining_resets_lazily_on_a_new_day():
    # stale/yesterday's date -> full quota available, no scheduler needed
    assert engine.has_quota_remaining(10, "2026-09-21", quota=10, today="2026-09-22") is True
    # never claimed (blank date, the DEFAULT_MEMBER sentinel) -> full quota
    assert engine.has_quota_remaining(0, "", quota=10, today="2026-09-22") is True


def test_has_quota_remaining_enforces_the_cap_within_the_same_day():
    assert engine.has_quota_remaining(9, "2026-09-22", quota=10, today="2026-09-22") is True
    assert engine.has_quota_remaining(10, "2026-09-22", quota=10, today="2026-09-22") is False
    assert engine.has_quota_remaining(11, "2026-09-22", quota=10, today="2026-09-22") is False


def test_record_claim_increments_within_a_day_and_resets_on_a_new_day():
    assert engine.record_claim(0, "", today="2026-09-22") == (1, "2026-09-22")
    assert engine.record_claim(4, "2026-09-22", today="2026-09-22") == (5, "2026-09-22")
    # stale date -> starts over at 1, doesn't keep incrementing yesterday's count
    assert engine.record_claim(9, "2026-09-21", today="2026-09-22") == (1, "2026-09-22")


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
