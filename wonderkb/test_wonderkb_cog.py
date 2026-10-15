"""The cog end to end against fakes (redbot and the other cogs faked, discord real)."""
import importlib
import sys
import types

import discord
import pytest

from wonderkb import engine


class _Cmd:
    def __init__(self, f, **kw):
        self.callback, self.kw = f, kw
        self.__name__ = getattr(f, "__name__", "cmd")

    def command(self, *a, **kw):
        return lambda f: _Cmd(f, **kw)

    def group(self, *a, **kw):
        return lambda f: _Cmd(f, **kw)


def _deco(*a, **kw):
    return lambda f: _Cmd(f, **kw)


def _passthrough(*a, **kw):
    return lambda f: f


class _Ctx:
    """Like Red's value context manager: awaitable for the value, or `async with` to edit it in place."""

    def __init__(self, store, key):
        self.store, self.key = store, key

    def __await__(self):
        async def _get():
            return self.store[self.key]
        return _get().__await__()

    async def __aenter__(self):
        return self.store[self.key]

    async def __aexit__(self, *a):
        return False


class _Value:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def __call__(self):
        return _Ctx(self.store, self.key)

    async def set(self, value):
        self.store[self.key] = value


class FakeConfig:
    def __init__(self):
        self.defaults, self.data = {}, {}

    @classmethod
    def get_conf(cls, *a, **kw):
        return cls()

    def register_guild(self, **d):
        self.defaults.update(d)

    def guild(self, guild):
        outer = self
        store = outer.data.setdefault(guild.id, {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                                                 for k, v in outer.defaults.items()})

        class _G:
            def __getattr__(self_inner, key):
                return _Value(store, key)

            async def all(self_inner):
                return store

        return _G()


@pytest.fixture
def kb(monkeypatch):
    class _Cog:
        listener = staticmethod(lambda *a, **kw: (lambda f: f))

    commands_ns = types.SimpleNamespace(
        Cog=_Cog, group=_deco, command=_deco, Context=object, guild_only=_passthrough,
        mod_or_permissions=_passthrough, admin_or_permissions=_passthrough)
    core = types.ModuleType("redbot.core")
    core.commands, core.Config = commands_ns, FakeConfig
    root = types.ModuleType("redbot")
    root.core = core
    for name, m in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns)):
        monkeypatch.setitem(sys.modules, name, m)
    monkeypatch.delitem(sys.modules, "wonderkb.wonderkb", raising=False)
    yield importlib.import_module("wonderkb.wonderkb")
    sys.modules.pop("wonderkb.wonderkb", None)


# ---------------------------------------------------------------- fakes


class FakeMessage:
    _n = 100

    def __init__(self, content="", embeds=(), author_id=5, channel=None, reference=None):
        FakeMessage._n += 1
        self.id = FakeMessage._n
        self.content, self.embeds = content, list(embeds)
        self.author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
        self.channel, self.reference = channel, reference
        self.guild = channel.guild if channel is not None else None
        self.jump_url = f"https://discord.com/channels/1/1/{self.id}"


class FakeChannel:
    def __init__(self, id, name, hidden=False, fail_history=False):
        self.id, self.name, self.hidden, self.fail_history = id, name, hidden, fail_history
        self.mention = f"<#{id}>"
        self.messages, self.sent, self.guild = [], [], None

    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=not self.hidden, read_message_history=not self.hidden)

    def say(self, content="", **kw):
        m = FakeMessage(content, channel=self, **kw)
        self.messages.append(m)
        return m

    async def history(self, limit=100):
        if self.fail_history:
            raise discord.HTTPException(types.SimpleNamespace(status=500, reason="boom"), "boom")
        for m in list(reversed(self.messages))[:limit]:
            yield m

    async def fetch_message(self, mid):
        for m in self.messages:
            if m.id == mid:
                return m
        raise discord.HTTPException(types.SimpleNamespace(status=404, reason="gone"), "gone")

    async def send(self, **kw):
        self.sent.append(kw)


class FakeGuild:
    def __init__(self, channels, id=1):
        self.id = id
        self.text_channels = channels
        self.default_role = types.SimpleNamespace(id="everyone")
        for c in channels:
            c.guild = self

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class FakeStore:
    def __init__(self):
        self.rows = {}

    async def add(self, gid, name, text, vec, model):
        self.rows[name] = {"text": text, "model": model}

    async def delete(self, gid, name):
        self.rows.pop(name, None)

    async def exists(self, gid, name):
        return name in self.rows

    async def get_all_metadata(self, gid):
        return {n: dict(m) for n, m in self.rows.items()}


