"""2.7.0: collection score on the leaderboard, trading 5 sell tokens up a rarity,
and the legendary drop announcement."""

import random

import pytest

from cardcollect import engine, views
from cardcollect.constants import MAX_COPIES_KEPT, TRADE_UP_COUNT
from cardcollect.models import Card, SellToken
from test_cardcollect_smoke import (
    FakeAttachment,
    FakeChannel,
    FakeCtx,
    FakeGuild,
    FakeMember,
    _admin,
    cog,  # noqa: F401  (fixture)
    fake_art_bytes,
)
from test_trade import TradeInter


def test_score_counts_each_different_card_by_rarity_and_finish():
    rarity = {1: "common", 2: "rare", 3: "epic", 4: "legendary"}
    assert engine.collection_score([1, 1, 2], {}, rarity) == 1 + 3  # spares don't count
    assert engine.collection_score([3, 4], {3: 1, 4: 2}, rarity) == 8 * 2 + 20 * 3
    assert engine.collection_score([99], {}, rarity) == 0  # a card that's gone from the pool


def test_trade_up_prefers_cards_you_dont_own_then_ones_with_room():
    pool = [Card(i, f"c{i}", "S", "epic", "") for i in (1, 2, 3)]
    for seed in range(30):
        assert engine.pick_trade_up(pool, "epic", [1, 1, 2], MAX_COPIES_KEPT, random.Random(seed)).card_id == 3
        assert engine.pick_trade_up(pool, "epic", [1, 1, 2, 3, 3], MAX_COPIES_KEPT, random.Random(seed)).card_id == 2
    assert engine.pick_trade_up(pool, "legendary", [], MAX_COPIES_KEPT) is None


async def setup(cog, gid):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    a, b = FakeMember(gid * 10 + 1, guild, display_name="Ann"), FakeMember(gid * 10 + 2, guild, display_name="Bo")
    guild.members = {m.id: m for m in (admin, a, b)}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    ids = {}
    for name, rarity in (("C1", "common"), ("C2", "common"), ("R1", "rare"), ("R2", "rare"), ("L1", "legendary")):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=f"{name} | Show")
    for cid, data in (await cog.config.guild(guild).pool()).items():
        ids[data["name"]] = int(cid)
    cog._rng = random.Random(gid)
    return guild, admin, a, b, channel, ids


async def set_state(cog, member, **fields):
    state = await cog._member_state(member)
    for k, v in fields.items():
        setattr(state, k, v)
    await cog._save_member_state(member, state)


@pytest.mark.asyncio
async def test_the_leaderboard_ranks_by_score_and_can_switch_to_card_count(cog):
    guild, _ad, a, b, channel, ids = await setup(cog, 801)
    await set_state(cog, a, collection=[ids["C1"], ids["C2"], ids["R1"], ids["R2"]])  # 8 points, 4 cards
    await set_state(cog, b, collection=[ids["L1"]], finishes={ids["L1"]: 1})  # 40 points, 1 card
    ctx = FakeCtx(a, guild, channel)
    await cog.card.commands["leaderboard"].callback(cog, ctx)
    view = ctx.sent[-1].view
    assert isinstance(view, views.LeaderboardView)
    first = ctx.sent[-1].embeds[0].description.splitlines()[0]
    assert b.mention in first and "40" in first
    press = TradeInter(a, channel)
    await view.children[1].callback(press)
    assert a.mention in press.response.edited["embed"].description.splitlines()[0]


@pytest.mark.asyncio
async def test_five_tokens_of_one_rarity_trade_up_for_a_card_one_rarity_higher(cog):
    guild, _ad, a, _b, channel, ids = await setup(cog, 802)
    tokens = [SellToken(f"t{i}", ids["C1"], "common", 0.0) for i in range(5)] + [SellToken("keep", ids["R1"], "rare", 0.0)]
    await set_state(cog, a, collection=[ids["R1"], ids["R1"]], sell_tokens=tokens)
    card, outcome, _set = await cog.trade_up(guild, a, [f"t{i}" for i in range(5)])
    assert card.card_id == ids["R2"] and outcome == "new"  # the rare they didn't own
    state = await cog._member_state(a)
    assert [t.token_id for t in state.sell_tokens] == ["keep"]


