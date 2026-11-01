"""2.3.0: the daily pack -- rolling (engine), buying and opening it (cog), and
the button on `.card daily` (views)."""

import random
from collections import Counter

import pytest
from redbot.core import bank

from cardcollect import engine, views
from cardcollect.constants import PACK_PITY_AFTER, PACK_SIZE, TIERS
from cardcollect.models import Card
from test_cardcollect_smoke import (
    FakeAttachment,
    FakeChannel,
    FakeCtx,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    _admin,
    cog,  # noqa: F401  (fixture)
    fake_art_bytes,
)

DAILY = {"common": 75, "rare": 20, "epic": 4.5, "legendary": 0.5}


def pool_of(spec):
    """spec: {"common": 10, ...} -> that many cards per tier, ids from 1."""
    cards, next_id = [], 1
    for tier, n in spec.items():
        for _ in range(n):
            cards.append(Card(next_id, f"{tier}{next_id}", "S", tier, ""))
            next_id += 1
    return cards


# --- engine ----------------------------------------------------------------------


def test_a_pack_is_three_different_cards_with_the_last_rare_or_better():
    pool = pool_of({"common": 20, "rare": 10, "epic": 5, "legendary": 2})
    for seed in range(300):
        cards = engine.roll_pack(pool, DAILY, PACK_SIZE, "rare", rng=random.Random(seed))
        assert len(cards) == 3 and len({c.card_id for c in cards}) == 3
        assert TIERS.index(cards[-1].rarity) >= TIERS.index("rare")


def test_the_first_two_slots_follow_the_daily_odds_and_the_guarantee_is_mostly_rare():
    pool = pool_of({"common": 30, "rare": 30, "epic": 30, "legendary": 30})
    first, last = Counter(), Counter()
    rng = random.Random(1)
    for _ in range(4000):
        cards = engine.roll_pack(pool, DAILY, PACK_SIZE, "rare", rng=rng)
        first[cards[0].rarity] += 1
        last[cards[-1].rarity] += 1
    assert 0.70 < first["common"] / 4000 < 0.80
    assert last["common"] == 0
    assert 0.75 < last["rare"] / 4000 < 0.85  # 20 / (20 + 4.5 + 0.5)


def test_pity_raises_the_guarantee_to_epic():
    assert engine.pack_floor(0, PACK_PITY_AFTER) == "rare"
    assert engine.pack_floor(PACK_PITY_AFTER - 1, PACK_PITY_AFTER) == "rare"
    assert engine.pack_floor(PACK_PITY_AFTER, PACK_PITY_AFTER) == "epic"
    pool = pool_of({"common": 10, "rare": 10, "epic": 3, "legendary": 1})
    for seed in range(200):
        cards = engine.roll_pack(pool, DAILY, PACK_SIZE, "epic", rng=random.Random(seed))
        assert cards[-1].rarity in ("epic", "legendary")


def test_a_pool_without_rares_still_fills_the_guaranteed_slot():
    pool = pool_of({"common": 5})
    cards = engine.roll_pack(pool, DAILY, PACK_SIZE, "rare", rng=random.Random(3))
    assert len(cards) == 3


def test_a_tiny_pool_gives_a_smaller_pack():
    cards = engine.roll_pack(pool_of({"rare": 2}), DAILY, PACK_SIZE, "rare", rng=random.Random(3))
    assert len(cards) == 2


def test_boosts_make_a_card_more_likely_within_its_tier():
    pool = pool_of({"common": 4})
    hits = Counter()
    rng = random.Random(9)
    for _ in range(3000):
        hits[engine.pick_card_for_tier(pool, "common", rng, {1: 5.0}).card_id] += 1
    assert hits[1] > 2 * hits[2]  # 5 / 8 vs 1 / 8


# --- the cog ---------------------------------------------------------------------


async def setup(cog, gid, spec=None, balance=100_000):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    member = FakeMember(gid * 10 + 1, guild)
    guild.members = {admin.id: admin, member.id: member}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    for tier, n in (spec or {"common": 6, "rare": 3, "epic": 2, "legendary": 1}).items():
        for i in range(n):
            ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
            await cog.card.commands["addcard"].callback(cog, ctx, tier, name_and_series=f"{tier}{i} | Show")
    bank.balances[(guild.id, member.id)] = balance
    cog._rng = random.Random(gid)
    return guild, admin, member, channel


@pytest.mark.asyncio
async def test_opening_a_pack_charges_the_price_and_grants_three_cards(cog):
    guild, _admin_, member, _channel = await setup(cog, 401)
    result = await cog.open_pack(guild, member)
    assert isinstance(result, dict) and result["price"] == 30000
    assert bank.balances[(guild.id, member.id)] == 70000
    state = await cog._member_state(member)
    assert len(state.collection) + len(state.sell_tokens) == 3
    assert state.pack_date == engine.today_str("America/Los_Angeles")