class FakeAssistant:
    def __init__(self, has_key=True):
        self.has_key, self.store, self.batches, self.db = has_key, FakeStore(), [], self
        self.embedding_store = self.store

    def get_conf(self, guild):
        return object()

    async def can_call_llm(self, conf, ctx=None):
        return self.has_key

    async def request_embeddings_batch(self, texts, conf):
        self.batches.append(list(texts))
        return [[float(len(t))] for t in texts], "text-embedding-3-small"

    async def request_embedding_with_info(self, text, conf):
        return [float(len(text))], "text-embedding-3-small"


class FakeShopCog:
    def __init__(self, shops):
        self.config = types.SimpleNamespace(guild=lambda g: types.SimpleNamespace(shops=self._shops))
        self._s = shops

    async def _shops(self):
        return self._s


class FakeAliasCog:
    def __init__(self, entries):
        async def guild_aliases(g):
            return [types.SimpleNamespace(name=n, command=c) for n, c in entries]

        async def global_aliases():
            return []

        self._aliases = types.SimpleNamespace(get_guild_aliases=guild_aliases, get_global_aliases=global_aliases)


class FakeCmd:
    def __init__(self, name, level="NONE", parents=()):
        self.qualified_name, self.hidden, self.parents = name, False, list(parents)
        self.requires = types.SimpleNamespace(privilege_level=types.SimpleNamespace(name=level), user_perms=None)


class FakeHeistCog:
    """A third-party cog: one public command, one admin command, and a public-looking child of the admin one."""
    qualified_name = "Heist"

    def walk_commands(self):
        admin = FakeCmd("heist reset", level="ADMIN")
        yield FakeCmd("heist")
        yield admin
        yield FakeCmd("heist reset all", parents=[admin])


class FakeBot:
    def __init__(self, guilds, cogs):
        self.guilds, self.cogs = guilds, cogs
        self.user = types.SimpleNamespace(id=99, mention="<@99>")

    def get_cog(self, name):
        return self.cogs.get(name)


class FakeCtx:
    def __init__(self, guild):
        self.guild, self.sent = guild, []

    async def send(self, *a, **kw):
        self.sent.append(a[0] if a else kw)

    def typing(self):
        class _T:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return _T()


SHOPS = {"wondershop - colors": {"giftable": True, "stock": {
    "Rose": {"price": 20000, "amount": None, "description": "", "role_id": 1},
    "Cloud": {"price": 20000, "amount": None, "description": "", "role_id": 2}}}}


def build(kb, *, has_key=True, with_shop=True, rules_text="Be kind to everyone."):
    rules = FakeChannel(10, "📜・rules")
    guide = FakeChannel(11, "📖・server-guide")
    modchat = FakeChannel(12, "🛡️・mod-chat")
    private = FakeChannel(13, "staff-secrets", hidden=True)
    rules.say(rules_text)
    guide.say("Welcome", embeds=[types.SimpleNamespace(to_dict=lambda: {"title": "Colors", "description": "Buy them in the shop."})])
    guild = FakeGuild([rules, guide, modchat, private])
    assistant = FakeAssistant(has_key)
    cogs = {"Assistant": assistant, "Alias": FakeAliasCog([("removemy", "selfrole remove")]), "Heist": FakeHeistCog()}
    if with_shop:
        cogs["Shop"] = FakeShopCog(SHOPS)
    bot = FakeBot([guild], cogs)
    cog = kb.WonderKB(bot)
    return cog, guild, assistant, {"rules": rules, "guide": guide, "mod": modchat, "private": private}


# ---------------------------------------------------------------- syncing


async def test_first_sync_reads_channels_shop_aliases_and_seed(kb):
    cog, guild, assistant, ch = build(kb)
    result = await cog.sync_guild(guild)
    assert result["ok"] and result["added"] > 20 and result["deleted"] == 0
    names = set(assistant.store.rows)
    assert any(n.startswith("kb:ch:10:") for n in names)                  # rules
    assert any(n.startswith("kb:ch:11:") for n in names)                  # server-guide, embed text included
    assert "kb:shop:wondershop - colors:__howto__" in names
    assert any("Rose" in r["text"] and "20,000" in r["text"] for n, r in assistant.store.rows.items() if n.startswith("kb:shop:"))
    assert "kb:alias:removemy" in names
    assert any(n.startswith("kb:seed:") for n in names)
    assert "Buy them in the shop." in " ".join(r["text"] for r in assistant.store.rows.values())


