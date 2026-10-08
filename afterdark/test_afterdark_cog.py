"""Behaviour tests for the AfterDark cog against in-memory fakes of Red/discord.

These prove the cog's logic (who is admitted, revoked, warned, invited) and that
it imports cleanly under the stub. They do NOT prove live Discord behaviour:
real button clicks, role edits, DMs and this bot's exact discord.py API shape
still need a live check after deploy.
"""
import asyncio
import copy
import random
from types import SimpleNamespace

import discord
import pytest

from afterdark import afterdark as ad
from afterdark import constants as C
from afterdark import engine
from afterdark.models import Invite

DAY = C.DAY
NOW = 1_800_000_000.0

AGE_ROLE = C.DEFAULT_ADULT_AGE_ROLE_IDS[0]
UNDERAGE_ROLE = C.DEFAULT_UNDERAGE_ROLE_IDS[0]
STAFF_ROLE = C.DEFAULT_EXEMPT_ROLE_IDS[0]
FEET_CHANNEL = 776525958675955733
FEET_ROLE = 7000000000000001
LURKER_ROLE = 7000000000000002


# ---------------------------------------------------------------- fakes

class FakeRole:
    def __init__(self, rid, name="role", position=1, managed=False):
        self.id, self.name, self.position, self.managed = rid, name, position, managed
        self.guild = None

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]


class FakeMessage:
    def __init__(self, mid, channel, embeds=None, view=None):
        self.id, self.channel = mid, channel
        self.embeds = list(embeds or [])
        self.view = view
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeMember:
    _next_id = 1000

    def __init__(self, guild, name="m", roles=(), bot=False, dm_open=True):
        FakeMember._next_id += 1
        self.id = FakeMember._next_id
        self.display_name = name
        self.mention = f"<@{self.id}>"
        self.bot = bot
        self.guild = guild
        self.roles = list(roles)
        self.dm_open = dm_open
        self.dms = []
        self.guild_permissions = SimpleNamespace(administrator=False, manage_guild=False)
        guild.members.append(self)

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]

    async def send(self, content=None, embed=None, view=None):
        if not self.dm_open:
            raise discord.Forbidden("closed")
        self.dms.append({"content": content, "embed": embed, "view": view})
        return FakeMessage(900000 + len(self.dms), SimpleNamespace(id=555))


class FakeChannel:
    def __init__(self, cid, name="chan"):
        self.id, self.name = cid, name
        self.overwrites = {}
        self.sent = []
        self.messages = {}

    async def set_permissions(self, member, overwrite=None, reason=None):
        if overwrite is None:
            self.overwrites.pop(member, None)
        else:
            self.overwrites[member] = overwrite

    async def send(self, text=None, **kwargs):
        self.sent.append(text)
        embed = kwargs.get("embed")
        message = FakeMessage(len(self.sent), self, [embed] if embed is not None else [], kwargs.get("view"))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound("gone")
        return self.messages[mid]

    mention = "#chan"


class FakeGuild:
    def __init__(self):
        self.id, self.name = 42, "Wonderland"
        self.members, self.roles, self.channels = [], {}, {}
        self.me = SimpleNamespace(top_role=FakeRole(1, "bot", position=100))

    def add_role(self, rid, name="role", **kw):
        role = FakeRole(rid, name, **kw)
        role.guild = self
        self.roles[rid] = role
        return role

    def add_channel(self, cid, name="chan"):
        self.channels[cid] = FakeChannel(cid, name)
        return self.channels[cid]

    def get_role(self, rid):
        return self.roles.get(rid)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_thread(self, tid):
        return self.channels.get(("thread", tid))


class FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]
        self.cogs = {}
        self.views = []

    def get_cog(self, name):
        return self.cogs.get(name)

    def get_guild(self, gid):
        return next((g for g in self.guilds if g.id == gid), None)

    def get_user(self, uid):
        return None

    def add_view(self, view):
        self.views.append(view)


class _Value:
    def __init__(self, store, name):
        self.store, self.name = store, name

    async def __call__(self):
        return copy.deepcopy(self.store[self.name])

    async def set(self, value):
        self.store[self.name] = copy.deepcopy(value)


class _Scope:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        if name.startswith("_") or name not in self.store:
            raise AttributeError(name)
        return _Value(self.store, name)

    async def all(self):
        return copy.deepcopy(self.store)


class FakeConfig:
    def __init__(self):
        self.store = {}

    @classmethod
    def get_conf(cls, *args, **kwargs):
        return cls()

    def register_guild(self, **defaults):
        self.store = copy.deepcopy(defaults)

    def guild(self, guild):
        return _Scope(self.store)

    def guild_from_id(self, gid):
        return _Scope(self.store)


class FakeCtx:
    def __init__(self, guild, author):
        self.guild, self.author, self.sent = guild, author, []
        self.channel = FakeChannel(1)

    async def send(self, text=None, **kwargs):
        self.sent.append(text)

    async def send_help(self):
        pass


class FakeInteraction:
    def __init__(self, user, guild=None):
        self.user, self.guild = user, guild
        self.message = None
        self.replies, self.edits = [], []
        self.response = SimpleNamespace(send_message=self._send, edit_message=self._edit)

    async def _send(self, text, ephemeral=False):
        self.replies.append(text)

    async def _edit(self, **kwargs):
        self.edits.append(kwargs)


def levelup_with(levels):
    """levels: {user_id: level}"""
    users = {uid: SimpleNamespace(level=lvl) for uid, lvl in levels.items()}
    return SimpleNamespace(db=SimpleNamespace(get_conf=lambda guild: SimpleNamespace(users=users)))


# -------------------------------------------------------------- fixtures

