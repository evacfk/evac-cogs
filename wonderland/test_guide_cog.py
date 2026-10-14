"""The guide cog end to end against fakes (redbot faked, discord real)."""
import importlib
import sys
import types

import discord
import pytest

from wonderland.content import HOME_KEY, MAP_KEY


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


def _deco(*a, **kw):
    return lambda f: _Cmd(f, **kw)


def _passthrough(*a, **kw):
    return lambda f: f


class FakeConfigValue:
    def __init__(self, store, key):
        self.store, self.key = store, key

    async def __call__(self):
        return self.store.get(self.key)

    async def set(self, value):
        self.store[self.key] = value


class FakeConfig:
    def __init__(self):
        self.data = {}
        self.defaults = {}

    @classmethod
    def get_conf(cls, *a, **kw):
        return cls()

    def register_guild(self, **defaults):
        self.defaults.update(defaults)

    def guild(self, guild):
        outer = self

        class _G:
            @property
            def publish(self_inner):
                store = outer.data.setdefault(guild.id, {})
                store.setdefault("publish", outer.defaults["publish"])
                return FakeConfigValue(store, "publish")
        return _G()


@pytest.fixture
def mod(monkeypatch):
    commands_ns = types.SimpleNamespace(
        Cog=object, hybrid_group=_deco, group=_deco, command=_deco, Context=object, guild_only=_passthrough,
        cooldown=_passthrough, BucketType=types.SimpleNamespace(user=1), mod_or_permissions=_passthrough)
    core = types.ModuleType("redbot.core")
    core.commands, core.Config = commands_ns, FakeConfig
    root = types.ModuleType("redbot")
    root.core = core
    for name, m in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns)):
        monkeypatch.setitem(sys.modules, name, m)
    for name in ("wonderland.guide", "wonderland.embeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    m = importlib.import_module("wonderland.guide")
    yield m
    for name in ("wonderland.guide", "wonderland.embeds"):
        sys.modules.pop(name, None)


class FakeMessage:
    _next = 1000

    def __init__(self, **kw):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.kw = kw
        self.deleted = False
        self.edits = []
        self.view = kw.get("view")

    async def delete(self):
        self.deleted = True

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeChannel:
    def __init__(self, id, name):
        self.id, self.name = id, name
        self.sent = []
        self.store = {}
        self.mention = f"<#{id}>"

    async def send(self, **kw):
        m = FakeMessage(**kw)
        self.sent.append(m)
        self.store[m.id] = m
        return m

    async def fetch_message(self, mid):
        if mid not in self.store:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="x"), "gone")
        return self.store[mid]


class FakeGuild:
    id = 1

    def __init__(self, channels):
        self.channels = channels


class FakeBot:
    def __init__(self, commands=(), mods=(), cogs=None):
        self.commands, self.mods, self.cogs = set(commands), set(mods), cogs or {}

    def get_command(self, name):
        return types.SimpleNamespace(enabled=True) if name in self.commands else None

    async def is_mod(self, member):
        return member.id in self.mods

    def get_cog(self, name):
        return self.cogs.get(name)


class FakeCtx:
    def __init__(self, author_id=5, guild=None, interaction=None):
        self.author = types.SimpleNamespace(id=author_id)
        self.guild = guild or FakeGuild([])
        self.interaction = interaction
        self.message = FakeMessage()
        self.sent = []

    async def send(self, *a, **kw):
        m = FakeMessage(**kw)
        m.args = a
        self.sent.append(m)
        return m

    def typing(self):
        class _T:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return _T()


def make(mod, **bot_kw):
    return mod.Wonderland(FakeBot(**bot_kw))


async def test_prefix_reply_is_short_lived_and_invoking_message_removed(mod):
    cog = make(mod, commands={"payday"})
    ctx = FakeCtx()
    await cog.wonderland.callback(cog, ctx)
    sent = ctx.sent[0]
    assert sent.kw["delete_after"] == mod.PREFIX_REPLY_TTL
    assert "ephemeral" not in sent.kw
    assert ctx.message.deleted
    assert sent.kw["view"].message is sent
    assert "Wonderland Guide" in sent.kw["embed"].title


async def test_slash_reply_is_ephemeral_and_nothing_is_deleted(mod):
    cog = make(mod)
    ctx = FakeCtx(interaction=object())
    await cog.wonderland.callback(cog, ctx)
    sent = ctx.sent[0]
    assert sent.kw["ephemeral"] is True
    assert "delete_after" not in sent.kw
    assert not ctx.message.deleted


async def test_missing_manage_messages_does_not_break_the_reply(mod):
    cog = make(mod)
    ctx = FakeCtx()

    async def boom():
        raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "no")
    ctx.message.delete = boom
    await cog.wonderland.callback(cog, ctx)
    assert len(ctx.sent) == 1


async def test_page_shows_only_live_commands(mod):
    cog = make(mod, commands={"payday", "shop"})
    ctx = FakeCtx()
    await cog.wonderland.subs["page"].callback(cog, ctx, "economy")
    text = ctx.sent[0].kw["embed"].description
    assert "`.payday`" in text and "`.shop`" in text
    assert ".lottery" not in text and ".bank balance" not in text