async def test_every_loaded_cog_gets_an_overview_but_never_its_admin_commands(kb):
    cog, guild, assistant, ch = build(kb)
    result = await cog.sync_guild(guild)
    assert result["notes"]["cogs"] == "1 entries"                         # Heist only; Assistant is skipped, odd fakes ignored
    text = assistant.store.rows["kb:cog:Heist"]["text"]
    assert text.endswith("Commands anyone can run: .heist.")
    assert "reset" not in text                                            # admin command and its child both excluded


async def test_second_sync_embeds_nothing(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.sync_guild(guild)
    calls = len(assistant.batches)
    again = await cog.sync_guild(guild)
    assert again["ok"] and again["added"] == 0 and again["deleted"] == 0
    assert len(assistant.batches) == calls                                 # no API call at all


async def test_an_edited_message_reembeds_only_that_entry(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.sync_guild(guild)
    ch["rules"].messages[0].content = "Be kind. No spam."
    result = await cog.sync_guild(guild)
    assert result["added"] == 1
    assert assistant.batches[-1] == ["From #rules: Be kind. No spam."]


async def test_a_deleted_message_is_forgotten(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.sync_guild(guild)
    ch["rules"].messages.clear()
    result = await cog.sync_guild(guild)
    assert result["deleted"] == 1
    assert not any(n.startswith("kb:ch:10:") for n in assistant.store.rows)


async def test_a_failed_channel_read_never_wipes_what_it_knew(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.sync_guild(guild)
    before = {n for n in assistant.store.rows if n.startswith("kb:ch:10:")}
    assert before
    ch["rules"].fail_history = True
    result = await cog.sync_guild(guild)
    assert result["ok"] and "could not read" in result["notes"]["#rules"]
    assert {n for n in assistant.store.rows if n.startswith("kb:ch:10:")} == before


async def test_a_private_channel_is_never_read(kb):
    cog, guild, assistant, ch = build(kb)
    ch["private"].say("mod only plans")
    await cog.config.guild(guild).sources.set(["rules", "13"])
    result = await cog.sync_guild(guild)
    assert "not visible to @everyone" in result["notes"]["#staff-secrets"]
    assert "mod only plans" not in " ".join(r["text"] for r in assistant.store.rows.values())


async def test_hand_added_answers_survive_a_sync(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.sync_guild(guild)
    ctx = FakeCtx(guild)
    await cog.kb_add.callback(cog, ctx, text="How do I get VIP? | Boost the server.")
    key = next(n for n in assistant.store.rows if n.startswith("kbm:"))
    await cog.sync_guild(guild)
    assert key in assistant.store.rows


async def test_no_api_key_means_no_writes(kb):
    cog, guild, assistant, ch = build(kb, has_key=False)
    result = await cog.sync_guild(guild)
    assert not result["ok"] and "API key" in result["error"] and assistant.store.rows == {}


async def test_missing_shop_and_wonderland_are_skipped_not_fatal(kb):
    cog, guild, assistant, ch = build(kb, with_shop=False)
    result = await cog.sync_guild(guild)
    assert result["ok"]
    assert "not loaded" in result["notes"]["shop"] and "not loaded" in result["notes"]["guide"]


async def test_missing_assistant_cog_is_reported(kb):
    cog, guild, assistant, ch = build(kb)
    cog.bot.cogs.pop("Assistant")
    assert (await cog.sync_guild(guild))["error"] == "The Assistant cog is not loaded."


async def test_one_bad_guild_does_not_kill_the_loop(kb):
    cog, guild, assistant, ch = build(kb)
    bad = types.SimpleNamespace(id=2)
    cog.bot.guilds = [bad, guild]                                          # `bad` has no channels or config shape
    await cog.config.guild(guild).last_sync.set(0.0)
    await cog.sync_loop.coro(cog)
    assert assistant.store.rows                                           # the good guild still synced


# ---------------------------------------------------------------- unanswered questions


def ask_and_refuse(ch, text="how do i buy a color", reply="I'm not sure, try .wld search color or ask a mod."):
    asked = ch["rules"].say(text)
    return ch["rules"].say(reply, author_id=99, reference=types.SimpleNamespace(resolved=None, message_id=asked.id)), asked


async def test_an_unsure_reply_goes_to_mod_chat_once(kb):
    cog, guild, assistant, ch = build(kb)
    reply, asked = ask_and_refuse(ch)
    await cog.on_message(reply)
    assert len(ch["mod"].sent) == 1
    embed = ch["mod"].sent[0]["embed"]
    assert "how do i buy a color" in embed.description
    assert ".kb add how do i buy a color | <your answer>" in embed.fields[-1].value
    again, _ = ask_and_refuse(ch)                                          # same question, different case below
    await cog.on_message(again)
    assert len(ch["mod"].sent) == 1                                        # deduped


async def test_normal_answers_and_other_authors_are_ignored(kb):
    cog, guild, assistant, ch = build(kb)
    reply, _ = ask_and_refuse(ch, reply="Use .puzzle mypieces")
    await cog.on_message(reply)
    impostor, _ = ask_and_refuse(ch, text="another q")
    impostor.author = types.SimpleNamespace(id=5, mention="<@5>")          # a member typing the phrase
    await cog.on_message(impostor)
    assert ch["mod"].sent == []


async def test_logging_can_be_turned_off(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.config.guild(guild).log_unsure.set(False)
    reply, _ = ask_and_refuse(ch)
    await cog.on_message(reply)
    assert ch["mod"].sent == []


async def test_no_mod_channel_means_no_crash(kb):
    cog, guild, assistant, ch = build(kb)
    guild.text_channels.remove(ch["mod"])
    reply, _ = ask_and_refuse(ch)
    await cog.on_message(reply)


# ---------------------------------------------------------------- commands


async def test_kb_add_remove_and_list(kb):
    cog, guild, assistant, ch = build(kb)
    ctx = FakeCtx(guild)
    await cog.kb_add.callback(cog, ctx, text="no separator here")
    assert "Use `.kb add" in ctx.sent[-1] and assistant.store.rows == {}
    await cog.kb_add.callback(cog, ctx, text="How do I get VIP? | Boost the server.")
    (name,) = assistant.store.rows
    assert assistant.store.rows[name]["text"] == "Q: How do I get VIP?\nA: Boost the server."
    await cog.kb_list.callback(cog, ctx)
    assert "How do I get VIP?" in ctx.sent[-1]
    await cog.kb_remove.callback(cog, ctx, key=name[len("kbm:"):])
    assert assistant.store.rows == {} and ctx.sent[-1] == "Forgotten."
    await cog.kb_remove.callback(cog, ctx, key="nope")
    assert "No hand-added answer" in ctx.sent[-1]


async def test_kb_remove_cannot_delete_automatic_entries(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.sync_guild(guild)
    ctx = FakeCtx(guild)
    await cog.kb_remove.callback(cog, ctx, key="kb:seed:anything")
    assert "No hand-added answer" in ctx.sent[-1]
    assert any(n.startswith("kb:seed:") for n in assistant.store.rows)


async def test_sources_add_refuses_a_private_channel(kb):
    cog, guild, assistant, ch = build(kb)
    ctx = FakeCtx(guild)
    await cog.kb_sources_add.callback(cog, ctx, channel=ch["private"])
    assert "not visible to @everyone" in ctx.sent[-1]
    assert "13" not in await cog.config.guild(guild).sources()
    await cog.kb_sources_add.callback(cog, ctx, channel=ch["mod"])
    assert "12" in await cog.config.guild(guild).sources() or "Now reading" in ctx.sent[-1]


async def test_sources_remove_clears_that_channels_entries(kb):
    cog, guild, assistant, ch = build(kb)
    await cog.sync_guild(guild)
    assert any(n.startswith("kb:ch:10:") for n in assistant.store.rows)
    await cog.kb_sources_remove.callback(cog, FakeCtx(guild), channel=ch["rules"])
    assert not any(n.startswith("kb:ch:10:") for n in assistant.store.rows)


async def test_status_and_sync_commands_report(kb):
    cog, guild, assistant, ch = build(kb)
    ctx = FakeCtx(guild)
    await cog.kb_sync.callback(cog, ctx)
    assert ctx.sent[-1].startswith("Synced.")
    await cog.kb_status.callback(cog, ctx)
    text = ctx.sent[-1]
    assert "Assistant cog: loaded" in text and "automatic" in text and "<#12>" in text


async def test_shopdump_shows_the_structure(kb):
    cog, guild, assistant, ch = build(kb)
    ctx = FakeCtx(guild)
    await cog.kb_shopdump.callback(cog, ctx)
    assert "wondershop - colors" in ctx.sent[-1] and "Entries it can read: 2" in ctx.sent[-1]


async def test_version_probe(kb):
    cog, guild, assistant, ch = build(kb)
    ctx = FakeCtx(guild)
    await cog.kb_version.callback(cog, ctx)
    assert ctx.sent[-1] == "WonderKB v1.1.1"