@pytest.mark.asyncio
async def test_only_one_pack_a_day(cog):
    guild, _a, member, _c = await setup(cog, 402)
    assert isinstance(await cog.open_pack(guild, member), dict)
    again = await cog.open_pack(guild, member)
    assert isinstance(again, str) and "already used" in again
    assert bank.balances[(guild.id, member.id)] == 70000  # charged once
    assert await cog.pack_offer(guild, member) is None


@pytest.mark.asyncio
async def test_not_enough_wondercoin_opens_nothing_and_charges_nothing(cog):
    guild, _a, member, _c = await setup(cog, 403, balance=29999)
    result = await cog.open_pack(guild, member)
    assert isinstance(result, str) and "30,000" in result and "29,999" in result
    state = await cog._member_state(member)
    assert state.collection == [] and state.pack_date == ""
    assert bank.balances[(guild.id, member.id)] == 29999


@pytest.mark.asyncio
async def test_pity_counts_packs_without_an_epic_and_then_guarantees_one(cog, monkeypatch):
    guild, _a, member, _c = await setup(cog, 404, spec={"common": 30, "rare": 10, "epic": 3})
    today = ["2026-11-01"]
    monkeypatch.setattr(engine, "today_str", lambda tz, now=None: today[0])
    no_epics = {"common": 1, "rare": 1, "epic": 0, "legendary": 0}  # a normal pack can't roll an epic
    await cog.config.guild(guild).daily_weights.set(no_epics)
    bank.balances[(guild.id, member.id)] = 10_000_000
    for expected in range(1, PACK_PITY_AFTER + 1):
        today[0] = f"2026-11-{expected:02d}"
        result = await cog.open_pack(guild, member)
        assert not result["pity_used"] and result["pity"] == expected
    today[0] = "2026-11-20"
    result = await cog.open_pack(guild, member)
    assert result["pity_used"] and result["entries"][-1][0].rarity == "epic" and result["pity"] == 0


@pytest.mark.asyncio
async def test_a_failure_after_paying_refunds_the_member(cog, monkeypatch):
    guild, _a, member, _c = await setup(cog, 405)

    def boom(*args, **kwargs):
        raise RuntimeError("roll failed")

    monkeypatch.setattr(engine, "roll_pack", boom)
    result = await cog.open_pack(guild, member)
    assert "refunded" in result
    assert bank.balances[(guild.id, member.id)] == 100_000
    assert (await cog._member_state(member)).pack_date == ""


@pytest.mark.asyncio
async def test_packs_can_be_turned_off_and_priced_in_settings(cog):
    guild, admin, member, _c = await setup(cog, 406)
    await cog._apply_pack_price(guild, admin, {"price": "15000"})
    assert await cog.pack_offer(guild, member) == 15000
    await cog._apply_packs_enabled(guild, admin, False)
    assert await cog.pack_offer(guild, member) is None
    assert "off" in await cog.open_pack(guild, member)


# --- the button on `.card daily` --------------------------------------------------


class Inter(FakeInteraction):
    """FakeInteraction whose responses accept views (the pack confirm step sends one)."""

    def __init__(self, user, channel, message=None):
        super().__init__(user, channel, message)
        self.sent_views = []

        async def send_message(content=None, ephemeral=False, view=None, **kwargs):
            self.response.messages.append((content, ephemeral))
            self.sent_views.append(view)

        async def edit_message(content=None, view=None, **kwargs):
            self.response.edited = {"content": content, "view": view}

        self.response.send_message = send_message
        self.response.edit_message = edit_message

    async def edit_original_response(self, content=None, attachments=None, view=None, **kwargs):
        self.original_edit = {"content": content, "view": view}


@pytest.mark.asyncio
async def test_daily_reply_carries_the_pack_button_and_opening_posts_the_cards(cog):
    guild, _a, member, channel = await setup(cog, 407)
    ctx = FakeCtx(member, guild, channel)
    await cog.card.commands["daily"].callback(cog, ctx)
    daily_msg = ctx.sent[-1]
    pack_view = daily_msg.view
    assert isinstance(pack_view, views.PackView) and "30,000" in pack_view.button.label

    press = Inter(member, channel, message=daily_msg)
    await pack_view.button.callback(press)
    confirm = press.sent_views[-1]
    assert isinstance(confirm, views.PackConfirmView) and press.response.messages[-1][1] is True  # private

    yes = Inter(member, channel)
    await confirm.confirm.callback(yes)
    reveal = channel.sent[-1]
    assert "opened today's pack" in reveal.content and reveal.files  # the three cards, as one image
    assert pack_view.button.disabled
    assert bank.balances[(guild.id, member.id)] == 70000

    # a second `.card daily` the same day has no pack button
    ctx2 = FakeCtx(member, guild, channel)
    await cog.card.commands["daily"].callback(cog, ctx2)
    assert ctx2.sent[-1].view is None


