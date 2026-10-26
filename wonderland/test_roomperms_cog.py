"""The Discord-facing half of roomperms against fakes. Real discord.py overwrites; redbot is faked."""
import copy
import importlib
import sys
import types

import discord
import pytest

EVERYONE, MEMBER_ROLE, MUTED = 1, 10, 20
OWNER, GUEST, MOD, FRIEND = 100, 101, 102, 103
SOURCE_ID, ROOM_ID, MODLOG_ID = 500, 600, 397513667052175360


# ---------------------------------------------------------------- fakes

class FakeRole:
    def __init__(self, id):
        self.id, self.mention = id, f"<@&{id}>"

    def __hash__(self):
        return hash(("role", self.id))


class FakeMember:
    def __init__(self, id, roles=(), bot=False):
        self.id, self.roles, self.bot, self.mention = id, list(roles), bot, f"<@{id}>"
        self.moved = []

    async def move_to(self, channel, reason=None):
        self.moved.append((channel, reason))

    def __hash__(self):
        return hash(("member", self.id))


class FakeVoice:
    def __init__(self, id, guild=None, overwrites=None):
        self.id, self.guild = id, guild
        self.overwrites = dict(overwrites or {})
        self.mention = f"<#{id}>"
        self.sets = []
        self.members = []

    def overwrites_for(self, target):
        existing = self.overwrites.get(target)
        return discord.PermissionOverwrite.from_pair(*existing.pair()) if existing else discord.PermissionOverwrite()

    async def set_permissions(self, target, overwrite=None, reason=None):
        self.sets.append((target, overwrite, reason))
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite


class FakeTextChannel:
    def __init__(self, id):
        self.id, self.sent = id, []

    async def send(self, text, **kw):
        self.sent.append(text)


class FakeEntry:
    def __init__(self, user, channel_id, action=None):
        self.user = user
        self.action = action or discord.AuditLogAction.overwrite_update
        self.target = types.SimpleNamespace(id=channel_id)
        self.created_at = discord.utils.utcnow()


class FakeGuild:
    def __init__(self):
        self.id, self.owner_id = 1, 999
        self.default_role = FakeRole(EVERYONE)
        self.me = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_roles=True))
        self.channels, self.roles, self.members, self.entries = {}, {}, {}, []

    def get_channel(self, id):
        return self.channels.get(id)

    def get_role(self, id):
        return self.roles.get(id)

    def get_member(self, id):
        return self.members.get(id)

    async def audit_logs(self, limit=None):
        for entry in self.entries:
            yield entry


class FakeAutoRoomCog:
    """Stands in for PhasecoreX's AutoRoom. The class matters: the wrapper is installed on the class."""
    qualified_name = "AutoRoom"
    infos = {}

    async def get_autoroom_source_config(self, source):
        return copy.deepcopy(self.config_template)

    async def get_autoroom_info(self, channel):
        return self.infos.get(channel.id)

    async def get_bot_roles(self, guild):
        return list(self.bot_roles)

    def check_if_member_or_role_allowed(self, channel, member_or_role):
        """AutoRoom's own rule: can they Connect on the source? Tests decide who can't via `blocked`."""
        return member_or_role.id not in self.blocked


def make_autoroom_class():
    cls = type("AutoRoom", (FakeAutoRoomCog,), {})
    cls.infos = {}
    cls.blocked = set()
    cls.bot_roles = []
    cls.config_template = {
        "perm_owner_manage_channels": True,
        "perms": {
            "allow": {"view_channel": True, "connect": True, "send_messages": True},
            "lock": {"connect": False},
            "deny": {"connect": False},
            "owner": {"view_channel": True, "connect": True, "send_messages": True,
                      "manage_channels": True, "manage_messages": True},
            "access": {"view_channel": True, "connect": True, "send_messages": True},
        },
    }
    return cls


class FakeBot:
    def __init__(self):
        self.cogs, self.staff = {}, set()
        self.mod_roles, self.admin_roles, self.roles_fail = [], [], False

    async def get_mod_roles(self, guild):
        if self.roles_fail:
            raise RuntimeError("no roles for you")
        return list(self.mod_roles)

    async def get_admin_roles(self, guild):
        return list(self.admin_roles)

    def get_cog(self, name):
        return self.cogs.get(name)

    async def is_mod(self, member):
        return member.id in self.staff

    async def is_admin(self, member):
        return False


