"""2.12.0: the trade board -- have/want listings with persistent buttons."""

import time

import pytest

from cardcollect import views
from test_cardcollect_smoke import cog  # noqa: F401  (fixture)
from test_trade import TradeInter, set_state, setup


class ListingInter(TradeInter):
    def __init__(self, user, channel, message):
        super().__init__(user, channel)
        self.message = message


async def post(cog, guild, member, channel, have="", want="Emilia"):
    return await cog.post_listing(guild, member, have, want, channel)


@pytest.mark.asyncio
async def test_a_listing_posts_to_the_trade_channel_with_its_buttons(cog):
    guild, a, b, channel, ids = await setup(cog, 1201)
    from test_cardcollect_smoke import FakeChannel
    board = FakeChannel(120199, guild)
    await cog._apply_trade_channel(guild, a, board)
    await set_state(cog, a, collection=[ids["Rem"], ids["Ram"]])
    reply = await post(cog, guild, a, channel, have="Rem, Ram", want="any Emilia")
    assert board.mention in reply
    message = board.sent[-1]
    assert isinstance(message.view, views.ListingView)
    embed = message.embeds[0]
    assert "Rem" in embed.fields[0].value and "any Emilia" in embed.fields[1].value
    listings = await cog.config.guild(guild).listings()
    assert next(iter(listings.values()))["message_id"] == message.id


@pytest.mark.asyncio
async def test_offering_opens_a_trade_with_the_listed_cards_already_on_the_table(cog):
    guild, a, b, channel, ids = await setup(cog, 1202)
    await set_state(cog, a, collection=[ids["Rem"], ids["Ram"]])
    await post(cog, guild, a, channel, have="Rem, Ram")
    listing_msg = channel.sent[-1]
    await set_state(cog, a, collection=[ids["Rem"]])  # traded Ram away since posting
    press = ListingInter(b, channel, listing_msg)
    await cog.listing_offer(press)
    trade_msg = channel.sent[-1]
    trade = trade_msg.view
    assert isinstance(trade, views.TradeView) and trade.a is b and trade.b is a
    assert trade.offers[a.id]["cards"] == [ids["Rem"]]
    assert a.mention in trade_msg.content


@pytest.mark.asyncio
async def test_posters_cant_offer_on_their_own_listing_and_only_they_or_admins_close_it(cog):
    guild, a, b, channel, ids = await setup(cog, 1203)
    await set_state(cog, a, collection=[ids["Rem"]])
    await post(cog, guild, a, channel, have="Rem")
    msg = channel.sent[-1]
    own = ListingInter(a, channel, msg)
    await cog.listing_offer(own)
    assert "your own listing" in own.response.messages[-1][0]
    other = ListingInter(b, channel, msg)
    await cog.listing_close(other)
    assert "Only the person" in other.response.messages[-1][0]
    mine = ListingInter(a, channel, msg)
    await cog.listing_close(mine)
    assert "closed" in mine.response.edited["embed"].description
    late = ListingInter(b, channel, msg)
    await cog.listing_offer(late)
    assert "closed" in late.response.messages[-1][0]


@pytest.mark.asyncio
async def test_listings_expire_and_a_new_one_replaces_the_old(cog):
    guild, a, b, channel, ids = await setup(cog, 1204)
    await set_state(cog, a, collection=[ids["Rem"]])
    await post(cog, guild, a, channel, have="Rem")
    first = channel.sent[-1]
    assert "old one was closed" in await post(cog, guild, a, channel, want="Ram")
    listings = await cog.config.guild(guild).listings()
    assert [l["open"] for l in listings.values()] == [False, True]
    second = channel.sent[-1]
    async with cog.config.guild(guild).listings() as stored:
        for l in stored.values():
            l["created"] = time.time() - 4 * 86400
    press = ListingInter(b, channel, second)
    await cog.listing_offer(press)
    assert "expired" in press.response.messages[-1][0]
    assert first is not second


@pytest.mark.asyncio
async def test_a_listing_needs_something_and_only_your_own_cards(cog):
    guild, a, _b, channel, ids = await setup(cog, 1205)
    assert "at least one" in await post(cog, guild, a, channel, want="")
    assert "don't have" in await post(cog, guild, a, channel, have="Emilia")


def test_the_listing_form_fits_discords_limits():
    for box in (views.ListingModal.have, views.ListingModal.want):
        assert len(box.label) <= 45 and len(box.placeholder or "") <= 100
