"""2.6.0: the weekly featured series and limited event cards."""

import random

import pytest

from cardcollect import engine
from cardcollect.models import Card
from cardcollect.settings_panel import PanelError
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


# --- engine ----------------------------------------------------------------------


def test_weeks_start_on_monday():
    assert engine.week_start("2026-11-02") == "2026-11-02"  # a Monday
    assert engine.week_start("2026-11-08") == "2026-11-02"  # the Sunday after
    assert engine.week_start("2026-11-09") == "2026-11-09"


def test_the_featured_pick_avoids_recent_series_while_others_are_left():
    sets = {"A": [], "B": [], "C": []}
    for seed in range(50):
        assert engine.pick_featured(sets, ["A", "B"], random.Random(seed)) == "C"
    assert engine.pick_featured(sets, ["A", "B", "C"], random.Random(1)) in sets  # all used: any of them
    assert engine.pick_featured({}, [], random.Random(1)) is None


def test_event_cards_only_roll_while_their_event_is_live():
    events = {"wonderween": {"name": "Wonderween", "start": "2026-10-20", "end": "2026-10-31"}}
    card = Card(1, "Witch", "S", "rare", "", event="Wonderween")
    assert engine.event_status(events["wonderween"], "2026-10-19") == "upcoming"
    assert engine.can_roll(card, events, "2026-10-20") and engine.can_roll(card, events, "2026-10-31")
    assert not engine.can_roll(card, events, "2026-11-01")
    assert not engine.can_roll(card, {}, "2026-10-25")  # its event was removed
    assert not engine.can_roll(Card(2, "Old", "S", "rare", "", retired=True), events, "2026-10-25")
    assert engine.can_roll(Card(3, "Plain", "S", "rare", ""), events, "2026-11-01")


def test_boosts_cover_the_featured_series_and_event_cards():
    pool = [Card(1, "a", "Hero", "rare", ""), Card(2, "b", "Other", "rare", ""),
            Card(3, "c", "Hero", "rare", "", event="E")]
    assert engine.roll_boosts(pool, "Hero", 3.0) == {1: 3.0, 3: 3.0}
    assert engine.roll_boosts(pool, "Hero", 3.0, 3.0) == {1: 3.0, 3: 9.0}
    assert engine.roll_boosts(pool, None, 3.0) == {}


# --- the cog ---------------------------------------------------------------------


async def setup(cog, gid, series=("Hero", "Villain")):
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    guild.members = {admin.id: admin}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    await cog.config.guild(guild).channel_id.set(channel.id)
    for name in series:
        for i in range(3):
            ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
            await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series=f"{name}{i} | {name}")
    cog._rng = random.Random(gid)
    return guild, admin, channel


def at(monkeypatch, day):
    monkeypatch.setattr(engine, "today_str", lambda tz, now=None: day)


@pytest.mark.asyncio
async def test_a_series_is_featured_once_a_week_and_announced(cog, monkeypatch):
    guild, _admin_, channel = await setup(cog, 701)
    at(monkeypatch, "2026-11-03")
    first = await cog.featured_series(guild)
    assert first in ("Hero", "Villain")
    assert "featured series" in channel.sent[-1].content and first in channel.sent[-1].content
    posts = len(channel.sent)
    at(monkeypatch, "2026-11-08")
    assert await cog.featured_series(guild) == first and len(channel.sent) == posts  # same week: no new pick
    at(monkeypatch, "2026-11-09")
    assert await cog.featured_series(guild) not in (None, first)  # next week: the other one
    boosts = await cog._roll_boosts(guild)
    assert len(boosts) == 3 and set(boosts.values()) == {3.0}


@pytest.mark.asyncio
async def test_the_featured_series_can_be_turned_off_or_rerolled(cog, monkeypatch):
    guild, admin, channel = await setup(cog, 702)
    at(monkeypatch, "2026-11-03")
    first = await cog.featured_series(guild)
    second = await cog._apply_featured_reroll(guild, admin, None)
    assert first not in second and "featured" in channel.sent[-1].content
    await cog._apply_featured_enabled(guild, admin, False)
    assert await cog.featured_series(guild) is None and await cog._roll_boosts(guild) == {}
    with pytest.raises(PanelError):
        await cog._apply_featured_reroll(guild, admin, None)


@pytest.mark.asyncio
async def test_chat_in_a_new_week_triggers_the_pick_only_once(cog, monkeypatch):
    guild, _admin_, channel = await setup(cog, 703)
    at(monkeypatch, "2026-11-03")
    await cog._maybe_roll_featured(guild)
    await cog._maybe_roll_featured(guild)
    assert sum("featured series" in (m.content or "") for m in channel.sent) == 1


@pytest.mark.asyncio
async def test_an_event_tags_a_series_and_its_cards_only_roll_during_it(cog, monkeypatch):
    guild, admin, _channel = await setup(cog, 704)
    msg = await cog._apply_event_save(guild, admin, None, {"name": "Wonderween", "start": "2026-10-20",
                                                           "end": "2026-10-31", "series": "villain"}, None)
    assert "Tagged 3 cards" in msg
    at(monkeypatch, "2026-11-05")
    assert {c.series for c in await cog._rollable_pool_cards(guild)} == {"Hero"}
    at(monkeypatch, "2026-10-25")
    assert {c.series for c in await cog._rollable_pool_cards(guild)} == {"Hero", "Villain"}
    # in packs event cards weigh 3x; in drops they don't
    await cog._apply_featured_enabled(guild, admin, False)  # isolate the event boost
    pack = await cog._roll_boosts(guild, for_pack=True)
    pool = {c.card_id: c for c in await cog._pool_cards(guild)}
    assert pack and all(pool[cid].event == "Wonderween" and w == 3.0 for cid, w in pack.items())
    assert await cog._roll_boosts(guild) == {}  # drops and dailies: no event boost
    # removing the event makes them regular cards again
    await cog._apply_event_remove(guild, admin, "wonderween")
    at(monkeypatch, "2026-11-05")
    assert {c.series for c in await cog._rollable_pool_cards(guild)} == {"Hero", "Villain"}