class FakeConfig:
    """Just enough of Red's Config for `config.guild(g).roomperms()` and `.roomperms.set(...)`."""

    def __init__(self):
        self.store = {}
        self.reads = 0

    def guild(self, guild):
        store, outer = self.store, self

        class _Value:
            async def __call__(self):
                outer.reads += 1
                return store.get("roomperms", {"enabled": True})

            async def set(self, value):
                store["roomperms"] = value

        return types.SimpleNamespace(roomperms=_Value())


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def rpc(monkeypatch):
    """Import wonderland.roomperms_cog with a faked redbot and fake Role/Member/VoiceChannel classes."""
    class _Cog:
        listener = staticmethod(lambda *a, **kw: (lambda f: f))

    commands_ns = types.SimpleNamespace(Cog=_Cog)
    core = types.ModuleType("redbot.core")
    core.commands = commands_ns
    root = types.ModuleType("redbot")
    root.core = core
    for name, module in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(discord, "Role", FakeRole)
    monkeypatch.setattr(discord, "Member", FakeMember)
    monkeypatch.setattr(discord, "VoiceChannel", FakeVoice)
    sys.modules.pop("wonderland.roomperms_cog", None)
    module = importlib.import_module("wonderland.roomperms_cog")
    monkeypatch.setattr(module, "GUARD_DELAY", 0)
    yield module
    sys.modules.pop("wonderland.roomperms_cog", None)


def make_host(rpc, bot, config=None):
    class Host(rpc.RoomPermsMixin):
        def __init__(self):
            self.bot = bot
            self.config = config or FakeConfig()
            self._rp_setup()

    return Host()


@pytest.fixture
def world(rpc):
    """A guild with a Room Creator source, one AutoRoom made from it, and the cog installed."""
    guild = FakeGuild()
    everyone, member_role, muted = guild.default_role, FakeRole(MEMBER_ROLE), FakeRole(MUTED)
    for role in (everyone, member_role, muted):
        guild.roles[role.id] = role
    owner = FakeMember(OWNER, [member_role])
    guest = FakeMember(GUEST, [member_role, muted])         # a muted member
    friend = FakeMember(FRIEND, [member_role])              # an ordinary member
    mod = FakeMember(MOD, [member_role])
    for member in (owner, guest, friend, mod):
        guild.members[member.id] = member

    source = FakeVoice(SOURCE_ID, guild, {
        everyone: discord.PermissionOverwrite(connect=False, view_channel=False),
        member_role: discord.PermissionOverwrite(connect=True, view_channel=True),
        muted: discord.PermissionOverwrite(connect=False, view_channel=False),
    })
    room = FakeVoice(ROOM_ID, guild, {
        everyone: discord.PermissionOverwrite(connect=False, view_channel=False),
        member_role: discord.PermissionOverwrite(connect=True, view_channel=True),
        muted: discord.PermissionOverwrite(connect=False, view_channel=False),
        owner: discord.PermissionOverwrite(connect=True, view_channel=True, manage_roles=True),
    })
    modlog = FakeTextChannel(MODLOG_ID)
    guild.channels.update({SOURCE_ID: source, ROOM_ID: room, MODLOG_ID: modlog})

    bot = FakeBot()
    autoroom_cls = make_autoroom_class()
    autoroom_cls.infos[ROOM_ID] = {"source_channel": SOURCE_ID, "owner": OWNER}
    bot.cogs["AutoRoom"] = autoroom_cls()
    host = make_host(rpc, bot)
    return types.SimpleNamespace(guild=guild, bot=bot, host=host, room=room, source=source, modlog=modlog,
                                 owner=owner, guest=guest, friend=friend, mod=mod, muted=muted,
                                 member_role=member_role, everyone=everyone, autoroom_cls=autoroom_cls)


async def edit_room(world, change):
    """Apply `change(room.overwrites)` the way a Discord edit would, then fire the update event."""
    before = FakeVoice(ROOM_ID, world.guild, copy.deepcopy(world.room.overwrites))
    change(world.room.overwrites)
    await world.host.on_guild_channel_update(before, world.room)


# ---------------------------------------------------------------- the wrapper

async def owner_perms(world):
    cfg = await world.bot.cogs["AutoRoom"].get_autoroom_source_config(world.source)
    return cfg["perms"]


async def test_before_patching_owners_do_not_get_manage_roles(world):
    assert "manage_roles" not in (await owner_perms(world))["owner"]


async def test_after_patching_only_the_owner_set_gains_manage_roles(world):
    assert world.host._rp_patch() is True
    perms = await owner_perms(world)
    assert perms["owner"]["manage_roles"] is True and perms["owner"]["manage_channels"] is True
    for shared in ("allow", "lock", "deny", "access"):
        assert "manage_roles" not in perms[shared]


