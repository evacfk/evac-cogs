"""Fakes for the contest cog tests (not loaded by Red; only imported by tests). Needs the real discord.py."""
import copy
import itertools
import sys
import types

import discord

_ids = itertools.count(50_000)


class _Val:
    def __init__(self, bucket, key, default):
        self.bucket, self.key, self.default = bucket, key, default

    def current(self):
        return copy.deepcopy(self.bucket.get(self.key, self.default))

    def __call__(self):
        val = self

        class _Call:
            def __await__(self):
                async def _get():
                    return val.current()
                return _get().__await__()

            async def __aenter__(self):
                self.obj = val.current()
                return self.obj

            async def __aexit__(self, *exc):
                if exc[0] is None:
                    val.bucket[val.key] = copy.deepcopy(self.obj)

        return _Call()

    async def set(self, value):
        self.bucket[self.key] = copy.deepcopy(value)

    async def clear(self):
        self.bucket.pop(self.key, None)


class _Group:
    def __init__(self, bucket, defaults):
        self._bucket, self._defaults = bucket, defaults

    def __getattr__(self, name):
        if name.startswith("_") or name not in self._defaults:
            raise AttributeError(name)
        return _Val(self._bucket, name, self._defaults[name])

    async def all(self):
        out = copy.deepcopy(self._defaults)
        for k, v in self._bucket.items():
            out[k] = copy.deepcopy(v)
        return out


class FakeConfig:
    def __init__(self):
        self.data, self.gd = {}, {}

    @classmethod
    def get_conf(cls, *a, **k):
        return cls()

    def register_guild(self, **d):
        self.gd = d

    def guild(self, guild):
        return _Group(self.data.setdefault(guild.id, {}), self.gd)


class FakeRole:
    def __init__(self, guild, rid, name, position=10):
        self.guild, self.id, self.name, self.position = guild, rid, name, position
        self.icon_given = False

    def __eq__(self, o):
        return getattr(o, "id", None) == self.id

    def __hash__(self):
        return hash(self.id)

    def __lt__(self, o):
        return self.position < o.position

    def __gt__(self, o):
        return self.position > o.position

    @property
    def mention(self):
        return f"<@&{self.id}>"


class FakeMessage:
    def __init__(self, channel, content=None, **kw):
        self.id = next(_ids)
        self.channel, self.content = channel, content
        self.embed, self.files, self.view = kw.get("embed"), kw.get("files") or [], kw.get("view")
        self.deleted = self.pinned = False
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        for k in ("content", "embed", "view"):
            if k in kw:
                setattr(self, k, kw[k])
        if "attachments" in kw:
            self.files = list(kw["attachments"])

    async def delete(self):
        self.deleted = True

    async def pin(self):
        self.pinned = True

    @property
    def custom_ids(self):
        return [getattr(c, "custom_id", None) for c in (self.view.children if self.view else [])]


class FakeChannel:
    def __init__(self, guild, cid, name="chan", private=False):
        self.guild, self.id, self.name = guild, cid, name
        self.sent, self.overwrites = [], {}
        self.deleted = False
        self.category = None
        if private:
            self.overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, **kw):
        msg = FakeMessage(self, content=content, **kw)
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        for m in self.sent:
            if m.id == mid:
                return m
        return FakeMessage(self)

    def overwrites_for(self, target):
        return copy.copy(self.overwrites.get(target, discord.PermissionOverwrite()))

    async def set_permissions(self, target, *, overwrite=None, reason=None):
        self.overwrites[target] = overwrite

    def permissions_for(self, target):
        return types.SimpleNamespace(view_channel=self.overwrites.get(target, discord.PermissionOverwrite()).view_channel is True)

    async def delete(self, reason=None):
        self.deleted = True
        self.guild.channels.pop(self.id, None)


class FakeMember:
    def __init__(self, guild, uid, roles=(), name=None, admin=False):
        self.guild, self.id = guild, uid
        self.display_name = name or f"user{uid}"
        self.roles = [guild.default_role, *roles]
        self.dms, self.dm_closed, self.bot = [], False, False
        self.guild_permissions = types.SimpleNamespace(administrator=admin, manage_guild=admin)
        guild.members.append(self)

    @property
    def mention(self):
        return f"<@{self.id}>"

    async def send(self, content=None, **kw):
        if self.dm_closed:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "closed")
        msg = types.SimpleNamespace(content=content, view=kw.get("view"))
        self.dms.append(msg)
        return msg

    async def add_roles(self, *roles, reason=None):
        for r in roles:
            if r not in self.roles:
                self.roles.append(r)


