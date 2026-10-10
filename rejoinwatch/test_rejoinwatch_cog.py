"""Behavior + import-smoke tests for the RejoinWatch cog class.

redbot is faked (and discord.py topped up with any missing names) *inside a fixture*, so
these run the same with or without the real libraries installed and never touch the shared
root conftest.py.

What this proves: the decision logic (what counts as a leave, when the user is warned, when
mods are alerted, DM-then-fallback delivery, button permissions, double-click and
leave/rejoin race guards) and clean imports. It does NOT prove live Discord behaviour (real
audit-log entries, real DMs, button rendering, this bot's exact discord.py API shape).
"""
import asyncio
import copy
import importlib
import itertools
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import discord

NOW = datetime.now(timezone.utc)
DAY = 86400
_MSG_IDS = itertools.count(5000)


# ------------------------------------------------------------------ fakes

class _Call:
    """What Red's Value() returns: awaitable AND an async context manager."""

    def __init__(self, val):
        self.val = val

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.val.current())
        return _get().__await__()

    async def __aenter__(self):
        self.obj = copy.deepcopy(self.val.current())
        return self.obj

    async def __aexit__(self, *exc):
        if exc[0] is None:
            self.val.store(self.obj)


class _Val:
    def __init__(self, bucket, key, default):
        self.bucket, self.key, self.default = bucket, key, default

    def current(self):
        return self.bucket.get(self.key, copy.deepcopy(self.default))

    def store(self, value):
        self.bucket[self.key] = copy.deepcopy(value)

    def __call__(self):
        return _Call(self)

    async def set(self, value):
        self.store(value)


class _Group:
    def __init__(self, bucket, defaults):
        self._bucket, self._defaults = bucket, defaults

    def __getattr__(self, name):
        if name not in self._defaults:
            raise AttributeError(name)
        return _Val(self._bucket, name, self._defaults[name])

    def all(self):
        async def _all():
            return {k: _Val(self._bucket, k, d).current() for k, d in self._defaults.items()}
        return _all()


class FakeConfig:
    def __init__(self):
        self.data = {}
        self.gd = {}

    @classmethod
    def get_conf(cls, *a, **k):
        return cls()

    def register_guild(self, **d):
        self.gd = d

    def guild_from_id(self, gid):
        return _Group(self.data.setdefault(("g", gid), {}), self.gd)

    def guild(self, guild):
        return self.guild_from_id(guild.id)

    def all_guilds(self):
        async def _all():
            return {gid: {k: _Val(b, k, d).current() for k, d in self.gd.items()}
                    for (kind, gid), b in self.data.items() if kind == "g"}
        return _all()


class Perms(types.SimpleNamespace):
    def __init__(self, **kw):
        super().__init__(**{"ban_members": False, "administrator": False, "view_audit_log": True, **kw})


class FakeUser:
    def __init__(self, uid, name=None, bot=False):
        self.id, self.name, self.bot = uid, name or f"user{uid}", bot

    def __str__(self):
        return self.name

    @property
    def mention(self):
        return f"<@{self.id}>"


class FakeMember(FakeUser):
    def __init__(self, guild, uid, *, roles=(), perms=None, dm_ok=True, bot=False, name=None):
        super().__init__(uid, name, bot)
        self.guild = guild
        self.roles = [types.SimpleNamespace(id=r) for r in roles]
        self.guild_permissions = perms or Perms()
        self.display_name = self.name
        self.created_at = NOW - timedelta(days=30)
        self.dm_ok, self.dms = dm_ok, []

    async def send(self, text):
        await asyncio.sleep(0)
        if not self.dm_ok:
            raise discord.Forbidden("DMs closed")
        self.dms.append(text)


class FakeMessage:
    def __init__(self, channel, content, kw):
        self.id = next(_MSG_IDS)
        self.channel, self.content, self.kw = channel, content, kw
        self.embeds = [kw["embed"]] if "embed" in kw else []
        self.view = kw.get("view")
        self.allowed_mentions = kw.get("allowed_mentions")