async def test_patching_twice_wraps_once(world):
    world.host._rp_patch()
    first = type(world.bot.cogs["AutoRoom"]).get_autoroom_source_config
    world.host._rp_patch()
    assert type(world.bot.cogs["AutoRoom"]).get_autoroom_source_config is first


async def test_switching_it_off_removes_the_grant_for_new_lookups(world):
    world.host._rp_patch()
    await world.host._rp_set_enabled(world.guild, False)
    assert "manage_roles" not in (await owner_perms(world))["owner"]
    await world.host._rp_set_enabled(world.guild, True)
    assert (await owner_perms(world))["owner"]["manage_roles"] is True


async def test_a_bot_without_manage_roles_grants_nothing_so_room_creation_cannot_fail(world):
    world.host._rp_patch()
    world.guild.me.guild_permissions.manage_roles = False
    assert "manage_roles" not in (await owner_perms(world))["owner"]


async def test_autoroom_own_owner_switch_off_means_no_grant(world):
    world.autoroom_cls.config_template["perm_owner_manage_channels"] = False
    world.host._rp_patch()
    assert "manage_roles" not in (await owner_perms(world))["owner"]


async def test_a_failure_inside_the_wrapper_hands_back_autoroom_s_own_answer(world):
    world.host._rp_patch()

    class Broken:
        def guild(self, guild):
            raise RuntimeError("config exploded")

    world.host.config = Broken()
    perms = await owner_perms(world)
    assert "manage_roles" not in perms["owner"] and perms["owner"]["connect"] is True


async def test_unpatch_restores_autoroom_exactly(world):
    cls = type(world.bot.cogs["AutoRoom"])
    original = cls.get_autoroom_source_config
    world.host._rp_patch()
    world.host._rp_unpatch()
    assert cls.get_autoroom_source_config is original
    assert "manage_roles" not in (await owner_perms(world))["owner"]


async def test_reloading_autoroom_gives_a_new_class_and_on_cog_add_wraps_it_again(world):
    world.host._rp_patch()
    new_cls = make_autoroom_class()
    world.bot.cogs["AutoRoom"] = new_cls()           # AutoRoom reloaded: a brand new class
    assert "manage_roles" not in (await owner_perms(world))["owner"]
    await world.host.on_cog_add(world.bot.cogs["AutoRoom"])
    assert (await owner_perms(world))["owner"]["manage_roles"] is True


async def test_on_cog_add_ignores_other_cogs(world):
    await world.host.on_cog_add(types.SimpleNamespace(qualified_name="Audio"))
    assert not world.host._rp_is_patched()


async def test_reloading_wonderland_replaces_its_own_old_wrapper_without_stacking(world, rpc):
    calls = []
    original = world.autoroom_cls.get_autoroom_source_config

    async def counting(self, source):
        calls.append(1)
        return await original(self, source)

    world.autoroom_cls.get_autoroom_source_config = counting
    world.host._rp_patch()
    second = make_host(rpc, world.bot)             # Wonderland reloaded: a new instance, the old wrapper still on the class
    second._rp_patch()
    cls = type(world.bot.cogs["AutoRoom"])
    assert cls.get_autoroom_source_config._wld_owner is second
    assert cls.get_autoroom_source_config._wld_base is counting
    assert (await owner_perms(world))["owner"]["manage_roles"] is True
    assert calls == [1]                            # AutoRoom's own method ran once, not once per stacked wrapper


async def test_no_autoroom_cog_loaded_is_not_an_error(world):
    world.bot.cogs.clear()
    assert world.host._rp_patch() is False
    world.host._rp_unpatch()


async def test_autoroom_without_the_method_logs_and_does_nothing(world):
    world.bot.cogs["AutoRoom"] = type("AutoRoom", (), {"qualified_name": "AutoRoom"})()   # a future AutoRoom, renamed
    assert world.host._rp_patch() is False
    assert not world.host._rp_is_patched()


async def test_status_text_reports_state(world):
    text = await world.host._rp_status(world.guild)
    assert "**on**" in text and "wrapper active: **no**" in text
    world.host._rp_patch()
    await world.host._rp_set_enabled(world.guild, False)
    text = await world.host._rp_status(world.guild)
    assert "**off**" in text and "wrapper active: **yes**" in text


# ---------------------------------------------------------------- the guard