class _Me:
    """The bot's own member: hashable so it can be a permission-overwrite key."""

    def __init__(self, top_role):
        self.top_role = top_role
        self.guild_permissions = types.SimpleNamespace(manage_channels=True, manage_roles=True)


class FakeGuild:
    def __init__(self, gid=1):
        self.id = gid
        self.members, self.roles, self.channels = [], [], {}
        self.default_role = FakeRole(self, gid, "@everyone", 0)
        self.me = _Me(FakeRole(self, 999, "Bot", 100))
        self.name = "Wonderland"
        self.icon_allowed = True
        self._next = 7000

    def add_role(self, rid, name, **kw):
        role = FakeRole(self, rid, name, **kw)
        self.roles.append(role)
        return role

    async def create_role(self, *, name, **kw):
        if "display_icon" in kw and not self.icon_allowed:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "boost")
        self._next += 1
        role = self.add_role(self._next, name, position=5)
        role.icon_given = "display_icon" in kw
        return role

    def add_channel(self, cid, name="chan", private=False):
        ch = FakeChannel(self, cid, name, private)
        self.channels[cid] = ch
        return ch

    async def create_text_channel(self, name, *, category=None, overwrites=None, **kw):
        self._next += 1
        ch = self.add_channel(self._next, name)
        ch.category, ch.overwrites = category, dict(overwrites or {})
        return ch

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return self.channels.get(cid)


class FakeBank:
    def __init__(self):
        self.deposits = []

    async def deposit_credits(self, member, amount):
        self.deposits.append((member.id, amount))

    async def get_currency_name(self, guild):
        return "wondercoins"


class FakeBot:
    def __init__(self, guild, cogs=None, owner_ids=()):
        self.guilds, self._cogs, self.owner_ids = [guild], cogs or {}, set(owner_ids)
        self.owner_msgs = []
        self.loop = types.SimpleNamespace(create_task=self._create_task)

    @staticmethod
    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    def get_cog(self, name):
        return self._cogs.get(name)

    async def is_owner(self, user):
        return user.id in self.owner_ids

    async def send_to_owners(self, text):
        self.owner_msgs.append(text)

    async def wait_until_red_ready(self):
        return None


class _Cmd:
    def __init__(self, f, **kw):
        self.callback, self.kw = f, kw
        self.__name__ = getattr(f, "__name__", "cmd")

    def command(self, *a, **kw):
        return lambda f: _Cmd(f, **kw)

    group = command

    def __call__(self, *a, **kw):
        return self.callback(*a, **kw)


def _deco(*a, **kw):
    return lambda f: _Cmd(f, **kw)


def _passthrough(*a, **kw):
    return lambda f: f


def install(monkeypatch, bank=None):
    class _Cog:
        @staticmethod
        def listener(*a, **kw):
            return lambda f: f

    commands_ns = types.SimpleNamespace(Cog=_Cog, group=_deco, command=_deco, Context=object, guild_only=_passthrough)
    core = types.ModuleType("redbot.core")
    core.Config, core.commands = FakeConfig, commands_ns
    core.bank = bank or FakeBank()
    root = types.ModuleType("redbot")
    root.core = core
    for name, mod in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns)):
        monkeypatch.setitem(sys.modules, name, mod)
    return core


class Click:
    """A fake component interaction that records what the cog answered."""

    def __init__(self, user, cid, guild=None, values=None):
        self.user, self.guild = user, guild
        self.data = {"custom_id": cid, **({"values": values} if values is not None else {})}
        self.out, outer = [], self

        class Resp:
            def is_done(self):
                return bool(outer.out)

            async def send_message(self, content=None, **kw):
                outer.out.append(("send", content, kw))

            async def edit_message(self, **kw):
                outer.out.append(("edit", kw.get("content"), kw))

        self.response = Resp()

    @property
    def text(self):
        return " ".join(str(o[1] or "") for o in self.out)


class FakeAttachment:
    def __init__(self, filename, data):
        self.filename, self._data, self.size = filename, data, len(data)

    async def read(self):
        return self._data


class FakeDM:
    """A direct message to the bot."""

    def __init__(self, author, content="", attachments=()):
        self.guild, self.author, self.content, self.attachments = None, author, content, list(attachments)
        self.replies = []
        outer = self

        class _Chan:
            async def send(self, content=None, **kw):
                outer.replies.append((content, kw.get("view")))

        self.channel = _Chan()