class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name = cid, name
        self.sent, self.fail = [], False

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, **kw):
        await asyncio.sleep(0)
        if self.fail:
            raise discord.Forbidden("no send")
        msg = FakeMessage(self, content, kw)
        self.sent.append(msg)
        return msg


class FakeGuild:
    def __init__(self):
        self.id, self.name = 1, "Wonderland"
        self.me = types.SimpleNamespace(guild_permissions=Perms())
        self.channels, self.audit, self.bans = {}, [], []
        self.ban_error = None
        self.reject_seconds_kw = False  # emulate an older discord.py without delete_message_seconds

    def add_channel(self, cid, name):
        self.channels[cid] = FakeChannel(cid, name)
        return self.channels[cid]

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def audit_logs(self, limit=None, action=None):
        for entry in self.audit:
            if entry.action == action:
                yield entry

    async def ban(self, target, **kw):
        await asyncio.sleep(0)
        if self.reject_seconds_kw and "delete_message_seconds" in kw:
            raise TypeError("unexpected keyword argument 'delete_message_seconds'")
        if self.ban_error is not None:
            raise self.ban_error
        self.bans.append((target.id, kw))


class FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]
        self._guild = guild
        self.loop = types.SimpleNamespace(create_task=self._create_task)

    @staticmethod
    def _create_task(coro):
        coro.close()  # never run the background prune loop in unit tests
        return types.SimpleNamespace(cancel=lambda: None)

    def get_guild(self, gid):
        return self._guild if gid == self._guild.id else None

    async def wait_until_red_ready(self):
        pass


class FakeResponse:
    def __init__(self):
        self.sent, self.deferred = [], False

    def is_done(self):
        return self.deferred or bool(self.sent)

    async def defer(self):
        await asyncio.sleep(0)
        self.deferred = True

    async def send_message(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))


class FakeInteraction:
    def __init__(self, user, guild, message, custom_id):
        self.user, self.guild, self.message = user, guild, message
        self.data = {"custom_id": custom_id}
        self.response, self.followup, self.edits = FakeResponse(), FakeFollowup(), []

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


class FakeCtx:
    def __init__(self, guild):
        self.guild, self.sent, self.helped = guild, [], False

    async def send(self, text=None, **kw):
        self.sent.append(text)

    async def send_help(self):
        self.helped = True


def footer(embed):
    return getattr(embed, "footer_text", None) or getattr(getattr(embed, "footer", None), "text", None)


# --------------------------------------------------------------- fixture

@pytest.fixture
def env(monkeypatch):
    class _HTTPException(Exception):
        pass

    class _Forbidden(_HTTPException):
        pass

    class _Object:
        def __init__(self, id):
            self.id = id

    class _AllowedMentions:
        def __init__(self, **kw):
            self.kw = kw

        @classmethod
        def none(cls):
            inst = cls()
            inst.is_none = True
            return inst

    class _View:
        def __init__(self, timeout=None):
            self.timeout, self.children = timeout, []

        def add_item(self, item):
            self.children.append(item)

    class _Button:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    # discord names the cog needs at import/call time: always the fakes, so behaviour is
    # identical whether or not real discord.py is installed.
    for name, value in (
        ("Forbidden", _Forbidden), ("HTTPException", _HTTPException), ("Object", _Object),
        ("AllowedMentions", _AllowedMentions),
        ("AuditLogAction", types.SimpleNamespace(kick="kick", ban="ban", member_prune="member_prune")),
        ("ui", types.SimpleNamespace(View=_View, Button=_Button)),
    ):
        monkeypatch.setattr(discord, name, value, raising=False)
    for name in ("User", "Member", "Guild", "Role", "TextChannel", "Interaction"):
        if not hasattr(discord, name):
            monkeypatch.setattr(discord, name, type(name, (), {}), raising=False)

    class _Cmd:
        def __init__(self, f, **kw):
            self.callback, self.kw = f, kw

        def command(self, *a, **kw):
            return lambda f: _Cmd(f, **kw)

    def _deco(*a, **kw):
        return lambda f: _Cmd(f, **kw)

    def _check(*a, **kw):
        return lambda f: f

    class _Cog:
        @staticmethod
        def listener(*a, **kw):
            return lambda f: f

    commands_ns = types.SimpleNamespace(
        Cog=_Cog, group=_deco, command=_deco, guild_only=_check,
        mod_or_permissions=_check, admin_or_permissions=_check, Context=object,
    )
    core = types.ModuleType("redbot.core")
    core.Config, core.commands = FakeConfig, commands_ns
    root = types.ModuleType("redbot")
    monkeypatch.setitem(sys.modules, "redbot", root)
    monkeypatch.setitem(sys.modules, "redbot.core", core)
    monkeypatch.delitem(sys.modules, "rejoinwatch.rejoinwatch", raising=False)
    mod = importlib.import_module("rejoinwatch.rejoinwatch")
    constants = importlib.import_module("rejoinwatch.constants")

    guild = FakeGuild()
    mod_ch = guild.add_channel(constants.DEFAULT_MOD_CHANNEL_ID, "mod-chat")
    cuddle = guild.add_channel(555, "cuddle")
    cog = mod.RejoinWatch(FakeBot(guild))
    cog.SETTLE_SECONDS = 0
    cog.config.guild_from_id(1).cuddle_channel_id.store(555)

    return types.SimpleNamespace(mod=mod, constants=constants, cog=cog, guild=guild,
                                 mod_ch=mod_ch, cuddle=cuddle)