async def test_deleting_the_muted_roles_deny_is_put_back_and_reported(world):
    await edit_room(world, lambda ow: ow.pop(world.muted))
    restored = world.room.overwrites[world.muted]
    assert restored.connect is False and restored.view_channel is False
    assert len(world.modlog.sent) == 1
    assert f"<#{ROOM_ID}>" in world.modlog.sent[0] and f"<@&{MUTED}>" in world.modlog.sent[0]
    assert "unknown" in world.modlog.sent[0]      # the audit log had no entry


async def test_the_mod_log_names_who_did_it_when_the_audit_log_says(world):
    world.guild.entries.append(FakeEntry(world.owner, ROOM_ID))
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert f"<@{OWNER}>" in world.modlog.sent[0].split("Changed by")[1]


async def test_letting_a_member_the_source_blocks_in_personally_is_undone(world):
    world.autoroom_cls.blocked = {GUEST}
    await edit_room(world, lambda ow: ow.__setitem__(
        world.guest, discord.PermissionOverwrite(connect=True, view_channel=True)))
    personal = world.room.overwrites[world.guest]
    assert personal.connect is None and personal.view_channel is True    # only the way in was closed
    assert len(world.modlog.sent) == 1 and f"<@{GUEST}>" in world.modlog.sent[0]


async def test_a_member_holding_a_denied_role_and_a_member_role_keeps_their_allow(world):
    # Regression: guest holds Muted (denied on the source) AND a member role (allowed). Discord lets the allow
    # win, AutoRoom says they may connect, so `.autoroom allow` and `.autoroom private` give them a personal
    # allow. The guard must leave it alone, not undo AutoRoom's own work and flood the mod log.
    assert world.muted in world.guest.roles and world.member_role in world.guest.roles
    await edit_room(world, lambda ow: ow.__setitem__(
        world.guest, discord.PermissionOverwrite(connect=True, view_channel=True)))
    assert world.room.overwrites[world.guest].connect is True
    assert world.room.sets == [] and world.modlog.sent == []


async def test_letting_an_ordinary_friend_in_is_none_of_the_guards_business(world):
    await edit_room(world, lambda ow: ow.__setitem__(
        world.friend, discord.PermissionOverwrite(connect=True, view_channel=True)))
    assert world.room.overwrites[world.friend].connect is True
    assert world.room.sets == [] and world.modlog.sent == []


async def test_owners_own_public_private_changes_are_not_guarded(world):
    def go_public(ow):
        ow[world.everyone] = discord.PermissionOverwrite(connect=True, view_channel=True)
        ow[world.member_role] = discord.PermissionOverwrite(connect=None, view_channel=None)
    await edit_room(world, go_public)
    assert world.room.sets == [] and world.modlog.sent == []


async def test_a_mod_editing_the_room_on_purpose_is_left_alone(world):
    world.bot.staff.add(MOD)
    world.guild.entries.append(FakeEntry(world.mod, ROOM_ID))
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.muted not in world.room.overwrites and world.modlog.sent == []


async def test_a_non_staff_actor_in_the_audit_log_is_still_guarded(world):
    world.guild.entries.append(FakeEntry(world.owner, ROOM_ID))
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.muted in world.room.overwrites


async def test_an_owner_the_source_blocks_keeps_their_own_overwrite(world):
    owner = world.owner
    world.autoroom_cls.blocked = {OWNER}
    await edit_room(world, lambda ow: ow.__setitem__(
        owner, discord.PermissionOverwrite(connect=True, view_channel=True, manage_roles=True, send_messages=True)))
    assert world.room.overwrites[owner].connect is True


async def test_a_blocked_staff_member_keeps_a_personal_allow(world):
    world.bot.staff.add(MOD)
    world.autoroom_cls.blocked = {MOD}
    await edit_room(world, lambda ow: ow.__setitem__(
        world.mod, discord.PermissionOverwrite(connect=True, view_channel=True)))
    assert world.room.overwrites[world.mod].connect is True and world.modlog.sent == []


async def test_a_channel_that_is_not_an_autoroom_is_ignored(world):
    other = FakeVoice(777, world.guild, {world.muted: discord.PermissionOverwrite(connect=True)})
    before = FakeVoice(777, world.guild, {})
    await world.host.on_guild_channel_update(before, other)
    assert other.sets == [] and world.modlog.sent == []


async def test_an_edit_that_changes_no_overwrites_is_ignored_cheaply(world):
    before = FakeVoice(ROOM_ID, world.guild, copy.deepcopy(world.room.overwrites))
    await world.host.on_guild_channel_update(before, world.room)    # e.g. a rename
    assert world.room.sets == [] and world.modlog.sent == []


