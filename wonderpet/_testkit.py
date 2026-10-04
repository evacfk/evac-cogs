"""Shared fakes for the wonderpet cog tests (not loaded by Red; only imported by tests).

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


class FakeRole:
    def __init__(self, guild, rid, name, position=10, hoist=False, managed=False):
        self.guild, self.id, self.name, self.position = guild, rid, name, position
        self.hoist, self.managed = hoist, managed
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True
        if self in self.guild.roles:
            self.guild.roles.remove(self)

    async def edit(self, **kw):
        self.name = kw.get("name", self.name)

    def __eq__(self, o):
        return getattr(o, "id", None) == self.id

    def __hash__(self):
        return hash(self.id)

    def __lt__(self, o):
        return self.position < o.position

    def __gt__(self, o):
        return self.position > o.position

    def __ge__(self, o):
        return self.position >= o.position

    @property
    def mention(self):
        return f"<@&{self.id}>"

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]


class FakeMessage:
    def __init__(self, channel, content=None, embed=None, **kw):
        self.id = next(_ids)
        self.channel, self.content, self.embed, self.kw = channel, content, embed, kw
        self.reactions, self.deleted, self.view = [], False, kw.get("view")

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def delete(self):
        self.deleted = True

    async def edit(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeChannel:
    def __init__(self, guild, cid, name="chan"):
        self.guild, self.id, self.name = guild, cid, name
        self.sent = []
        self.members = []

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

    async def fetch_message(self, mid):
        return self.get_partial_message(mid)


class FakeMember:
    def __init__(self, guild, uid, joined_at, roles=(), bot=False, name=None):
        self.guild, self.id, self.joined_at, self.bot = guild, uid, joined_at, bot
        self.display_name = name or f"user{uid}"
        self.name = self.display_name
        self.roles = [guild.default_role, *roles]
        self.display_avatar = None
        guild.members.append(self)

    @property
    def mention(self):
        return f"<@{self.id}>"

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def add_roles(self, *roles, reason=None):
        for r in roles:
            if r not in self.roles:
                self.roles.append(r)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]

    def role_ids(self):
        return {r.id for r in self.roles if r != self.guild.default_role}


class FakeGuild:
    def __init__(self, gid=1):
        self.id = gid
        self.members, self.roles, self.channels = [], [], {}
        self.default_role = FakeRole(self, gid, "@everyone", 0)
        self.me = types.SimpleNamespace(top_role=FakeRole(self, 999, "Bot", 100))
        self.name = "Wonderland"

    _next_role = 5000

    async def create_role(self, *, name, **kw):
        FakeGuild._next_role += 1
        return self.add_role(FakeGuild._next_role, name, hoist=kw.get("hoist", False))

    def add_role(self, rid, name, **kw):
        role = FakeRole(self, rid, name, **kw)
        self.roles.append(role)
        return role

    def add_channel(self, cid, name="chan"):
        ch = FakeChannel(self, cid, name)
        self.channels[cid] = ch
        return ch

    @property
    def voice_channels(self):
        return list(self.channels.values())

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return self.channels.get(cid)

    get_channel_or_thread = get_channel


class FakeBot:
    def __init__(self, guild, cogs=None):
        self.guilds = [guild]
        self._cogs = cogs or {}
        self.loop = types.SimpleNamespace(create_task=self._create_task)

    @staticmethod
    def _create_task(coro):
        coro.close()  # background loops never run in unit tests
        return types.SimpleNamespace(cancel=lambda: None)

    def get_cog(self, name):
        return self._cogs.get(name)

    async def wait_until_red_ready(self):
        return None


class FakeBank:
    def __init__(self):
        self.deposits, self.withdrawals, self.balances = [], [], {}

    async def deposit_credits(self, member, amount):
        self.deposits.append((member.id, amount))
        self.balances[member.id] = self.balances.get(member.id, 0) + amount

    async def withdraw_credits(self, member, amount):
        if self.balances.get(member.id, 0) < amount:
            raise ValueError("insufficient funds")
        self.balances[member.id] -= amount
        self.withdrawals.append((member.id, amount))

    async def get_currency_name(self, guild):
        return "wondercoins"


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


def install(monkeypatch, bank=None, data_path=None):
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
    core.bank = bank or FakeBank()
    root = types.ModuleType("redbot")
    root.core = core
    dm = types.ModuleType("redbot.core.data_manager")
    dm.cog_data_path = lambda cog=None: data_path
    core.data_manager = dm
    for name, mod in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns),
                      ("redbot.core.data_manager", dm)):
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
    for name in ("Member", "Guild", "Role", "TextChannel", "VoiceChannel", "Message", "Interaction"):
        if not hasattr(discord, name):
            monkeypatch.setattr(discord, name, type(name, (), {}), raising=False)
    if not hasattr(discord, "AllowedMentions"):
        monkeypatch.setattr(discord, "AllowedMentions", _AllowedMentions, raising=False)
    if not hasattr(discord, "Colour"):
        monkeypatch.setattr(discord, "Colour", lambda v=0: v, raising=False)
    if not hasattr(discord.Embed, "set_thumbnail"):
        monkeypatch.setattr(discord.Embed, "set_thumbnail", lambda self, url=None: self, raising=False)
    try:
        discord.Color(1)
    except TypeError:  # root stub's Color takes a name; accept an int too
        monkeypatch.setattr(discord, "Color", lambda v=0: v, raising=False)
    return core


def install_ui(monkeypatch):
    """Top up discord.ui / enums when running under the repo's minimal discord stub."""
    import discord

    class _Item:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.value = kw.get("default")

    class _View:
        def __init__(self, *a, timeout=180, **kw):
            self.timeout, self.children = timeout, []
            for name in dir(type(self)):
                f = getattr(type(self), name, None)
                spec = getattr(f, "__button_spec__", None)
                if spec is not None:
                    item = _Item(**spec)
                    item.callback = f
                    setattr(self, name, item)
                    self.children.append(item)

        def add_item(self, item):
            self.children.append(item)

        def stop(self):
            pass

    class _Modal:
        def __init__(self, *a, title=None, timeout=None, **kw):
            self.title, self.children = title, []

        def add_item(self, item):
            self.children.append(item)

    def _button(**spec):
        def deco(f):
            f.__button_spec__ = spec
            return f
        return deco

    class _Poll:
        def __init__(self, question, duration, **kw):
            self.question, self.duration, self.answers = question, duration, []

        def add_answer(self, *, text, emoji=None):
            self.answers.append(text)

    real = hasattr(discord, "Client")  # the real library: use it as-is
    if not real:  # always replace: other cogs' conftests patch the shared stub in different ways
        ns = type("ui", (), {})()
        ns.View, ns.Modal, ns.TextInput, ns.Button, ns.button = _View, _Modal, _Item, _Item, _button
        monkeypatch.setattr(discord, "ui", ns, raising=False)
    import types as _t
    for name, value in (
        ("ButtonStyle", _t.SimpleNamespace(success=1, secondary=2, primary=3, danger=4)),
        ("TextStyle", _t.SimpleNamespace(short=1, paragraph=2)),
        ("EntityType", _t.SimpleNamespace(voice=2, external=3)),
        ("PrivacyLevel", _t.SimpleNamespace(guild_only=2)),
    ):
        if not real:
            monkeypatch.setattr(discord, name, value, raising=False)
    if not real:
        monkeypatch.setattr(discord, "Poll", _Poll, raising=False)
