import pytest

from cardcollect.models import ActiveDrop, Card, MemberState, SellToken


def test_card_rejects_bad_rarity():
    with pytest.raises(ValueError):
        Card(1, "Name", "Series", "mythic", "1.png")


def test_card_roundtrip():
    card = Card(1, "Alice", "Some Anime", "rare", "1.png", favourites=500, added_by=None)
    data = card.to_dict()
    restored = Card.from_dict(1, data)
    assert restored == card


def test_sell_token_roundtrip():
    token = SellToken("abc123", card_id=4, rarity="epic", claimed_at=1234.5)
    restored = SellToken.from_dict(token.to_dict())
    assert restored == token


def test_member_state_owns_and_roundtrip():
    token = SellToken("abc123", card_id=4, rarity="epic", claimed_at=1234.5)
    state = MemberState(
        collection=[1, 2, 3],
        showcase_card_ids=[2],
        sell_tokens=[token],
        daily_claims=3,
        daily_claims_date="2026-09-22",
    )
    assert state.owns(2) is True
    assert state.owns(99) is False

    restored = MemberState.from_dict(state.to_dict())
    assert restored.collection == [1, 2, 3]
    assert restored.showcase_card_ids == [2]
    assert restored.sell_tokens[0] == token
    assert restored.daily_claims == 3
    assert restored.daily_claims_date == "2026-09-22"


def test_member_state_defaults():
    state = MemberState()
    assert state.collection == []
    assert state.showcase_card_ids == []
    assert state.sell_tokens == []
    assert state.daily_claims == 0
    assert state.daily_claims_date == ""


def test_member_state_from_dict_migrates_legacy_single_favorite():
    """Pre-showcase member data only ever had `favorite_card_id`. Reading it
    back must not silently drop that pick -- it becomes a one-card showcase
    instead of an empty one."""
    legacy_data = {"collection": [7], "favorite_card_id": 7, "sell_tokens": []}
    restored = MemberState.from_dict(legacy_data)
    assert restored.showcase_card_ids == [7]

    # but once showcase_card_ids is actually present, it wins outright --
    # no attempt to merge it with a stale favorite_card_id lying around
    mixed_data = {"collection": [7, 8], "favorite_card_id": 7, "showcase_card_ids": [8], "sell_tokens": []}
    restored2 = MemberState.from_dict(mixed_data)
    assert restored2.showcase_card_ids == [8]


def test_active_drop_emoji_lookup():
    drop = ActiveDrop(
        message_id=None,
        guild_id=1,
        channel_id=2,
        cards=[
            {"card_id": 10, "emoji": "🍉", "position": 0},
            {"card_id": 11, "emoji": "⭐", "position": 1},
        ],
        decoy_emojis=["🔥", "💧"],
    )
    assert drop.is_real_emoji("🍉") is True
    assert drop.is_real_emoji("🔥") is False
    assert drop.emoji_for("⭐")["card_id"] == 11
    assert drop.emoji_for("👽") is None
    assert drop.is_test is False
