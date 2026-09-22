import random

import pytest

from cardcollect import engine
from cardcollect.constants import DEFAULT_DROP_WEIGHTS, DEFAULT_SELL_PRICES, DEFAULT_TIER_CUTOFFS, EMOJI_POOL
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


def test_pick_drop_emojis_distinct():
    rng = random.Random(3)
    emojis = engine.pick_drop_emojis(EMOJI_POOL, 3, rng)
    assert len(emojis) == 3
    assert len(set(emojis)) == 3


def test_pick_drop_emojis_too_many_raises():
    with pytest.raises(ValueError):
        engine.pick_drop_emojis(["a", "b"], 3)


def test_pick_decoy_emojis_excludes_real():
    rng = random.Random(5)
    real = ["🍉", "🍇", "🍊"]
    decoys = engine.pick_decoy_emojis(EMOJI_POOL, real, 5, rng)
    assert set(decoys).isdisjoint(real)
    assert len(decoys) == len(set(decoys))


def test_build_drop_produces_distinct_emoji_per_card():
    pool = make_pool()
    rng = random.Random(11)
    drop = engine.build_drop(
        pool, DEFAULT_DROP_WEIGHTS, 3, EMOJI_POOL, True, 5, guild_id=1, channel_id=2, rng=rng
    )
    assert drop is not None
    assert len(drop.cards) == 3
    emojis = [c["emoji"] for c in drop.cards]
    assert len(set(emojis)) == 3
    assert set(emojis).isdisjoint(drop.decoy_emojis)
    assert drop.is_test is False


def test_build_drop_is_test_flag_threaded_through():
    pool = make_pool()
    drop = engine.build_drop(
        pool, DEFAULT_DROP_WEIGHTS, 2, EMOJI_POOL, False, 0, guild_id=1, channel_id=2,
        rng=random.Random(1), is_test=True,
    )
    assert drop.is_test is True
    assert drop.decoy_emojis == []


def test_build_drop_empty_pool_returns_none():
    drop = engine.build_drop([], DEFAULT_DROP_WEIGHTS, 3, EMOJI_POOL, True, 5, 1, 2, rng=random.Random(1))
    assert drop is None


def test_build_drop_falls_back_when_rolled_tier_is_empty():
    # only commons in the pool -- every roll into rare/epic/legendary must
    # fall back rather than fail the whole drop
    pool = [Card(1, "Only Common", "Series", "common", "1.png")]
    drop = engine.build_drop(pool, DEFAULT_DROP_WEIGHTS, 3, EMOJI_POOL, True, 5, 1, 2, rng=random.Random(2))
    assert drop is not None
    assert all(c["card_id"] == 1 for c in drop.cards)


def test_reaction_add_order_contains_all_and_is_a_permutation():
    pool = make_pool()
    drop = engine.build_drop(pool, DEFAULT_DROP_WEIGHTS, 3, EMOJI_POOL, True, 5, 1, 2, rng=random.Random(9))
    order = engine.reaction_add_order(drop, random.Random(9))
    expected = {c["emoji"] for c in drop.cards} | set(drop.decoy_emojis)
    assert set(order) == expected
    assert len(order) == len(expected)


def test_resolve_claim_picks_among_reactors():
    rng = random.Random(4)
    winner = engine.resolve_claim([10, 20, 30], rng)
    assert winner in (10, 20, 30)


def test_resolve_claim_empty_returns_none():
    assert engine.resolve_claim([]) is None


def test_make_sell_token_and_price():
    token = engine.make_sell_token(card_id=5, rarity="epic")
    assert token.card_id == 5
    assert token.rarity == "epic"
    assert engine.sell_price("epic", DEFAULT_SELL_PRICES) == DEFAULT_SELL_PRICES["epic"]
    assert engine.sell_price("unknown_tier", DEFAULT_SELL_PRICES) == 0


def test_would_be_dupe():
    assert engine.would_be_dupe([1, 2, 3], 2) is True
    assert engine.would_be_dupe([1, 2, 3], 99) is False