class World:
    pass


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setattr(ad, "Config", FakeConfig)
    w = World()
    w.guild = FakeGuild()
    g = w.guild
    w.adult = g.add_role(C.DEFAULT_ADULT_ROLE_ID, "Adult Chat")
    w.rabbit = g.add_role(C.DEFAULT_RABBIT_ROLE_ID, "rabbit")
    w.age = g.add_role(AGE_ROLE, "18-21")
    w.under = g.add_role(UNDERAGE_ROLE, "13-17")
    w.staff = g.add_role(STAFF_ROLE, "Mod")
    w.feet_role = g.add_role(FEET_ROLE, "cat")
    w.lurker = g.add_role(LURKER_ROLE, "Lurker")
    w.feet = g.add_channel(FEET_CHANNEL, "feet")
    w.log = g.add_channel(C.DEFAULT_LOG_CHANNEL_ID, "mod-commands")
    w.bot = FakeBot(g)
    w.bot.cogs["Lurker"] = SimpleNamespace(
        config=SimpleNamespace(guild=lambda gg: SimpleNamespace(lurker_role_id=_async_value(LURKER_ROLE)))
    )
    w.cog = ad.AfterDark(w.bot)
    w.clock = [NOW]
    w.cog._clock = lambda: w.clock[0]
    w.cog._rng = random.Random(3)
    w.levels = {}
    w.bot.cogs["LevelUp"] = SimpleNamespace(
        db=SimpleNamespace(get_conf=lambda guild: SimpleNamespace(
            users={uid: SimpleNamespace(level=lvl) for uid, lvl in w.levels.items()}))
    )

    def member(name="m", roles=None, level=5, **kw):
        base = [w.adult, w.age] if roles is None else roles
        m = FakeMember(g, name, base, **kw)
        w.levels[m.id] = level
        return m

    w.member = member
    return w


def _async_value(value):
    async def getter():
        return value

    return getter


async def set_cfg(w, **kwargs):
    for key, value in kwargs.items():
        await getattr(w.cog.config.guild(w.guild), key).set(value)


async def add_feet(w, mode="roles"):
    await set_cfg(
        w,
        interests={"feet": {"key": "feet", "name": "Feet", "emoji": "x",
                            "channel_id": FEET_CHANNEL, "role_id": FEET_ROLE}},
        access_mode=mode,
    )


def log_text(w):
    return "\n".join(t for t in w.log.sent if t)


# ----------------------------------------------------- import / lifecycle

def test_build_probe_string_is_stable():
    assert C.BUILD.startswith("afterdark build:")


def test_commands_build_under_the_stub(w):
    # Decorator chains (groups, nested groups, listeners) executed at import.
    assert w.cog.afterdark is not None
    assert w.cog.afterdark_interest is not None


async def test_cog_load_registers_persistent_views_and_unload_cleans_up(w):
    import asyncio

    await w.cog.cog_load()
    assert len(w.bot.views) == 3          # rabbit button, invitation DM, moderator review prompt
    task = w.cog._task
    assert task is not None and not task.done()
    await w.cog.cog_unload()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


# ------------------------------------------------------ rabbit button

async def test_button_admits_eligible_member(w):
    m = w.member("alice", level=3)
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_rabbit_claim(inter)
    assert w.rabbit in m.roles
    assert "through" in inter.replies[0]
    assert "entered Rabbit Hole via button" in log_text(w)


async def test_button_level_too_low_gets_hint_and_no_role(w):
    m = w.member(level=2)
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_rabbit_claim(inter)
    assert w.rabbit not in m.roles
    assert inter.replies[0].startswith("Not yet")


async def test_button_excluded_member_told_to_contact_modmail(w):
    m = w.member()
    await set_cfg(w, excluded={str(m.id): {"by": 1, "reason": "x", "ts": 0}})
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_rabbit_claim(inter)
    assert w.rabbit not in m.roles
    assert inter.replies[0] == C.CONTACT_TEXT


async def test_button_underage_gets_vague_refusal(w):
    m = w.member(roles=[w.adult, w.under])
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_rabbit_claim(inter)
    assert w.rabbit not in m.roles
    assert inter.replies[0] == "Not for you."
    assert "age" not in inter.replies[0].lower()


async def test_button_fails_closed_when_levelup_unreadable(w):
    del w.bot.cogs["LevelUp"]
    m = w.member()
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_rabbit_claim(inter)
    assert w.rabbit not in m.roles
    assert "can't find you" in inter.replies[0]


async def test_button_reports_hierarchy_problem_without_granting(w):
    w.rabbit.position = 200  # above the bot
    m = w.member()
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_rabbit_claim(inter)
    assert w.rabbit not in m.roles
    assert "went wrong" in inter.replies[0]
    assert "cannot manage" in log_text(w)


async def test_button_is_idempotent_for_existing_holder(w):
    m = w.member()
    m.roles.append(w.rabbit)
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_rabbit_claim(inter)
    assert "already" in inter.replies[0]


# ------------------------------------------------------------ interests

async def test_interest_requires_rabbit_hole_first(w):
    await add_feet(w)
    m = w.member()
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_interest_toggle(inter, "feet")
    assert w.feet_role not in m.roles
    assert "haven't found the rabbit" in inter.replies[0]


async def test_interest_join_then_leave_roles_mode(w):
    await add_feet(w)
    m = w.member()
    m.roles.append(w.rabbit)
    await w.cog.handle_interest_toggle(FakeInteraction(m, w.guild), "feet")
    assert w.feet_role in m.roles
    state = await w.cog._get_state(w.guild)
    assert state["feet"][str(m.id)]["since"] == NOW
    await w.cog.handle_interest_toggle(FakeInteraction(m, w.guild), "feet")
    assert w.feet_role not in m.roles
    assert str(m.id) not in state["feet"]


