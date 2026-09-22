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
    state = MemberState(collection=[1, 2, 3], favorite_card_id=2, sell_tokens=[token])
    assert state.owns(2) is True
    assert state.owns(99) is False

    restored = MemberState.from_dict(state.to_dict())
    assert restored.collection == [1, 2, 3]
    assert restored.favorite_card_id == 2
    assert restored.sell_tokens[0] == token


def test_member_state_defaults():
    state = MemberState()
    assert state.collection == []
    assert state.favorite_card_id is None
    assert state.sell_tokens == []


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
