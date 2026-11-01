"""2.5.0: `.card trade @member` -- offers by name or number, both sides
accepting, the all-at-once swap of cards and wondercoin, and finishes moving
with a traded last copy."""

import random

import pytest
from redbot.core import bank

from cardcollect import engine, views
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


class TradeInter(Inter):
    def __init__(self, user, channel):
        super().__init__(user, channel)
        self.done = False
        self.modal = None

        async def defer(ephemeral=False):
            self.done = True

        async def edit_message(content=None, embed=None, view=None, **kwargs):
            self.done = True
            self.response.edited = {"content": content, "embed": embed, "view": view}

        async def send_message(content=None, ephemeral=False, view=None, **kwargs):
            self.done = True
            self.response.messages.append((content, ephemeral))

        async def send_modal(modal):
            self.modal = modal

        self.response.defer = defer
        self.response.edit_message = edit_message
        self.response.send_message = send_message
        self.response.send_modal = send_modal
        self.response.is_done = lambda: self.done

    async def edit_original_response(self, content=None, embed=None, view=None, **kwargs):
        self.original_edit = {"content": content, "embed": embed, "view": view}


def test_an_offer_needs_a_copy_for_every_card_in_it():
    assert engine.missing_for_offer([1, 2, 2], [2, 2, 1]) == []
    assert engine.missing_for_offer([1, 2], [2, 2]) == [2]
    assert engine.missing_for_offer([], [5]) == [5]


async def setup(cog, gid):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    a, b = FakeMember(gid * 10 + 1, guild, display_name="Ann"), FakeMember(gid * 10 + 2, guild, display_name="Bo")
    guild.members = {m.id: m for m in (admin, a, b)}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    ids = {}
    for name, rarity in (("Rem", "epic"), ("Ram", "rare"), ("Emilia", "legendary")):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=f"{name} | Re:Zero")
    for cid, data in (await cog.config.guild(guild).pool()).items():
        ids[data["name"]] = int(cid)
    bank.balances[(guild.id, a.id)] = 10_000
    bank.balances[(guild.id, b.id)] = 10_000
    cog._rng = random.Random(gid)
    return guild, a, b, channel, ids


async def set_state(cog, member, **fields):
    state = await cog._member_state(member)
    for k, v in fields.items():
        setattr(state, k, v)
    await cog._save_member_state(member, state)


async def open_trade(cog, guild, a, b, channel):
    await cog.card.commands["trade"].callback(cog, FakeCtx(a, guild, channel), b)
    message = channel.sent[-1]
    return message.view


@pytest.mark.asyncio
async def test_offers_resolve_names_and_numbers_from_your_own_cards(cog):
    guild, a, _b, _c, ids = await setup(cog, 601)
    await set_state(cog, a, collection=[ids["Rem"], ids["Ram"], ids["Ram"]])
    assert await cog.resolve_offer(guild, a, f"rem, #{ids['Ram']}, {ids['Ram']}") == (
        [ids["Rem"], ids["Ram"], ids["Ram"]], None)
    cards, problem = await cog.resolve_offer(guild, a, "Emilia")
    assert cards == [] and "don't have" in problem
    cards, problem = await cog.resolve_offer(guild, a, "rem, rem")
    assert cards == [] and "copies" in problem
    cards, problem = await cog.resolve_offer(guild, a, "ra")  # Ram only; Rem doesn't contain "ra"
    assert cards == [ids["Ram"]] and problem is None