async def test_member_never_sees_the_mod_page_or_dropdown_option(mod):
    cog = make(mod)
    ctx = FakeCtx(author_id=5)
    await cog.wonderland.subs["mod"].callback(cog, ctx)
    assert "Mod Cheat Sheet" not in (ctx.sent[0].kw["embed"].title or "")
    await cog.wonderland.callback(cog, ctx)
    values = [o.value for o in ctx.sent[-1].kw["view"].children[0].options]
    assert "mod" not in values and MAP_KEY in values and HOME_KEY in values


async def test_mod_gets_the_mod_page_and_option(mod):
    cog = make(mod, mods={9})
    ctx = FakeCtx(author_id=9)
    await cog.wonderland.subs["mod"].callback(cog, ctx)
    assert "Mod Cheat Sheet" in ctx.sent[0].kw["embed"].title
    values = [o.value for o in ctx.sent[0].kw["view"].children[0].options]
    assert values[-1] == "mod"


async def test_map_links_live_channels_and_falls_back_to_bold_names(mod):
    guild = FakeGuild([FakeChannel(11, "🧸・cuddle"), FakeChannel(12, "🎮 • Room Creator")])
    cog = make(mod)
    ctx = FakeCtx(guild=guild)
    await cog.wonderland.subs["map"].callback(cog, ctx)
    text = ctx.sent[0].kw["embed"].description
    assert "<#11>" in text and "<#12>" in text and "**pillow-talk**" in text


async def test_search_reports_hits_and_misses(mod):
    cog = make(mod, commands={"fish", "fishinfo"})
    ctx = FakeCtx()
    await cog.wonderland.subs["search"].callback(cog, ctx, word="fish")
    assert "`.fish`" in ctx.sent[0].kw["embed"].description
    await cog.wonderland.subs["search"].callback(cog, ctx, word="zzzz")
    assert "Nothing found" in ctx.sent[1].kw["embed"].description


async def test_stats_uses_the_mystats_cog_and_replies_privately(mod):
    class FakeStats:
        async def build_embed(self, ctx, member):
            return discord.Embed(title=f"stats for {member.id}")
    cog = make(mod, cogs={"MyStats": FakeStats()})
    ctx = FakeCtx(author_id=5)
    await cog.wonderland.subs["stats"].callback(cog, ctx, None)
    assert ctx.sent[0].kw["embed"].title == "stats for 5"
    assert ctx.sent[0].kw["delete_after"] == mod.PREFIX_REPLY_TTL


async def test_stats_without_mystats_says_so(mod):
    cog = make(mod)
    ctx = FakeCtx()
    await cog.wonderland.subs["stats"].callback(cog, ctx, None)
    assert "aren't available" in ctx.sent[0].args[0]


async def test_dropdown_only_answers_to_the_person_who_asked(mod):
    cog = make(mod)
    view = mod.GuideView(cog, author_id=5, is_mod=False)
    replies = []

    async def send_message(*a, **kw):
        replies.append((a, kw))
    stranger = types.SimpleNamespace(user=types.SimpleNamespace(id=6), response=types.SimpleNamespace(send_message=send_message))
    owner = types.SimpleNamespace(user=types.SimpleNamespace(id=5), response=types.SimpleNamespace(send_message=send_message))
    assert await view.interaction_check(stranger) is False and replies[0][1]["ephemeral"] is True
    assert await view.interaction_check(owner) is True


async def test_view_timeout_cleans_up_the_message(mod):
    cog = make(mod)
    view = mod.GuideView(cog, 5, False)
    view.message = FakeMessage()
    await view.on_timeout()
    assert view.message.deleted


async def test_publish_posts_every_public_page_then_edits_in_place(mod):
    guild = FakeGuild([])
    cog = make(mod)
    ctx = FakeCtx(guild=guild)
    channel = FakeChannel(77, "server-guide")
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel)
    public = [p for p in mod.PAGES if not p.mod_only]
    assert len(channel.sent) == len(public) + 1      # every page plus the map
    assert all(m.kw["embed"].footer.text == mod.PUBLISH_FOOTER for m in channel.sent)
    assert "Mod Cheat Sheet" not in " ".join(m.kw["embed"].title for m in channel.sent)
    first_ids = [m.id for m in channel.sent]
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel)
    assert len(channel.sent) == len(first_ids)        # nothing new posted
    assert all(m.edits for m in channel.sent)


async def test_publish_reposts_a_message_that_was_deleted(mod):
    cog = make(mod)
    ctx = FakeCtx()
    channel = FakeChannel(77, "server-guide")
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel)
    count = len(channel.sent)
    channel.store.pop(channel.sent[0].id)
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel)
    assert len(channel.sent) == count + 1


async def test_publish_to_a_new_channel_does_not_touch_the_old_posts(mod):
    cog = make(mod)
    ctx = FakeCtx()
    old, new = FakeChannel(77, "a"), FakeChannel(78, "b")
    await cog.wonderland.subs["publish"].callback(cog, ctx, old)
    await cog.wonderland.subs["publish"].callback(cog, ctx, new)
    assert all(not m.edits for m in old.sent)
    assert len(new.sent) == len(old.sent)


async def test_version_probe(mod):
    cog = make(mod)
    ctx = FakeCtx()
    await cog.wonderland.subs["version"].callback(cog, ctx)
    assert ctx.sent[0].args[0].startswith("Wonderland guide v")
