"""The 2.0.0 card game through the real cog code: the pick-your-card draw,
owner priority and pity, the wishlist, series sets, card requests, and the
`.card` / `.card find` / `.card sell` views. Fake discord objects come from
test_cardcollect_smoke."""

import asyncio
import random

import discord
import pytest

import cardcollect.cardcollect as cc_module
from cardcollect import views
from cardcollect.constants import PICK_BUTTON_PREFIX, SET_BONUS_BASE, SET_BONUS_PER_CARD, WISHLIST_MAX
from cardcollect.models import MemberState
from test_cardcollect_smoke import (
    AdminInteraction,
    FakeAttachment,
    FakeChannel,
    FakeCtx,
    FakeGuild,
    FakeHTTPResponse,
    FakeInteraction,
    FakeMember,
    FakeMessage,
    _admin,
    cog,  # noqa: F401  (fixture)
    fake_art_bytes,
)


async def add_cards(cog, guild, admin, channel, names, rarity="common", series="Anime"):
    ids = []
    for name in names:
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=f"{name} | {series}")
    pool = await cog.config.guild(guild).pool()
    by_name = {d["name"]: int(k) for k, d in pool.items()}
    return [by_name[n] for n in names]


async def ready_draw(cog, gid, n_members=3, n_cards=2, is_test=False, series="Anime"):
    """A guild with `n_cards` pool cards and a drop posted that shows all of them."""
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    members = [FakeMember(gid * 10 + 1 + i, guild) for i in range(n_members)]
    guild.members = {m.id: m for m in [admin, *members]}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    ids = await add_cards(cog, guild, admin, channel, [f"Card{i}" for i in range(n_cards)], series=series)
    conf = cog.config.guild(guild)
    await conf.drop_size.set(n_cards)
    await conf.draw_window_seconds.set(600)
    cog._rng = random.Random(gid)
    assert await cog._post_drop(channel, guild, is_test=is_test) is None
    message = channel.sent[-1]
    drop = cog.active_drops[message.id]
    return guild, admin, members, channel, drop, message, ids


def card_at(drop, position):
    return drop.card_at(position)["card_id"]


async def state_of(cog, guild, member):
    return await cog._member_state_by_id(guild, member.id)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --- the drop and the picks ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_drop_has_one_numbered_pick_button_per_card_and_no_claim_flow(cog):
    guild, admin, members, channel, drop, message, ids = await ready_draw(cog, 301, n_cards=3)
    assert [b.custom_id for b in message.view.children] == [f"{PICK_BUTTON_PREFIX}{i}" for i in range(3)]
    assert message.files and "draw closes" in message.content
    assert len(drop.cards) == 3 and drop.picks == {}
    assert not hasattr(drop, "wrong_guesses")


@pytest.mark.asyncio
async def test_picking_records_the_pick_and_changing_it_moves_it(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 302)
    first = await cog.handle_pick(a, message.id, 0)
    assert "You're in the draw" in first and drop.picks == {a.id: 0}
    again = await cog.handle_pick(a, message.id, 0)
    assert "already in" in again
    moved = await cog.handle_pick(a, message.id, 1)
    assert "Switched" in moved and drop.picks == {a.id: 1}
    assert len(drop.picks) == 1, "one pick per member"


@pytest.mark.asyncio
async def test_pick_counts_are_never_shown_in_the_private_reply(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 303)
    await cog.handle_pick(b, message.id, 0)
    reply = await cog.handle_pick(a, message.id, 0)
    assert "picked" not in reply.lower() and "others" not in reply.lower()


@pytest.mark.asyncio
async def test_a_pick_on_an_unknown_or_closed_drop_is_refused(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 304)
    assert await cog.handle_pick(a, 424242, 0) == cc_module.MSG_OVER
    drop.closes_at = 0.0  # the window has passed
    assert await cog.handle_pick(a, message.id, 0) == cc_module.MSG_OVER
    assert drop.picks == {}


@pytest.mark.asyncio
async def test_a_pick_for_a_card_number_that_does_not_exist_is_refused(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 305)
    assert await cog.handle_pick(a, message.id, 4) == cc_module.MSG_OVER


@pytest.mark.asyncio
async def test_picking_a_card_you_own_warns_privately_about_the_priority_rule(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 306)
    state = MemberState(collection=[card_at(drop, 0)])
    await cog._save_member_state_by_id(guild, a.id, state)
    assert "only win a spare" in await cog.handle_pick(a, message.id, 0)
    await cog._save_member_state_by_id(guild, a.id, MemberState(collection=[card_at(drop, 0)] * 2))
    assert "copy limit" in await cog.handle_pick(a, message.id, 0)