async def test_interest_overwrite_mode_and_cleanup_of_both_kinds(w):
    await add_feet(w, mode="overwrites")
    m = w.member()
    m.roles.append(w.rabbit)
    await w.cog.handle_interest_toggle(FakeInteraction(m, w.guild), "feet")
    assert m in w.feet.overwrites and w.feet_role not in m.roles
    # Flip to roles mode while the member also holds the role: removal cleans both.
    m.roles.append(w.feet_role)
    removed = await w.cog._revoke_all(m, reason="test")
    assert m not in w.feet.overwrites and w.feet_role not in m.roles and w.rabbit not in m.roles
    assert "Rabbit Hole" in removed


async def test_interest_overwrite_cap_refuses(w):
    await add_feet(w, mode="overwrites")
    for i in range(C.OVERWRITE_SOFT_CAP):
        w.feet.overwrites[object()] = None
    m = w.member()
    m.roles.append(w.rabbit)
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_interest_toggle(inter, "feet")
    assert m not in w.feet.overwrites
    assert "went wrong" in inter.replies[0]
    assert "100-overwrite" in log_text(w)


async def test_lapsed_member_cannot_rejoin_and_is_told_modmail(w):
    await add_feet(w)
    m = w.member()
    m.roles.append(w.rabbit)
    await set_cfg(w, lapsed={"feet": [m.id]})
    inter = FakeInteraction(m, w.guild)
    await w.cog.handle_interest_toggle(inter, "feet")
    assert w.feet_role not in m.roles
    assert C.CONTACT_TEXT in inter.replies[0]


async def test_clear_command_unblocks_a_lapsed_member(w):
    await add_feet(w)
    m = w.member()
    await set_cfg(w, lapsed={"feet": [m.id]}, declined=[m.id])
    ctx = FakeCtx(w.guild, w.member("mod"))
    await w.cog.afterdark_clear.func(w.cog, ctx, m, "feet")
    assert (await w.cog.config.guild(w.guild).lapsed()) == {}
    assert (await w.cog.config.guild(w.guild).declined()) == [m.id]  # invite decline untouched
    await w.cog.afterdark_clear.func(w.cog, ctx, m, "invite")
    assert (await w.cog.config.guild(w.guild).declined()) == []


# --------------------------------------------------------------- exclude

async def test_exclude_revokes_everything_and_blocks_regrant(w):
    await add_feet(w)
    m = w.member()
    m.roles += [w.rabbit, w.feet_role]
    await set_cfg(w, invites={str(m.id): Invite(ts=NOW).to_dict()})
    ctx = FakeCtx(w.guild, w.member("mod"))
    await w.cog.afterdark_exclude.func(w.cog, ctx, m, reason="spam")
    assert w.rabbit not in m.roles and w.feet_role not in m.roles
    assert str(m.id) in await w.cog.config.guild(w.guild).excluded()
    assert await w.cog.config.guild(w.guild).invites() == {}
    # Mod grant is refused until unexclude
    await w.cog.afterdark_grant.func(w.cog, ctx, m)
    assert w.rabbit not in m.roles and "exclude list" in ctx.sent[-1]
    await w.cog.afterdark_unexclude.func(w.cog, ctx, m)
    await w.cog.afterdark_grant.func(w.cog, ctx, m)
    assert w.rabbit in m.roles


async def test_mod_grant_skips_level_but_still_requires_roles(w):
    ctx = FakeCtx(w.guild, w.member("mod"))
    low = w.member(level=0)
    await w.cog.afterdark_grant.func(w.cog, ctx, low)
    assert w.rabbit in low.roles
    no_adult = w.member(roles=[w.age])
    await w.cog.afterdark_grant.func(w.cog, ctx, no_adult)
    assert w.rabbit not in no_adult.roles and "Adult Chat" in ctx.sent[-1]
    kid = w.member(roles=[w.adult, w.under])
    await w.cog.afterdark_grant.func(w.cog, ctx, kid)
    assert w.rabbit not in kid.roles and "underage" in ctx.sent[-1]


# ------------------------------------------------------------ listeners

async def test_losing_adult_chat_revokes_immediately(w):
    await add_feet(w)
    m = w.member()
    m.roles += [w.rabbit, w.feet_role]
    before = SimpleNamespace(roles=list(m.roles), bot=False, guild=w.guild)
    m.roles.remove(w.adult)
    m.roles_before = before
    await w.cog.on_member_update(before, m)
    assert w.rabbit not in m.roles and w.feet_role not in m.roles
    assert "Revoked Rabbit Hole" in log_text(w) and "no_adult_chat" in log_text(w)


async def test_gaining_underage_role_revokes(w):
    m = w.member()
    m.roles.append(w.rabbit)
    before = SimpleNamespace(roles=list(m.roles))
    m.roles.append(w.under)
    await w.cog.on_member_update(before, m)
    assert w.rabbit not in m.roles


async def test_lurker_role_pauses_all_revocation(w):
    """Lurker strips Adult Chat and the rabbit but adds its own role first."""
    m = w.member()
    m.roles += [w.rabbit, w.lurker]
    before = SimpleNamespace(roles=list(m.roles))
    m.roles.remove(w.adult)
    await w.cog.on_member_update(before, m)
    assert w.rabbit in m.roles  # untouched
    assert log_text(w) == ""


async def test_manual_rabbit_for_ineligible_member_is_stripped_eligible_is_kept(w):
    ok = w.member()
    bad = w.member(roles=[w.age])  # no Adult Chat
    for m in (ok, bad):
        before = SimpleNamespace(roles=list(m.roles))
        m.roles.append(w.rabbit)
        await w.cog.on_member_update(before, m)
    assert w.rabbit in ok.roles
    assert w.rabbit not in bad.roles


async def test_interest_role_without_rabbit_is_removed(w):
    await add_feet(w)
    m = w.member()
    before = SimpleNamespace(roles=list(m.roles))
    m.roles.append(w.feet_role)
    await w.cog.on_member_update(before, m)
    assert w.feet_role not in m.roles


