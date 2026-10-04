"""Behaviour tests for WonderPet against fakes (redbot faked, discord real or stubbed).

Proves: card posting, once-a-day care limits, treats charging the bank, hatching, the warning ladder
(each level announced once, pings only on the serious ones), loss -> memorial -> new egg, daily repost,
restart forgiveness, staff-only commands and server-life reactions. It does NOT prove live Discord
behaviour (real buttons, select menus, attachments, permissions).
"""
import asyncio
import importlib
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from wonderpet import _testkit as tk

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=LA).timestamp()   # Saturday noon, after the 9am daily post
H = 3600
STAFF = 1556159237451681803


@pytest.fixture
def mod(monkeypatch, tmp_path):
    tk.install(monkeypatch, data_path=tmp_path)
    tk.install_ui(monkeypatch)
    for name in ("wonderpet.wonderpet", "wonderpet.embeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    m = importlib.import_module("wonderpet.wonderpet")
    yield m
    for name in ("wonderpet.wonderpet", "wonderpet.embeds"):
        sys.modules.pop(name, None)


GUILD = types.SimpleNamespace(id=1)   # same id the fake guild uses


class Member:
    def __init__(self, uid, roles=()):
        self.id, self.bot, self.roles, self.guild = uid, False, list(roles), GUILD

    @property
    def mention(self):
        return f"<@{self.id}>"


class Interaction:
    def __init__(self, guild, member):
        self.guild, self.user = guild, member
        self.sent = []
        outer = self

        class Resp:
            async def send_message(self, text=None, **kw):
                outer.sent.append((text, kw))

        self.response = Resp()

    @property
    def text(self):
        return self.sent[-1][0]


class PetGuild(tk.FakeGuild):
    pass


@pytest.fixture
async def env(mod):  # async: real discord.py Views need a running loop
    g = PetGuild()
    chan = g.add_channel(100, "cuddle")
    ping = g.add_role(900, "Pet Watch")
    bank = tk.FakeBank()
    bot = tk.FakeBot(g)
    bot.add_view = lambda v: None
    bot.is_owner = lambda m: _false()
    mod.bank = bank
    cog = mod.WonderPet(bot)
    cog.render_delay = 0
    clock = types.SimpleNamespace(t=T0)
    cog._now = lambda: clock.t
    e = types.SimpleNamespace(mod=mod, cog=cog, guild=g, chan=chan, ping=ping, bank=bank, clock=clock)
    return e


async def _false():
    return False


async def _setup(env, pet_stage=None):
    gconf = env.cog.config.guild(env.guild)
    await gconf.channel_id.set(env.chan.id)
    await gconf.ping_role_id.set(env.ping.id)
    s = await gconf.all()
    await env.cog._new_egg(env.guild, s, env.clock.t, announce=False)
    if pet_stage:
        pet = await gconf.pet()
        pet["stage"] = pet_stage
        await gconf.pet.set(pet)


async def _pet(env):
    return await env.cog.config.guild(env.guild).pet()


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def texts(env):
    return [m.content for m in env.chan.sent if m.content]


async def test_newegg_posts_one_card_with_buttons(env):
    await _setup(env)
    cards = [m for m in env.chan.sent if m.embed is not None]
    assert len(cards) == 1 and cards[0].kw["view"] is env.cog.view
    s = await env.cog.config.guild(env.guild).all()
    assert s["card_message_id"] == cards[0].id and s["pet"]["stage"] == "egg"


async def test_each_free_action_once_per_member_per_day(env):
    await _setup(env, "baby")
    a, b = Member(1), Member(2)
    ia = Interaction(env.guild, a)
    await env.cog.handle_action(ia, "feed")
    assert "fed" in ia.text
    await env.cog.handle_action(ia, "feed")
    assert "already" in ia.text
    await env.cog.handle_action(ia, "play")          # a different action is still available
    assert "played" in ia.text
    ib = Interaction(env.guild, b)
    await env.cog.handle_action(ib, "feed")          # another member can feed too
    assert "fed" in ib.text
    pet = await _pet(env)
    assert pet["carers"] == {"1": 2, "2": 1}
    week = (await env.cog.config.guild(env.guild).all())["week_carers"]
    assert list(week.values()) == [{"1": 2, "2": 1}]
    env.clock.t += 24 * H                             # tomorrow it resets
    await env.cog.handle_action(ia, "feed")
    assert "fed" in ia.text


async def test_card_redraw_is_batched_not_per_click(env):
    await _setup(env, "baby")
    card = [m for m in env.chan.sent if m.embed is not None][0]
    edits = []
    orig = card.edit

    async def spy(**kw):
        edits.append(kw)
        await orig(**kw)

    card.edit = spy
    for uid in range(1, 6):
        await env.cog.handle_action(Interaction(env.guild, Member(uid)), "feed")
    await _settle()
    await asyncio.sleep(0.01)
    assert 1 <= len(edits) <= 2


async def test_egg_hatches_after_enough_love_and_time(env):
    await _setup(env)
    for uid in range(1, 7):
        await env.cog.handle_action(Interaction(env.guild, Member(uid)), "feed")
    assert (await _pet(env))["stage"] == "egg"        # six taps but it is brand new
    env.clock.t += 13 * H
    await env.cog.handle_action(Interaction(env.guild, Member(99)), "play")
    pet = await _pet(env)
    assert pet["stage"] == "baby"
    assert any("hatched" in t for t in texts(env))


async def test_treat_costs_coins_and_boosts_more_than_free_care(env):
    await _setup(env, "baby")
    m = Member(5)
    env.bank.balances[5] = 10_000
    pet = await _pet(env)
    pet["hunger"] = 10.0
    await env.cog.config.guild(env.guild).pet.set(pet)
    ia = Interaction(env.guild, m)
    await env.cog.handle_treat(ia, "snack")
    assert env.bank.withdrawals == [(5, 2000)] and (await _pet(env))["hunger"] == 40.0
    await env.cog.handle_treat(ia, "feast")        # 8,000 needed, only 8,000 left: allowed
    assert env.bank.withdrawals[-1] == (5, 8000) and (await _pet(env))["clean"] >= 80


async def test_treat_refused_without_funds_and_changes_nothing(env):
    await _setup(env, "baby")
    ia = Interaction(env.guild, Member(5))
    before = await _pet(env)
    await env.cog.handle_treat(ia, "snack")
    assert "need" in ia.text and env.bank.withdrawals == []
    after = await _pet(env)
    assert after["care_points"] == before["care_points"]
    assert (await env.cog.config.member(Member(5)).daily()) == {}   # nothing was recorded for the failed purchase


async def test_treat_daily_cap_and_not_for_eggs(env):
    await _setup(env)
    ia = Interaction(env.guild, Member(5))
    env.bank.balances[5] = 100_000
    await env.cog.handle_treat(ia, "snack")
    assert "aren't available" in ia.text and env.bank.withdrawals == []
    pet = await _pet(env)
    pet["stage"] = "baby"
    await env.cog.config.guild(env.guild).pet.set(pet)
    for _ in range(3):
        await env.cog.handle_treat(ia, "snack")
    assert len(env.bank.withdrawals) == 3
    await env.cog.handle_treat(ia, "snack")
    assert "already" in ia.text and len(env.bank.withdrawals) == 3


async def test_warning_ladder_announces_each_level_once_and_pings_only_serious(env):
    await _setup(env, "baby")
    pet = await _pet(env)
    pet.update(hunger=100.0, happy=100.0, clean=100.0, last_tick_ts=env.clock.t)
    await env.cog.config.guild(env.guild).pet.set(pet)
    for _ in range(120):                      # a long quiet stretch, ticking every simulated hour
        env.clock.t += H
        await env.cog._tick(env.guild, env.clock.t)
    said = texts(env)
    pings = [t for t in said if t.startswith("<@&900>")]
    assert sum("feeling" in t for t in said) == 1                       # worried: once, no ping
    assert not any("feeling" in t for t in pings)
    assert len(pings) == 3                                              # sick, critical, final
    assert sum("passed away" in t or "ran away" in t for t in said) == 1
    s = await env.cog.config.guild(env.guild).all()
    assert not s["pet"] and s["history"][-1]["end"] in ("died", "ran_away")
    assert s["card_message_id"] is None
    assert [m for m in env.chan.sent if m.embed is not None and not m.deleted] == []


async def test_new_egg_arrives_a_day_after_losing_the_pet(env):
    await _setup(env, "baby")
    pet = await _pet(env)
    pet.update(hunger=0.0, happy=0.0, clean=0.0, neglect_hours=71.5, weak="hunger", last_tick_ts=env.clock.t)
    await env.cog.config.guild(env.guild).pet.set(pet)
    env.clock.t += H
    await env.cog._tick(env.guild, env.clock.t)
    assert not (await _pet(env))
    env.clock.t += 23 * H
    await env.cog._tick(env.guild, env.clock.t)
    assert not (await _pet(env))
    env.clock.t += 2 * H
    await env.cog._tick(env.guild, env.clock.t)
    pet = await _pet(env)
    assert pet and pet["stage"] == "egg" and pet["id"] == 2
    assert any("new egg" in t for t in texts(env))


async def test_a_restart_gap_does_not_hurt_the_pet(env):
    await _setup(env, "baby")
    pet = await _pet(env)
    pet.update(hunger=100.0, happy=100.0, clean=100.0, last_tick_ts=env.clock.t)
    await env.cog.config.guild(env.guild).pet.set(pet)
    env.clock.t += 5 * 24 * H                 # bot was down for five days
    await env.cog._tick(env.guild, env.clock.t)
    pet = await _pet(env)
    assert pet["alive"] and pet["neglect_hours"] == 0 and pet["hunger"] > 80


async def test_card_is_reposted_fresh_once_a_day_after_the_hour(env):
    await _setup(env, "baby")
    gconf = env.cog.config.guild(env.guild)
    old = (await gconf.all())["card_message_id"]
    env.clock.t += 24 * H                     # next day, noon: past the 9am hour
    await env.cog._tick(env.guild, env.clock.t)
    new = (await gconf.all())["card_message_id"]
    assert new != old and [m for m in env.chan.sent if m.id == old][0].deleted
    await env.cog._tick(env.guild, env.clock.t + 600)
    assert (await gconf.all())["card_message_id"] == new      # same day: no second repost


async def test_before_the_daily_hour_the_card_is_only_edited(env):
    await _setup(env, "baby")
    gconf = env.cog.config.guild(env.guild)
    old = (await gconf.all())["card_message_id"]
    early = datetime(2026, 10, 11, 7, 0, tzinfo=LA).timestamp()
    await env.cog._tick(env.guild, early)
    assert (await gconf.all())["card_message_id"] == old


async def test_only_staff_can_change_things(env):
    await _setup(env)
    sent = []

    class Ctx:
        guild = env.guild

        def __init__(self, author):
            self.author, self.sent = author, sent

        async def send(self, text=None, **kw):
            sent.append(text)

    fn = getattr(env.mod.WonderPet.pet_hour, "callback", env.mod.WonderPet.pet_hour)
    await fn(env.cog, Ctx(Member(1)), 7)
    assert "Only Staff" in sent[-1] and (await env.cog.config.guild(env.guild).daily_hour()) == 9
    staff = Member(2, roles=[types.SimpleNamespace(id=STAFF)])
    await fn(env.cog, Ctx(staff), 7)
    assert (await env.cog.config.guild(env.guild).daily_hour()) == 7


async def test_newegg_needs_confirmation_when_a_pet_is_alive(env):
    await _setup(env, "baby")
    sent = []
    staff = Member(2, roles=[types.SimpleNamespace(id=STAFF)])

    class Ctx:
        guild, author = env.guild, staff

        async def send(self, text=None, **kw):
            sent.append(text)

    fn = getattr(env.mod.WonderPet.pet_newegg, "callback", env.mod.WonderPet.pet_newegg)
    await fn(env.cog, Ctx(), "")
    assert "confirm" in sent[-1] and (await _pet(env))["stage"] == "baby"
    await fn(env.cog, Ctx(), "confirm")
    s = await env.cog.config.guild(env.guild).all()
    assert s["pet"]["stage"] == "egg" and s["history"][-1]["end"] == "replaced"


async def test_server_life_reactions_are_rate_limited_and_cheer_the_pet_up(env):
    await _setup(env, "baby")
    pet = await _pet(env)
    pet["happy"] = 50.0
    await env.cog.config.guild(env.guild).pet.set(pet)
    await env.cog.on_wonder_birthday(env.guild, [Member(7)])
    assert (await _pet(env))["happy"] == 55.0 and any("birthday" in t for t in texts(env))
    n = len(texts(env))
    await env.cog.on_wonder_anniversary(env.guild, [Member(8)])
    assert len(texts(env)) == n                              # within three hours: stays quiet
    env.clock.t += 4 * H
    await env.cog.on_wonder_event_start(env.guild, "Movie Night")
    assert any("Movie Night" in t for t in texts(env))


async def test_eggs_ignore_server_life(env):
    await _setup(env)
    await env.cog.on_wonder_birthday(env.guild, [Member(7)])
    assert not any("birthday" in t for t in texts(env))


async def test_data_deletion_removes_a_user_everywhere(env):
    await _setup(env, "baby")
    await env.cog.handle_action(Interaction(env.guild, Member(5)), "feed")
    await env.cog.red_delete_data_for_user(requester="user", user_id=5)
    s = await env.cog.config.guild(env.guild).all()
    assert s["pet"]["carers"] == {} and all("5" not in w for w in s["week_carers"].values())


async def test_card_embed_shows_weekly_carers_and_time_left(env):
    await _setup(env, "baby")
    await env.cog.handle_action(Interaction(env.guild, Member(5)), "feed")
    s = await env.cog.config.guild(env.guild).all()
    embed, file = await env.cog._card(env.guild, s, env.clock.t)
    fields = {f.name: f.value for f in embed.fields}
    assert "<@5>" in fields["Top carers this week"] and file is None
    s["pet"].update(neglect_hours=40.0, warn_level=3)
    s["pet"].update(hunger=5.0, happy=5.0, clean=5.0)
    embed, _ = await env.cog._card(env.guild, s, env.clock.t)
    assert any("Time left" in f.name for f in embed.fields)