@pytest.mark.asyncio
async def test_the_button_press_records_before_it_acknowledges(cog):
    """A slow acknowledgement must not lose a pick made before the draw closed."""
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 307)
    inter = FakeInteraction(a, channel, message=message, defer_delay=5)
    task = asyncio.ensure_future(message.view.children[1].callback(inter))
    await asyncio.sleep(0)
    assert drop.picks == {a.id: 1}, "recorded before the acknowledgement finished"
    await task
    assert inter.response.deferred is True  # ephemeral acknowledgement
    assert "You're in the draw" in inter.followup.messages[-1][0]


@pytest.mark.asyncio
async def test_a_press_after_a_restart_gets_a_friendly_reply(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 308)
    persistent = views.DrawView(cog, None)
    assert len(persistent.children) == 5 and persistent.timeout is None
    cog.active_drops.clear()  # as after a restart
    inter = FakeInteraction(a, channel, message=message)
    await persistent.children[0].callback(inter)
    assert "over" in inter.followup.messages[-1][0].lower()


@pytest.mark.asyncio
async def test_cog_load_registers_the_persistent_pick_and_review_views(cog):
    await cog.cog_load()
    kinds = {type(v) for v in cog.bot.added_views}
    assert views.DrawView in kinds and views.RequestReviewView in kinds
    assert [v.timeout for v in cog.bot.added_views if isinstance(v, views.DrawView)] == [None]


# --- resolving the draw ----------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_awards_each_card_edits_the_drop_in_place_and_wastes_unpicked(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 310, n_cards=3)
    await cog.handle_pick(a, message.id, 0)
    await cog.handle_pick(b, message.id, 1)
    sent_before = len(channel.sent)
    results = await cog._resolve_drop(drop)
    assert [r["winner"] for r in results] == [a.id, b.id, None]
    assert (await state_of(cog, guild, a)).collection == [card_at(drop, 0)]
    assert (await state_of(cog, guild, b)).collection == [card_at(drop, 1)]
    assert (await state_of(cog, guild, c)).collection == []
    assert len(channel.sent) == sent_before, "results replace the drop message; no new message"
    assert message.view is None or message.edits >= 1
    assert message.embeds and "Draw" in message.content
    assert message.id not in cog.active_drops
    stats = await cog.config.guild(guild).stats()
    assert (stats["drops"], stats["cards_offered"], stats["picks"], stats["cards_unpicked"]) == (1, 3, 2, 1)


@pytest.mark.asyncio
async def test_resolving_twice_awards_once(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 311, n_cards=1)
    await cog.handle_pick(a, message.id, 0)
    await asyncio.gather(cog._resolve_drop(drop), cog._resolve_drop(drop))
    assert (await state_of(cog, guild, a)).collection == [card_at(drop, 0)]


@pytest.mark.asyncio
async def test_a_pick_after_the_draw_started_is_not_counted(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 312, n_cards=1)
    await cog.handle_pick(a, message.id, 0)
    await cog._resolve_drop(drop)
    assert await cog.handle_pick(b, message.id, 0) == cc_module.MSG_OVER
    assert (await state_of(cog, guild, b)).collection == []


@pytest.mark.asyncio
async def test_a_non_owner_beats_an_owner_for_the_same_card(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 313, n_cards=1)
    cid = card_at(drop, 0)
    await cog._save_member_state_by_id(guild, a.id, MemberState(collection=[cid]))
    await cog.handle_pick(a, message.id, 0)
    await cog.handle_pick(b, message.id, 0)
    results = await cog._resolve_drop(drop)
    assert results[0]["winner"] == b.id and results[0]["outcome"] == "new"
    assert (await state_of(cog, guild, a)).collection == [cid], "the owner keeps their copy, nothing more"
    assert (await state_of(cog, guild, a)).pity == 0, "losing on priority doesn't build pity"


@pytest.mark.asyncio
async def test_an_owner_alone_wins_a_spare_and_at_the_limit_a_sell_token(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 314, n_cards=2)
    c0, c1 = card_at(drop, 0), card_at(drop, 1)
    await cog._save_member_state_by_id(guild, a.id, MemberState(collection=[c0]))
    await cog._save_member_state_by_id(guild, b.id, MemberState(collection=[c1, c1]))
    await cog.handle_pick(a, message.id, 0)
    await cog.handle_pick(b, message.id, 1)
    results = await cog._resolve_drop(drop)
    assert results[0]["outcome"] == "duplicate" and results[1]["outcome"] == "sell_token"
    assert (await state_of(cog, guild, a)).collection == [c0, c0]
    sb = await state_of(cog, guild, b)
    assert sb.collection == [c1, c1] and len(sb.sell_tokens) == 1 and sb.sell_tokens[0].card_id == c1


