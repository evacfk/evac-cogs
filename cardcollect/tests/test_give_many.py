"""`.card give @member 1,2,Rem`: several cards in one command, all or nothing."""

import pytest
from redbot.core import bank

from test_cardcollect_smoke import FakeCtx, cog  # noqa: F401  (fixture)
from test_trade import set_state, setup


async def give(cog, guild, a, b, channel, arg):
    ctx = FakeCtx(a, guild, channel)
    await cog.card.commands["give"].callback(cog, ctx, b, card_arg=arg)
    return ctx


@pytest.mark.asyncio
async def test_several_cards_move_in_one_command_by_number_and_name(cog):
    guild, a, b, channel, ids = await setup(cog, 701)
    await set_state(cog, a, collection=[ids["Rem"], ids["Ram"], ids["Emilia"]], finishes={ids["Emilia"]: 2})
    ctx = await give(cog, guild, a, b, channel, f"{ids['Rem']},{ids['Emilia']}, ram")
    assert (await cog._member_state(a)).collection == []
    receiver = await cog._member_state(b)
    assert sorted(receiver.collection) == sorted(ids.values()) and receiver.finish_of(ids["Emilia"]) == 2
    assert any("3 cards gifted" in (e.title or "") for m in ctx.sent for e in m.embeds)


@pytest.mark.asyncio
async def test_one_bad_entry_moves_nothing(cog):
    guild, a, b, channel, ids = await setup(cog, 702)
    await set_state(cog, a, collection=[ids["Rem"]])
    ctx = await give(cog, guild, a, b, channel, f"{ids['Rem']},{ids['Emilia']}")  # doesn't own Emilia
    assert "don't have" in ctx.sent[-1].content
    assert (await cog._member_state(a)).collection == [ids["Rem"]]
    assert (await cog._member_state(b)).collection == []


@pytest.mark.asyncio
async def test_the_same_card_twice_needs_two_copies(cog):
    guild, a, b, channel, ids = await setup(cog, 703)
    await set_state(cog, a, collection=[ids["Rem"]])
    ctx = await give(cog, guild, a, b, channel, f"{ids['Rem']},{ids['Rem']}")
    assert "copies" in ctx.sent[-1].content or "so many" in ctx.sent[-1].content
    assert (await cog._member_state(a)).collection == [ids["Rem"]]
    await set_state(cog, a, collection=[ids["Rem"], ids["Rem"]])
    await give(cog, guild, a, b, channel, f"{ids['Rem']},{ids['Rem']}")
    assert (await cog._member_state(b)).collection == [ids["Rem"], ids["Rem"]]


@pytest.mark.asyncio
async def test_a_card_over_the_copy_limit_becomes_a_token_and_a_set_still_pays(cog):
    guild, a, b, channel, ids = await setup(cog, 704)
    await set_state(cog, a, collection=[ids["Rem"], ids["Ram"], ids["Emilia"]])
    await set_state(cog, b, collection=[ids["Rem"], ids["Rem"]])
    before = bank.balances[(guild.id, b.id)]
    ctx = await give(cog, guild, a, b, channel, f"{ids['Rem']},{ids['Ram']},{ids['Emilia']}")
    receiver = await cog._member_state(b)
    assert len(receiver.sell_tokens) == 1  # Rem was past the limit
    assert bank.balances[(guild.id, b.id)] > before  # Re:Zero set completed
    assert any("completed" in m.content for m in ctx.sent if getattr(m, "content", None))
