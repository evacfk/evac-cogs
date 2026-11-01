"""2.8.0: selling by rarity keeps duplicates for started upgrades, the clearer
duplicate-token wording, and "you can upgrade X now" nudges."""

import random

import pytest
from redbot.core import bank

from cardcollect import embeds, engine, views
from cardcollect.models import MemberState, SellToken
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


def test_tokens_for_a_card_mid_upgrade_are_kept_by_bulk_sales():
    tokens = [SellToken("a", 1, "rare", 0.0), SellToken("b", 2, "rare", 0.0), SellToken("c", 3, "rare", 0.0)]
    # card 1 is Holo (on its way to Prismatic), card 2 is already Prismatic, card 3 is plain
    assert engine.kept_for_upgrades(tokens, {1: 1, 2: 2}, [1, 2, 3]) == {"a"}
    assert engine.kept_for_upgrades(tokens, {1: 1}, [2, 3]) == set()  # doesn't own card 1 any more


def test_plural_rarity_names():
    assert [embeds.plural(t) for t in ("common", "rare", "epic", "legendary")] == [
        "commons", "rares", "epics", "legendaries"]


async def setup(cog, gid):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    a = FakeMember(gid * 10 + 1, guild)
    guild.members = {admin.id: admin, a.id: a}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    for name, rarity in (("Holo One", "rare"), ("Plain One", "rare"), ("Other", "common")):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=f"{name} | Show")
    ids = {d["name"]: int(k) for k, d in (await cog.config.guild(guild).pool()).items()}
    cog._rng = random.Random(gid)
    return guild, admin, a, channel, ids


async def set_state(cog, member, **fields):
    state = await cog._member_state(member)
    for k, v in fields.items():
        setattr(state, k, v)
    await cog._save_member_state(member, state)


@pytest.mark.asyncio
async def test_sell_rares_skips_the_ones_saved_for_a_prismatic(cog):
    guild, _ad, a, channel, ids = await setup(cog, 901)
    await set_state(cog, a, collection=[ids["Holo One"], ids["Plain One"]], finishes={ids["Holo One"]: 1},
                    sell_tokens=[SellToken("h1", ids["Holo One"], "rare", 0.0), SellToken("h2", ids["Holo One"], "rare", 0.0),
                                 SellToken("p1", ids["Plain One"], "rare", 0.0)])
    ctx = FakeCtx(a, guild, channel)
    await cog.card.commands["sell"].callback(cog, ctx)
    view = ctx.sent[-1].view
    assert "(2 kept for upgrades)" in ctx.sent[-1].embeds[0].description
    assert view.sell_buttons["rare"].label.startswith("Sell rares (1 ·")
    await view.sell_buttons["rare"].callback(TradeInter(a, channel))
    assert sorted(t.token_id for t in (await cog._member_state(a)).sell_tokens) == ["h1", "h2"]
    # still sellable on purpose, from the list
    view.sell_lists["rare"]._values = ["h1"]
    await view.sell_lists["rare"].callback(TradeInter(a, channel))
    assert [t.token_id for t in (await cog._member_state(a)).sell_tokens] == ["h2"]


@pytest.mark.asyncio
async def test_the_sell_screen_fits_discords_limits_with_every_rarity(cog):
    guild, _ad, a, channel, ids = await setup(cog, 902)
    tokens = [SellToken(f"{r}{i}", ids["Other"], r, 0.0) for r in ("common", "rare", "epic", "legendary") for i in range(30)]
    await set_state(cog, a, collection=[ids["Other"]], sell_tokens=tokens)
    view = views.SellView(cog, a)
    await view.start(FakeCtx(a, guild, channel))
    for mode in ("sell", "tradeup"):
        view.mode = mode
        await view._build()
        rows = {}
        for item in view.children:
            rows.setdefault(item.row, []).append(item)
            if hasattr(item, "options"):
                assert len(item.options) <= 25 and len(item.placeholder) <= 150
                assert all(len(o.label) <= 100 and len(o.description or "") <= 100 for o in item.options)
            else:
                assert len(item.label) <= 80
        assert max(rows) <= 4 and all(len(items) <= 5 for items in rows.values())
        assert all(len(items) == 1 for r, items in rows.items() if r)  # a dropdown fills its own row


@pytest.mark.asyncio
async def test_a_daily_duplicate_explains_the_token_and_points_at_the_upgrade(cog, monkeypatch):
    guild, _ad, a, channel, ids = await setup(cog, 903)
    await cog.config.guild(guild).daily_weights.set({"common": 0, "rare": 1, "epic": 0, "legendary": 0})
    await set_state(cog, a, collection=[ids["Holo One"], ids["Holo One"], ids["Plain One"], ids["Plain One"]])
    ctx = FakeCtx(a, guild, channel)
    await cog.card.commands["daily"].callback(cog, ctx)
    text = ctx.sent[-1].content
    assert "duplicate token" in text and "keep it to upgrade" in text and "`.card sell`" in text
    assert "enough duplicates to upgrade" in text and "50,000" in text
    assert "copy limit" not in text


@pytest.mark.asyncio
async def test_draw_results_list_who_can_upgrade_now(cog):
    from test_cardcollect_draw import ready_draw
    guild, admin, members, channel, drop, message, ids = await ready_draw(cog, 904, n_members=1, n_cards=1)
    winner = members[0]
    await set_state(cog, winner, collection=[ids[0], ids[0]])
    cog.register_pick(winner, message.id, 0)
    await cog._resolve_drop(drop)
    embed = message.embeds[-1]
    ready = next(f for f in embed.fields if f.name == "Upgrades ready")
    assert f"<@{winner.id}>" in ready.value and "Holo" in ready.value
    assert "duplicate token" in embed.description


@pytest.mark.asyncio
async def test_a_failed_payout_gives_the_tokens_back(cog, monkeypatch):
    guild, _ad, a, channel, ids = await setup(cog, 977)
    await set_state(cog, a, sell_tokens=[SellToken("x1", ids["Plain One"], "rare", 0.0)])

    async def broken(member, amount):
        raise RuntimeError("bank down")

    monkeypatch.setattr("cardcollect.cardcollect.bank.deposit_credits", broken)
    with pytest.raises(RuntimeError):
        await cog.sell_tokens(guild, a, ["x1"])
    assert [t.token_id for t in (await cog._member_state(a)).sell_tokens] == ["x1"]