@pytest.mark.asyncio
async def test_pity_grows_for_a_lost_lottery_and_resets_on_the_next_win(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 315, n_cards=1)
    await cog.handle_pick(a, message.id, 0)
    await cog.handle_pick(b, message.id, 0)
    results = await cog._resolve_drop(drop)
    winner, loser = (a, b) if results[0]["winner"] == a.id else (b, a)
    assert results[0]["contested"] is True
    assert (await state_of(cog, guild, loser)).pity == 1
    assert (await state_of(cog, guild, winner)).pity == 0
    assert (await cog.config.guild(guild).stats())["contested"] == 1
    # the loser sees the bonus when they pick again
    cog.active_drops.clear()
    cog._rng = random.Random(1)
    await cog._post_drop(channel, guild, is_test=False)
    message2 = channel.sent[-1]
    reply = await cog.handle_pick(loser, message2.id, 0)
    assert "Pity bonus" in reply and "1.25" in reply


@pytest.mark.asyncio
async def test_a_test_drop_runs_the_draw_but_awards_nothing(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 316, n_cards=1, is_test=True)
    reply = await cog.handle_pick(a, message.id, 0)
    assert "Test drop" in reply
    results = await cog._resolve_drop(drop)
    assert results[0]["winner"] == a.id
    assert (await state_of(cog, guild, a)).collection == []
    assert (await cog.config.guild(guild).stats()).get("drops", 0) == 0
    assert "Test" in message.content


@pytest.mark.asyncio
async def test_results_are_posted_as_a_new_message_if_the_drop_message_was_deleted(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 317, n_cards=1)
    await cog.handle_pick(a, message.id, 0)

    async def gone(**kwargs):
        raise discord.NotFound(FakeHTTPResponse(404, "Not Found"), "deleted")

    message.edit = gone
    sent_before = len(channel.sent)
    await cog._resolve_drop(drop)
    assert len(channel.sent) == sent_before + 1 and channel.sent[-1].embeds
    assert (await state_of(cog, guild, a)).collection == [card_at(drop, 0)]


@pytest.mark.asyncio
async def test_the_drop_closes_by_itself_when_the_window_ends(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 318, n_cards=1)
    drop.task.cancel()
    await cog.handle_pick(a, message.id, 0)
    await asyncio.wait_for(cog._close_drop_later(drop, 0.01), 2)
    assert (await state_of(cog, guild, a)).collection == [card_at(drop, 0)]


# --- the wishlist -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_wishlist_toggle_add_remove_owned_and_cap(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 320, n_cards=2)
    c0, c1 = ids
    assert "Added" in await cog.toggle_wish(guild, a, c0)
    assert (await state_of(cog, guild, a)).wishlist == [c0]
    assert "Took" in await cog.toggle_wish(guild, a, c0)
    await cog._save_member_state_by_id(guild, a.id, MemberState(collection=[c1]))
    assert "already own" in await cog.toggle_wish(guild, a, c1)
    assert "no longer" in await cog.toggle_wish(guild, a, 9999)
    await cog._save_member_state_by_id(guild, b.id, MemberState(wishlist=list(range(1000, 1000 + WISHLIST_MAX))))
    assert "full" in await cog.toggle_wish(guild, b, c0)


@pytest.mark.asyncio
async def test_winning_a_wished_card_takes_it_off_the_wishlist(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 321, n_cards=1)
    cid = card_at(drop, 0)
    await cog.toggle_wish(guild, a, cid)
    await cog.handle_pick(a, message.id, 0)
    await cog._resolve_drop(drop)
    state = await state_of(cog, guild, a)
    assert state.collection == [cid] and state.wishlist == []


@pytest.mark.asyncio
async def test_wish_pings_name_wishers_but_never_owners_or_opted_out_members(cog):
    guild = FakeGuild(322)
    admin = _admin(3220, guild)
    wishers = [FakeMember(3221 + i, guild) for i in range(3)]
    guild.members = {m.id: m for m in [admin, *wishers]}
    channel = FakeChannel(32200, guild)
    cog.bot.register_channel(channel)
    (cid,) = await add_cards(cog, guild, admin, channel, ["Frieren"])
    w1, w2, w3 = wishers
    await cog._save_member_state_by_id(guild, w1.id, MemberState(wishlist=[cid]))
    await cog._save_member_state_by_id(guild, w2.id, MemberState(wishlist=[cid], wish_pings_off=True))
    await cog._save_member_state_by_id(guild, w3.id, MemberState(wishlist=[cid], collection=[cid]))  # owns it
    await cog.config.guild(guild).drop_size.set(1)
    await cog._post_drop(channel, guild, is_test=False)
    await settle()
    reply = channel.sent[-1]
    assert reply.content and "Frieren" in reply.content and w1.mention in reply.content
    assert w2.mention not in reply.content and w3.mention not in reply.content
    assert getattr(reply, "replied_to", None) is not None