# ---------------------------------------------------------------- helpers

def cfg_val(env, key):
    return getattr(env.cog.config.guild_from_id(1), key)


def stored(env):
    return cfg_val(env, "leaves").current()


def seed(env, uid, *days_ago):
    leaves = stored(env)
    leaves[str(uid)] = [NOW.timestamp() - d * DAY for d in days_ago]
    cfg_val(env, "leaves").store(leaves)


async def leave(env, user):
    await env.cog.on_raw_member_remove(types.SimpleNamespace(guild_id=1, user=user))


def kick_entry(uid, seconds_ago=1, action="kick"):
    return types.SimpleNamespace(
        action=action, target=types.SimpleNamespace(id=uid),
        created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
    )


def mod_member(env, uid=900):
    return FakeMember(env.guild, uid, roles=[env.constants.DEFAULT_MOD_ROLE_ID], name="modperson")


async def click(env, user, message, action, uid=7):
    inter = FakeInteraction(user, env.guild, message, f"rejoinwatch:{action}:{uid}")
    await env.cog.on_interaction(inter)
    return inter


# ------------------------------------------------------------- leaving

async def test_first_leave_is_recorded_silently(env):
    await leave(env, FakeUser(7))
    assert len(stored(env)["7"]) == 1
    assert env.mod_ch.sent == [] and env.cuddle.sent == []


async def test_second_leave_alerts_mods_with_ban_buttons(env):
    seed(env, 7, 10)
    await leave(env, FakeUser(7, "wanderer"))
    assert len(stored(env)["7"]) == 2
    (msg,) = env.mod_ch.sent
    assert "has left 2 times now" in msg.embeds[0].description
    assert "wanderer" in msg.embeds[0].description
    ids = [b.custom_id for b in msg.view.children]
    assert ids == ["rejoinwatch:ban:7", "rejoinwatch:keep:7"]
    assert msg.view.timeout is None  # survives restarts: buttons are answered by on_interaction
    assert msg.allowed_mentions.is_none  # no role/user pings in the mod channel
    assert env.cuddle.sent == []  # nothing goes to the user on a repeat leave


async def test_third_leave_alerts_again(env):
    seed(env, 7, 20, 10)
    await leave(env, FakeUser(7))
    assert "has left 3 times now" in env.mod_ch.sent[0].embeds[0].description


async def test_expired_leaves_do_not_count(env):
    seed(env, 7, 200)  # outside the 180-day window
    await leave(env, FakeUser(7))
    assert len(stored(env)["7"]) == 1
    assert env.mod_ch.sent == []


async def test_kick_is_not_a_leave(env):
    env.guild.audit.append(kick_entry(7))
    await leave(env, FakeUser(7))
    assert stored(env) == {}
    assert env.mod_ch.sent == []


