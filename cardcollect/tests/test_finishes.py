"""2.4.0: card finishes (Holo, Prismatic) -- the upgrade rules (engine), buying
an upgrade (cog), finishes moving with a gifted last copy, rendering (still and
animated, with the render cache), and the upgrade button on `.card find`."""

import io
import random

import pytest
from PIL import Image
from redbot.core import bank

from cardcollect import engine, imagegen, views
from cardcollect.constants import DEFAULT_UPGRADE_COSTS
from cardcollect.models import Card, MemberState, SellToken
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
from test_packs import Inter

COSTS = DEFAULT_UPGRADE_COSTS


def token(card_id, tid):
    return SellToken(tid, card_id, "rare", 0.0)


# --- engine ----------------------------------------------------------------------


def test_defaults_are_one_duplicate_and_50k_then_two_and_100k():
    assert COSTS == {"holo": {"dupes": 1, "coins": 50000}, "prismatic": {"dupes": 2, "coins": 100000}}


def test_upgrading_needs_the_card_and_enough_duplicates():
    assert not engine.upgrade_plan([], [], 7, 0, COSTS)["ok"]
    plan = engine.upgrade_plan([7], [], 7, 0, COSTS)
    assert not plan["ok"] and "needs 1 duplicate" in plan["reason"] and plan["finish"] == "holo"
    plan = engine.upgrade_plan([7], [token(8, "x")], 7, 0, COSTS)  # a token of another card doesn't count
    assert not plan["ok"]


def test_sell_tokens_are_spent_before_the_spare():
    plan = engine.upgrade_plan([7, 7], [token(7, "a")], 7, 0, COSTS)
    assert plan["ok"] and plan["token_ids"] == ["a"] and plan["spares"] == 0 and plan["coins"] == 50000
    plan = engine.upgrade_plan([7, 7], [token(7, "a")], 7, 1, COSTS)
    assert plan["ok"] and plan["finish"] == "prismatic" and plan["token_ids"] == ["a"] and plan["spares"] == 1


def test_prismatic_is_the_top():
    plan = engine.upgrade_plan([7, 7], [token(7, "a"), token(7, "b")], 7, 2, COSTS)
    assert not plan["ok"] and "top finish" in plan["reason"]


def test_member_state_keeps_finishes_only_for_owned_cards():
    state = MemberState.from_dict(MemberState(collection=[3], finishes={3: 2, 4: 1}).to_dict())
    assert state.finishes == {3: 2, 4: 1}
    assert state.finish_of(3) == 2 and state.finish_of(4) == 0  # 4 isn't owned


# --- the cog ---------------------------------------------------------------------


async def setup(cog, gid, balance=500_000):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    a, b = FakeMember(gid * 10 + 1, guild), FakeMember(gid * 10 + 2, guild)
    guild.members = {m.id: m for m in (admin, a, b)}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
    await cog.card.commands["addcard"].callback(cog, ctx, "epic", name_and_series="Shiny | Show")
    card_id = int(next(iter(await cog.config.guild(guild).pool())))
    bank.balances[(guild.id, a.id)] = balance
    cog._rng = random.Random(gid)
    return guild, admin, a, b, channel, card_id


async def give_state(cog, member, **fields):
    state = await cog._member_state(member)
    for key, value in fields.items():
        setattr(state, key, value)
    await cog._save_member_state(member, state)


@pytest.mark.asyncio
async def test_an_upgrade_spends_the_duplicate_and_the_wondercoin(cog):
    guild, _ad, a, _b, _c, cid = await setup(cog, 501)
    await give_state(cog, a, collection=[cid], sell_tokens=[token(cid, "t1")])
    result = await cog.upgrade_card(guild, a, cid)
    assert isinstance(result, dict) and result["finish"] == "holo"
    state = await cog._member_state(a)
    assert state.finish_of(cid) == 1 and state.sell_tokens == [] and state.collection == [cid]
    assert bank.balances[(guild.id, a.id)] == 450_000