@pytest.mark.asyncio
async def test_wish_pings_cap_the_names_and_count_the_rest(cog):
    guild = FakeGuild(323)
    admin = _admin(3230, guild)
    many = [FakeMember(3231 + i, guild) for i in range(14)]
    guild.members = {m.id: m for m in [admin, *many]}
    channel = FakeChannel(32300, guild)
    cog.bot.register_channel(channel)
    (cid,) = await add_cards(cog, guild, admin, channel, ["Fern"])
    for m in many:
        await cog._save_member_state_by_id(guild, m.id, MemberState(wishlist=[cid]))
    await cog.config.guild(guild).drop_size.set(1)
    await cog._post_drop(channel, guild, is_test=False)
    await settle()
    text = channel.sent[-1].content
    assert text.count("<@") == 10 and "+4 more" in text


@pytest.mark.asyncio
async def test_no_ping_message_when_nobody_wished_for_the_cards(cog):
    guild, admin, members, channel, drop, message, ids = await ready_draw(cog, 324)
    await settle()
    assert channel.sent[-1] is message


# --- series sets ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completing_a_series_set_pays_a_bonus_once(cog):
    guild = FakeGuild(330)
    admin = _admin(3300, guild)
    a = FakeMember(3301, guild)
    guild.members = {3300: admin, 3301: a}
    channel = FakeChannel(33000, guild)
    cog.bot.register_channel(channel)
    ids = await add_cards(cog, guild, admin, channel, ["One", "Two", "Three"], series="Frieren")
    paid = cc_module.bank.deposit_credits.calls
    paid.clear()
    await cog._save_member_state_by_id(guild, a.id, MemberState(collection=ids[:2]))
    # win the third through the draw
    await cog.config.guild(guild).drop_size.set(1)
    cog._rng = random.Random(3)
    ok = None
    for _ in range(20):  # the drop rolls one of three cards; keep going until it's the missing one
        await cog._post_drop(channel, guild, is_test=False)
        msg = next(m for m in reversed(channel.sent) if m.id in cog.active_drops)
        drop = cog.active_drops[msg.id]
        if card_at(drop, 0) == ids[2]:
            ok = (drop, msg)
            break
        await cog._resolve_drop(drop)
    assert ok, "never rolled the missing card"
    drop, msg = ok
    await cog.handle_pick(a, msg.id, 0)
    await cog._resolve_drop(drop)
    state = await state_of(cog, guild, a)
    assert sorted(state.collection) == sorted(ids) and state.sets_claimed == ["Frieren"]
    assert paid == [(a, SET_BONUS_BASE + 3 * SET_BONUS_PER_CARD)]
    assert any("Frieren" in (f.value or "") + (e.description or "") for e in msg.embeds for f in (e.fields or [None]) if f is not None) or "completed" in str(msg.embeds[0].to_dict())


@pytest.mark.asyncio
async def test_a_duplicate_never_pays_a_set_bonus_retroactively(cog):
    guild = FakeGuild(331)
    admin = _admin(3310, guild)
    a = FakeMember(3311, guild)
    guild.members = {3310: admin, 3311: a}
    channel = FakeChannel(33100, guild)
    cog.bot.register_channel(channel)
    ids = await add_cards(cog, guild, admin, channel, ["One", "Two", "Three"], series="Frieren")
    paid = cc_module.bank.deposit_credits.calls
    paid.clear()
    await cog._save_member_state_by_id(guild, a.id, MemberState(collection=list(ids)))  # already complete, never paid
    state = await state_of(cog, guild, a)
    card = await cog._card_by_id(guild, ids[0])
    outcome, _ = cog._grant_card(state, card, {})
    assert outcome == "duplicate"
    assert await cog._complete_set(guild, a, state, card, outcome) is None
    assert paid == []


@pytest.mark.asyncio
async def test_sets_need_three_cards_in_one_series(cog):
    guild = FakeGuild(332)
    admin = _admin(3320, guild)
    guild.members = {3320: admin}
    channel = FakeChannel(33200, guild)
    cog.bot.register_channel(channel)
    await add_cards(cog, guild, admin, channel, ["One", "Two"], series="Tiny")
    state = MemberState()
    embed = await cog.sets_embed(guild, state, "Your")
    assert "Tiny" not in (embed.description or "")