async def test_ban_audit_entry_is_not_a_leave(env):
    env.guild.audit.append(kick_entry(7, action="ban"))
    await leave(env, FakeUser(7))
    assert stored(env) == {}


async def test_ban_event_is_not_a_leave_even_without_audit_access(env):
    env.guild.me.guild_permissions = Perms(view_audit_log=False)
    await env.cog.on_member_ban(env.guild, FakeUser(7))
    await leave(env, FakeUser(7))
    assert stored(env) == {}


async def test_prune_is_not_a_leave(env):
    env.guild.audit.append(kick_entry(0, action="member_prune"))
    await leave(env, FakeUser(7))
    assert stored(env) == {}


async def test_old_kick_entry_does_not_hide_a_later_leave(env):
    env.guild.audit.append(kick_entry(7, seconds_ago=600))
    await leave(env, FakeUser(7))
    assert len(stored(env)["7"]) == 1


async def test_kick_of_someone_else_does_not_hide_this_leave(env):
    env.guild.audit.append(kick_entry(8))
    await leave(env, FakeUser(7))
    assert len(stored(env)["7"]) == 1


async def test_without_audit_access_leave_is_counted(env):
    env.guild.me.guild_permissions = Perms(view_audit_log=False)
    env.guild.audit.append(kick_entry(7))
    await leave(env, FakeUser(7))
    assert len(stored(env)["7"]) == 1


async def test_bots_and_disabled_tracking_are_ignored(env):
    await leave(env, FakeUser(7, bot=True))
    assert stored(env) == {}
    cfg_val(env, "enabled").store(False)
    await leave(env, FakeUser(8))
    await env.cog.on_member_join(FakeMember(env.guild, 8))
    assert stored(env) == {} and env.mod_ch.sent == []


async def test_leave_in_unknown_guild_is_ignored(env):
    await env.cog.on_raw_member_remove(types.SimpleNamespace(guild_id=999, user=FakeUser(7)))
    assert stored(env) == {}


# ------------------------------------------------------------- rejoining

async def test_rejoin_after_one_leave_dms_the_warning_and_tells_mods(env):
    seed(env, 7, 5)
    member = FakeMember(env.guild, 7, name="wanderer")
    await env.cog.on_member_join(member)
    (dm,) = member.dms
    assert "Wonderland" in dm and "may be banned" in dm
    assert env.cuddle.sent == []
    (msg,) = env.mod_ch.sent
    text = msg.embeds[0].description
    assert "has rejoined after leaving 1 time" in text and "warned via DM" in text
    assert msg.view is None  # no buttons: they were only just warned


async def test_dm_closed_falls_back_to_cuddle_with_a_ping(env):
    seed(env, 7, 5)
    member = FakeMember(env.guild, 7, dm_ok=False)
    await env.cog.on_member_join(member)
    (post,) = env.cuddle.sent
    assert post.content.startswith("<@7> ") and "may be banned" in post.content
    assert post.allowed_mentions.kw == {"users": [member]}  # pings only this member
    assert "warned in <#555>" in env.mod_ch.sent[0].embeds[0].description


async def test_dm_closed_and_no_fallback_channel_flags_mods(env):
    seed(env, 7, 5)
    cfg_val(env, "cuddle_channel_id").store(None)
    await env.cog.on_member_join(FakeMember(env.guild, 7, dm_ok=False))
    assert "warn them manually" in env.mod_ch.sent[0].embeds[0].description


async def test_dm_closed_and_fallback_post_fails_flags_mods(env):
    seed(env, 7, 5)
    env.cuddle.fail = True
    await env.cog.on_member_join(FakeMember(env.guild, 7, dm_ok=False))
    assert "warn them manually" in env.mod_ch.sent[0].embeds[0].description