async def test_without_the_autoroom_cog_the_guard_does_nothing(world):
    world.bot.cogs.clear()
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.muted not in world.room.overwrites


async def test_a_room_whose_source_is_gone_is_left_alone(world):
    del world.guild.channels[SOURCE_ID]
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.muted not in world.room.overwrites


async def test_the_guard_checks_again_so_a_second_edit_during_the_fix_is_caught(world):
    original = world.room.set_permissions
    state = {"n": 0}

    async def sneaky(target, overwrite=None, reason=None):
        await original(target, overwrite=overwrite, reason=reason)
        state["n"] += 1
        if state["n"] == 1:   # while we were fixing, the owner removed the quarantine-style deny again
            world.room.overwrites.pop(world.muted, None)

    world.room.set_permissions = sneaky
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.room.overwrites[world.muted].connect is False
    assert len(world.modlog.sent) == 1     # one report per owner action, not per pass


async def test_a_forbidden_fix_does_not_loop_and_does_not_crash(world):
    attempts = []

    async def forbidden(target, overwrite=None, reason=None):
        attempts.append(target)
        raise discord.HTTPException(types.SimpleNamespace(status=403, reason="Missing Permissions"), "no")

    world.room.set_permissions = forbidden
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert len(attempts) == 1                 # one try, no retry loop


async def test_a_pending_check_swallows_further_events_for_the_same_room(world):
    world.host._rp_pending.add(ROOM_ID)
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.muted not in world.room.overwrites        # the pending run will re-read the room itself


async def test_missing_mod_log_channel_does_not_stop_the_fix(world):
    del world.guild.channels[MODLOG_ID]
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.room.overwrites[world.muted].connect is False


async def test_roles_autoroom_adds_on_purpose_are_not_fought(world):
    # AutoRoom gives admin/mod/bot roles an allow in every room, even if the source happens to deny that role.
    staff_role = FakeRole(30)
    world.guild.roles[30] = staff_role
    world.source.overwrites[staff_role] = discord.PermissionOverwrite(connect=False)
    world.room.overwrites[staff_role] = discord.PermissionOverwrite(connect=True, view_channel=True)
    world.bot.admin_roles = [staff_role]
    await edit_room(world, lambda ow: ow.__setitem__(
        world.friend, discord.PermissionOverwrite(connect=True)))
    assert world.room.overwrites[staff_role].connect is True
    assert world.room.sets == [] and world.modlog.sent == []


async def test_bot_roles_and_mod_roles_are_exempt_too(world):
    bot_role, mod_role = FakeRole(31), FakeRole(32)
    for role in (bot_role, mod_role):
        world.guild.roles[role.id] = role
        world.source.overwrites[role] = discord.PermissionOverwrite(connect=False)
        world.room.overwrites[role] = discord.PermissionOverwrite(connect=True)
    world.autoroom_cls.bot_roles = [bot_role]
    world.bot.mod_roles = [mod_role]
    await edit_room(world, lambda ow: ow.__setitem__(world.friend, discord.PermissionOverwrite(connect=True)))
    assert world.room.sets == []


async def test_if_the_staff_roles_cannot_be_listed_the_guard_stands_down_rather_than_guess(world):
    world.bot.roles_fail = True
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.muted not in world.room.overwrites and world.modlog.sent == []


async def test_the_mod_log_is_rate_limited_per_room_but_every_fix_still_happens(world):
    await edit_room(world, lambda ow: ow.pop(world.muted))
    await edit_room(world, lambda ow: ow.pop(world.muted))
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.room.overwrites[world.muted].connect is False
    assert len(world.room.sets) == 3          # fixed every time
    assert len(world.modlog.sent) == 1        # reported once inside the cooldown


async def test_the_mod_log_reports_again_after_the_cooldown(world, rpc, monkeypatch):
    monkeypatch.setattr(rpc, "GUARD_LOG_COOLDOWN", 0)
    await edit_room(world, lambda ow: ow.pop(world.muted))
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert len(world.modlog.sent) == 2


async def test_the_on_off_switch_is_read_once_not_on_every_voice_event(world):
    world.host._rp_patch()
    for _ in range(5):
        await owner_perms(world)
    assert world.host.config.reads == 1
    await world.host._rp_set_enabled(world.guild, False)
    reads_after_toggle = world.host.config.reads
    assert "manage_roles" not in (await owner_perms(world))["owner"]
    assert world.host.config.reads == reads_after_toggle    # the switch answered from memory, and it is off