# --- card requests ---------------------------------------------------------------------


async def request_setup(cog, gid):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    asker = FakeMember(gid * 10 + 1, guild)
    guild.members = {admin.id: admin, asker.id: asker}
    review = FakeChannel(gid * 100, guild)
    other = FakeChannel(gid * 100 + 1, guild)
    cog.bot.register_channel(review)
    cog.bot.register_channel(other)
    await cog._apply_request_channel(guild, admin, review)
    return guild, admin, asker, review, other


@pytest.mark.asyncio
async def test_requests_are_closed_until_a_review_channel_is_set(cog):
    guild = FakeGuild(340)
    asker = FakeMember(3401, guild)
    assert "aren't open" in await cog.submit_request(guild, asker, "Frieren")


@pytest.mark.asyncio
async def test_a_request_lands_in_the_review_channel_with_buttons(cog):
    guild, admin, asker, review, other = await request_setup(cog, 341)
    reply = await cog.submit_request(guild, asker, "  Frieren   from Sousou no Frieren ")
    assert "Request sent" in reply
    post = review.sent[-1]
    assert post.embeds and isinstance(post.view, views.RequestReviewView)
    assert "Frieren from Sousou no Frieren" in str(post.embeds[0].to_dict())


@pytest.mark.asyncio
async def test_request_limits_duplicates_and_empty_text(cog):
    guild, admin, asker, review, other = await request_setup(cog, 342)
    assert "which character" in await cog.submit_request(guild, asker, " ")
    await cog.submit_request(guild, asker, "One")
    assert "already waiting" in await cog.submit_request(guild, asker, "ONE")
    await cog.submit_request(guild, asker, "Two")
    await cog.submit_request(guild, asker, "Three")
    assert "3 requests" in await cog.submit_request(guild, asker, "Four")


@pytest.mark.asyncio
async def test_only_admins_can_review_and_a_request_is_reviewed_once(cog):
    guild, admin, asker, review, other = await request_setup(cog, 343)
    await cog.submit_request(guild, asker, "Frieren")
    post = review.sent[-1]
    nope = AdminInteraction(asker, guild, message=post)
    await cog.review_request(nope, True)
    assert "Only admins" in nope.sent[0][0]
    yes = AdminInteraction(admin, guild, message=post)
    await cog.review_request(yes, True)
    assert yes.edits and yes.edits[0]["view"] is None
    again = AdminInteraction(admin, guild, message=post)
    await cog.review_request(again, False)
    assert "already approved" in again.sent[0][0]
    assert any("approved" in (d or "") for d in asker.dms)


@pytest.mark.asyncio
async def test_a_declined_request_is_never_wishlisted_later(cog):
    guild, admin, asker, review, other = await request_setup(cog, 344)
    await cog.submit_request(guild, asker, "Frieren")
    await cog.review_request(AdminInteraction(admin, guild, message=review.sent[-1]), False)
    (cid,) = await add_cards(cog, guild, admin, other, ["Frieren"])
    assert (await state_of(cog, guild, asker)).wishlist == []


@pytest.mark.asyncio
async def test_an_approved_request_wishlists_the_card_when_it_is_added(cog):
    guild, admin, asker, review, other = await request_setup(cog, 345)
    await cog.submit_request(guild, asker, "Frieren, Sousou no Frieren")
    await cog.review_request(AdminInteraction(admin, guild, message=review.sent[-1]), True)
    (cid,) = await add_cards(cog, guild, admin, other, ["Frieren"])
    assert (await state_of(cog, guild, asker)).wishlist == [cid]
    assert any("wishlist" in (d or "") for d in asker.dms)
    # and only once
    (cid2,) = await add_cards(cog, guild, admin, other, ["Frieren Two"])
    assert (await state_of(cog, guild, asker)).wishlist == [cid]


@pytest.mark.asyncio
async def test_the_request_channel_setting_can_be_cleared(cog):
    guild, admin, asker, review, other = await request_setup(cog, 346)
    assert await cog.config.guild(guild).request_channel_id() == review.id
    await cog._apply_request_channel(guild, admin, None)
    assert not await cog.config.guild(guild).request_channel_id()


# --- `.card find` -----------------------------------------------------------------------


async def find(cog, ctx_user, guild, channel, query):
    ctx = FakeCtx(ctx_user, guild, channel)
    await cog.card.commands["find"].callback(cog, ctx, query=query)
    return ctx.sent[-1]