async def test_member_leaving_clears_tracking_and_invite(w):
    await add_feet(w)
    m = w.member()
    state = await w.cog._get_state(w.guild)
    state["feet"] = {str(m.id): engine.new_membership(NOW)}
    await set_cfg(w, invites={str(m.id): Invite(ts=NOW).to_dict()},
                  granted={str(m.id): {"ts": 1, "via": "button", "by": None}})
    await w.cog.on_member_remove(m)
    assert state["feet"] == {}
    assert await w.cog.config.guild(w.guild).invites() == {}
    assert await w.cog.config.guild(w.guild).granted() == {}


# -------------------------------------------------------------- activity

async def test_message_in_interest_channel_refreshes_clock_and_clears_warning(w):
    await add_feet(w)
    m = w.member()
    state = await w.cog._get_state(w.guild)
    state["feet"] = {str(m.id): {"since": NOW - 20 * DAY, "last": NOW - 10 * DAY, "warned": NOW - 3 * DAY}}
    msg = SimpleNamespace(guild=w.guild, author=m, channel=SimpleNamespace(id=FEET_CHANNEL))
    await w.cog.on_message(msg)
    assert state["feet"][str(m.id)]["last"] == NOW
    assert state["feet"][str(m.id)]["warned"] == 0.0


async def test_untracked_poster_and_other_channels_are_ignored(w):
    await add_feet(w)
    m = w.member()
    state = await w.cog._get_state(w.guild)
    state["feet"] = {}
    await w.cog.on_message(SimpleNamespace(guild=w.guild, author=m, channel=SimpleNamespace(id=FEET_CHANNEL)))
    assert state["feet"] == {}
    state["feet"] = {str(m.id): {"since": 1.0, "last": 1.0, "warned": 0.0}}
    await w.cog.on_message(SimpleNamespace(guild=w.guild, author=m, channel=SimpleNamespace(id=999)))
    assert state["feet"][str(m.id)]["last"] == 1.0


async def test_thread_inside_interest_channel_counts(w):
    await add_feet(w)
    m = w.member()
    state = await w.cog._get_state(w.guild)
    state["feet"] = {str(m.id): {"since": 1.0, "last": 1.0, "warned": 0.0}}
    w.guild.channels[("thread", 5551)] = SimpleNamespace(parent_id=FEET_CHANNEL)
    await w.cog.on_message(SimpleNamespace(guild=w.guild, author=m, channel=SimpleNamespace(id=5551)))
    assert state["feet"][str(m.id)]["last"] == NOW


async def test_reaction_counts_as_activity(w):
    await add_feet(w)
    m = w.member()
    state = await w.cog._get_state(w.guild)
    state["feet"] = {str(m.id): {"since": 1.0, "last": 1.0, "warned": 0.0}}
    payload = SimpleNamespace(guild_id=w.guild.id, user_id=m.id, channel_id=FEET_CHANNEL)
    await w.cog.on_raw_reaction_add(payload)
    assert state["feet"][str(m.id)]["last"] == NOW


async def test_bot_messages_never_count(w):
    await add_feet(w)
    b = w.member(bot=True)
    state = await w.cog._get_state(w.guild)
    state["feet"] = {str(b.id): {"since": 1.0, "last": 1.0, "warned": 0.0}}
    await w.cog.on_message(SimpleNamespace(guild=w.guild, author=b, channel=SimpleNamespace(id=FEET_CHANNEL)))
    assert state["feet"][str(b.id)]["last"] == 1.0


# ----------------------------------------------------------------- sweep

async def tracked(w, idle_days, *, warned=0.0, roles_extra=(), name="x"):
    m = w.member(name)
    m.roles += [w.rabbit, w.feet_role, *roles_extra]
    state = await w.cog._get_state(w.guild)
    state.setdefault("feet", {})[str(m.id)] = {
        "since": NOW - idle_days * DAY, "last": NOW - idle_days * DAY, "warned": warned}
    return m


async def test_sweep_dry_run_changes_nothing_but_reports(w):
    await add_feet(w)
    kid = w.member(roles=[w.adult, w.under])
    kid.roles.append(w.rabbit)
    idle = await tracked(w, 15)
    summary = await w.cog._sweep_guild(w.guild)  # dry_run defaults to True
    await w.cog._report_sweep(w.guild, summary)
    assert summary["dry"] is True
    assert w.rabbit in kid.roles and w.feet_role in idle.roles
    assert await w.cog.config.guild(w.guild).lapsed() == {}
    assert idle.dms == []
    assert "would revoke" in log_text(w) and "would remove" in log_text(w)


async def test_sweep_live_revokes_ineligible_and_removes_inactive(w):
    await add_feet(w)
    await set_cfg(w, dry_run=False)
    kid = w.member(roles=[w.adult, w.under])
    kid.roles.append(w.rabbit)
    idle = await tracked(w, 15)
    active = await tracked(w, 2, name="active")
    await w.cog._sweep_guild(w.guild)
    assert w.rabbit not in kid.roles
    assert w.feet_role not in idle.roles and w.rabbit in idle.roles  # only the interest is lost
    assert engine.lapsed_has(await w.cog.config.guild(w.guild).lapsed(), "feet", idle.id)
    assert idle.dms and C.CONTACT_TEXT in idle.dms[0]["content"]
    assert w.feet_role in active.roles


async def test_warning_sent_once_then_not_again(w):
    await add_feet(w)
    await set_cfg(w, dry_run=False)
    m = await tracked(w, 8)
    await w.cog._sweep_guild(w.guild)
    assert len(m.dms) == 1 and "removed in about" in m.dms[0]["content"]
    await w.cog._sweep_guild(w.guild)
    assert len(m.dms) == 1  # same cycle: no second warning
    assert w.feet_role in m.roles


async def test_member_with_closed_dms_is_not_marked_warned(w):
    await add_feet(w)
    await set_cfg(w, dry_run=False)
    m = await tracked(w, 8)
    m.dm_open = False
    await w.cog._sweep_guild(w.guild)
    state = await w.cog._get_state(w.guild)
    assert state["feet"][str(m.id)]["warned"] == 0.0  # will retry next sweep