@pytest.mark.asyncio
async def test_epic_pack_pulls_are_announced_in_the_drop_channel(cog):
    guild, _a, member, channel = await setup(cog, 408)
    drops = FakeChannel(40899, guild)
    await cog.config.guild(guild).channel_id.set(drops.id)
    epic = Card(1, "Big One", "Show", "epic", "")
    result = {"entries": [(epic, "new", None)], "pity_used": False, "pity": 0, "set_lines": [], "price": 0}
    await cog.announce_pack_hits(guild, member, result, channel.id)
    assert drops.sent and "Big One" in drops.sent[-1].content
    drops.sent.clear()
    await cog.announce_pack_hits(guild, member, result, drops.id)  # opened in the drop channel itself
    assert not drops.sent


@pytest.mark.asyncio
async def test_sell_prices_are_editable_in_settings_and_apply_to_tokens_already_held(cog):
    from cardcollect.models import SellToken
    guild, admin, member, _c = await setup(cog, 409)
    state = await cog._member_state(member)
    state.sell_tokens = [SellToken("t1", 1, "rare", 0.0)]
    await cog._save_member_state(member, state)
    await cog._apply_sell_prices(guild, admin, {"common": "500", "rare": "2,000".replace(",", ""), "epic": "8000",
                                                "legendary": "20000"})
    assert await cog.config.guild(guild).sell_prices() == {"common": 500, "rare": 2000, "epic": 8000, "legendary": 20000}
    assert await cog.sell_tokens(guild, member) == (1, 2000)


# --- 2.10.0: gifting today's pack ----------------------------------------------------


@pytest.mark.asyncio
async def test_gifting_a_pack_charges_the_buyer_and_gives_the_cards_to_the_recipient(cog):
    guild, _a, member, channel = await setup(cog, 410)
    friend = FakeMember(4109, guild)
    guild.members[friend.id] = friend
    result = await cog.gift_pack(guild, member, friend)
    assert isinstance(result, dict) and result["gifted_by"] is member
    assert bank.balances[(guild.id, member.id)] == 70_000
    buyer, receiver = await cog._member_state(member), await cog._member_state(friend)
    assert buyer.collection == [] and buyer.pack_date  # used up the buyer's pack
    assert len(receiver.collection) + len(receiver.sell_tokens) == 3 and receiver.pack_date == ""  # theirs is untouched
    assert "already used" in await cog.open_pack(guild, member)
    text, _file = await cog.pack_result_message(guild, friend, result)
    assert text.startswith(f"\U0001f381 {member.mention} gifted {friend.mention} a pack!")


@pytest.mark.asyncio
async def test_you_cant_gift_a_pack_to_yourself_or_a_bot(cog):
    guild, _a, member, _c = await setup(cog, 411)
    assert "your own pack" in await cog.gift_pack(guild, member, member)
    assert "Bots" in await cog.gift_pack(guild, member, FakeMember(4119, guild, bot=True))
    assert bank.balances[(guild.id, member.id)] == 100_000


@pytest.mark.asyncio
async def test_the_gift_button_picks_a_member_then_sends_the_pack(cog):
    guild, _a, member, channel = await setup(cog, 412)
    friend = FakeMember(4129, guild)
    guild.members[friend.id] = friend
    ctx = FakeCtx(member, guild, channel)
    await cog.card.commands["daily"].callback(cog, ctx)
    pack_view = ctx.sent[-1].view
    press = Inter(member, channel)
    await pack_view.gift_button.callback(press)
    gift = press.sent_views[-1]
    assert isinstance(gift, views.GiftPackView) and gift.send.disabled
    gift.picker._values = [friend]
    pick = Inter(member, channel)
    await gift.picker.callback(pick)
    assert not gift.send.disabled and friend.mention in pick.response.edited["content"]
    await gift.send.callback(Inter(member, channel))
    assert "gifted" in channel.sent[-1].content and pack_view.button.disabled and pack_view.gift_button.disabled
    assert len((await cog._member_state(friend)).collection) + len((await cog._member_state(friend)).sell_tokens) == 3


@pytest.mark.asyncio
async def test_a_gifted_reveal_names_the_recipient_in_upgrade_nudges(cog):
    from cardcollect.models import Card
    guild, _a, member, _c = await setup(cog, 413)
    friend = FakeMember(4139, guild)
    guild.members[friend.id] = friend
    card = (await cog._pool_cards(guild))[0]
    state = await cog._member_state(friend)
    state.collection = [card.card_id, card.card_id]
    await cog._save_member_state(friend, state)
    result = {"entries": [(card, "duplicate", None)], "pity_used": False, "pity": 0, "set_lines": [],
              "price": 0, "gifted_by": member}
    text, _file = await cog.pack_result_message(guild, friend, result)
    assert f"{friend.mention} has enough duplicates" in text and "You have" not in text