@pytest.mark.asyncio
async def test_find_with_no_match_offers_a_request_button(cog):
    guild, admin, asker, review, other = await request_setup(cog, 350)
    msg = await find(cog, asker, guild, other, "Nobody")
    labels = [getattr(c, "label", None) for c in msg.view.children]
    assert "Request this" in labels and "Nothing in the pool" in msg.embeds[0].title
    inter = AdminInteraction(asker, guild, message=msg)
    button = next(c for c in msg.view.children if getattr(c, "label", None) == "Request this")
    await button.callback(inter)
    assert isinstance(inter.modal, views.RequestModal) and inter.modal.text.default == "Nobody"


@pytest.mark.asyncio
async def test_find_with_several_matches_lists_them_and_opens_one(cog):
    guild, admin, asker, review, other = await request_setup(cog, 351)
    await _seed(cog, guild, admin, other)
    msg = await find(cog, asker, guild, other, "cyberpunk")
    view = msg.view
    assert view.card is None and "matches" in msg.embeds[0].title
    select = view.opener
    assert len(select.options) == 2
    select._values = [select.options[0].value]
    await view._on_open(AdminInteraction(asker, guild, message=msg))
    assert view.card is not None and view.from_list


async def _seed(cog, guild, admin, channel):
    from test_cardcollect_smoke import _seed_search_pool
    return await _seed_search_pool(cog, guild, admin, channel)


@pytest.mark.asyncio
async def test_find_with_one_match_opens_the_card_with_a_wishlist_button_for_non_owners(cog):
    guild, admin, asker, review, other = await request_setup(cog, 352)
    ids = await _seed(cog, guild, admin, other)
    msg = await find(cog, asker, guild, other, "nami")
    view = msg.view
    assert view.card.name == "Nami" and msg.files
    assert "Add to wishlist" in view.personal.label.replace("☆ ", "")
    inter = AdminInteraction(asker, guild, message=msg)
    await view._on_wish(inter)
    assert (await state_of(cog, guild, asker)).wishlist == [ids["Nami"]]
    assert "wishlist" in view.personal.label.lower() and "remove" in view.personal.label.lower()


@pytest.mark.asyncio
async def test_find_detail_for_an_owner_offers_the_showcase_button(cog):
    guild, admin, asker, review, other = await request_setup(cog, 353)
    ids = await _seed(cog, guild, admin, other)
    await cog._save_member_state_by_id(guild, asker.id, MemberState(collection=[ids["Nami"]]))
    msg = await find(cog, asker, guild, other, "nami")
    assert "showcase" in msg.view.personal.label.lower()
    await msg.view._on_showcase(AdminInteraction(asker, guild, message=msg))
    assert (await state_of(cog, guild, asker)).showcase_card_ids == [ids["Nami"]]


@pytest.mark.asyncio
async def test_find_admin_controls_exist_only_for_admins_and_work(cog):
    guild, admin, asker, review, other = await request_setup(cog, 354)
    ids = await _seed(cog, guild, admin, other)
    plain = await find(cog, asker, guild, other, "nami")
    assert not hasattr(plain.view, "rarity")
    msg = await find(cog, admin, guild, other, "nami")
    view = msg.view
    assert view.rarity and view.replace_art and view.retire
    view.rarity._values = ["legendary"]
    await view._on_rarity(AdminInteraction(admin, guild, message=msg))
    assert (await cog._card_by_id(guild, ids["Nami"])).rarity == "legendary"
    await view._on_retire(AdminInteraction(admin, guild, message=msg))
    assert (await cog._card_by_id(guild, ids["Nami"])).retired is True
    assert view.retire.label.startswith("Restore")
    await view._on_retire(AdminInteraction(admin, guild, message=msg))
    assert (await cog._card_by_id(guild, ids["Nami"])).retired is False


@pytest.mark.asyncio
async def test_find_buttons_refuse_other_members(cog):
    guild, admin, asker, review, other = await request_setup(cog, 355)
    await _seed(cog, guild, admin, other)
    msg = await find(cog, asker, guild, other, "nami")
    stranger = AdminInteraction(FakeMember(999001, guild), guild, message=msg)
    assert await msg.view.interaction_check(stranger) is False


@pytest.mark.asyncio
async def test_find_mine_scope_only_lists_owned_cards(cog):
    guild, admin, asker, review, other = await request_setup(cog, 356)
    ids = await _seed(cog, guild, admin, other)
    await cog._save_member_state_by_id(guild, asker.id, MemberState(collection=[ids["Rebecca"]]))
    msg = await find(cog, asker, guild, other, "e")
    view = msg.view
    await view._on_scope(AdminInteraction(asker, guild, message=msg))
    assert [c.name for c in view.cards] == ["Rebecca"]


