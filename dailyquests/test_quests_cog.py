"""Daily quests: the cog side (dailyquests/quests_cog.py) against fake Config, bank, cogs and channels."""
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

from dailyquests import quests as Q
from dailyquests import quests_cog as QC

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
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))

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
    # "guide" needs no other cog, so it is always on offer; the older tests count on a fixed set of three
    extra["disabled"] = sorted(set(extra.get("disabled", [])) | set(extra.pop("keep_off", ["guide"])))
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
    assert await world.cog.quest_summary(m) == {"streak": 1, "best": 1, "sets": 1, "badge": ""}
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




# ------------------------------------------------------------------ pet treat, card pick, duel, guide

def _cfg(**values):
    return SimpleNamespace(**{k: (lambda v=v: _ret(v)) for k, v in values.items()})


async def test_the_card_pick_is_offered_where_drops_land_unless_they_are_test_drops(world):
    values = {"test_mode": False, "channel_id": 10}
    world.cog._cogs["CardCollect"] = SimpleNamespace(
        config=SimpleNamespace(guild=lambda g: SimpleNamespace(**{k: (lambda k=k: _ret(values[k])) for k in values})))
    await switch_on(world)
    m = member(world.guild, 1)
    conf = await world.cog._q_conf(world.guild)
    assert (await world.cog._q_status("pick", m, conf))[0] == QC.OPEN
    values["test_mode"] = True
    assert (await world.cog._q_status("pick", m, conf))[2] == "drops are in test mode"
    values["test_mode"] = False
    world.cuddle.everyone = False
    assert (await world.cog._q_status("pick", m, conf))[0] == QC.OFF


async def test_a_duel_is_offered_only_with_coins_for_the_smallest_wager_and_no_cooldown(world):
    cooldown = {"left": None}

    class Duel:
        config = SimpleNamespace(guild=lambda g: SimpleNamespace(min_bet=lambda: _ret(500)))

        async def _check_cooldown(self, guild, member):
            return cooldown["left"]

    world.cog._cogs["Duel"] = Duel()
    world.cog._commands.add("duel")
    await switch_on(world)
    m = member(world.guild, 1)
    conf = await world.cog._q_conf(world.guild)
    status = lambda: world.cog._q_status("duel", m, conf)
    assert (await status())[2] == "can't cover the smallest wager"
    world.balances[1] = 500
    assert (await status())[0] == QC.OPEN
    cooldown["left"] = 30
    assert (await status())[2] == "on duel cooldown"


async def test_the_guide_quest_counts_a_real_lookup_through_its_hook(world):
    await switch_on(world, keep_off=[])
    m = member(world.guild, 1)
    conf = await world.cog._q_conf(world.guild)
    assert (await world.cog._q_status("guide", m, conf))[0] == QC.OPEN
    world.cmds.everyone = False
    assert (await world.cog._q_status("guide", m, conf))[0] == QC.OFF          # #bot-commands private: never a quest
    world.cmds.everyone = True
    await world.cog._q_set(world.guild, targets={})
    assert await world.cog._q_record(m, "guide", 1, source=Q.COMMAND) is None   # a command can't count it
    await world.cog._q_panel_embed(m)
    state(world, m)["assigned"] = ["guide", "card", "chat"]
    await world.cog._q_record(m, "guide", 1, source=Q.HOOK)                      # the guide's wonderland_quest hook
    assert state(world, m)["progress"]["guide"] == 1 and "guide" in state(world, m)["paid"]
    assert state(world, m)["progress"]["guide"] == 1
    assert world.balances[1] > 0



# ------------------------------------------------------------------ streaks, prizes, XP, rerolls, packs, post quests

class LevelUpFake:
    """LevelUp as the quests use it: a profile with xp, check_levelups, and a separate weekly board that must stay put."""

    def __init__(self):
        self.profile = SimpleNamespace(xp=0, level=1)
        self.weekly = SimpleNamespace(xp=0)
        self.checked, self.ignored = 0, set()
        fake = self
        conf = SimpleNamespace(ignoredusers=self.ignored, get_profile=lambda m: fake.profile)
        self.db = SimpleNamespace(get_conf=lambda g: conf)

    async def check_levelups(self, **kw):
        self.checked += 1
        assert kw["profile"] is self.profile and not kw.get("channel")

    def save(self, *a):
        pass