@pytest.mark.asyncio
async def test_prismatic_uses_the_spare_when_tokens_run_out_and_keeps_one_copy(cog):
    guild, _ad, a, _b, _c, cid = await setup(cog, 502)
    await give_state(cog, a, collection=[cid, cid], sell_tokens=[token(cid, "t1")], finishes={cid: 1})
    result = await cog.upgrade_card(guild, a, cid)
    assert result["finish"] == "prismatic"
    state = await cog._member_state(a)
    assert state.collection == [cid] and state.sell_tokens == [] and state.finish_of(cid) == 2
    assert bank.balances[(guild.id, a.id)] == 400_000


@pytest.mark.asyncio
async def test_not_enough_wondercoin_changes_nothing(cog):
    guild, _ad, a, _b, _c, cid = await setup(cog, 503, balance=49_999)
    await give_state(cog, a, collection=[cid], sell_tokens=[token(cid, "t1")])
    result = await cog.upgrade_card(guild, a, cid)
    assert isinstance(result, str) and "50,000" in result
    state = await cog._member_state(a)
    assert state.finish_of(cid) == 0 and len(state.sell_tokens) == 1
    assert bank.balances[(guild.id, a.id)] == 49_999


@pytest.mark.asyncio
async def test_upgrade_costs_are_editable(cog):
    guild, admin, a, _b, _c, cid = await setup(cog, 504)
    await cog._apply_upgrade_costs(guild, admin, {"holo_dupes": "0", "holo_coins": "10", "prismatic_dupes": "3",
                                                  "prismatic_coins": "7"})
    await give_state(cog, a, collection=[cid])
    assert (await cog.upgrade_card(guild, a, cid))["coins"] == 10
    assert "needs 3 duplicates" in await cog.upgrade_card(guild, a, cid)


@pytest.mark.asyncio
async def test_giving_away_your_last_copy_takes_the_finish_with_it(cog):
    guild, _ad, a, b, channel, cid = await setup(cog, 505)
    await give_state(cog, a, collection=[cid], finishes={cid: 2}, showcase_card_ids=[cid])
    await cog.card.commands["give"].callback(cog, FakeCtx(a, guild, channel), b, card_arg=str(cid))
    giver, receiver = await cog._member_state(a), await cog._member_state(b)
    assert giver.collection == [] and giver.finishes == {} and giver.showcase_card_ids == []
    assert receiver.collection == [cid] and receiver.finish_of(cid) == 2


@pytest.mark.asyncio
async def test_giving_away_a_spare_leaves_the_finish_on_the_copy_you_keep(cog):
    guild, _ad, a, b, channel, cid = await setup(cog, 506)
    await give_state(cog, a, collection=[cid, cid], finishes={cid: 1})
    await cog.card.commands["give"].callback(cog, FakeCtx(a, guild, channel), b, card_arg=str(cid))
    assert (await cog._member_state(a)).finish_of(cid) == 1
    assert (await cog._member_state(b)).finish_of(cid) == 0


@pytest.mark.asyncio
async def test_a_receiver_keeps_the_better_finish(cog):
    guild, _ad, a, b, channel, cid = await setup(cog, 507)
    await give_state(cog, a, collection=[cid], finishes={cid: 1})
    await give_state(cog, b, collection=[cid], finishes={cid: 2})
    await cog.card.commands["give"].callback(cog, FakeCtx(a, guild, channel), b, card_arg=str(cid))
    assert (await cog._member_state(b)).finish_of(cid) == 2
    await give_state(cog, a, collection=[cid], finishes={cid: 2})
    await give_state(cog, b, collection=[cid, cid], finishes={cid: 1})  # at the limit: the gift becomes a token
    await cog.card.commands["give"].callback(cog, FakeCtx(a, guild, channel), b, card_arg=str(cid))
    receiver = await cog._member_state(b)
    assert len(receiver.sell_tokens) == 1 and receiver.finish_of(cid) == 2


# --- rendering ---------------------------------------------------------------------


def art():
    return fake_art_bytes((40, 60, 120))