@pytest.mark.asyncio
async def test_trade_up_needs_five_of_the_same_rarity_and_not_legendary(cog):
    guild, _ad, a, _b, _c, ids = await setup(cog, 803)
    mixed = [SellToken(f"c{i}", ids["C1"], "common", 0.0) for i in range(4)] + [SellToken("r", ids["R1"], "rare", 0.0)]
    legend = [SellToken(f"l{i}", ids["L1"], "legendary", 0.0) for i in range(5)]
    await set_state(cog, a, sell_tokens=mixed + legend)
    assert "same rarity" in await cog.trade_up(guild, a, ["c0", "c1", "c2", "c3", "r"])
    assert "exactly" in await cog.trade_up(guild, a, ["c0", "c1", "c2"])
    assert "top rarity" in await cog.trade_up(guild, a, [f"l{i}" for i in range(5)])
    assert len((await cog._member_state(a)).sell_tokens) == 10  # nothing was spent


@pytest.mark.asyncio
async def test_the_sell_view_offers_trade_up_only_with_enough_tokens(cog):
    guild, _ad, a, _b, channel, ids = await setup(cog, 804)
    await set_state(cog, a, sell_tokens=[SellToken(f"t{i}", ids["C1"], "common", 0.0) for i in range(4)])
    view = views.SellView(cog, a)
    await view.start(FakeCtx(a, guild, channel))
    assert view.trade_up is None
    await set_state(cog, a, sell_tokens=[SellToken(f"t{i}", ids["C1"], "common", 0.0) for i in range(5)])
    view = views.SellView(cog, a)
    await view.start(FakeCtx(a, guild, channel))
    assert view.trade_up is not None and view.trade_up.min_values == TRADE_UP_COUNT
    view.trade_up._values = [f"t{i}" for i in range(5)]
    await view._on_trade_up(TradeInter(a, channel))
    assert "traded up" in channel.sent[-1].content
    assert (await cog._member_state(a)).sell_tokens == []


class RoleGuild(FakeGuild):
    def __init__(self, id_, role):
        super().__init__(id_)
        self.role = role

    def get_role(self, role_id):
        return self.role if role_id == self.role.id else None


class Role:
    id = 4242
    mention = "<@&4242>"


@pytest.mark.asyncio
async def test_a_drop_with_a_legendary_gets_the_big_announcement_and_optional_ping(cog):
    guild = RoleGuild(805, Role())
    admin = _admin(8050, guild)
    channel = FakeChannel(80500, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
    await cog.card.commands["addcard"].callback(cog, ctx, "legendary", name_and_series="Big | Show")
    await cog.config.guild(guild).drop_size.set(1)
    assert await cog._post_drop(channel, guild, is_test=False) is None
    assert "LEGENDARY DROP" in channel.sent[-1].content and "<@&" not in channel.sent[-1].content
    await cog._apply_legendary_role(guild, admin, Role())
    assert await cog._post_drop(channel, guild, is_test=False) is None
    assert "<@&4242>" in channel.sent[-1].content
    assert await cog._post_drop(channel, guild, is_test=True) is None
    assert "<@&4242>" not in channel.sent[-1].content  # test drops never ping


@pytest.mark.asyncio
async def test_a_drop_without_a_legendary_is_unchanged(cog):
    guild = FakeGuild(806)
    admin = _admin(8060, guild)
    channel = FakeChannel(80600, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Small | Show")
    assert await cog._post_drop(channel, guild, is_test=False) is None
    assert channel.sent[-1].content.startswith("\U0001f3b4 **A drop!**")


@pytest.mark.asyncio
async def test_every_rarity_gets_its_own_trade_up_list_even_with_lots_of_tokens(cog):
    # live report: with 25+ rare tokens, the single trade-up list was all rares and commons couldn't be picked
    guild, _ad, a, _b, channel, ids = await setup(cog, 807)
    rares = [SellToken(f"r{i:02d}", ids["R1" if i % 2 else "R2"], "rare", 0.0) for i in range(30)]
    commons = [SellToken(f"c{i}", ids["C1"], "common", 0.0) for i in range(6)]
    await set_state(cog, a, sell_tokens=rares + commons)
    view = views.SellView(cog, a)
    await view.start(FakeCtx(a, guild, channel))
    assert set(view.trade_ups) == {"common", "rare"}
    common_list = view.trade_ups["common"]
    assert sorted(o.value for o in common_list.options) == [f"c{i}" for i in range(6)]
    assert "C1 (1)" in [o.label for o in common_list.options]  # same card twice: told apart
    assert len(view.trade_ups["rare"].options) == 25
    assert "first 25 rare" in view.message.embeds[0].fields[-1].value
    common_list._values = [f"c{i}" for i in range(5)]
    await common_list.callback(TradeInter(a, channel))
    state = await cog._member_state(a)
    assert ids["R2"] in state.collection or ids["R1"] in state.collection
    assert [t.token_id for t in state.sell_tokens if t.rarity == "common"] == ["c5"]
