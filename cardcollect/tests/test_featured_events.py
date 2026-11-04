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