async def test_staff_are_exempt_from_inactivity(w):
    await add_feet(w)
    await set_cfg(w, dry_run=False)
    mod = await tracked(w, 30, roles_extra=[w.staff])
    admin = await tracked(w, 30, name="admin")
    admin.guild_permissions = SimpleNamespace(administrator=True, manage_guild=False)
    await w.cog._sweep_guild(w.guild)
    assert w.feet_role in mod.roles and w.feet_role in admin.roles


async def test_lurker_members_are_never_touched_and_get_a_fresh_clock(w):
    await add_feet(w)
    await set_cfg(w, dry_run=False)
    # Lurker stripped Adult Chat and the rabbit; their stored interest entry is ancient.
    m = w.member(roles=[w.age, w.lurker])
    state = await w.cog._get_state(w.guild)
    state["feet"] = {str(m.id): {"since": NOW - 60 * DAY, "last": NOW - 60 * DAY, "warned": 0.0}}
    await w.cog._sweep_guild(w.guild)
    assert engine.lapsed_has(await w.cog.config.guild(w.guild).lapsed(), "feet", m.id) is False
    assert m.dms == []
    assert state["feet"][str(m.id)]["since"] == NOW  # clock restarted for their return


async def test_sweep_skips_a_lurker_who_still_holds_the_rabbit_role(w):
    """A lurker with no Adult Chat looks ineligible, but is only paused."""
    await set_cfg(w, dry_run=False)
    m = w.member(roles=[w.age, w.lurker])  # Adult Chat stripped by the Lurker cog
    m.roles.append(w.rabbit)
    summary = await w.cog._sweep_guild(w.guild)
    assert w.rabbit in m.roles
    assert summary["revoke"] == []


async def test_lurker_holding_a_per_user_overwrite_loses_it(w):
    await add_feet(w, mode="overwrites")
    await set_cfg(w, dry_run=False)
    m = w.member(roles=[w.age, w.lurker])
    w.feet.overwrites[m] = object()
    state = await w.cog._get_state(w.guild)
    state["feet"] = {str(m.id): engine.new_membership(NOW)}
    await w.cog._sweep_guild(w.guild)
    assert m not in w.feet.overwrites
    assert engine.lapsed_has(await w.cog.config.guild(w.guild).lapsed(), "feet", m.id) is False


async def test_sweep_circuit_breaker_aborts_without_acting(w):
    await set_cfg(w, dry_run=False, sweep_max=1)
    kids = []
    for _ in range(2):
        kid = w.member(roles=[w.adult, w.under])
        kid.roles.append(w.rabbit)
        kids.append(kid)
    summary = await w.cog._sweep_guild(w.guild)
    await w.cog._report_sweep(w.guild, summary)
    assert summary["aborted"] is True
    assert all(w.rabbit in k.roles for k in kids)
    assert "ABORTED" in log_text(w)


async def test_sweep_does_not_revoke_on_low_level_or_unreadable_level(w):
    await set_cfg(w, dry_run=False)
    m = w.member(level=0)
    m.roles.append(w.rabbit)
    del w.bot.cogs["LevelUp"]
    await w.cog._sweep_guild(w.guild)
    assert w.rabbit in m.roles


async def test_sweep_adopts_role_holders_only_when_live(w):
    await add_feet(w)
    m = w.member()
    m.roles += [w.rabbit, w.feet_role]
    summary = await w.cog._sweep_guild(w.guild)  # dry
    state = await w.cog._get_state(w.guild)
    assert summary["adopted"] == 1 and str(m.id) not in state.get("feet", {})
    await set_cfg(w, dry_run=False)
    await w.cog._sweep_guild(w.guild)
    assert state["feet"][str(m.id)]["since"] == NOW


async def test_state_survives_flush_to_config(w):
    await add_feet(w)
    await set_cfg(w, dry_run=False)
    m = w.member()
    m.roles += [w.rabbit, w.feet_role]
    await w.cog._sweep_guild(w.guild)
    saved = await w.cog.config.guild(w.guild).interest_state()
    assert str(m.id) in saved["feet"]


# ----------------------------------------------------------- invitations

async def test_candidates_exclude_everyone_who_should_be_skipped(w):
    good = w.member("good")
    w.member("low", level=1)
    w.member("kid", roles=[w.adult, w.under])
    w.member("noadult", roles=[w.age])
    w.member("bot", bot=True)
    holder = w.member("holder")
    holder.roles.append(w.rabbit)
    declined = w.member("declined")
    pending = w.member("pending")
    lurk = w.member("lurk")
    lurk.roles.append(w.lurker)
    excl = w.member("excl")
    await set_cfg(w, declined=[declined.id], invites={str(pending.id): Invite(ts=NOW).to_dict()},
                  excluded={str(excl.id): {"by": 1, "reason": "", "ts": 0}})
    cfg = await w.cog.config.guild(w.guild).all()
    assert await w.cog._invite_candidates(w.guild, cfg) == [good.id]


def prompts(w):
    """Review prompts posted to the log channel, oldest first."""
    return [m for m in w.log.messages.values() if m.view is not None]


def mod_click(w, message, name="mod"):
    mod = w.member(name, roles=[w.staff])
    click = FakeInteraction(mod, w.guild)
    click.message = message
    return click, mod


async def test_review_prompt_goes_to_the_highest_level_member_first(w):
    low, high, mid = w.member("low", level=4), w.member("high", level=40), w.member("mid", level=12)
    await set_cfg(w, invites_enabled=True)
    await w.cog._sweep_guild(w.guild)
    (prompt,) = prompts(w)
    assert str(high.id) in prompt.embeds[0].description or high.mention in prompt.embeds[0].description
    assert any(f.value == "40" for f in prompt.embeds[0].fields)
    assert high.dms == []                                  # nothing is sent until a mod says yes