async def test_an_edit_that_lands_while_a_check_is_running_is_still_caught(world):
    # The owner's first edit needs no fix (it only adds an exempt staff member). While the guard is busy
    # looking at that, the owner removes a protected deny. That second event is dropped as "already
    # pending". With no fix made in the first run, nothing but the dirty flag can make the guard look again.
    world.autoroom_cls.blocked = {MOD}
    real_is_mod = world.bot.is_mod
    fired = {"done": False}

    async def is_mod_then_second_edit(member):
        if not fired["done"]:
            fired["done"] = True
            before = FakeVoice(ROOM_ID, world.guild, copy.deepcopy(world.room.overwrites))
            world.room.overwrites.pop(world.muted)                # the owner's second edit, mid-run
            await world.host.on_guild_channel_update(before, world.room)
        return await real_is_mod(member)

    world.bot.staff.add(MOD)
    world.bot.is_mod = is_mod_then_second_edit
    await edit_room(world, lambda ow: ow.__setitem__(
        world.mod, discord.PermissionOverwrite(connect=True, view_channel=True)))
    assert fired["done"]
    assert world.room.overwrites[world.muted].connect is False
    assert world.room.overwrites[world.mod].connect is True        # the staff member was left alone
    assert world.host._rp_pending == set() and world.host._rp_dirty == set()


# ---------------------------------------------------------------- disconnecting people who got in

async def test_someone_the_owner_let_in_against_the_rules_is_disconnected_with_the_fix(world):
    world.autoroom_cls.blocked = {GUEST}
    world.room.members = [world.owner, world.guest, world.friend]
    await edit_room(world, lambda ow: ow.__setitem__(
        world.guest, discord.PermissionOverwrite(connect=True, view_channel=True)))
    assert world.guest.moved == [(None, "Room guard: not allowed in this room")]
    assert world.friend.moved == [] and world.owner.moved == []
    assert f"disconnected: <@{GUEST}>" in world.modlog.sent[0]


async def test_a_member_whose_allow_beats_their_deny_is_never_disconnected(world):
    # Guest holds Muted and a member role; the source lets them in. A fix elsewhere in the room must not touch them.
    world.room.members = [world.guest]
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert world.room.overwrites[world.muted].connect is False      # the fix happened
    assert world.guest.moved == []


async def test_restoring_a_role_deny_removes_a_blocked_person_who_walked_into_the_gap(world):
    loner = FakeMember(104, [world.muted])                          # holds only Muted: the source refuses them
    world.guild.members[104] = loner
    world.autoroom_cls.blocked = {104}
    world.room.members = [world.owner, loner]
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert loner.moved and world.owner.moved == []


async def test_staff_bots_the_owner_and_the_server_owner_are_never_disconnected(world):
    bot_user = FakeMember(105, [world.muted], bot=True)
    server_owner = FakeMember(999, [world.muted])
    world.guild.members.update({105: bot_user, 999: server_owner})
    world.owner.roles.append(world.muted)
    world.mod.roles.append(world.muted)
    world.bot.staff.add(MOD)
    world.autoroom_cls.blocked = {OWNER, MOD, 105, 999}
    world.room.members = [world.owner, world.mod, bot_user, server_owner]
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert all(m.moved == [] for m in world.room.members)


async def test_a_failed_disconnect_does_not_stop_the_report(world):
    world.autoroom_cls.blocked = {GUEST}

    async def refuse(channel, reason=None):
        raise discord.HTTPException(types.SimpleNamespace(status=403, reason="Missing Permissions"), "no")

    world.guest.move_to = refuse
    world.room.members = [world.guest]
    await edit_room(world, lambda ow: ow.__setitem__(
        world.guest, discord.PermissionOverwrite(connect=True)))
    assert len(world.modlog.sent) == 1 and "disconnected" not in world.modlog.sent[0]


async def test_if_discord_refuses_every_fix_the_mods_are_told(world):
    async def forbidden(target, overwrite=None, reason=None):
        raise discord.HTTPException(types.SimpleNamespace(status=403, reason="Missing Permissions"), "no")

    world.room.set_permissions = forbidden
    await edit_room(world, lambda ow: ow.pop(world.muted))
    assert len(world.modlog.sent) == 1
    assert "could not fix" in world.modlog.sent[0] and f"<#{ROOM_ID}>" in world.modlog.sent[0]
