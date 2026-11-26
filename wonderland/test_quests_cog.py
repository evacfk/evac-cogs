"""Daily quests: the cog side (wonderland/quests_cog.py) against fake Config, bank, cogs and channels."""
import copy
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

try:
    from redbot.core import bank as _red_bank, commands as _red_commands   # noqa: F401
    _real_red = hasattr(_red_commands.Cog, "listener") and hasattr(_red_bank, "deposit_credits")
except ImportError:
    _real_red = False
if not _real_red:
    pytest.skip("real Red not installed", allow_module_level=True)

import discord
from redbot.core import bank

from wonderland import quests as Q
from wonderland import quests_cog as QC

GUILD_ID, LURKER, BOOSTER = 5, 900, 901


# ------------------------------------------------------------------ fakes

class Value:
    """Red Config value: await it, `.set()` it, or `async with` it."""

    def __init__(self, store, key, default):
        self.store, self.key, self.default = store, key, default

    def _get(self):
        if self.key not in self.store:
            self.store[self.key] = copy.deepcopy(self.default)
        return self.store[self.key]

    def __call__(self):
        return self

    def __await__(self):
        async def get():
            return copy.deepcopy(self._get())
        return get().__await__()

    async def set(self, value):
        self.store[self.key] = copy.deepcopy(value)

    async def __aenter__(self):
        return self._get()

    async def __aexit__(self, *exc):
        return False


class Group:
    def __init__(self, store, defaults):
        self._store, self._defaults = store, defaults

    def __getattr__(self, name):
        return Value(self._store, name, self._defaults.get(name))

    async def all(self):
        return copy.deepcopy({**self._defaults, **self._store})


class FakeConfig:
    def __init__(self, guild_defaults, member_defaults):
        self.guilds, self.members = {}, {}
        self.gd, self.md = guild_defaults, member_defaults

    def guild(self, g):
        return Group(self.guilds.setdefault(g.id, {}), self.gd)

    def member(self, m):
        return Group(self.members.setdefault((m.guild.id, m.id), {}), self.md)

    async def all_guilds(self):
        return {gid: dict(data) for gid, data in self.guilds.items()}

    async def all_members(self):
        out = {}
        for (gid, mid), data in self.members.items():
            out.setdefault(gid, {})[mid] = data
        return out

    def member_from_ids(self, gid, mid):
        store = self.members

        class Clear:
            async def clear(self_inner):
                store.pop((gid, mid), None)
        return Clear()


class Channel:
    def __init__(self, cid, name, public=True, can_send=True, members_hidden=()):
        self.id, self.name, self.public, self.can_send, self.hidden = cid, name, public, can_send, set(members_hidden)
        self.everyone = True        # False: a role-gated channel (Pillow Talk style) members can see, @everyone can't
        self.mention = f"<#{cid}>"

    def permissions_for(self, who):
        if getattr(who, "is_default", False):
            return discord.Permissions(view_channel=self.public and self.everyone)
        visible = self.public and who.id not in self.hidden
        return discord.Permissions(view_channel=visible, send_messages=visible and self.can_send,
                                   send_messages_in_threads=visible, add_reactions=visible)


class Guild:
    def __init__(self, channels):
        self.id = GUILD_ID
        self.default_role = SimpleNamespace(id=GUILD_ID, is_default=True)
        self._channels = {c.id: c for c in channels}
        self.text_channels = self.channels = list(channels)
        self.members = []

    def get_channel(self, cid):
        return self._channels.get(cid)


def member(guild, mid, roles=()):
    m = SimpleNamespace(id=mid, guild=guild, bot=False, roles=[SimpleNamespace(id=r) for r in (GUILD_ID, *roles)],
                        mention=f"<@{mid}>")
    guild.members.append(m)
    return m


class Cards:
    def __init__(self):
        self.pulled = set()

    async def _member_state(self, m):
        return SimpleNamespace(daily_pull_date=Q.today() if m.id in self.pulled else "")