def test_finishes_change_the_art_and_the_border_but_not_the_size():
    card = Card(1, "A", "Show", "epic", "")
    plain, holo, prism = (imagegen.render_card(card, art(), finish=f) for f in (0, 1, 2))
    assert plain.size == holo.size == prism.size == (400, 560)
    border = (200, 4)  # top edge, mid-border
    assert plain.getpixel(border)[:3] == imagegen.RARITY_COLORS["epic"]
    assert holo.getpixel(border)[:3] != imagegen.RARITY_COLORS["epic"]
    assert plain.getpixel((200, 200)) != holo.getpixel((200, 200)) != prism.getpixel((200, 200))


def test_a_prismatic_single_card_is_a_looping_animation_and_holo_stays_still():
    card = Card(1, "A", "Show", "epic", "")
    gif = imagegen.render_single(card, art(), 1, 2, animate=True)
    assert imagegen.is_gif(gif)
    img = Image.open(gif)
    assert img.n_frames == imagegen.ANIMATION_FRAMES and img.info.get("loop") == 0
    assert not imagegen.is_gif(imagegen.render_single(card, art(), 1, 1, animate=True))


def test_the_showcase_animates_only_when_a_card_in_it_is_prismatic():
    a, b = Card(1, "A", "S", "epic", ""), Card(2, "B", "S", "rare", "")
    assert not imagegen.is_gif(imagegen.render_showcase([(a, art()), (b, art())], finishes={1: 1}, animate=True))
    assert imagegen.is_gif(imagegen.render_showcase([(a, art()), (b, art())], finishes={1: 2}, animate=True))


@pytest.mark.asyncio
async def test_animated_renders_are_cached_until_the_art_changes(cog, monkeypatch):
    guild, _ad, _a, _b, _c, cid = await setup(cog, 508)
    card = await cog._card_by_id(guild, cid)
    calls = []
    real = imagegen.render_single

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(imagegen, "render_single", counting)
    first = await cog.render_card_file(guild, card, cog._read_card_image(guild, cid), 1, 2)
    second = await cog.render_card_file(guild, card, cog._read_card_image(guild, cid), 1, 2)
    assert first.filename.endswith(".gif") and len(calls) == 1
    await cog.replace_card_art(guild, cid, fake_art_bytes((10, 200, 10)))
    await cog.render_card_file(guild, card, cog._read_card_image(guild, cid), 1, 2)
    assert len(calls) == 2
    cached = list((cog.data_path / str(guild.id) / "render_cache").glob(f"{cid}-*.gif"))
    assert len(cached) == 1  # the render of the old art was cleared out
    assert second.filename.endswith(".gif")


# --- the button on a card page -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_card_page_offers_the_upgrade_and_confirming_shows_it_off(cog):
    guild, _ad, a, _b, channel, cid = await setup(cog, 509)
    await give_state(cog, a, collection=[cid, cid])
    view = views.FindView(cog, a, str(cid))
    await view.start(FakeCtx(a, guild, channel))
    assert view.upgrade.label == "Upgrade to Holo"

    press = Inter(a, channel)
    await view._on_upgrade(press)
    confirm = press.sent_views[-1]
    assert isinstance(confirm, views.UpgradeConfirmView)
    assert "50,000" in press.response.messages[-1][0] and "spare copy" in press.response.messages[-1][0]

    await confirm.confirm.callback(Inter(a, channel))
    assert (await cog._member_state(a)).finish_of(cid) == 1
    assert "upgraded" in channel.sent[-1].content and channel.sent[-1].files
    assert view.upgrade.label == "Upgrade to Prismatic"  # the page refreshed


@pytest.mark.asyncio
async def test_without_duplicates_the_button_explains_what_it_needs(cog):
    guild, _ad, a, _b, channel, cid = await setup(cog, 510)
    await give_state(cog, a, collection=[cid])
    view = views.FindView(cog, a, str(cid))
    await view.start(FakeCtx(a, guild, channel))
    press = Inter(a, channel)
    await view._on_upgrade(press)
    text, private = press.response.messages[-1]
    assert private and "needs 1 duplicate" in text and "50,000 wondercoin" in text
