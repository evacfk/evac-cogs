"""Shared fakes for the secrethunt cog tests (not loaded by Red; only imported by tests).

Installed per test via the `cogmod` fixture with monkeypatch, so nothing leaks into the
repo-root stub that other cogs' tests rely on, and the same tests run unchanged against
the real discord.py (only redbot is always faked).
"""
import copy
import itertools
import sys
import types

_ids = itertools.count(10_000)


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
        # Same merge as redbot's Config.nested_update, including its limitation: a stored dict under a key
        # whose default is not a dict (e.g. None) raises, exactly like it does on the real bot.
        def merge(current, defaults):
            for key, value in current.items():
                if isinstance(value, dict):
                    defaults[key] = merge(value, defaults.get(key, {}))
                else:
                    defaults[key] = copy.deepcopy(value)
            return defaults
        return merge(self._bucket, copy.deepcopy(self._defaults))

    async def clear(self):
        self._bucket.clear()


class FakeConfig:
    def __init__(self):
        self.data, self.gd, self.md, self.glob = {}, {}, {}, {}

    @classmethod
    def get_conf(cls, *a, **k):
        return cls()

    def register_guild(self, **d):
        self.gd = d

    def register_member(self, **d):
        self.md = d

    def register_global(self, **d):
        self.glob = d

    def guild(self, guild):
        return self.guild_from_id(guild.id)

    def guild_from_id(self, gid):
        return _Group(self.data.setdefault(("g", gid), {}), self.gd)

    def member(self, m):
        return self.member_from_ids(m.guild.id, m.id)

    def member_from_ids(self, gid, uid):
        return _Group(self.data.setdefault(("m", gid, uid), {}), self.md)

    async def all_members(self, guild=None):
        out = {}
        for key, bucket in self.data.items():
            if key[0] == "m" and (guild is None or key[1] == guild.id) and bucket:
                out[key[2]] = {k: copy.deepcopy(bucket.get(k, d)) for k, d in self.md.items()}
        return out



def http_error(kind: str):
    """A discord.Forbidden / NotFound / HTTPException that constructs under both real discord.py and the stub."""
    import discord
    status = {"Forbidden": 403, "NotFound": 404}.get(kind, 500)
    return getattr(discord, kind)(types.SimpleNamespace(status=status, reason=kind), kind)


class FakeRole:
    def __init__(self, guild, rid, name):
        self.guild, self.id, self.name = guild, rid, name
        self.edits, self.fail_edit = [], False

    @property
    def mention(self):
        return f"<@&{self.id}>"

    async def edit(self, **kw):
        if self.fail_edit:
            raise http_error("Forbidden")
        self.edits.append(kw)

    def __eq__(self, o):
        return getattr(o, "id", None) == self.id

    def __hash__(self):
        return hash(self.id)


class FakeMessage:
    def __init__(self, channel, content=None, embed=None, author=None, **kw):
        self.id = next(_ids)
        self.channel, self.content, self.embed, self.kw = channel, content, embed, kw
        self.guild = getattr(channel, "guild", None)
        self.author = author
        self.pinned = self.deleted = False
        self.removed = []  # (emoji, member) pairs removed by the cog
        self.attachments = []
        self.type = kw.get("type", 0)

    async def pin(self, **kw):
        if getattr(self.channel, "fail_pin", False):
            raise http_error("Forbidden")
        self.pinned = True
        import discord
        pins_add = getattr(getattr(discord, "MessageType", None), "pins_add", None)
        if pins_add is not None:  # Discord posts "<bot> pinned a message to this channel"; so does the fake
            self.channel.sent.append(FakeMessage(self.channel, author=types.SimpleNamespace(id=999), type=pins_add))

    async def unpin(self, **kw):
        self.pinned = False

    async def delete(self):
        self.deleted = True
        if self in getattr(self.channel, "sent", []):
            self.channel.sent.remove(self)

    async def remove_reaction(self, emoji, member):
        self.removed.append((str(emoji), member.id))

    async def edit(self, **kw):
        for key in ("content", "embed"):
            if key in kw:
                setattr(self, key, kw[key])


class _Partial:
    """A message that does not exist (any more): every call on it 404s, like a real deleted message."""

    def __init__(self, mid):
        self.id, self.removed = mid, []

    async def edit(self, **kw):
        raise http_error("NotFound")

    async def remove_reaction(self, emoji, member):
        raise http_error("NotFound")

    async def unpin(self, **kw):
        raise http_error("NotFound")

    async def delete(self):
        raise http_error("NotFound")