class Economy:
    def __init__(self, base, role_credits):
        self.base, self.role_credits = base, role_credits
        self.last = {}
        eco = self

        class Conf:
            def guild(self_inner, g):
                return SimpleNamespace(PAYDAY_CREDITS=lambda: _ret(eco.base))

            def role(self_inner, r):
                return SimpleNamespace(PAYDAY_CREDITS=lambda: _ret(eco.role_credits.get(r.id, 0)))

            def member(self_inner, m):
                return SimpleNamespace(next_payday=lambda: _ret(eco.last.get(m.id, 0)))
        self.config = Conf()


async def _ret(value):
    return value


class Harness(QC.QuestsMixin):
    def __init__(self, guild, cogs, commands=("fish", "stars", "card daily")):
        self.config = FakeConfig({"quests": QC.DEFAULT_GUILD_QUESTS}, {"quests": {}})
        self._commands, self._cogs = set(commands), cogs
        self.bot = SimpleNamespace(get_cog=lambda name: self._cogs.get(name))
        self._q_setup()
        self.guild = guild

    def _exists(self, name):
        return name in self._commands

    async def _lurker_role_id(self, guild):
        return LURKER


@pytest.fixture
def world(monkeypatch):
    balances = {}

    async def deposit(m, amount):
        balances[m.id] = balances.get(m.id, 0) + amount
        return balances[m.id]

    monkeypatch.setattr(bank, "deposit_credits", deposit)
    monkeypatch.setattr(bank, "get_balance", lambda m: _ret(balances.get(m.id, 0)))
    monkeypatch.setattr(bank, "is_global", lambda: _ret(False))
    monkeypatch.setattr(bank, "get_currency_name", lambda g: _ret("wc"))
    cuddle, cmds, casino = Channel(10, "💬cuddle"), Channel(QC.BOT_COMMANDS_CHANNEL_ID, "bot-commands"), Channel(30, "wondercasino")
    guild = Guild([cuddle, cmds, casino])
    cards = Cards()
    gamble = SimpleNamespace(_hub_channel=lambda g: _ret(casino), config=SimpleNamespace(
        guild=lambda g: SimpleNamespace(games=lambda: _ret({"plant": {}, "canvas": {}}))))
    eco = Economy(3000, {BOOSTER: 0})
    ext = SimpleNamespace(db=SimpleNamespace(get_conf=lambda g: SimpleNamespace(
        stack_paydays=False, role_bonuses={BOOSTER: 0.5}, role_static_bonuses={})))
    cog = Harness(guild, {"CardCollect": cards, "GambleThreads": gamble, "Economy": eco, "ExtendedEconomy": ext})
    return SimpleNamespace(cog=cog, guild=guild, cards=cards, balances=balances, cuddle=cuddle, cmds=cmds, eco=eco)


async def switch_on(w, **extra):
    await w.cog._q_set(w.guild, enabled=True, **extra)


def stats(w):
    return w.cog.config.guilds[GUILD_ID]["quests"]["stats"][Q.today()]


def state(w, m):
    return w.cog.config.members[(GUILD_ID, m.id)]["quests"]


# ------------------------------------------------------------------ switched off

async def test_switched_off_nothing_is_tracked_dealt_or_paid(world):
    m = member(world.guild, 1)
    assert await world.cog.quests_open(world.guild) is False
    assert await world.cog._q_record(m, "card", 1, source=Q.HOOK) is None
    embed = await world.cog._q_panel_embed(m)
    assert "aren't open yet" in embed.description
    assert world.cog.config.members == {} and world.balances == {}


# ------------------------------------------------------------------ dealing

async def test_only_quests_this_member_can_do_today_are_dealt(world):
    await switch_on(world)
    m = member(world.guild, 1)
    embed = await world.cog._q_panel_embed(m)
    assert sorted(state(world, m)["assigned"]) == ["card", "chat", "fish"]   # star: LevelUp not loaded; rest: cogs missing
    assert "⬜" in embed.description and "New quests in" in embed.fields[0].value
    assert stats(world)["opens"] == 1 and stats(world)["offered"] == {"card": 1, "chat": 1, "fish": 1}
    await world.cog._q_panel_embed(m)
    assert stats(world)["opens"] == 1 and stats(world)["clicks"] == 2             # unique openers vs clicks
    assert state(world, m)["assigned"] == sorted(state(world, m)["assigned"], key=state(world, m)["assigned"].index)