async def test_equal_levels_go_oldest_account_first(w):
    newer, older = w.member("a", level=9), w.member("b", level=9)
    older.id, newer.id = 10, 20
    w.levels[10] = w.levels[20] = 9
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    assert (await w.cog.config.guild(w.guild).invite_round())["prompt"]["user_id"] == 10


async def test_yes_sends_the_invitation_and_shows_the_next_candidate(w):
    first, second = w.member("first", level=30), w.member("second", level=20)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    click, _ = mod_click(w, prompts(w)[0])
    await w.cog.handle_review(click, True)

    assert len(first.dms) == 1 and "Rabbit Hole" in first.dms[0]["embed"].description
    assert str(first.id) in await w.cog.config.guild(w.guild).invites()
    assert click.edits and click.edits[0]["view"] is None
    assert "Invitation sent" in click.edits[0]["embed"].fields[-1].value
    assert len(prompts(w)) == 2                            # the next candidate is up
    assert (await w.cog.config.guild(w.guild).invite_round())["prompt"]["user_id"] == second.id
    assert (await w.cog.config.guild(w.guild).invite_round())["sent"] == 1


async def test_not_now_skips_them_but_keeps_them_in_the_pool(w):
    first, second = w.member("first", level=30), w.member("second", level=20)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    click, _ = mod_click(w, prompts(w)[0])
    await w.cog.handle_review(click, False)

    assert first.dms == [] and "Not now" in click.edits[0]["embed"].fields[-1].value
    cfg = await w.cog.config.guild(w.guild).all()
    assert str(first.id) not in cfg["invites"] and first.id not in cfg["declined"]     # not locked out
    assert cfg["invite_round"]["prompt"]["user_id"] == second.id                      # moved on
    assert first.id in await w.cog._invite_candidates(w.guild, cfg)                    # still in the pool


async def test_not_now_returns_after_the_snooze_and_not_before(w):
    first = w.member("first", level=30)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    click, _ = mod_click(w, prompts(w)[0])
    await w.cog.handle_review(click, False)
    cfg = await w.cog.config.guild(w.guild).all()
    pool, _ = await w.cog._review_pool(w.guild, cfg, NOW + 6 * DAY)
    assert pool == []
    pool, _ = await w.cog._review_pool(w.guild, cfg, NOW + 8 * DAY)
    assert pool == [first.id]


async def test_closed_dms_are_snoozed_and_the_round_moves_on(w):
    closed = w.member("closed", level=30, dm_open=False)
    ok = w.member("ok", level=20)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    click, _ = mod_click(w, prompts(w)[0])
    await w.cog.handle_review(click, True)
    assert "DMs closed" in click.edits[0]["embed"].fields[-1].value
    cfg = await w.cog.config.guild(w.guild).all()
    assert cfg["invite_round"]["sent"] == 0 and cfg["invite_round"]["prompt"]["user_id"] == ok.id
    assert str(closed.id) in cfg["invite_snoozed"]


async def test_round_stops_at_the_target(w):
    for i in range(6):
        w.member(f"m{i}", level=10 + i)
    await set_cfg(w, invites_enabled=True, invite_min=2, invite_max=2)
    await w.cog._start_round(w.guild)
    for _ in range(2):
        click, _ = mod_click(w, prompts(w)[-1])
        await w.cog.handle_review(click, True)
    rnd = await w.cog.config.guild(w.guild).invite_round()
    assert rnd["sent"] == 2 and rnd["prompt"] == {} and len(prompts(w)) == 2
    assert "today's 2 invitations" in click.edits[0]["embed"].fields[-1].value


async def test_non_mod_cannot_answer_a_prompt(w):
    w.member("m", level=10)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    stranger = w.member("stranger")
    click = FakeInteraction(stranger, w.guild)
    click.message = prompts(w)[0]
    await w.cog.handle_review(click, True)
    assert click.replies == ["Only moderators can answer this."]
    assert (await w.cog.config.guild(w.guild).invite_round())["prompt"] != {}


async def test_two_mods_clicking_at_once_send_one_invitation(w):
    a = w.member("a", level=30)
    w.member("b", level=20)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    prompt = prompts(w)[0]
    c1, _ = mod_click(w, prompt, "mod1")
    c2, _ = mod_click(w, prompt, "mod2")
    await asyncio.gather(w.cog.handle_review(c1, True), w.cog.handle_review(c2, True))
    assert len(a.dms) == 1
    assert "out of date" in " ".join(c1.replies + c2.replies)


async def test_member_who_became_ineligible_before_the_click_is_skipped(w):
    first, second = w.member("first", level=30), w.member("second", level=20)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    first.roles.append(w.rabbit)                             # got in some other way meanwhile
    click, _ = mod_click(w, prompts(w)[0])
    await w.cog.handle_review(click, True)
    assert first.dms == [] and "no longer eligible" in click.edits[0]["embed"].fields[-1].value


async def test_one_round_per_la_day_and_only_when_enabled(w):
    for i in range(8):
        w.member(f"m{i}", level=10 + i)
    await w.cog._sweep_guild(w.guild)
    assert prompts(w) == []                                  # off by default
    await set_cfg(w, invites_enabled=True)
    first = await w.cog._sweep_guild(w.guild)
    assert first["invited"] == 1 and len(prompts(w)) == 1
    second = await w.cog._sweep_guild(w.guild)
    assert second["invited"] == 0 and len(prompts(w)) == 1   # same day: still the one waiting prompt
    w.clock[0] += DAY
    third = await w.cog._sweep_guild(w.guild)
    assert third["invited"] == 1 and len(prompts(w)) == 2
    assert "Replaced" in prompts(w)[0].edits[-1]["embed"].fields[-1].value   # yesterday's prompt was closed