class FakeChannel:
    def __init__(self, guild, cid, name="chan"):
        self.guild, self.id, self.name = guild, cid, name
        self.sent = []
        self.fail_pin = False

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, **kw):
        if getattr(self, "fail_send", False):
            raise http_error("Forbidden")
        msg = FakeMessage(self, content=content, **kw)
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        return next((m for m in self.sent if m.id == mid), None) or _Partial(mid)

    async def history(self, limit=None):
        for m in list(reversed(self.sent))[:limit]:
            yield m


class FakeThread(FakeChannel):
    """A forum post. Like discord.py, Guild.get_channel does not return these; get_channel_or_thread does."""

    def __init__(self, guild, cid, name, archived=False, locked=False):
        super().__init__(guild, cid, name)
        self.archived, self.locked = archived, locked

    async def edit(self, **kw):
        if self.locked:
            raise http_error("Forbidden")
        self.archived = kw.get("archived", self.archived)

    async def send(self, content=None, **kw):
        if self.archived:
            raise http_error("Forbidden")  # strict: the cog has to unarchive before it posts
        return await super().send(content, **kw)


class FakeMember:
    def __init__(self, guild, uid, bot=False, name=None):
        self.guild, self.id, self.bot = guild, uid, bot
        self.display_name = name or f"user{uid}"
        self.roles = []
        self.dms = []
        self.dm_closed = False
        guild.members.append(self)

    @property
    def mention(self):
        return f"<@{self.id}>"

    async def send(self, content=None, **kw):
        if self.dm_closed:
            raise http_error("Forbidden")
        self.dms.append(content)

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, gid=1):
        self.id = gid
        self.members, self.roles, self.channels, self.threads = [], [], {}, {}
        self.name = "Wonderland"
        self.created_roles = []

    async def create_role(self, *, name, **kw):
        role = FakeRole(self, 7000 + len(self.roles), name)
        role.kw = kw
        self.roles.append(role)
        self.created_roles.append(role)
        return role

    def add_role(self, rid, name):
        role = FakeRole(self, rid, name)
        self.roles.append(role)
        return role

    def add_channel(self, cid, name="chan"):
        ch = FakeChannel(self, cid, name)
        self.channels[cid] = ch
        return ch

    def add_thread(self, tid, name="post", **kw):
        thread = FakeThread(self, tid, name, **kw)
        self.threads[tid] = thread
        return thread

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return self.channels.get(cid)  # channels only, as in discord.py

    def get_channel_or_thread(self, cid):
        return self.channels.get(cid) or self.threads.get(cid)


class FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]
        self.user = types.SimpleNamespace(id=999)
        self.loop = types.SimpleNamespace(create_task=self._create_task)

    @staticmethod
    def _create_task(coro):
        coro.close()  # background loops never run in unit tests
        return types.SimpleNamespace(cancel=lambda: None)

    def get_guild(self, gid):
        return next((g for g in self.guilds if g.id == gid), None)

    async def get_valid_prefixes(self, guild=None):
        return ["."]

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


def install(monkeypatch):
    """Fake redbot (always) and top up discord with any names the cog touches."""
    import discord

    class _Cog:
        @staticmethod
        def listener(*a, **kw):
            return lambda f: f

    commands_ns = types.SimpleNamespace(
        Cog=_Cog, group=_deco, command=_deco, Context=object, guild_only=_passthrough,
        mod_or_permissions=_passthrough, admin_or_permissions=_passthrough, has_permissions=_passthrough,
    )
    core = types.ModuleType("redbot.core")
    core.Config, core.commands = FakeConfig, commands_ns
    root = types.ModuleType("redbot")
    root.core = core
    for name, mod in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns)):
        monkeypatch.setitem(sys.modules, name, mod)

    class _AllowedMentions:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        @classmethod
        def none(cls):
            return cls(users=False, roles=False, everyone=False)

    for name in ("Forbidden", "NotFound", "HTTPException"):
        if not hasattr(discord, name):
            monkeypatch.setattr(discord, name, type(name, (Exception,), {}), raising=False)
    for name in ("Member", "Guild", "Role", "TextChannel", "Thread", "Message"):
        if not hasattr(discord, name):
            monkeypatch.setattr(discord, name, type(name, (), {}), raising=False)
    if not hasattr(discord, "AllowedMentions"):
        monkeypatch.setattr(discord, "AllowedMentions", _AllowedMentions, raising=False)
    if not hasattr(discord, "Colour"):
        monkeypatch.setattr(discord, "Colour", lambda v=0: v, raising=False)
    if not hasattr(discord.Embed, "set_footer"):
        monkeypatch.setattr(discord.Embed, "set_footer", lambda self, text=None, **kw: self, raising=False)
    for color in ("gold", "dark_grey", "green", "orange", "red"):
        if not hasattr(discord.Color, color):
            monkeypatch.setattr(discord.Color, color, classmethod(lambda cls, c=color: cls(c)), raising=False)
    return core