async def test_rejoin_after_two_leaves_is_mods_only_with_buttons(env):
    seed(env, 7, 20, 5)
    member = FakeMember(env.guild, 7, dm_ok=False)
    await env.cog.on_member_join(member)
    assert member.dms == [] and env.cuddle.sent == []  # the user hears nothing more
    (msg,) = env.mod_ch.sent
    assert "joined after previously leaving the server twice" in msg.embeds[0].description
    assert [b.custom_id for b in msg.view.children] == ["rejoinwatch:ban:7", "rejoinwatch:keep:7"]


async def test_rejoin_with_no_history_is_silent(env):
    member = FakeMember(env.guild, 7)
    await env.cog.on_member_join(member)
    assert member.dms == [] and env.mod_ch.sent == [] and env.cuddle.sent == []


async def test_rejoin_with_only_expired_history_is_silent(env):
    seed(env, 7, 200)
    member = FakeMember(env.guild, 7)
    await env.cog.on_member_join(member)
    assert member.dms == [] and env.mod_ch.sent == []


async def test_bot_joins_are_ignored(env):
    seed(env, 7, 5)
    await env.cog.on_member_join(FakeMember(env.guild, 7, bot=True))
    assert env.mod_ch.sent == []


async def test_missing_mod_channel_does_not_crash(env):
    seed(env, 7, 5)
    cfg_val(env, "mod_channel_id").store(123456)
    member = FakeMember(env.guild, 7)
    await env.cog.on_member_join(member)
    assert len(member.dms) == 1  # the user is still warned


async def test_full_story_leave_rejoin_leave_rejoin(env):
    user, member = FakeUser(7), FakeMember(env.guild, 7)
    await leave(env, user)                      # 1st leave: silent
    await env.cog.on_member_join(member)        # 1st rejoin: warned
    await leave(env, user)                      # 2nd leave: mod alert
    await env.cog.on_member_join(member)        # 2nd rejoin: mods only
    assert len(member.dms) == 1
    titles = [m.embeds[0].title for m in env.mod_ch.sent]
    assert titles == ["Rejoined after leaving", "Repeat leaver", "Rejoined after repeated leaves"]


async def test_fast_rejoin_waits_for_the_leave_to_be_recorded(env):
    """The leave handler sleeps (to let ban/kick events land). A rejoin during that
    window must queue behind it, or the warning is silently skipped."""
    env.cog.SETTLE_SECONDS = 0.05
    member = FakeMember(env.guild, 7)
    leaving = asyncio.create_task(leave(env, FakeUser(7)))
    await asyncio.sleep(0)  # leave handler is now holding the lock, sleeping
    joining = asyncio.create_task(env.cog.on_member_join(member))
    await asyncio.gather(leaving, joining)
    assert len(member.dms) == 1


# -------------------------------------------------------------- buttons

async def _alert(env, uid=7):
    seed(env, uid, 10)
    await leave(env, FakeUser(uid))
    return env.mod_ch.sent[-1]


async def test_mod_role_can_ban_and_the_alert_is_updated(env):
    msg = await _alert(env)
    inter = await click(env, mod_member(env), msg, "ban")
    assert len(env.guild.bans) == 1
    banned_id, kw = env.guild.bans[0]
    assert banned_id == 7 and kw["delete_message_seconds"] == 0  # no message purge
    assert "modperson" in kw["reason"] and "2 times" in kw["reason"]
    (edit,) = inter.edits
    assert edit["view"] is None  # buttons removed
    assert footer(edit["embed"]) == "Banned by modperson"


async def test_ban_members_permission_is_enough(env):
    msg = await _alert(env)
    clicker = FakeMember(env.guild, 901, perms=Perms(ban_members=True))
    await click(env, clicker, msg, "ban")
    assert len(env.guild.bans) == 1


async def test_non_mod_is_refused_and_nothing_happens(env):
    msg = await _alert(env)
    inter = await click(env, FakeMember(env.guild, 902), msg, "ban")
    assert env.guild.bans == [] and inter.edits == []
    assert inter.response.sent == [("Only moderators can use these buttons.", True)]
    # the refused click must not burn the message: a real mod can still act
    await click(env, mod_member(env), msg, "ban")
    assert len(env.guild.bans) == 1