async def test_a_deleted_prompt_is_replaced_on_the_next_sweep(w):
    w.member("m", level=10)
    await set_cfg(w, invites_enabled=True)
    await w.cog._sweep_guild(w.guild)
    gone = prompts(w)[0]
    del w.log.messages[gone.id]
    await w.cog._sweep_guild(w.guild)
    assert len(prompts(w)) == 1 and prompts(w)[0].id != gone.id


async def test_everyone_snoozed_ends_the_round_quietly(w):
    only = w.member("only", level=10)
    await set_cfg(w, invites_enabled=True)
    await w.cog._start_round(w.guild)
    click, _ = mod_click(w, prompts(w)[0])
    await w.cog.handle_review(click, False)
    assert (await w.cog.config.guild(w.guild).invite_round())["done"] is True
    assert "nobody left" in log_text(w)


async def test_invite_now_starts_a_round_but_sends_nothing(w):
    m = w.member("m", level=10)
    ctx = FakeCtx(w.guild, w.member("admin"))
    await w.cog.invite_now.func(w.cog, ctx)
    assert len(prompts(w)) == 1 and m.dms == []


async def test_clear_invite_also_lifts_a_snooze(w):
    m = w.member("m")
    await set_cfg(w, invite_snoozed={str(m.id): NOW + 5 * DAY})
    ctx = FakeCtx(w.guild, w.member("mod"))
    await w.cog.afterdark_clear.func(w.cog, ctx, m, "invite")
    assert await w.cog.config.guild(w.guild).invite_snoozed() == {}


async def test_invitation_expires_after_30_days_and_member_is_eligible_again(w):
    m = w.member("m")
    await set_cfg(w, invites={str(m.id): Invite(ts=NOW - 30 * DAY).to_dict()})
    summary = await w.cog._sweep_guild(w.guild)
    assert summary["expired_invites"] == 1
    assert await w.cog.config.guild(w.guild).invites() == {}
    cfg = await w.cog.config.guild(w.guild).all()
    assert m.id in await w.cog._invite_candidates(w.guild, cfg)


async def test_accept_grants_rabbit_and_clears_invite(w):
    m = w.member()
    await set_cfg(w, invites={str(m.id): Invite(ts=NOW).to_dict()})
    inter = FakeInteraction(m)
    await w.cog.handle_invite_accept(inter)
    assert w.rabbit in m.roles
    assert await w.cog.config.guild(w.guild).invites() == {}
    assert inter.edits and inter.edits[0]["view"] is None


async def test_accept_rechecks_eligibility_at_click_time(w):
    m = w.member()
    await set_cfg(w, invites={str(m.id): Invite(ts=NOW).to_dict()},
                  excluded={str(m.id): {"by": 1, "reason": "", "ts": 0}})
    inter = FakeInteraction(m)
    await w.cog.handle_invite_accept(inter)
    assert w.rabbit not in m.roles
    assert inter.replies == [C.CONTACT_TEXT]


async def test_decline_is_permanent(w):
    m = w.member()
    await set_cfg(w, invites={str(m.id): Invite(ts=NOW).to_dict()})
    await w.cog.handle_invite_decline(FakeInteraction(m))
    assert await w.cog.config.guild(w.guild).declined() == [m.id]
    assert await w.cog.config.guild(w.guild).invites() == {}
    cfg = await w.cog.config.guild(w.guild).all()
    assert m.id not in await w.cog._invite_candidates(w.guild, cfg)


async def test_stale_invitation_button_reports_expired(w):
    m = w.member()
    inter = FakeInteraction(m)
    await w.cog.handle_invite_accept(inter)
    assert w.rabbit not in m.roles
    assert inter.edits and "expired" in inter.edits[0]["embed"].title.lower()


# ------------------------------------------------------- posts / levelup

async def test_level_reader_defaults(w):
    m = w.member(level=7)
    assert await w.cog._get_level(m) == 7
    stranger = FakeMember(w.guild, "new", [])
    assert await w.cog._get_level(stranger) == 0  # no profile yet -> level 0
    del w.bot.cogs["LevelUp"]
    assert await w.cog._get_level(m) is None


async def test_post_command_sends_then_edits_same_message(w):
    ctx = FakeCtx(w.guild, w.member("admin"))
    clue = w.guild.add_channel(C.DEFAULT_CLUE_CHANNEL_ID, "pillow-talk")
    sent = []

    async def send(text=None, **kwargs):
        sent.append(kwargs)
        return FakeMessage(77, clue)

    clue.send = send
    await w.cog.afterdark_post.func(w.cog, ctx, None)
    saved = await w.cog.config.guild(w.guild).rabbit_post()
    assert saved == {"channel_id": C.DEFAULT_CLUE_CHANNEL_ID, "message_id": 77}
    assert sent and sent[0]["embed"].title == "\U0001F407"


async def test_interest_add_validates_and_registers_view(w):
    ctx = FakeCtx(w.guild, w.member("admin"))
    chan = w.feet
    await w.cog.interest_add.func(w.cog, ctx, "Feet!", chan, w.feet_role, "x", name="Feet")
    cfg = await w.cog.config.guild(w.guild).all()
    assert list(cfg["interests"]) == ["feet"] and cfg["interests"]["feet"]["role_id"] == FEET_ROLE
    assert w.guild.id in w.cog._interest_views
    await w.cog.interest_add.func(w.cog, ctx, "!!!", chan, None, "")
    assert "usable" in ctx.sent[-1]


async def test_set_inactivity_rejects_bad_pairs(w):
    ctx = FakeCtx(w.guild, w.member("admin"))
    await w.cog.set_inactivity.func(w.cog, ctx, 14, 7)
    assert await w.cog.config.guild(w.guild).warn_days() == 7  # unchanged defaults
    await w.cog.set_inactivity.func(w.cog, ctx, 3, 10)
    assert await w.cog.config.guild(w.guild).warn_days() == 3
    assert await w.cog.config.guild(w.guild).remove_days() == 10


