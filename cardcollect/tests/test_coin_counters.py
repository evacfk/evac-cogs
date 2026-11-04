"""2.14.0: `.card diagnostics` counts the wondercoin this cog pays out and takes in, lists card requests, and attaches
the whole pool (it replaced `.card viewpool`); an event tag left on an old card no longer does anything."""

import pytest

from cardcollect import embeds
from cardcollect.constants import SET_BONUS_BASE, SET_BONUS_PER_CARD
from cardcollect.models import Card, MemberState, SellToken
from test_cardcollect_draw import add_cards, request_setup
from test_cardcollect_smoke import FakeCtx, cog  # noqa: F401  (fixture)
import test_finishes as finishes
import test_packs as packs


async def stats_of(cog, guild):
    return await cog.config.guild(guild).stats()


@pytest.mark.asyncio
async def test_an_upgrade_counts_its_wondercoin(cog):
    guild, _ad, a, _b, _c, cid = await finishes.setup(cog, 601)
    await finishes.give_state(cog, a, collection=[cid], sell_tokens=[finishes.token(cid, "t1")])
    await cog.upgrade_card(guild, a, cid)
    stats = await stats_of(cog, guild)
    assert stats["coin_upgrades"] == 50_000 and stats["coin_since"]


@pytest.mark.asyncio
async def test_a_pack_counts_what_it_cost(cog):
    guild, _admin, member, _channel = await packs.setup(cog, 602)
    await cog.open_pack(guild, member)
    assert (await stats_of(cog, guild))["coin_packs"] == 30_000


@pytest.mark.asyncio
async def test_selling_tokens_counts_what_was_paid(cog):
    guild, _ad, a, _b, _c, cid = await finishes.setup(cog, 603)
    await finishes.give_state(cog, a, collection=[cid], sell_tokens=[SellToken("t1", cid, "rare", 0.0)])
    sold, total = await cog.sell_tokens(guild, a)
    assert (sold, total) == (1, 5000)
    assert (await stats_of(cog, guild))["coin_token_sales"] == 5000


@pytest.mark.asyncio
async def test_a_set_bonus_counts_what_was_paid(cog):
    guild, admin, _asker, _review, channel = await request_setup(cog, 604)
    member = guild.members[admin.id]
    ids = await add_cards(cog, guild, admin, channel, ["One", "Two", "Three"], series="Frieren")
    state = MemberState(collection=list(ids))
    card = await cog._card_by_id(guild, ids[2])
    done = await cog._complete_set(guild, member, state, card, "new")
    assert done is not None
    assert (await stats_of(cog, guild))["coin_set_bonus"] == SET_BONUS_BASE + 3 * SET_BONUS_PER_CARD


def test_the_report_nets_paid_out_against_taken_in():
    stats = {"coin_set_bonus": 12_500, "coin_token_sales": 5_000, "coin_packs": 30_000, "coin_upgrades": 50_000,
             "coin_since": 1_700_000_000}
    embed = embeds.diagnostics_embed({}, 0.02, 900, 0, "G", stats)
    wondercoin = next(f.value for f in embed.fields if f.name.startswith("Wondercoin"))
    assert "17,500" in wondercoin and "80,000" in wondercoin and "-62,500" in wondercoin


def test_a_report_without_any_coin_counters_has_no_wondercoin_section():
    embed = embeds.diagnostics_embed({}, 0.02, 900, 0, "G", {"drops": 1})
    assert not any(f.name.startswith("Wondercoin") for f in embed.fields)


@pytest.mark.asyncio
async def test_diagnostics_attaches_the_pool_and_counts_requests(cog):
    guild, admin, asker, _review, channel = await request_setup(cog, 605)
    ids = await add_cards(cog, guild, admin, channel, ["Alpha", "Beta"], series="Show")
    await cog.set_card_retired(guild, ids[1], True)
    assert "Request sent" in await cog.submit_request(guild, asker, "Nami from One Piece")
    ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["diagnostics"].callback(cog, ctx)
    message = ctx.sent[-1]
    fields = {f.name: f.value for f in message.embeds[0].fields}
    assert "2** characters" in fields["Pool"] and "1 retired" in fields["Pool"]
    assert "1 pending" in fields["Card requests"]
    pool_file = next(f for f in message.files if f.filename == "cardcollect_pool.txt")
    pool_file.fp.seek(0)
    lines = pool_file.fp.read().decode().splitlines()
    assert len(lines) == 2 and lines[1].endswith("[retired]") and not lines[0].endswith("[retired]")


@pytest.mark.asyncio
async def test_an_old_event_tag_is_ignored_and_dropped_on_the_next_save(cog):
    guild, admin, _asker, _review, channel = await request_setup(cog, 606)
    ids = await add_cards(cog, guild, admin, channel, ["Alpha"], series="Show")
    async with cog.config.guild(guild).pool() as pool:
        pool[str(ids[0])]["event"] = "Wonderween 2026"  # what an event-tagged card looked like before 2.14.0
    rollable = await cog._rollable_pool_cards(guild)
    assert [c.card_id for c in rollable] == ids  # an event card outside its dates used to be skipped
    assert "event" not in rollable[0].to_dict()
    assert isinstance(rollable[0], Card)