async def finish_day(w, m, day, monkeypatch, key="card"):
    monkeypatch.setattr(Q, "today", lambda *a, **k: day)
    await w.cog._q_panel_embed(m)
    await w.cog._q_record(m, key, 1, source=Q.HOOK)


def next_day(day):
    return Q.yesterday_of(day) if False else (Q.date.fromisoformat(day) + Q.timedelta(days=1)).isoformat()


async def test_one_finished_quest_a_day_keeps_the_streak_and_day_three_pays_its_prize(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"], multiplier=1.0)
    m = member(world.guild, 1)
    day = "2026-11-02"
    for n in range(3):
        await finish_day(world, m, day, monkeypatch)
        day = next_day(day)
    assert state(world, m)["streak"] == 3 and state(world, m)["ms_given"] == [3]
    assert state(world, m)["notes"] and "3 days" in state(world, m)["notes"][0]["label"]
    assert world.balances[1] == 1000 * 3 + 0       # three card quests; the 3-day prize is XP only (0 coins)


async def test_a_missed_day_resets_the_streak_and_prizes_pay_again(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    day = "2026-11-02"
    for n in range(3):
        await finish_day(world, m, day, monkeypatch)
        day = next_day(day)
    day = next_day(next_day(day))
    await finish_day(world, m, day, monkeypatch)
    assert state(world, m)["streak"] == 1 and state(world, m)["best_streak"] == 3 and state(world, m)["ms_given"] == []


async def test_seven_day_streak_pays_a_payday_xp_and_a_free_pack_voucher(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"], multiplier=1.0)
    m = member(world.guild, 1)
    day = "2026-11-02"
    for n in range(7):
        await finish_day(world, m, day, monkeypatch)
        day = next_day(day)
    st = state(world, m)
    assert st["streak"] == 7 and st["packs"] == 1 and 7 in st["ms_given"]
    assert world.balances[1] == 1000 * 7 + 3000 + 3000   # seven quests + the 7-day prize + the weekly prize (5 days)
    assert Q.badge_text(7) == "🏅 Week Warrior"


async def test_quest_xp_goes_to_the_profile_not_the_weekly_board(world, monkeypatch):
    lu = LevelUpFake()
    world.cog._cogs["LevelUp"] = lu
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    await finish_day(world, m, "2026-11-02", monkeypatch)
    assert lu.profile.xp == 50 and lu.weekly.xp == 0 and lu.checked == 1
    assert state(world, m)["xp"] == 50


async def test_the_holiday_xp_weekend_multiplies_quest_xp(world, monkeypatch):
    lu = LevelUpFake()
    world.cog._cogs["LevelUp"] = lu
    await switch_on(world, keep_off=["guide", "star"], xp_boost={"start": "2026-11-27", "end": "2026-11-29", "multiplier": 2.0})
    m = member(world.guild, 1)
    await finish_day(world, m, "2026-11-26", monkeypatch)
    assert lu.profile.xp == 50
    await finish_day(world, m, "2026-11-27", monkeypatch)
    assert lu.profile.xp == 50 + 100


async def test_ignored_levelup_users_get_no_quest_xp_and_missing_levelup_is_fine(world, monkeypatch):
    lu = LevelUpFake()
    lu.ignored.add(1)
    world.cog._cogs["LevelUp"] = lu
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    await finish_day(world, m, "2026-11-02", monkeypatch)
    assert lu.profile.xp == 0
    world.cog._cogs.pop("LevelUp")
    other = member(world.guild, 2)
    await finish_day(world, other, "2026-11-02", monkeypatch)
    assert state(world, other)["xp"] == 0 and world.balances[2] == 1000


async def test_weekly_prize_pays_once_on_the_fifth_active_day(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"], multiplier=1.0, weekly={"days": 2, "coins": 1.0, "xp": 0, "pack": False})
    m = member(world.guild, 1)
    for day in ("2026-11-23", "2026-11-24", "2026-11-25"):       # Monday to Wednesday
        await finish_day(world, m, day, monkeypatch)
    assert world.balances[1] == 1000 * 3 + 3000                   # one weekly payday, not two
    assert state(world, m)["week_paid"] == Q.week_key("2026-11-24")


async def test_reroll_costs_climb_and_a_swapped_away_quest_is_not_dealt_back(world, monkeypatch):
    selfie = Channel(40, "selfie")                       # guide, card, chat, fish, selfie: five on offer, three dealt
    world.guild._channels[40] = selfie
    world.guild.text_channels.append(selfie)
    await switch_on(world, keep_off=[])
    m = member(world.guild, 1)
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    paid = []

    async def withdraw(who, amount):
        world.balances[who.id] -= amount
        paid.append(amount)
    monkeypatch.setattr(bank, "withdraw_credits", withdraw)
    await world.cog._q_panel_embed(m)
    world.balances[1] = 1_000_000
    first = list(state(world, m)["assigned"])
    text1 = await world.cog._q_reroll(m, first[0])
    text2 = await world.cog._q_reroll(m, state(world, m)["assigned"][0])
    assert paid == [1000, 5000] and state(world, m)["rerolls"] == 2
    assert "next reroll today costs 5,000" in text1 and "costs 10,000" in text2
    now = state(world, m)["assigned"]
    assert len(set(now)) == 3 and not set(state(world, m)["swapped"]) & set(now)
    assert world.balances[1] == 1_000_000 - 6000
    third = await world.cog._q_reroll(m, now[0])         # nothing left to swap in: not charged
    assert "nothing else" in third and paid == [1000, 5000]


async def test_a_reroll_you_cannot_afford_changes_nothing(world, monkeypatch):
    await switch_on(world, keep_off=[])
    m = member(world.guild, 1)
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    await world.cog._q_panel_embed(m)
    before = list(state(world, m)["assigned"])

    async def withdraw(who, amount):
        raise ValueError("no")
    monkeypatch.setattr(bank, "withdraw_credits", withdraw)
    text = await world.cog._q_reroll(m, before[0])
    assert state(world, m)["assigned"] == before and state(world, m)["rerolls"] == 0
    assert "don't have enough" in text or "nothing else" in text


async def test_selfie_counts_only_an_image_in_its_channel(world):
    selfie = Channel(40, "📸・selfie")
    world.guild._channels[40] = selfie
    world.guild.text_channels.append(selfie)
    await switch_on(world, keep_off=[])
    m = member(world.guild, 1)
    await world.cog._q_panel_embed(m)
    state(world, m)["assigned"] = ["selfie", "card", "fish"]
    img = SimpleNamespace(content_type="image/png")
    txt = SimpleNamespace(content_type="text/plain")
    base = dict(guild=world.guild, author=m, channel=selfie, content="", created_at=SimpleNamespace(timestamp=lambda: 1.0), id=1)
    await world.cog._q_message(SimpleNamespace(**base, attachments=[txt]))
    assert state(world, m)["progress"].get("selfie", 0) == 0
    await world.cog._q_message(SimpleNamespace(**base, attachments=[img]))
    assert state(world, m)["progress"]["selfie"] == 1 and "selfie" in state(world, m)["paid"]


async def test_counting_ignores_words_and_wrong_counts_that_get_deleted(world, monkeypatch):
    counting = Channel(41, "counting")
    world.guild._channels[41] = counting
    world.guild.text_channels.append(counting)
    monkeypatch.setattr(QC, "COUNT_CHECK_SECONDS", 0)
    deleted = set()

    async def fetch_message(mid):
        if mid in deleted:
            raise QC.discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
    counting.fetch_message = fetch_message
    await switch_on(world, keep_off=[])
    m = member(world.guild, 1)
    await world.cog._q_panel_embed(m)
    state(world, m)["assigned"] = ["counting", "card", "fish"]

    def msg(mid, content):
        return SimpleNamespace(guild=world.guild, author=m, channel=counting, content=content, attachments=[], id=mid,
                               created_at=SimpleNamespace(timestamp=lambda: float(mid)))
    await world.cog._q_message(msg(1, "lol nice"))
    deleted.add(2)
    await world.cog._q_message(msg(2, "17"))
    assert state(world, m)["progress"].get("counting", 0) == 0
    await world.cog._q_message(msg(3, "18"))
    assert "counting" in state(world, m)["paid"]


async def test_the_panel_shows_streak_week_xp_and_packs(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"], multiplier=1.0, xp_boost={"start": "2026-11-02", "end": "2026-11-09", "multiplier": 2.0})
    m = member(world.guild, 1)
    day = "2026-11-02"
    for n in range(3):
        await finish_day(world, m, day, monkeypatch)
        day = next_day(day)
    monkeypatch.setattr(Q, "today", lambda *a, **k: Q.date.fromisoformat(day).isoformat())
    # the last finished day was day-1; showing the panel for that same day keeps the streak alive
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-04")
    embed, extras = await world.cog._q_panel_parts(m, count_open=False)
    text = embed.description + "".join(f.value for f in embed.fields)
    assert "Streak" in text and "🔥" in text and "This week" in text and "Holiday XP" in text and "XP" in text
    assert "reroll" not in extras or extras["reroll"] == 1000


async def test_a_voucher_pack_opens_through_cardcollect_and_is_used_up(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    await world.cog._q_panel_embed(m)
    state(world, m)["packs"] = 1
    calls = []

    class CC(Cards):
        config = SimpleNamespace(guild=lambda g: SimpleNamespace(pack_price=lambda: _ret(10000)))

        async def open_bonus_pack(self, guild, who, price=0):
            calls.append(price)
            return {"entries": [], "bonus": True}

        async def pack_result_message(self, guild, who, result):
            return "🎁 opened a quest pack:", None
    world.cog._cogs["CardCollect"] = CC()
    text, _file = await world.cog._q_open_pack(m, "voucher")
    assert calls == [0] and state(world, m)["packs"] == 0 and "<#" in text
    assert "daily-quest streak" in world.guild.get_channel(QC.BOT_COMMANDS_CHANNEL_ID).sent[0][0]
    again, _ = await world.cog._q_open_pack(m, "voucher")
    assert "no free packs" in again and calls == [0]


async def test_a_full_set_pack_is_half_price_by_default_once_a_day_and_can_be_off(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    await world.cog._q_panel_embed(m)
    st = state(world, m)
    st["paid"] = list(st["assigned"])
    calls = []

    class CC(Cards):
        config = SimpleNamespace(guild=lambda g: SimpleNamespace(pack_price=lambda: _ret(10000)))

        async def open_bonus_pack(self, guild, who, price=0):
            calls.append(price)
            return {"entries": [], "bonus": True}

        async def pack_result_message(self, guild, who, result):
            return "ok", None
    world.cog._cogs["CardCollect"] = CC()
    await world.cog._q_open_pack(m, "set")
    assert calls == [5000]
    again, _ = await world.cog._q_open_pack(m, "set")
    assert calls == [5000] and "no full-set pack" in again


async def test_admin_specs_save_and_reject_bad_input(world):
    from dailyquests import quests_panel as QP
    from dailyquests.settings_panel import PanelError
    await switch_on(world)
    g, cog = world.guild, world.cog
    assert "XP" in await QP._apply_xp(cog, g, None, {"xp": "75"}) and (await cog._q_conf(g))["xp_per_quest"] == 75
    with pytest.raises(PanelError):
        await QP._apply_xp(cog, g, None, {"xp": "-1"})
    await QP._apply_boost(cog, g, None, {"start": "2026-12-24", "end": "2026-12-26", "multiplier": "3"})
    assert (await cog._q_conf(g))["xp_boost"] == {"start": "2026-12-24", "end": "2026-12-26", "multiplier": 3.0}
    with pytest.raises(PanelError):
        await QP._apply_boost(cog, g, None, {"start": "2026-12-26", "end": "2026-12-24", "multiplier": "2"})
    await QP._boost_end(cog, g, None, None)
    assert (await cog._q_conf(g))["xp_boost"]["start"] == ""
    await QP._apply_rerolls(cog, g, None, {"costs": "500 2000"})
    assert (await cog._q_conf(g))["reroll_costs"] == [500, 2000]
    with pytest.raises(PanelError):
        await QP._apply_rerolls(cog, g, None, {"costs": "abc"})
    await QP._apply_weekly(cog, g, None, {"days": "4", "coins": "0.5", "xp": "100", "pack": "yes"})
    assert (await cog._q_conf(g))["weekly"] == {"days": 4, "coins": 0.5, "xp": 100, "pack": True}
    await QP._milestone_save(cog, g, None, "7", {"coins": "2", "xp": "10", "pack": "no"}, None)
    assert Q.prize_for((await cog._q_conf(g))["milestones"], 7) == {"coins": 2.0, "xp": 10, "pack": False}
    await QP._apply_set_pack(cog, g, None, "free")
    assert (await cog._q_conf(g))["set_pack"] == "free"
    assert len(QP.build_specs()) >= 15
    member(g, 77)
    estimate = await cog._q_estimate(g)
    assert "weekly prize" in estimate and "Rerolls" in estimate


# ------------------------------------------------------------------ heist quests (read off the Heist cog's tallies)

class HeistFake:
    """maxcogs' Heist: lifetime stats {success, caught, fail} per user, jail and debt, no events."""

    def __init__(self):
        self.users = {}
        fake = self

        class User:
            def __init__(self, uid):
                self.uid = uid

            def _d(self):
                return fake.users.setdefault(self.uid, {"stats": {"success": 0, "caught": 0, "fail": 0}, "jail": None, "debt": 0})

            def stats(self):
                return _ret(self._d()["stats"])

            def jail(self):
                return _ret(self._d()["jail"])

            def debt(self):
                return _ret(self._d()["debt"])
        self.config = SimpleNamespace(user=lambda m: User(m.id))

    def end(self, uid, outcome):
        stats = self.users.setdefault(uid, {"stats": {"success": 0, "caught": 0, "fail": 0}, "jail": None, "debt": 0})["stats"]
        stats[outcome] += 1


async def heist_world(world, monkeypatch, quests=("heist", "getaway")):
    world.cog._cogs["Heist"] = heist = HeistFake()
    world.cog._commands.add("heist start")
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    await switch_on(world, keep_off=[k for k in ("guide", "star", "heist", "getaway") if k not in quests])
    m = member(world.guild, 1)
    return heist, m


async def test_heist_quests_are_offered_only_when_the_member_can_start_one(world, monkeypatch):
    heist, m = await heist_world(world, monkeypatch)
    conf = await world.cog._q_conf(world.guild)
    assert (await world.cog._q_status("heist", m, conf))[0] == QC.OPEN
    heist.users[1] = {"stats": {}, "jail": {"end_time": 9e12}, "debt": 0}
    assert (await world.cog._q_status("getaway", m, conf))[1:] == (None, "in jail")
    heist.users[1]["jail"], heist.users[1]["debt"] = None, 500
    assert (await world.cog._q_status("heist", m, conf))[2] == "owes a debt"
    heist.users[1]["debt"] = 0
    world.cog._commands.discard("heist start")
    assert (await world.cog._q_status("heist", m, conf))[0] == QC.OFF
    world.cog._cogs.pop("Heist")
    assert (await world.cog._q_status("heist", m, conf))[2] == "Heist not loaded"


async def test_a_heist_that_ended_before_the_quest_was_dealt_does_not_count(world, monkeypatch):
    heist, m = await heist_world(world, monkeypatch)
    heist.end(1, "success")                                   # yesterday's heist: part of the baseline
    await world.cog._q_panel_embed(m)
    state(world, m)["assigned"] = ["heist", "getaway", "card"]
    await world.cog._q_panel_embed(m)
    assert state(world, m)["paid"] == [] and state(world, m)["base"] == {"heist": 1, "getaway": 1}


async def test_any_ended_heist_finishes_heist_and_only_a_clean_win_makes_the_getaway(world, monkeypatch):
    heist, m = await heist_world(world, monkeypatch)
    await world.cog._q_panel_embed(m)
    state(world, m)["assigned"] = ["heist", "getaway", "card"]
    await world.cog._q_panel_embed(m)                         # baselines are set when the quests are first seen
    heist.end(1, "caught")                                    # police got them: the heist ended, no getaway
    await world.cog._q_panel_embed(m, count_open=False)
    assert state(world, m)["paid"] == ["heist"] and world.balances[1] == 1000
    heist.end(1, "success")
    await world.cog._q_panel_embed(m, count_open=False)
    assert sorted(state(world, m)["paid"]) == ["getaway", "heist"] and world.balances[1] == 2000


async def test_a_failed_heist_counts_as_finished_but_not_as_a_getaway(world, monkeypatch):
    heist, m = await heist_world(world, monkeypatch)
    await world.cog._q_panel_embed(m)
    state(world, m)["assigned"] = ["heist", "getaway", "card"]
    await world.cog._q_panel_embed(m)
    heist.end(1, "fail")
    await world.cog._q_panel_embed(m, count_open=False)
    assert state(world, m)["paid"] == ["heist"]


async def test_a_heist_quest_cannot_be_counted_by_a_hook_or_command(world, monkeypatch):
    heist, m = await heist_world(world, monkeypatch)
    for source in (Q.HOOK, Q.COMMAND, Q.CHAT):
        assert await world.cog._q_record(m, "heist", 1, source=source) is None


async def test_a_rerolled_in_heist_quest_counts_from_the_reroll(world, monkeypatch):
    heist, m = await heist_world(world, monkeypatch, quests=("heist",))
    await world.cog._q_panel_embed(m)
    st = state(world, m)
    st["assigned"] = ["card", "chat", "fish"]
    st["base"] = {}

    async def withdraw(who, amount):
        world.balances[who.id] = world.balances.get(who.id, 0) - amount
    monkeypatch.setattr(bank, "withdraw_credits", withdraw)
    world.balances[1] = 100_000
    monkeypatch.setattr(Q, "deal", lambda candidates, *a, **k: ["heist"] if "heist" in candidates else [])
    await world.cog._q_reroll(m, "fish")
    assert state(world, m)["base"] == {"heist": 0}
    heist.end(1, "success")
    await world.cog._q_panel_embed(m, count_open=False)
    assert "heist" in state(world, m)["paid"]


class PackCC(Cards):
    config = SimpleNamespace(guild=lambda g: SimpleNamespace(pack_price=lambda: _ret(10000)))

    def __init__(self, price=5000):
        super().__init__()
        self.price, self.hits = price, []

    async def open_bonus_pack(self, guild, who, price=0):
        return {"entries": [], "bonus": True, "price": price}

    async def pack_result_message(self, guild, who, result):
        return f"\U0001f381 {who.mention} opened a quest pack:\nCard A\nCard B", "FILE"

    async def announce_pack_hits(self, guild, who, result, posted_in_id):
        self.hits.append(posted_in_id)


async def test_a_full_set_pack_is_posted_in_bot_commands_with_its_own_wording(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    await world.cog._q_panel_embed(m)
    st = state(world, m)
    st["paid"] = list(st["assigned"])
    cards = PackCC()
    world.cog._cogs["CardCollect"] = cards
    private, file = await world.cog._q_open_pack(m, "set")
    cmds = world.guild.get_channel(QC.BOT_COMMANDS_CHANNEL_ID)
    assert len(cmds.sent) == 1
    posted, kw = cmds.sent[0]
    assert posted.splitlines()[0] == "\U0001f381 <@1> finished all of their daily quests and bought a pack with it for 5,000 wondercoin:"
    assert "Card A" in posted and "Card B" in posted and kw["file"] == "FILE"
    assert cards.hits == [QC.BOT_COMMANDS_CHANNEL_ID]
    assert cmds.mention in private and file is None


async def test_a_free_set_pack_and_a_streak_voucher_have_their_own_wording(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    await world.cog._q_panel_embed(m)
    st = state(world, m)
    st["paid"] = list(st["assigned"])
    st["packs"] = 1
    world.cog._cogs["CardCollect"] = PackCC()
    await QC.QuestsMixin._q_open_pack(world.cog, m, "voucher")
    cmds = world.guild.get_channel(QC.BOT_COMMANDS_CHANNEL_ID)
    assert cmds.sent[0][0].splitlines()[0] == "\U0001f381 <@1> earned a free pack from their daily-quest streak:"
    conf = await world.cog._q_conf(world.guild)
    await world.cog.config.guild(world.guild).quests.set({**conf, "set_pack": "free"})
    await world.cog._q_open_pack(m, "set")
    assert cmds.sent[1][0].splitlines()[0] == "\U0001f381 <@1> finished all of their daily quests and got a free pack:"


async def test_a_pack_that_cannot_post_falls_back_to_the_private_reply(world, monkeypatch):
    await switch_on(world, keep_off=["guide", "star"])
    m = member(world.guild, 1)
    monkeypatch.setattr(Q, "today", lambda *a, **k: "2026-11-02")
    await world.cog._q_panel_embed(m)
    st = state(world, m)
    st["paid"] = list(st["assigned"])
    world.cog._cogs["CardCollect"] = PackCC()
    cmds = world.guild.get_channel(QC.BOT_COMMANDS_CHANNEL_ID)

    async def boom(*a, **k):
        raise discord.HTTPException(SimpleNamespace(status=403, reason="no"), "Missing Access")
    cmds.send = boom
    private, file = await world.cog._q_open_pack(m, "set")
    assert "bought a pack" in private and "Card A" in private and file == "FILE"