@pytest.mark.asyncio
async def test_event_input_is_checked(cog):
    guild, admin, _channel = await setup(cog, 705)
    with pytest.raises(PanelError, match="date"):
        await cog._apply_event_save(guild, admin, None, {"name": "X", "start": "soon", "end": "2026-01-01"}, None)
    with pytest.raises(PanelError, match="before it starts"):
        await cog._apply_event_save(guild, admin, None, {"name": "X", "start": "2026-02-01", "end": "2026-01-01"}, None)
    await cog._apply_event_save(guild, admin, None, {"name": "X", "start": "2026-01-01", "end": "2026-01-02"}, None)
    with pytest.raises(PanelError, match="already"):
        await cog._apply_event_save(guild, admin, None, {"name": "x", "start": "2026-01-01", "end": "2026-01-02"}, None)
    with pytest.raises(PanelError, match="no card has"):
        await cog._apply_event_save(guild, admin, None, {"name": "Y", "start": "2026-01-01", "end": "2026-01-02",
                                                         "series": "Nope"}, None)
    edited = await cog._apply_event_save(guild, admin, "x", {"start": "2026-03-01", "end": "2026-03-05"}, None)
    assert "2026-03-01 to 2026-03-05" in edited


@pytest.mark.asyncio
async def test_a_single_card_can_be_made_an_event_card(cog, monkeypatch):
    guild, admin, _channel = await setup(cog, 706)
    await cog._apply_event_save(guild, admin, None, {"name": "Winter", "start": "2026-12-01", "end": "2026-12-31"}, None)
    card = (await cog._pool_cards(guild))[0]
    tagged = await cog.apply_card_event(guild, card.card_id, "winter")
    assert tagged.event == "Winter"
    at(monkeypatch, "2026-11-05")
    assert card.card_id not in {c.card_id for c in await cog._rollable_pool_cards(guild)}
    member = FakeMember(7061, guild)
    embed, *_ = await cog.card_detail(guild, member, tagged)
    assert any(f.name == "Event card" and "drops when it starts" in f.value for f in embed.fields)
    assert (await cog.apply_card_event(guild, card.card_id, "")).event == ""


@pytest.mark.asyncio
async def test_the_admin_event_dropdown_has_no_empty_values(cog, monkeypatch):
    from cardcollect import views
    guild, admin, channel = await setup(cog, 707)
    await cog._apply_event_save(guild, admin, None, {"name": "Winter", "start": "2026-12-01", "end": "2026-12-31"}, None)
    card = (await cog._pool_cards(guild))[0]
    view = views.FindView(cog, admin, str(card.card_id))
    await view.start(FakeCtx(admin, guild, channel))
    values = [o.value for o in view.event_select.options]
    assert all(values) and len(values) == 2  # Discord rejects an empty option value
    view.event_select._values = [values[1]]
    await view._on_event(_Inter(admin, channel))
    assert (await cog._card_by_id(guild, card.card_id)).event == "Winter"
    view.event_select._values = [values[0]]
    await view._on_event(_Inter(admin, channel))
    assert (await cog._card_by_id(guild, card.card_id)).event == ""


@pytest.mark.asyncio
async def test_tagging_an_event_redraws_a_cached_animated_card(cog, monkeypatch):
    from cardcollect import imagegen
    guild, admin, _channel = await setup(cog, 708)
    await cog._apply_event_save(guild, admin, None, {"name": "Winter", "start": "2026-12-01", "end": "2026-12-31"}, None)
    card = (await cog._pool_cards(guild))[0]
    calls = []
    real = imagegen.render_single
    monkeypatch.setattr(imagegen, "render_single", lambda *a, **k: calls.append(1) or real(*a, **k))
    art = cog._read_card_image(guild, card.card_id)
    await cog.render_card_file(guild, card, art, 1, 2)
    tagged = await cog.apply_card_event(guild, card.card_id, "winter")
    await cog.render_card_file(guild, tagged, art, 1, 2)
    assert len(calls) == 2  # the event badge is part of the picture, so the cached GIF can't be reused


def test_diagnostics_show_the_legendary_rate_and_economy():
    from cardcollect import embeds
    stats = {"drops": 40, "cards_offered": 120, "picks": 90, "cards_unpicked": 4, "contested": 9,
             "legendaries_offered": 4, "drops_legend_tracked": 36, "packs": 7, "upgrades": 2, "trades": 1}
    embed = embeds.diagnostics_embed({}, 0.02, 900, 0, "G", stats)
    text = "\n".join(f.value for f in embed.fields)
    assert "about 1 in 9.0 drops" in text and "Packs opened: **7**" in text


class _Inter:
    def __init__(self, user, channel):
        from test_trade import TradeInter
        self.__dict__.update(TradeInter(user, channel).__dict__)
        self.guild = channel.guild

    async def edit_original_response(self, **kwargs):
        self.original_edit = kwargs