async def test_hidden_or_unpostable_channels_take_quests_out(world):
    await switch_on(world)
    m = member(world.guild, 1)
    world.cuddle.hidden.add(1)                    # this member can't see #cuddle
    world.cmds.everyone = False                   # member can see it, @everyone can't: private, never a quest
    conf = await world.cog._q_conf(world.guild)
    assert (await world.cog._q_status("chat", m, conf))[0] == QC.OFF
    assert (await world.cog._q_status("fish", m, conf))[0] == QC.OFF
    assert (await world.cog._q_status("card", m, conf))[0] == QC.OPEN


async def test_lurkers_get_no_quests(world):
    await switch_on(world)
    m = member(world.guild, 1, roles=(LURKER,))
    embed = await world.cog._q_panel_embed(m)
    assert "unlock once you're active" in embed.description
    assert await world.cog._q_record(m, "card", 1, source=Q.HOOK) is None
    assert (GUILD_ID, 1) not in world.cog.config.members


async def test_something_already_done_today_is_not_dealt(world):
    await switch_on(world)
    m = member(world.guild, 1)
    world.cards.pulled.add(1)
    await world.cog._q_panel_embed(m)
    assert "card" not in state(world, m)["assigned"]


async def test_a_turned_off_quest_is_not_dealt(world):
    await switch_on(world, disabled=["fish"])
    m = member(world.guild, 1)
    await world.cog._q_panel_embed(m)
    assert "fish" not in state(world, m)["assigned"]


# ------------------------------------------------------------------ rewards

async def test_each_quest_pays_a_third_of_the_members_payday_and_a_set_pays_one(world):
    await switch_on(world)
    plain, boost = member(world.guild, 1), member(world.guild, 2, roles=(BOOSTER,))
    for m in (plain, boost):
        await world.cog._q_panel_embed(m)
        await world.cog._q_record(m, "card", 1, source=Q.HOOK)
    assert world.balances == {1: 1000, 2: 1500}                       # booster payday 4,500 (ExtendedEconomy +50%)
    for m in (plain, boost):
        for _ in range(3):
            await world.cog._q_record(m, "fish", 1, source=Q.COMMAND)
        for i in range(10):
            world.cog._q_chat_last.clear()
            await world.cog._q_record(m, "chat", 1, source=Q.CHAT)
    assert world.balances == {1: 3000, 2: 4500}                       # exactly one payday each, no bonus
    assert stats(world)["sets"] == 2 and stats(world)["paid"] == 7500
    assert state(world, plain)["streak"] == 1 and state(world, plain)["earned"] == 3000
    await world.cog._q_record(plain, "card", 1, source=Q.HOOK)       # nothing more today
    assert world.balances[1] == 3000


async def test_multiplier_scales_rewards(world):
    await switch_on(world, multiplier=0.75)
    m = member(world.guild, 1)
    await world.cog._q_panel_embed(m)
    await world.cog._q_record(m, "card", 1, source=Q.HOOK)
    assert world.balances == {1: 750}


async def test_payday_falls_back_to_the_guild_base_when_the_mirror_fails(world, caplog):
    world.cog._cogs["ExtendedEconomy"] = SimpleNamespace(db=SimpleNamespace(get_conf=lambda g: 1 / 0))
    m = member(world.guild, 2, roles=(BOOSTER,))
    assert await world.cog._q_payday(m) == 3000
    assert "using the guild base payday" in caplog.text


async def test_an_action_counts_once_only_from_its_own_source(world):
    await switch_on(world)
    m = member(world.guild, 1)
    await world.cog._q_panel_embed(m)
    assert await world.cog._q_record(m, "fish", 1, source=Q.HOOK) is None     # fish is only counted by its command
    assert await world.cog._q_record(m, "card", 1, source=Q.COMMAND) is None  # card only by its hook
    assert await world.cog._q_record(m, "nope", 1, source=Q.HOOK) is None
    assert world.balances == {}