@pytest.mark.asyncio
async def test_both_accepting_swaps_cards_and_wondercoin_at_once(cog):
    guild, a, b, channel, ids = await setup(cog, 602)
    await set_state(cog, a, collection=[ids["Rem"]], finishes={ids["Rem"]: 2})
    await set_state(cog, b, collection=[ids["Ram"]])
    trade = await open_trade(cog, guild, a, b, channel)
    assert isinstance(trade, views.TradeView)

    await trade.set_offer(TradeInter(a, channel), a, "Rem", "")
    await trade.set_offer(TradeInter(b, channel), b, "Ram", "2,500")
    await trade.accept.callback(TradeInter(a, channel))
    assert (await cog._member_state(a)).collection == [ids["Rem"]]  # one accept isn't enough
    final = TradeInter(b, channel)
    await trade.accept.callback(final)

    sa, sb = await cog._member_state(a), await cog._member_state(b)
    assert sa.collection == [ids["Ram"]] and sb.collection == [ids["Rem"]]
    assert sb.finish_of(ids["Rem"]) == 2 and sa.finishes == {}  # the Prismatic went with the last copy
    assert bank.balances[(guild.id, a.id)] == 12_500 and bank.balances[(guild.id, b.id)] == 7_500
    assert "Trade complete" in final.original_edit["embed"].description and trade.done


@pytest.mark.asyncio
async def test_changing_an_offer_clears_both_accepts(cog):
    guild, a, b, channel, ids = await setup(cog, 603)
    await set_state(cog, a, collection=[ids["Rem"], ids["Ram"]])
    await set_state(cog, b, collection=[ids["Emilia"]])
    trade = await open_trade(cog, guild, a, b, channel)
    await trade.set_offer(TradeInter(a, channel), a, "Rem, Ram", "")
    await trade.set_offer(TradeInter(b, channel), b, "Emilia", "")
    await trade.accept.callback(TradeInter(b, channel))
    await trade.set_offer(TradeInter(a, channel), a, "Ram", "")  # sneaky swap after b said yes
    assert trade.accepted == set()
    await trade.accept.callback(TradeInter(a, channel))
    assert (await cog._member_state(b)).collection == [ids["Emilia"]]  # nothing swapped


@pytest.mark.asyncio
async def test_a_trade_that_can_no_longer_happen_changes_nothing(cog):
    guild, a, b, channel, ids = await setup(cog, 604)
    await set_state(cog, a, collection=[ids["Rem"]])
    await set_state(cog, b, collection=[ids["Ram"]])
    trade = await open_trade(cog, guild, a, b, channel)
    await trade.set_offer(TradeInter(a, channel), a, "Rem", "")
    await trade.set_offer(TradeInter(b, channel), b, "Ram", "9000")
    bank.balances[(guild.id, b.id)] = 100  # spent it elsewhere meanwhile
    await trade.accept.callback(TradeInter(a, channel))
    last = TradeInter(b, channel)
    await trade.accept.callback(last)
    assert "wondercoin" in last.original_edit["embed"].description and not trade.done
    assert (await cog._member_state(a)).collection == [ids["Rem"]]
    assert bank.balances[(guild.id, a.id)] == 10_000 and bank.balances[(guild.id, b.id)] == 100


@pytest.mark.asyncio
async def test_only_the_two_traders_can_use_it_and_bad_offers_are_refused(cog):
    guild, a, b, channel, ids = await setup(cog, 605)
    await set_state(cog, a, collection=[ids["Rem"]])
    trade = await open_trade(cog, guild, a, b, channel)
    outsider = FakeMember(99, guild)
    inter = TradeInter(outsider, channel)
    assert await trade.interaction_check(inter) is False
    bad = TradeInter(a, channel)
    await trade.set_offer(bad, a, "Rem", "lots")
    assert "whole number" in bad.response.messages[-1][0]
    empty = TradeInter(a, channel)
    await trade.accept.callback(empty)
    assert "Nothing" in empty.response.messages[-1][0]


@pytest.mark.asyncio
async def test_you_cant_trade_with_yourself_or_a_bot(cog):
    guild, a, _b, channel, _ids = await setup(cog, 606)
    ctx = FakeCtx(a, guild, channel)
    await cog.card.commands["trade"].callback(cog, ctx, a)
    assert "yourself" in ctx.sent[-1].content
    await cog.card.commands["trade"].callback(cog, ctx, FakeMember(7, guild, bot=True))
    assert "Bots" in ctx.sent[-1].content