async def test_no_button_dismisses_without_banning(env):
    msg = await _alert(env)
    inter = await click(env, mod_member(env), msg, "keep")
    assert env.guild.bans == []
    assert footer(inter.edits[0]["embed"]) == "Dismissed by modperson, no ban"
    assert inter.edits[0]["view"] is None


async def test_double_click_bans_only_once(env):
    msg = await _alert(env)
    one, two = mod_member(env, 900), mod_member(env, 901)
    i1 = FakeInteraction(one, env.guild, msg, "rejoinwatch:ban:7")
    i2 = FakeInteraction(two, env.guild, msg, "rejoinwatch:ban:7")
    await asyncio.gather(env.cog.on_interaction(i1), env.cog.on_interaction(i2))
    assert len(env.guild.bans) == 1
    assert len(i1.edits) + len(i2.edits) == 1
    assert [("Someone has already handled this.", True)] in (i1.response.sent, i2.response.sent)


async def test_failed_ban_keeps_the_buttons_and_can_be_retried(env):
    msg = await _alert(env)
    env.guild.ban_error = discord.Forbidden("hierarchy")
    first = await click(env, mod_member(env), msg, "ban")
    assert first.edits == [] and env.guild.bans == []
    assert "missing Ban Members" in first.followup.sent[0][0]
    env.guild.ban_error = None
    second = await click(env, mod_member(env), msg, "ban")  # claim was released
    assert len(env.guild.bans) == 1 and len(second.edits) == 1


async def test_http_error_on_ban_is_reported(env):
    msg = await _alert(env)
    env.guild.ban_error = discord.HTTPException("boom")
    inter = await click(env, mod_member(env), msg, "ban")
    assert "Discord refused the ban" in inter.followup.sent[0][0]


async def test_ban_falls_back_to_the_days_keyword_on_older_discordpy(env):
    msg = await _alert(env)
    env.guild.reject_seconds_kw = True
    await click(env, mod_member(env), msg, "ban")
    (_, kw), = env.guild.bans
    assert kw["delete_message_days"] == 0 and "delete_message_seconds" not in kw


async def test_ban_button_on_a_rejoin_alert_works_too(env):
    seed(env, 7, 20, 5)
    await env.cog.on_member_join(FakeMember(env.guild, 7))
    msg = env.mod_ch.sent[-1]
    inter = await click(env, mod_member(env), msg, "ban")
    assert env.guild.bans[0][0] == 7 and footer(inter.edits[0]["embed"]) == "Banned by modperson"


async def test_other_cogs_buttons_are_ignored(env):
    msg = await _alert(env)
    inter = FakeInteraction(mod_member(env), env.guild, msg, "othercog:ban:7")
    await env.cog.on_interaction(inter)
    assert inter.response.sent == [] and not inter.response.deferred
    inter = FakeInteraction(mod_member(env), env.guild, msg, "")
    inter.data = {}
    await env.cog.on_interaction(inter)
    assert env.guild.bans == []


# ---------------------------------------------------------------- pruning

async def test_prune_guild_drops_only_expired_and_writes_once(env):
    seed(env, 1, 300)
    leaves = stored(env)
    leaves["2"] = [NOW.timestamp() - 300 * DAY, NOW.timestamp() - DAY]
    leaves["3"] = [NOW.timestamp() - DAY]
    cfg_val(env, "leaves").store(leaves)
    assert await env.cog._prune_guild(env.guild) is True
    after = stored(env)
    assert set(after) == {"2", "3"} and len(after["2"]) == 1
    assert await env.cog._prune_guild(env.guild) is False


# --------------------------------------------------------------- commands

async def test_check_shows_leaves_or_none(env):
    ctx = FakeCtx(env.guild)
    await env.cog.rw_check.callback(env.cog, ctx, FakeUser(7))
    assert "no leaves on record" in ctx.sent[-1]
    seed(env, 7, 20, 5)
    await env.cog.rw_check.callback(env.cog, ctx, FakeUser(7))
    assert "has left 2 times" in ctx.sent[-1]


