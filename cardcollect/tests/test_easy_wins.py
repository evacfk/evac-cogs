"""2.9.0: who has a spare (on `.card find`), the FEATURED tag on drop and pack
cards, score and rank in the `.card` header, and the half-price pack on a
streak bonus day."""

import random

import pytest
from redbot.core import bank

from cardcollect import engine, imagegen, views
from cardcollect.models import Card
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


async def setup(cog, gid, n=3):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    a, b, c = (FakeMember(gid * 10 + i, guild, display_name=f"M{i}") for i in (1, 2, 3))
    guild.members = {m.id: m for m in (admin, a, b, c)}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    for i in range(n):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series=f"Card{i} | Hero")
    ids = sorted(int(k) for k in await cog.config.guild(guild).pool())
    cog._rng = random.Random(gid)
    return guild, admin, a, b, c, channel, ids


async def set_state(cog, member, **fields):
    state = await cog._member_state(member)
    for k, v in fields.items():
        setattr(state, k, v)
    await cog._save_member_state(member, state)


@pytest.mark.asyncio
async def test_a_card_page_lists_members_with_a_spare_to_trade(cog):
    guild, _ad, a, b, c, _ch, ids = await setup(cog, 1001)
    await set_state(cog, b, collection=[ids[0], ids[0]])  # has a spare
    await set_state(cog, c, collection=[ids[0]])  # only their own copy
    card = await cog._card_by_id(guild, ids[0])
    embed, *_ = await cog.card_detail(guild, a, card)
    field = next(f for f in embed.fields if f.name == "Spares to trade for")
    assert f"<@{b.id}>" in field.value and f"<@{c.id}>" not in field.value
    embed, *_ = await cog.card_detail(guild, b, card)  # your own spare isn't listed to you
    assert not any(f.name == "Spares to trade for" for f in embed.fields)


def test_featured_cards_get_a_tag_on_the_drop_image():
    card = Card(1, "A", "Hero", "rare", "")
    art = fake_art_bytes((20, 20, 60))
    plain = imagegen.render_card(card, art, number=1)
    tagged = imagegen.render_card(card, art, number=1, featured=True)
    assert plain.getpixel((30, 86)) != tagged.getpixel((30, 86))
    r, g, b = tagged.getpixel((30, 86))[:3]
    assert r > 200 and g > 160 and b < 60  # the gold tag (slightly see-through)


@pytest.mark.asyncio
async def test_the_drop_tags_this_weeks_featured_series(cog, monkeypatch):
    guild, _ad, _a, _b, _c, channel, ids = await setup(cog, 1002)
    await cog.config.guild(guild).channel_id.set(channel.id)
    seen = {}
    real = imagegen.render_drop

    def spy(entries, is_test=False, numbered=True, featured_ids=()):
        seen["ids"] = set(featured_ids)
        return real(entries, is_test, numbered, featured_ids)

    monkeypatch.setattr(imagegen, "render_drop", spy)
    assert await cog._post_drop(channel, guild, is_test=False) is None
    assert seen["ids"] and seen["ids"] <= set(ids)  # the only series, so it's featured


@pytest.mark.asyncio
async def test_the_collection_header_shows_score_and_rank(cog):
    guild, _ad, a, b, _c, channel, ids = await setup(cog, 1003)
    await set_state(cog, a, collection=[ids[0]])  # 3 points
    await set_state(cog, b, collection=[ids[0], ids[1]])  # 6 points
    assert await cog.standing(guild, a) == (3, 2, 2)
    ctx = FakeCtx(a, guild, channel)
    await cog.card.callback(cog, ctx)
    assert "score **3**" in ctx.sent[-1].content and "#2 of 2" in ctx.sent[-1].content


@pytest.mark.asyncio
async def test_a_streak_bonus_day_halves_todays_pack(cog, monkeypatch):
    guild, _ad, a, _b, _c, channel, _ids = await setup(cog, 1004, n=6)
    monkeypatch.setattr(engine, "today_str", lambda tz, now=None: "2026-11-07")
    await set_state(cog, a, daily_pull_date="2026-11-06", daily_streak=6)
    bank.balances[(guild.id, a.id)] = 100_000
    ctx = FakeCtx(a, guild, channel)
    await cog.card.commands["daily"].callback(cog, ctx)
    assert "half price" in ctx.sent[-1].content and "15,000" in ctx.sent[-1].view.button.label
    result = await cog.open_pack(guild, a)
    assert result["price"] == 15000 and bank.balances[(guild.id, a.id)] == 85_000


@pytest.mark.asyncio
async def test_an_ordinary_day_pays_full_price(cog, monkeypatch):
    guild, _ad, a, _b, _c, channel, _ids = await setup(cog, 1005, n=6)
    monkeypatch.setattr(engine, "today_str", lambda tz, now=None: "2026-11-07")
    await set_state(cog, a, daily_pull_date="2026-11-06", daily_streak=2)
    ctx = FakeCtx(a, guild, channel)
    await cog.card.commands["daily"].callback(cog, ctx)
    assert "30,000" in ctx.sent[-1].view.button.label and "half price" not in ctx.sent[-1].content


@pytest.mark.asyncio
async def test_reward_card_never_exceeds_the_cap_and_never_gives_legendary(cog):
    guild, admin, a, _b, _c, channel, ids = await setup(cog, 1010)
    for i, rarity in enumerate(("epic", "legendary")):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=f"High{i} | Hero")
    seen = set()
    for _ in range(60):
        got = await cog.reward_card(guild, a, "epic")
        assert got is not None
        seen.add(got["card"].rarity)
    assert seen <= {"common", "rare", "epic"}
    got = await cog.reward_card(guild, a, "rare")
    assert got["card"].rarity in ("common", "rare")


@pytest.mark.asyncio
async def test_reward_card_saves_the_card_and_turns_the_third_copy_into_a_sell_token(cog):
    guild, _ad, a, _b, _c, _ch, ids = await setup(cog, 1011, n=1)
    outcomes = [(await cog.reward_card(guild, a, "epic"))["outcome"] for _ in range(3)]
    state = await cog._member_state(a)
    assert state.collection == [ids[0], ids[0]]
    assert outcomes[2] == "sell_token" and len(state.sell_tokens) == 1


@pytest.mark.asyncio
async def test_reward_card_with_nothing_to_give_returns_none(cog):
    guild = FakeGuild(1012)
    m = FakeMember(12001, guild, display_name="M")
    assert await cog.reward_card(guild, m, "epic") is None  # empty pool
    assert await cog.reward_card(guild, m, "mythic") is None  # not a rarity