# --- `.card` collection tabs -------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_with_nothing_owned_shows_an_embed_and_tabs(cog):
    guild, admin, asker, review, other = await request_setup(cog, 360)
    await _seed(cog, guild, admin, other)
    ctx = FakeCtx(asker, guild, other)
    await cog.card.callback(cog, ctx)
    msg = ctx.sent[-1]
    assert msg.embeds and "No cards yet" in msg.embeds[0].description
    assert [c.label for c in msg.view.children if c.label] == ["Collection", "Sets", "★ Wishlist"]


@pytest.mark.asyncio
async def test_card_shows_the_gallery_and_the_tabs_switch_it(cog):
    guild, admin, asker, review, other = await request_setup(cog, 361)
    ids = await _seed(cog, guild, admin, other)
    await cog._save_member_state_by_id(guild, asker.id, MemberState(collection=[ids["Nami"]], wishlist=[ids["Lucy"]]))
    ctx = FakeCtx(asker, guild, other)
    await cog.card.callback(cog, ctx)
    msg = ctx.sent[-1]
    assert msg.files, "the collection gallery image"
    view = msg.view
    wish = await view.render("wishlist")
    assert wish["embed"] is not None and "Lucy" in str(wish["embed"].to_dict())
    assert view.pings is not None and view.remover is not None
    sets = await view.render("sets")
    assert sets["embed"] is not None and sets["file"] is None


@pytest.mark.asyncio
async def test_wishlist_tab_pings_toggle_and_remover_work(cog):
    guild, admin, asker, review, other = await request_setup(cog, 362)
    ids = await _seed(cog, guild, admin, other)
    await cog._save_member_state_by_id(guild, asker.id, MemberState(wishlist=[ids["Lucy"], ids["Nami"]]))
    view = views.CollectionView(cog, asker.id, asker, own=True)
    await view.render("wishlist")
    await view._on_pings(AdminInteraction(asker, guild))
    assert (await state_of(cog, guild, asker)).wish_pings_off is True
    view.remover._values = [str(ids["Lucy"])]
    await view._on_remove(AdminInteraction(asker, guild))
    assert (await state_of(cog, guild, asker)).wishlist == [ids["Nami"]]


@pytest.mark.asyncio
async def test_someone_elses_collection_has_no_wishlist_tab(cog):
    guild, admin, asker, review, other = await request_setup(cog, 363)
    await _seed(cog, guild, admin, other)
    ctx = FakeCtx(admin, guild, other)
    await cog.card.callback(cog, ctx, member=asker)
    labels = [c.label for c in ctx.sent[-1].view.children if c.label]
    assert "★ Wishlist" not in labels


@pytest.mark.asyncio
async def test_collection_buttons_refuse_everyone_but_the_invoker(cog):
    guild, admin, asker, review, other = await request_setup(cog, 364)
    view = views.CollectionView(cog, asker.id, asker, own=True)
    assert await view.interaction_check(AdminInteraction(admin, guild)) is False
    assert await view.interaction_check(AdminInteraction(asker, guild)) is True


# --- `.card sell` ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_with_no_tokens_says_so_and_has_no_buttons(cog):
    guild, admin, asker, review, other = await request_setup(cog, 370)
    ctx = FakeCtx(asker, guild, other)
    await cog.card.commands["sell"].callback(cog, ctx)
    assert "No sell tokens" in ctx.sent[-1].content and ctx.sent[-1].view is None


async def tokens_for(cog, guild, member, rarities):
    from cardcollect import engine

    state = MemberState(sell_tokens=[engine.make_sell_token(1000 + i, r) for i, r in enumerate(rarities)])
    await cog._save_member_state_by_id(guild, member.id, state)
    return state.sell_tokens


@pytest.mark.asyncio
async def test_sell_all_pays_out_every_token(cog):
    guild, admin, asker, review, other = await request_setup(cog, 371)
    await tokens_for(cog, guild, asker, ["common", "rare"])
    prices = await cog.config.guild(guild).sell_prices()
    paid = cc_module.bank.deposit_credits.calls
    paid.clear()
    ctx = FakeCtx(asker, guild, other)
    await cog.card.commands["tokens"].callback(cog, ctx) if "tokens" in cog.card.commands else await cog.card.commands["sell"].callback(cog, ctx)
    view = ctx.sent[-1].view
    await view._on_sell_all(AdminInteraction(asker, guild, message=ctx.sent[-1]))
    assert paid == [(asker, prices["common"] + prices["rare"])]
    assert (await state_of(cog, guild, asker)).sell_tokens == []


