"""The command end to end against fakes (redbot faked, discord real): who it reads, what it sends."""
import importlib
import sys
import types

import discord
import pytest


class _Cmd:
    def __init__(self, f, **kw):
        self.callback, self.kw, self.subs = f, kw, {}
        self.__name__ = getattr(f, "__name__", "cmd")

    def command(self, *a, **kw):
        def deco(f):
            c = _Cmd(f, **kw)
            self.subs[kw.get("name", f.__name__)] = c
            return c
        return deco

    def __call__(self, *a, **kw):
        return self.callback(*a, **kw)


def _deco(*a, **kw):
    return lambda f: _Cmd(f, **kw)


def _passthrough(*a, **kw):
    return lambda f: f


class FakeBank:
    async def get_currency_name(self, guild):
        return "wondercoins"

    async def get_balance(self, member):
        return 12345


@pytest.fixture
def mod(monkeypatch):
    commands_ns = types.SimpleNamespace(
        Cog=object, group=_deco, command=_deco, Context=object, guild_only=_passthrough,
        cooldown=_passthrough, BucketType=types.SimpleNamespace(user=1))
    core = types.ModuleType("redbot.core")
    core.commands, core.bank = commands_ns, FakeBank()
    root = types.ModuleType("redbot")
    root.core = core
    for name, m in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns)):
        monkeypatch.setitem(sys.modules, name, m)
    for name in ("mystats.mystats", "mystats.embeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    m = importlib.import_module("mystats.mystats")
    yield m
    for name in ("mystats.mystats", "mystats.embeds"):
        sys.modules.pop(name, None)


class Ctx:
    def __init__(self, author, guild):
        self.author, self.guild, self.sent = author, guild, []

    def typing(self):
        class T:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return None
        return T()

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


class Member:
    display_avatar = None

    def __init__(self, uid, name, guild):
        self.id, self.display_name, self.guild = uid, name, guild


class Grp:
    def __init__(self, d):
        self.d = d

    async def all(self):
        return dict(self.d)


class Cfg:
    def __init__(self, member):
        self._m = member

    def member(self, m):
        return Grp(self._m)


class Bot:
    def __init__(self, **cogs):
        self.cogs = cogs

    def get_cog(self, n):
        return self.cogs.get(n)


GUILD = types.SimpleNamespace(id=1)


async def test_command_sends_one_embed_with_only_games_that_have_data(mod):
    duel = types.SimpleNamespace(config=Cfg({"wins": 2, "losses": 1, "coins_won": 900, "coins_lost": 100}))
    cog = mod.MyStats(Bot(Duel=duel))
    me = Member(5, "Fareed", GUILD)
    ctx = Ctx(me, GUILD)
    await cog.mystats.callback(cog, ctx, None)
    (content, kw), = ctx.sent
    e = kw["embed"]
    assert e.title.endswith("Stats: Fareed")
    assert "12,345 wondercoins" in e.description
    assert [f.name for f in e.fields] == ["\N{CROSSED SWORDS} Duels"]
    assert "2 won, 1 lost (66%)" in e.fields[0].value and "+800" in e.fields[0].value


async def test_command_for_another_member_reads_that_member(mod):
    seen = []

    class SpyCfg(Cfg):
        def member(self, m):
            seen.append(m.id)
            return super().member(m)

    cog = mod.MyStats(Bot(Duel=types.SimpleNamespace(config=SpyCfg({"wins": 1}))))
    ctx = Ctx(Member(5, "Me", GUILD), GUILD)
    await cog.mystats.callback(cog, ctx, Member(77, "Friend", GUILD))
    assert seen == [77]
    assert ctx.sent[0][1]["embed"].title.endswith("Stats: Friend")


async def test_new_member_gets_a_friendly_empty_state(mod):
    cog = mod.MyStats(Bot())
    ctx = Ctx(Member(5, "New", GUILD), GUILD)
    await cog.mystats.callback(cog, ctx, None)
    e = ctx.sent[0][1]["embed"]
    assert [f.name for f in e.fields] == ["Nothing yet"]


async def test_version_probe(mod):
    cog = mod.MyStats(Bot())
    ctx = Ctx(Member(5, "x", GUILD), GUILD)
    await cog.mystats.subs["version"].callback(cog, ctx)
    assert ctx.sent[0][0] == "MyStats v1.0.1"