async def test_clear_forgives_a_member(env):
    seed(env, 7, 20, 5)
    ctx = FakeCtx(env.guild)
    await env.cog.rw_clear.callback(env.cog, ctx, FakeUser(7))
    assert "Cleared 2 times" in ctx.sent[-1] and "7" not in stored(env)
    await env.cog.rw_clear.callback(env.cog, ctx, FakeUser(7))
    assert "nothing on record" in ctx.sent[-1]
    member = FakeMember(env.guild, 7)
    await env.cog.on_member_join(member)
    assert member.dms == []  # forgiven: no warning


async def test_retention_validates_and_prunes(env):
    ctx = FakeCtx(env.guild)
    seed(env, 7, 40)
    await env.cog.rw_retention.callback(env.cog, ctx, 3)
    assert "between" in ctx.sent[-1] and cfg_val(env, "retention_days").current() == 180
    await env.cog.rw_retention.callback(env.cog, ctx, 30)
    assert cfg_val(env, "retention_days").current() == 30
    assert stored(env) == {}  # the 40-day-old leave expired immediately


async def test_toggle_and_channel_setters(env):
    ctx = FakeCtx(env.guild)
    await env.cog.rw_toggle.callback(env.cog, ctx)
    assert cfg_val(env, "enabled").current() is False and "OFF" in ctx.sent[-1]
    await env.cog.rw_toggle.callback(env.cog, ctx)
    assert cfg_val(env, "enabled").current() is True
    new = env.guild.add_channel(777, "other")
    await env.cog.rw_fallback.callback(env.cog, ctx, new)
    await env.cog.rw_modchannel.callback(env.cog, ctx, new)
    assert cfg_val(env, "cuddle_channel_id").current() == 777
    assert cfg_val(env, "mod_channel_id").current() == 777
    await env.cog.rw_modrole.callback(env.cog, ctx, types.SimpleNamespace(id=42, mention="<@&42>"))
    assert cfg_val(env, "mod_role_id").current() == 42


async def test_warning_text_set_show_reset(env):
    ctx = FakeCtx(env.guild)
    await env.cog.rw_warning.callback(env.cog, ctx, text="Hey {server}, careful.")
    assert "Hey Wonderland, careful." in ctx.sent[-1]
    seed(env, 7, 5)
    member = FakeMember(env.guild, 7)
    await env.cog.on_member_join(member)
    assert member.dms == ["Hey Wonderland, careful."]
    await env.cog.rw_warning.callback(env.cog, ctx, text="reset")
    assert cfg_val(env, "warning_text").current() == env.constants.DEFAULT_WARNING


async def test_settings_reports_setup_problems(env):
    ctx = FakeCtx(env.guild)
    cfg_val(env, "cuddle_channel_id").store(None)
    env.guild.me.guild_permissions = Perms(view_audit_log=False)
    seed(env, 7, 5)
    await env.cog.rw_settings.callback(env.cog, ctx)
    out = ctx.sent[-1]
    assert "v" + env.constants.VERSION in out
    assert "NOT SET" in out and "NO, kicks" in out and "leaves on record: 1" in out
    # the hint must name a command that actually exists
    assert "`.rejoinwatch fallback #cuddle`" in out
    assert env.cog.rw_fallback.kw["name"] == "fallback"


async def test_group_with_no_subcommand_shows_help(env):
    ctx = FakeCtx(env.guild)
    await env.cog.rejoinwatch.callback(env.cog, ctx)
    assert ctx.helped


async def test_data_deletion_request_removes_the_member(env):
    seed(env, 7, 5)
    leaves = stored(env)
    leaves["8"] = [NOW.timestamp() - DAY]
    cfg_val(env, "leaves").store(leaves)
    await env.cog.red_delete_data_for_user(requester="user", user_id=7)
    assert set(stored(env)) == {"8"}


# ------------------------------------------------------------ import smoke

async def test_setup_registers_the_cog(env):
    pkg = importlib.import_module("rejoinwatch")
    added = []

    class _Bot(FakeBot):
        async def add_cog(self, cog):
            added.append(cog)

    await pkg.setup(_Bot(env.guild))
    assert isinstance(added[0], env.mod.RejoinWatch)
