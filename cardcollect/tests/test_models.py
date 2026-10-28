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
        pity=2,
        wishlist=[5, 6],
        wish_pings_off=True,
        sets_claimed=["Frieren"],
    )
    assert state.owns(2) is True
    assert state.owns(99) is False

    restored = MemberState.from_dict(state.to_dict())
    assert restored.collection == [1, 2, 3]
    assert restored.showcase_card_ids == [2]
    assert restored.sell_tokens[0] == token
    assert (restored.pity, restored.wishlist, restored.wish_pings_off, restored.sets_claimed) == (2, [5, 6], True, ["Frieren"])


def test_member_state_defaults():
    state = MemberState()
    assert state.collection == []
    assert state.showcase_card_ids == []
    assert state.sell_tokens == []
    assert (state.pity, state.wishlist, state.wish_pings_off, state.sets_claimed) == (0, [], False, [])


def test_member_state_ignores_and_drops_the_old_claim_quota_fields():
    restored = MemberState.from_dict({"collection": [1], "daily_claims": 4, "daily_claims_date": "2026-09-22"})
    assert restored.collection == [1]
    assert "daily_claims" not in restored.to_dict()


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


def _drop(**kwargs):
    return ActiveDrop(
        message_id=None,
        guild_id=1,
        channel_id=2,
        cards=[{"card_id": 10, "position": 0}, {"card_id": 11, "position": 1}],
        **kwargs,
    )


def test_active_drop_card_at_a_position():
    drop = _drop()
    assert drop.card_at(1)["card_id"] == 11
    assert drop.card_at(7) is None
    assert drop.picks == {} and drop.resolved is False and drop.is_test is False


def test_active_drop_closes_at_its_deadline():
    assert _drop().is_closed(10**12) is False  # no deadline set
    drop = _drop(closes_at=100.0)
    assert drop.is_closed(99.9) is False
    assert drop.is_closed(100.0) is True


def test_member_state_keeps_daily_pull_fields():
    from cardcollect.models import MemberState
    s = MemberState.from_dict({"daily_pull_date": "2026-10-04", "daily_streak": 6})
    assert MemberState.from_dict(s.to_dict()).daily_streak == 6
    assert MemberState.from_dict({}).daily_pull_date == ""


def test_card_series_alt_round_trips_and_defaults_to_empty_for_old_data():
    card = Card(card_id=1, name="Fern", series="Sousou no Frieren", rarity="rare", image_path="1.png", series_alt="Frieren")
    assert Card.from_dict(1, card.to_dict()).series_alt == "Frieren"

    old = {"name": "Old", "series": "S", "rarity": "common", "image_path": "x.png"}  # saved before series_alt existed
    assert Card.from_dict(2, old).series_alt == ""
    assert Card.from_dict(3, {**old, "series_alt": None}).series_alt == ""