@pytest.mark.asyncio
async def test_sell_one_sells_just_the_chosen_token(cog):
    guild, admin, asker, review, other = await request_setup(cog, 372)
    tokens = await tokens_for(cog, guild, asker, ["common", "epic"])
    ctx = FakeCtx(asker, guild, other)
    await cog.card.commands["sell"].callback(cog, ctx)
    view = ctx.sent[-1].view
    view.one._values = [tokens[1].token_id]
    await view._on_sell_one(AdminInteraction(asker, guild, message=ctx.sent[-1]))
    left = (await state_of(cog, guild, asker)).sell_tokens
    assert [t.token_id for t in left] == [tokens[0].token_id]


@pytest.mark.asyncio
async def test_sell_buttons_refuse_other_members(cog):
    guild, admin, asker, review, other = await request_setup(cog, 373)
    await tokens_for(cog, guild, asker, ["common"])
    ctx = FakeCtx(asker, guild, other)
    await cog.card.commands["sell"].callback(cog, ctx)
    assert await ctx.sent[-1].view.interaction_check(AdminInteraction(admin, guild)) is False


# --- consolidation and housekeeping --------------------------------------------------------------


def test_the_retired_commands_are_gone_and_the_new_aliases_exist(cog):
    commands = cog.card.commands
    for gone in ("quota", "version", "testmode", "testdrop", "setimage", "poolsearch_old", "info", "show", "pool"):
        assert gone not in commands, gone
    for kept in ("find", "sell", "daily", "give", "leaderboard", "showcase", "settings", "addcard", "importseries", "importpool",
                 "removecard", "resetpool", "viewpool", "diagnostics"):
        assert kept in commands, kept
    assert set(commands["find"].aliases) >= {"search", "info", "show", "pool", "poolsearch"}


def test_the_cog_is_version_2(cog):
    from cardcollect.constants import COG_VERSION

    assert COG_VERSION == "2.0.1"


@pytest.mark.asyncio
async def test_daily_pull_counts_in_the_diagnostics_stats_and_is_once_a_day(cog):
    guild, admin, asker, review, other = await request_setup(cog, 380)
    await add_cards(cog, guild, admin, other, ["Solo"])
    ctx = FakeCtx(asker, guild, other)
    await cog.card.commands["daily"].callback(cog, ctx)
    await cog.card.commands["daily"].callback(cog, ctx)
    assert "already pulled" in ctx.sent[-1].content
    daily = (await cog.config.guild(guild).stats())["daily_pulls"]
    assert sum(daily.values()) == 1
    assert (await state_of(cog, guild, asker)).collection


@pytest.mark.asyncio
async def test_diagnostics_reports_the_draw_counters(cog):
    guild, admin, (a, b, c), channel, drop, message, ids = await ready_draw(cog, 381, n_cards=1)
    await cog.handle_pick(a, message.id, 0)
    await cog._resolve_drop(drop)
    ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["diagnostics"].callback(cog, ctx)
    text = str([m.embeds[0].to_dict() for m in ctx.sent if m.embeds]) + str([m.content for m in ctx.sent])
    assert "pick" in text.lower()


@pytest.mark.asyncio
async def test_find_replace_art_swaps_only_the_picture(cog):
    from cardcollect import storage
    from test_cardcollect_smoke import FakeUpload, _inter, _queue_uploads

    guild, admin, asker, review, other = await request_setup(cog, 357)
    ids = await _seed(cog, guild, admin, other)
    msg = await find(cog, admin, guild, other, "nami")
    view = msg.view
    original = storage.read_card_image(cog.data_path, guild.id, ids["Nami"])
    _queue_uploads(cog, FakeUpload(admin, other, "", b"junk"))
    inter = _inter(admin, guild, other, msg)
    await view.replace_art.callback(inter)
    assert storage.read_card_image(cog.data_path, guild.id, ids["Nami"]) == original
    assert "isn't an image" in inter.followups[-1][0]
    mine = fake_art_bytes((7, 7, 7))
    _queue_uploads(cog, FakeUpload(admin, other, "", mine))
    inter = _inter(admin, guild, other, msg)
    await view.replace_art.callback(inter)
    assert storage.read_card_image(cog.data_path, guild.id, ids["Nami"]) == mine
    assert "New art saved" in inter.followups[-1][0] and view._listening is False
    pool = await cog.config.guild(guild).pool()
    assert pool[str(ids["Nami"])]["name"] == "Nami"