async def test_a_missed_hook_is_caught_up_when_the_panel_opens(world):
    await switch_on(world)
    m = member(world.guild, 1)
    await world.cog._q_panel_embed(m)
    world.cards.pulled.add(1)                    # pulled while wonderland was reloading: no hook arrived
    await world.cog._q_panel_embed(m)
    assert world.balances == {1: 1000} and "card" in state(world, m)["paid"]


# ------------------------------------------------------------------ listeners

def message(m, channel, at):
    return SimpleNamespace(guild=m.guild, author=m, channel=channel, created_at=at)


async def test_chat_counts_one_message_per_15_seconds_in_cuddle_only(world):
    await switch_on(world)
    m = member(world.guild, 1)
    t = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    for offset in (0, 5, 14, 15, 40):
        await world.cog._q_on_message(message(m, world.cuddle, t + timedelta(seconds=offset)))
    await world.cog._q_on_message(message(m, world.cmds, t + timedelta(seconds=90)))
    assert state(world, m)["progress"]["chat"] == 3


async def test_stars_count_only_when_levelup_actually_gave_one(world):
    await switch_on(world)
    world.cog._cogs["LevelUp"] = levelup = SimpleNamespace(stars={})
    m = member(world.guild, 1)
    ctx = SimpleNamespace(command=SimpleNamespace(qualified_name="stars"), guild=world.guild, author=m,
                          message=SimpleNamespace(created_at=datetime.now(timezone.utc)))
    await world.cog._q_on_command(ctx)                                   # on cooldown / bad target: no stamp
    assert "star" not in (world.cog.config.members.get((GUILD_ID, 1), {}).get("quests", {}).get("progress", {}))
    levelup.stars = {GUILD_ID: {1: datetime.now()}}
    await world.cog._q_on_command(ctx)
    assert state(world, m)["progress"]["star"] == 1


async def test_payday_claims_count_toward_the_share_who_open_quests(world):
    await switch_on(world)
    a, b = member(world.guild, 1), member(world.guild, 2)
    await world.cog._q_panel_embed(a)
    for m in (a, b):
        await world.cog._q_on_payday(SimpleNamespace(member=m, amount=3000))
        await world.cog._q_on_payday(SimpleNamespace(member=m, amount=3000))   # twice is still one person
    await world.cog._q_panel_embed(b)
    s = stats(world)
    assert (s["payday"], s["opens"], s["both"]) == (2, 2, 2)


async def test_hook_listener_never_raises(world):
    await switch_on(world)
    await world.cog._q_on_hook(SimpleNamespace(id=1), "card", 1)          # no guild: ignored, no exception


# ------------------------------------------------------------------ the rest

async def test_mystats_summary_and_data_deletion(world):
    await switch_on(world)
    m = member(world.guild, 1)
    assert await world.cog.quest_summary(m) is None
    await world.cog._q_panel_embed(m)
    await world.cog._q_record(m, "card", 1, source=Q.HOOK)
    for _ in range(3):
        await world.cog._q_record(m, "fish", 1, source=Q.COMMAND)
    await world.cog._q_record(m, "chat", 10, source=Q.CHAT)
    assert await world.cog.quest_summary(m) == {"streak": 1, "best": 1, "sets": 1}
    await world.cog._q_delete_member(1)
    assert (GUILD_ID, 1) not in world.cog.config.members


async def test_guide_line_follows_the_switch(world):
    assert world.cog._q_live is False
    await switch_on(world)
    assert world.cog._q_live is True
    await world.cog._q_set(world.guild, enabled=False)
    assert world.cog._q_live is False


async def test_faucet_estimate_uses_real_paydays_of_active_members(world):
    member(world.guild, 1)
    member(world.guild, 2, roles=(BOOSTER,))
    member(world.guild, 3, roles=(LURKER,))                               # lurkers left out
    text = await world.cog._q_estimate(world.guild)
    assert "2 active members, average payday 3,750" in text and "375,000 a day" in text