async def test_dryrun_toggle_is_logged(w):
    ctx = FakeCtx(w.guild, w.member("admin"))
    await w.cog.set_dryrun.func(w.cog, ctx, "off")
    assert await w.cog.config.guild(w.guild).dry_run() is False
    assert "OFF (LIVE)" in log_text(w)
    await w.cog.set_dryrun.func(w.cog, ctx, "maybe")
    assert "on" in ctx.sent[-1].lower()


async def test_defaults_match_the_agreed_design(w):
    cfg = await w.cog.config.guild(w.guild).all()
    assert cfg["min_level"] == 3 and cfg["warn_days"] == 7 and cfg["remove_days"] == 14
    assert cfg["dry_run"] is True and cfg["invites_enabled"] is False
    assert (cfg["invite_min"], cfg["invite_max"], cfg["invite_ttl_days"]) == (3, 4, 30)
    assert cfg["access_mode"] == "overwrites"
    assert cfg["rabbit_role_id"] == 1557705475091472445
    assert cfg["adult_role_id"] == 1400989955794276452
    assert 554841909412102169 in cfg["adult_age_role_ids"]  # 30+ role included


# ------------------------------------------------ v1.1.0: emoji guard + manual interest access

def test_valid_emoji_accepts_real_and_rejects_words():
    assert engine.valid_emoji("\U0001F9B6")
    assert engine.valid_emoji("<:foot:123456789012345678>")
    assert engine.valid_emoji("<a:wave:123456789012345678>")
    for bad in ("", "Feet", "x", ":foot:", "feet1", "<:foot:12>", "  "):
        assert not engine.valid_emoji(bad)


def test_split_emoji_and_name_moves_a_word_into_the_name():
    assert engine.split_emoji_and_name("Feet", "Lovers") == ("", "Feet Lovers")
    assert engine.split_emoji_and_name("\U0001F9B6", "Feet") == ("\U0001F9B6", "Feet")
    assert engine.split_emoji_and_name("", "Feet") == ("", "Feet")


async def test_default_access_mode_is_per_user_overwrites(w):
    assert await w.cog.config.guild(w.guild).access_mode() == "overwrites"


async def test_interest_add_with_a_word_in_the_emoji_slot_keeps_the_panel_valid(w):
    ctx = FakeCtx(w.guild, w.member("admin"))
    await w.cog.interest_add.func(w.cog, ctx, "feet", w.feet, None, "Feet", name="")
    cfg = await w.cog.config.guild(w.guild).all()
    assert cfg["interests"]["feet"]["emoji"] == "" and cfg["interests"]["feet"]["name"] == "Feet"
    assert "No valid emoji" in ctx.sent[-1]


def test_interest_view_drops_an_invalid_stored_emoji(w):
    from .models import Interest
    view = ad.InterestView(w.cog, [Interest(key="feet", name="Feet", emoji="Feet", channel_id=1, role_id=None)])
    assert view.children[0].emoji is None


def test_panel_embed_is_white_and_carries_the_rules():
    from . import embeds
    emb = embeds.panel_embed([], 7, 14, rules="1. Be 18+.\n2. Be kind.")
    assert emb.color.name == C.WHITE if hasattr(emb.color, "name") else emb.color.value == C.WHITE
    assert "1. Be 18+." in emb.description and "Rabbit Hole rules" in emb.title
    assert any("Tap a button" in f.value and "14 days" in f.value for f in emb.fields)


def test_panel_embed_without_rules_still_explains_the_buttons():
    from . import embeds
    emb = embeds.panel_embed([], 7, 14, rules="")
    assert "Tap a button" in emb.description and not emb.fields


def test_panel_embed_no_longer_lists_channels_twice():
    from . import embeds
    from .models import Interest
    emb = embeds.panel_embed([Interest(key="feet", name="Feet", emoji="Feet", channel_id=1, role_id=None)], 7, 14, rules="x")
    assert all("Open now" != f.name for f in emb.fields)


async def test_grantinterest_gives_a_per_user_overwrite_and_clears_a_lapse(w):
    await add_feet(w, mode="overwrites")
    m = w.member()
    await set_cfg(w, lapsed={"feet": [m.id]})
    m.roles.append(w.rabbit)
    ctx = FakeCtx(w.guild, w.member("mod"))
    await w.cog.afterdark_grantinterest.func(w.cog, ctx, m, "feet")
    assert m in w.feet.overwrites and w.feet_role not in m.roles
    assert not engine.lapsed_has(await w.cog.config.guild(w.guild).lapsed(), "feet", m.id)
    assert str(m.id) in (await w.cog._get_state(w.guild))["feet"]


async def test_grantinterest_requires_rabbit_hole_and_not_excluded(w):
    await add_feet(w, mode="overwrites")
    ctx = FakeCtx(w.guild, w.member("mod"))
    m = w.member()
    await w.cog.afterdark_grantinterest.func(w.cog, ctx, m, "feet")
    assert m not in w.feet.overwrites and "Rabbit Hole" in ctx.sent[-1]
    m.roles.append(w.rabbit)
    await set_cfg(w, excluded={str(m.id): {"by": 1, "reason": "x", "ts": 0}})
    await w.cog.afterdark_grantinterest.func(w.cog, ctx, m, "feet")
    assert m not in w.feet.overwrites and "Refused" in ctx.sent[-1]


async def test_revokeinterest_removes_access_without_recording_a_lapse(w):
    await add_feet(w, mode="overwrites")
    m = w.member()
    m.roles.append(w.rabbit)
    await w.cog.handle_interest_toggle(FakeInteraction(m, w.guild), "feet")
    assert m in w.feet.overwrites
    ctx = FakeCtx(w.guild, w.member("mod"))
    await w.cog.afterdark_revokeinterest.func(w.cog, ctx, m, "feet")
    assert m not in w.feet.overwrites
    assert not engine.lapsed_has(await w.cog.config.guild(w.guild).lapsed(), "feet", m.id)