async def test_plant_is_offered_only_when_the_member_can_drop_now(world, monkeypatch):
    class MinigameHub:
        __module__ = "fakemgh.minigamehub"
        _plant_last = {}
        plant_conf = {"enabled": True, "min": 1000}
        config = SimpleNamespace(guild=lambda g: SimpleNamespace(plant=lambda: _ret(MinigameHub.plant_conf)))

    monkeypatch.setitem(sys.modules, "fakemgh.plant",
                        SimpleNamespace(COOLDOWN_SECONDS=600, find_channel=lambda g: world.cuddle))
    world.cog._cogs["MinigameHub"] = hub = MinigameHub()
    await switch_on(world)
    m = member(world.guild, 1)
    conf = await world.cog._q_conf(world.guild)
    assert (await world.cog._q_status("plant", m, conf))[2] == "can't afford the smallest drop"
    world.balances[1] = 5000
    assert (await world.cog._q_status("plant", m, conf))[0] == QC.OPEN
    hub._plant_last[(GUILD_ID, 1)] = __import__("time").time()
    assert (await world.cog._q_status("plant", m, conf))[2] == "on cooldown"
    hub._plant_last.clear()
    world.cuddle.everyone = False                       # the drop would land somewhere private: never
    assert (await world.cog._q_status("plant", m, conf))[0] == QC.OFF


async def test_acrophobia_is_offered_only_while_a_round_is_running_or_still_planned_today(world):
    import time as _time
    now = _time.time()
    plan = {"date": Q.today(), "slots": [now + 3600], "done": []}

    class Verdict:
        _acro_status = None
        values = {"acro_enabled": True, "acro_current": {}, "acro_plan": plan, "channel_id": 10}
        config = SimpleNamespace(guild=lambda g: SimpleNamespace(
            **{k: (lambda k=k: _ret(Verdict.values[k])) for k in ("acro_enabled", "acro_current", "acro_plan", "channel_id")}))

    world.cog._cogs["Verdict"] = Verdict()
    await switch_on(world)
    m = member(world.guild, 1)
    conf = await world.cog._q_conf(world.guild)
    status = lambda: world.cog._q_status("acro", m, conf)
    assert (await status())[0] == QC.OPEN                                     # a round is planned for later today
    Verdict.values["acro_plan"] = {**plan, "done": plan["slots"]}
    assert (await status())[2] == "no round left today"                       # both rounds already ran
    Verdict.values["acro_plan"] = {**plan, "date": "2026-01-01"}
    assert (await status())[2] == "no round left today"                       # a stale plan is not a promise
    Verdict.values["acro_current"] = {"phase": "submit", "submit_ends_ts": now + 60}
    assert (await status())[0] == QC.OPEN                                     # entering a live round
    Verdict.values["acro_current"] = {"phase": "vote", "vote_ends_ts": now + 60}
    assert (await status())[0] == QC.OPEN                                     # voting counts too
    Verdict.values["acro_current"] = {"phase": "vote", "vote_ends_ts": now - 1}
    assert (await status())[2] == "round just closing"
    Verdict.values["acro_current"] = {}
    Verdict.values["acro_plan"] = plan
    Verdict.values["acro_enabled"] = False
    assert (await status())[2] == "Acrophobia is off"
    Verdict.values["acro_enabled"] = True
    world.cuddle.everyone = False                                             # the round's channel went private
    assert (await status())[0] == QC.OFF


async def test_a_member_dealt_a_quest_that_was_later_retired_still_gets_a_working_panel(world):
    await switch_on(world)
    m = member(world.guild, 1)
    world.cog.config.members[(GUILD_ID, m.id)] = {"quests": {**Q.fresh_state(), "day": Q.today(), "assigned": ["rsvp", "card", "chat"],
                                                             "progress": {"rsvp": 1}, "paid": ["rsvp"]}}
    embed = await world.cog._q_panel_embed(m)
    assert "rsvp" not in state(world, m)["assigned"] and "RSVP" not in embed.description

