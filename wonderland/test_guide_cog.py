"""The guide cog end to end against fakes (redbot faked, discord real)."""
import importlib
import sys
import types

import discord
import pytest

from wonderland.constants import SEARCH_TTL
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

            @property
            def economy(self_inner):
                store = outer.data.setdefault(guild.id, {})
                store.setdefault("economy", {"snapshots": []})

                class _Saved:
                    async def __aenter__(s):
                        return store["economy"]

                    async def __aexit__(s, *a):
                        return False
                return _Saved
        return _G()


@pytest.fixture
def mod(monkeypatch):
    class _Cog:
        # Red's Cog has a `listener()` decorator; the room-guard mixin uses it.
        listener = staticmethod(lambda *a, **kw: (lambda f: f))

    commands_ns = types.SimpleNamespace(
        Cog=_Cog, hybrid_group=_deco, group=_deco, command=_deco, Context=object, guild_only=_passthrough,
        cooldown=_passthrough, BucketType=types.SimpleNamespace(user=1), mod_or_permissions=_passthrough)
    core = types.ModuleType("redbot.core")
    core.commands, core.Config = commands_ns, FakeConfig
    root = types.ModuleType("redbot")
    root.core = core
    for name, m in (("redbot", root), ("redbot.core", core), ("redbot.core.commands", commands_ns)):
        monkeypatch.setitem(sys.modules, name, m)
    for name in ("wonderland.guide", "wonderland.embeds", "wonderland.economy_cog"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    m = importlib.import_module("wonderland.guide")
    yield m
    for name in ("wonderland.guide", "wonderland.embeds", "wonderland.economy_cog"):
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
    def __init__(self, id, name, hidden_from=()):
        self.id, self.name = id, name
        self.hidden_from = set(hidden_from)  # ids of members/roles that can't view it
        self.sent = []
        self.store = {}
        self.mention = f"<#{id}>"

    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=who.id not in self.hidden_from)

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
    default_role = types.SimpleNamespace(id="everyone")

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

    async def defer(self, *, ephemeral=False):
        self.deferred_ephemeral = ephemeral

    def typing(self, *, ephemeral=False):
        class _T:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return _T()


class Click:
    """A user tapping a button or picking from a menu."""

    def __init__(self, user_id, guild):
        self.user = types.SimpleNamespace(id=user_id)
        self.guild = guild
        self.deferred_ephemeral = None
        self.followups = []
        self.response = types.SimpleNamespace(defer=self._defer)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, *, ephemeral=False):
        self.deferred_ephemeral = ephemeral

    async def _send(self, *a, **kw):
        m = FakeMessage(**kw)
        m.args = a
        self.followups.append(m)
        return m


async def tap(stub, interaction):
    item = stub.open_button
    cb = getattr(item, "callback", None)
    if cb is None:
        await item(interaction, None)   # stub environment: the decorator left a plain method
    else:
        await cb(interaction)


async def shown(ctx, user_id=5):
    """What the person ends up seeing privately, for either way of invoking the command."""
    if ctx.interaction is not None:
        return ctx.sent[0].kw
    stub = ctx.sent[0].kw["view"]
    click = Click(user_id, ctx.guild)
    await tap(stub, click)
    assert click.deferred_ephemeral is True
    return click.followups[0].kw


def make(mod, **bot_kw):
    return mod.Wonderland(FakeBot(**bot_kw))


async def test_prefix_command_leaves_only_a_content_free_stub_in_the_channel(mod):
    cog = make(mod, commands={"payday"})
    ctx = FakeCtx()
    await cog.wonderland.callback(cog, ctx)
    stub = ctx.sent[0]
    assert stub.kw["delete_after"] == mod.STUB_TTL
    assert "embed" not in stub.kw                      # nothing from the guide is public
    assert "payday" not in stub.args[0] and "Economy" not in stub.args[0]
    assert isinstance(stub.kw["view"], mod.OpenView)
    assert ctx.message.deleted
    assert stub.kw["view"].message is stub


async def test_tapping_the_stub_gives_a_private_ephemeral_guide(mod):
    cog = make(mod, commands={"payday"})
    ctx = FakeCtx()
    await cog.wonderland.callback(cog, ctx)
    click = Click(5, ctx.guild)
    await tap(ctx.sent[0].kw["view"], click)
    reply = click.followups[0].kw
    assert click.deferred_ephemeral is True and reply["ephemeral"] is True
    assert "Wonderland Guide" in reply["embed"].title
    assert reply["view"].author_id == 5


async def test_each_person_who_taps_gets_their_own_copy_built_for_them(mod):
    guild = FakeGuild([FakeChannel(11, "🧸・cuddle"), FakeChannel(12, "🔢・counting", hidden_from={5})])
    cog = make(mod)
    ctx = FakeCtx(guild=guild)
    await cog.wonderland.subs["map"].callback(cog, ctx)
    stub = ctx.sent[0].kw["view"]
    c5, c6 = Click(5, guild), Click(6, guild)
    await tap(stub, c5)
    await tap(stub, c6)
    seen5 = c5.followups[0].kw["embed"].description
    seen6 = c6.followups[0].kw["embed"].description
    assert "<#12>" not in seen5 and "<#12>" in seen6
    assert c6.followups[0].kw["view"].author_id == 6


async def test_a_failing_builder_tells_the_person_instead_of_hanging(mod):
    cog = make(mod)

    async def broken(user):
        raise RuntimeError("boom")
    stub = mod.OpenView(broken)
    click = Click(5, FakeGuild([]))
    await tap(stub, click)
    assert "Something went wrong" in click.followups[0].args[0]
    assert click.followups[0].kw["ephemeral"] is True


async def test_stub_deletes_itself_when_it_times_out(mod):
    stub = mod.OpenView(None)
    stub.message = FakeMessage()
    await stub.on_timeout()
    assert stub.message.deleted


async def test_slash_reply_is_ephemeral_and_nothing_is_deleted(mod):
    cog = make(mod)
    ctx = FakeCtx(interaction=object())
    await cog.wonderland.callback(cog, ctx)
    sent = ctx.sent[0]
    assert ctx.deferred_ephemeral is True
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
    text = (await shown(ctx))["embed"].description
    assert "`.payday`" in text and "`.shop`" in text
    assert ".lottery" not in text and ".bank balance" not in text


async def test_member_never_sees_the_mod_page_or_button(mod):
    cog = make(mod)
    ctx = FakeCtx(author_id=5)
    await cog.wonderland.subs["mod"].callback(cog, ctx)
    reply = await shown(ctx, 5)
    assert "Mod Cheat Sheet" not in (reply["embed"].title or "")
    values = [b.key for b in reply["view"].children]
    assert "mod" not in values and MAP_KEY in values and HOME_KEY in values


async def test_mod_gets_the_mod_page_and_button(mod):
    cog = make(mod, mods={9})
    ctx = FakeCtx(author_id=9)
    await cog.wonderland.subs["mod"].callback(cog, ctx)
    reply = await shown(ctx, 9)
    assert "Mod Cheat Sheet" in reply["embed"].title
    values = [b.key for b in reply["view"].children]
    assert values[-1] == "mod"


async def test_map_links_live_channels_and_falls_back_to_bold_names(mod):
    guild = FakeGuild([FakeChannel(11, "🧸・cuddle"), FakeChannel(12, "🎮 • Room Creator")])
    cog = make(mod)
    ctx = FakeCtx(guild=guild)
    await cog.wonderland.subs["map"].callback(cog, ctx)
    text = (await shown(ctx))["embed"].description
    assert "<#11>" in text and "<#12>" in text and "**pillow-talk**" in text


def _map_text(mod, guild, member=None, public=False):
    cog = make(mod)
    return cog.render(MAP_KEY, guild, False, member=member, public=public).description


async def test_map_embed_is_titled_wonderland_channel_guide(mod):
    cog = make(mod)
    assert cog.render(MAP_KEY, FakeGuild([]), False).title == "🗺️ Wonderland Channel Guide"


async def test_map_hides_channels_the_member_cannot_open(mod):
    guild = FakeGuild([FakeChannel(11, "🧸・cuddle"), FakeChannel(12, "🔢・counting", hidden_from={5}),
                       FakeChannel(13, "❓・qotd")])
    text = _map_text(mod, guild, member=types.SimpleNamespace(id=5))
    assert "<#11>" in text and "<#13>" in text
    assert "<#12>" not in text and "counting" not in text


async def test_locked_channel_with_an_unlock_hint_stays_as_a_plain_name(mod):
    guild = FakeGuild([FakeChannel(12, "✅・verified-chat", hidden_from={5}),
                       FakeChannel(14, "🌙・pillow-talk", hidden_from={5})])
    text = _map_text(mod, guild, member=types.SimpleNamespace(id=5))
    assert "<#12>" not in text and "<#14>" not in text
    assert "**verified-chat**: for verified members (verify first)" in text
    assert "**pillow-talk**" in text and "(pick the role in role-select)" in text


async def test_verify_channel_disappears_for_someone_who_already_verified(mod):
    guild = FakeGuild([FakeChannel(13, "🔐・verify", hidden_from={5})])
    text = _map_text(mod, guild, member=types.SimpleNamespace(id=5))
    assert "verify**" not in text and "<#13>" not in text


async def test_verify_is_clickable_for_someone_who_can_still_see_it(mod):
    guild = FakeGuild([FakeChannel(13, "🔐・verify")])
    assert "<#13>" in _map_text(mod, guild, member=types.SimpleNamespace(id=6))


async def test_public_copy_never_links_verify_verified_chat_or_pillow_talk_even_if_everyone_can_see_them(mod):
    guild = FakeGuild([FakeChannel(11, "🧸・cuddle"), FakeChannel(12, "✅・verified-chat"),
                       FakeChannel(13, "🔐・verify"), FakeChannel(14, "🌙・pillow-talk")])
    text = _map_text(mod, guild, public=True)
    assert "<#11>" in text
    assert not any(f"<#{i}>" in text for i in (12, 13, 14))
    assert "**verify**" in text and "**verified-chat**" in text and "**pillow-talk**" in text


async def test_map_shows_the_gated_channel_to_a_member_who_has_access(mod):
    guild = FakeGuild([FakeChannel(12, "✅・verified-chat", hidden_from={5})])
    assert "<#12>" in _map_text(mod, guild, member=types.SimpleNamespace(id=6))


async def test_map_drops_a_section_with_nothing_visible(mod):
    everything = [FakeChannel(i, name, hidden_from={5}) for i, name in
                  enumerate(["🔐・verify", "❓・qotd", "🗝️・hall-of-secrets", "📸・selfies", "🖼️・share-your-pics",
                             "🌗・gm-and-gn-only", "🔢・counting"], start=100)]
    text = _map_text(mod, FakeGuild(everything), member=types.SimpleNamespace(id=5))
    assert "**OTHER**" not in text


async def test_map_unknown_channels_still_show_by_name_not_hidden(mod):
    text = _map_text(mod, FakeGuild([]), member=types.SimpleNamespace(id=5))
    assert "**pillow-talk**" in text


async def test_map_when_permissions_cannot_be_read_shows_the_channel(mod):
    class Odd(FakeChannel):
        def permissions_for(self, who):
            raise RuntimeError("no")
    text = _map_text(mod, FakeGuild([Odd(11, "🧸・cuddle")]), member=types.SimpleNamespace(id=5))
    assert "<#11>" in text


async def test_public_map_never_links_a_channel_everyone_cannot_open(mod):
    guild = FakeGuild([FakeChannel(11, "🧸・cuddle"), FakeChannel(12, "✅・verified-chat", hidden_from={"everyone"})])
    text = _map_text(mod, guild, public=True)
    assert "<#11>" in text and "<#12>" not in text and "**verified-chat**" in text


async def test_published_guide_uses_the_public_map(mod):
    guild = FakeGuild([FakeChannel(12, "✅・verified-chat", hidden_from={"everyone"})])
    cog = make(mod)
    ctx = FakeCtx(guild=guild)
    channel = FakeChannel(77, "server-guide")
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel)
    map_post = channel.sent[-1].kw["embed"].description
    assert "<#12>" not in map_post and "**verified-chat**" in map_post


async def test_button_map_is_filtered_for_the_person_who_picked_it(mod):
    guild = FakeGuild([FakeChannel(11, "🧸・cuddle"), FakeChannel(12, "🔢・counting", hidden_from={5})])
    cog = make(mod)
    view = mod.GuideView(cog, author_id=5, is_mod=False)
    button = next(b for b in view.children if b.key == MAP_KEY)
    edits = []

    async def edit_message(**kw):
        edits.append(kw)
    interaction = types.SimpleNamespace(
        guild=guild, user=types.SimpleNamespace(id=5),
        response=types.SimpleNamespace(edit_message=edit_message))
    await button.callback(interaction)
    text = edits[0]["embed"].description
    assert "<#11>" in text and "<#12>" not in text and "counting" not in text
    assert edits[0]["view"].author_id == 5


async def search(cog, ctx, word):
    await cog.wonderland.subs["search"].callback(cog, ctx, word=word)


async def test_slash_search_reports_hits_and_misses_privately(mod):
    cog = make(mod, commands={"fish", "fishinfo"})
    ctx = FakeCtx(interaction=object())
    await search(cog, ctx, "fish")
    assert "`.fish`" in (await shown(ctx))["embed"].description
    ctx2 = FakeCtx(interaction=object())
    await search(cog, ctx2, "zzzz")
    assert "Nothing found" in (await shown(ctx2))["embed"].description


async def test_prefix_search_answers_at_once_with_no_tap_to_open_stub(mod):
    cog = make(mod, commands={"fish", "fishinfo"})
    ctx = FakeCtx()
    await search(cog, ctx, "fish")
    assert len(ctx.sent) == 1
    sent = ctx.sent[0].kw
    assert "view" not in sent                      # no button to tap
    assert "`.fish`" in sent["embed"].description
    assert sent["delete_after"] == SEARCH_TTL      # it still cleans itself up
    assert ctx.message.deleted


async def test_prefix_search_with_no_match_also_answers_at_once(mod):
    cog = make(mod, commands={"fish"})
    ctx = FakeCtx()
    await search(cog, ctx, "zzzz")
    assert "view" not in ctx.sent[0].kw
    assert "Nothing found" in ctx.sent[0].kw["embed"].description


async def test_prefix_search_survives_missing_manage_messages(mod):
    cog = make(mod, commands={"fish"})
    ctx = FakeCtx()

    async def refuse():
        raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "no")
    ctx.message.delete = refuse
    await search(cog, ctx, "fish")
    assert "`.fish`" in ctx.sent[0].kw["embed"].description


async def test_prefix_search_never_lists_mod_only_matches_in_the_channel_but_tells_a_mod(mod):
    cog = make(mod, mods={5})
    ctx = FakeCtx(author_id=5)
    await search(cog, ctx, "roomperms")
    text = ctx.sent[0].kw["embed"].description
    assert "Nothing found" in text                 # the channel copy is the member-level answer
    assert "Mod-only matches are hidden" in text and "/wonderland search" in text


async def test_prefix_search_gives_a_member_no_mod_note(mod):
    cog = make(mod, mods={5})
    ctx = FakeCtx(author_id=6)
    await search(cog, ctx, "roomperms")
    text = ctx.sent[0].kw["embed"].description
    assert "Nothing found" in text and "Mod-only" not in text


async def test_slash_search_still_shows_a_mod_the_mod_only_matches_privately(mod):
    cog = make(mod, mods={5})
    ctx = FakeCtx(author_id=5, interaction=object())
    await search(cog, ctx, "roomperms")
    sent = (await shown(ctx))
    assert sent["ephemeral"] is True and "roomperms" in sent["embed"].description


# ---------------------------------------------------------------- .wld economy (mods)

class EcoGuild(FakeGuild):
    def __init__(self, members):
        super().__init__([])
        self.members = {m.id: m for m in members}

    def get_member(self, user_id):
        return self.members.get(user_id)


def eco_member(user_id, name, bot=False):
    return types.SimpleNamespace(id=user_id, display_name=name, bot=bot)


def install_bank(monkeypatch, accounts, is_global=False, currency="wondercoin"):
    """A fake redbot bank. Returns a dict recording the guild the report asked the bank for."""
    seen = {}

    async def get_currency_name(guild=None):
        return currency

    async def is_global_():
        return is_global

    async def get_leaderboard(positions=None, guild=None):
        seen["positions"], seen["guild"] = positions, guild
        return accounts
    bank = types.SimpleNamespace(get_currency_name=get_currency_name, is_global=is_global_,
                                 get_leaderboard=get_leaderboard)
    monkeypatch.setattr(sys.modules["redbot.core"], "bank", bank, raising=False)
    return seen


def fields(embed):
    return {f.name: f.value for f in embed.fields}


async def run_economy(cog, guild):
    ctx = FakeCtx(guild=guild)
    await cog.wonderland.subs["economy"].callback(cog, ctx)
    return ctx


async def test_economy_report_counts_members_only_and_saves_one_snapshot(mod, monkeypatch):
    guild = EcoGuild([eco_member(1, "Ann"), eco_member(2, "Bo"), eco_member(3, "BotBob", bot=True)])
    seen = install_bank(monkeypatch, [(1, {"balance": 2_000_000}), ("2", {"balance": 300}),
                                      (3, {"balance": 999_999}), (4, {"balance": 50_000})])
    cog = make(mod)
    ctx = await run_economy(cog, guild)
    sent = ctx.sent[0].kw
    assert sent["ephemeral"] is True
    embed = sent["embed"]
    assert "**2,000,300** wondercoin" in embed.description and "**2** members" in embed.description
    f = fields(embed)
    assert f["Top holders"] == "1. Ann: 2,000,000\n2. Bo: 300"          # the bot is not a holder
    assert "1 accounts of members who left (50,000" in f["Not counted"]
    assert "First snapshot saved" in f["Change over time"]
    assert seen == {"positions": None, "guild": guild}                    # local bank: ask for this guild
    saved = cog.config.data[guild.id]["economy"]["snapshots"]
    assert len(saved) == 1 and saved[0]["total"] == 2_000_300


async def test_a_second_run_the_same_day_does_not_add_a_second_snapshot(mod, monkeypatch):
    guild = EcoGuild([eco_member(1, "Ann")])
    install_bank(monkeypatch, [(1, {"balance": 10})])
    cog = make(mod)
    await run_economy(cog, guild)
    await run_economy(cog, guild)
    assert len(cog.config.data[guild.id]["economy"]["snapshots"]) == 1


async def test_economy_report_shows_the_change_since_an_earlier_snapshot(mod, monkeypatch):
    guild = EcoGuild([eco_member(1, "Ann")])
    install_bank(monkeypatch, [(1, {"balance": 1_500})])
    cog = make(mod)
    cog.config.data[guild.id] = {"economy": {"snapshots": [
        {"date": "2000-01-01", "total": 1_000, "holders": 1, "median": 1_000, "p90": 1_000, "top10_share": 1.0}]}}
    ctx = await run_economy(cog, guild)
    since = fields(ctx.sent[0].kw["embed"])["Since 2000-01-01"]
    assert "Total +500 (+50.0%)" in since
    assert len(cog.config.data[guild.id]["economy"]["snapshots"]) == 2     # the old one is kept


async def test_global_bank_asks_for_everyone_and_filters_members_here(mod, monkeypatch):
    guild = EcoGuild([eco_member(1, "Ann")])
    seen = install_bank(monkeypatch, [(1, {"balance": 10}), (99, {"balance": 5})], is_global=True)
    ctx = await run_economy(make(mod), guild)
    assert seen["guild"] is None
    assert "1 accounts of members who left (5" in fields(ctx.sent[0].kw["embed"])["Not counted"]


async def test_economy_report_when_nobody_holds_anything(mod, monkeypatch):
    guild = EcoGuild([eco_member(1, "Ann")])
    install_bank(monkeypatch, [(1, {"balance": 0})])
    ctx = await run_economy(make(mod), guild)
    assert "Nobody holds any wondercoin" in ctx.sent[0].kw["embed"].description


async def test_the_economy_report_is_on_the_mod_page_only(mod):
    cog = make(mod, mods={5})
    mod_text = cog.render("mod", FakeGuild([]), True).description
    member_text = cog.render("start", FakeGuild([]), False).description
    assert ".wld economy" in mod_text and ".wld economy" not in member_text


async def test_stats_uses_the_mystats_cog_and_replies_privately(mod):
    class FakeStats:
        async def build_embed(self, ctx, member):
            return discord.Embed(title=f"stats for {member.id}")
    cog = make(mod, cogs={"MyStats": FakeStats()})
    ctx = FakeCtx(author_id=5)
    await cog.wonderland.subs["stats"].callback(cog, ctx, None)
    assert "embed" not in ctx.sent[0].kw                       # the stub carries no stats
    assert (await shown(ctx, 5))["embed"].title == "stats for 5"
    assert (await shown(ctx, 8))["embed"].title == "stats for 8"   # whoever taps sees their own


async def test_stats_defer_is_ephemeral_so_the_slash_reply_is_never_public(mod):
    class FakeStats:
        async def build_embed(self, ctx, member):
            return discord.Embed(title="x")
    cog = make(mod, cogs={"MyStats": FakeStats()})
    ctx = FakeCtx(interaction=object())
    await cog.wonderland.subs["stats"].callback(cog, ctx, None)
    assert ctx.deferred_ephemeral is True
    assert ctx.sent[0].kw["ephemeral"] is True


async def test_stats_without_mystats_says_so(mod):
    cog = make(mod)
    ctx = FakeCtx()
    await cog.wonderland.subs["stats"].callback(cog, ctx, None)
    assert "aren't available" in ctx.sent[0].args[0]


async def test_buttons_only_answer_to_the_person_who_asked(mod):
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


async def test_publish_map_only_posts_one_message_and_keeps_other_page_ids(mod):
    cog = make(mod)
    ctx = FakeCtx()
    channel = FakeChannel(77, "server-guide")
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel, "all")
    total = len(channel.sent)
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel, "map")
    assert len(channel.sent) == total                 # the map was edited in place, nothing new
    assert "Published 1 message " in ctx.sent[-1].args[0]
    stored = (await cog.config.guild(ctx.guild).publish())["messages"]
    assert len(stored) == total                       # other pages still tracked
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel, "all")
    assert len(channel.sent) == total                 # a later full run edits, never duplicates


async def test_publish_map_only_into_a_fresh_channel_posts_just_the_map(mod):
    cog = make(mod)
    ctx = FakeCtx()
    channel = FakeChannel(78, "server-guide")
    await cog.wonderland.subs["publish"].callback(cog, ctx, channel, "map")
    assert len(channel.sent) == 1
    assert "Wonderland Channel Guide" in channel.sent[0].kw["embed"].title


async def test_version_probe(mod):
    cog = make(mod)
    ctx = FakeCtx()
    await cog.wonderland.subs["version"].callback(cog, ctx)
    assert ctx.sent[0].args[0].startswith("Wonderland guide v")


async def test_buttons_fit_discord_limits_and_highlight_the_current_topic(mod):
    cog = make(mod)
    for is_mod in (False, True):
        view = mod.GuideView(cog, 5, is_mod, "casino")
        assert len(view.children) <= 10 and all(b.row in (0, 1) for b in view.children)
        assert sum(1 for b in view.children if b.row == 0) <= 5
        current = [b for b in view.children if b.style == discord.ButtonStyle.primary]
        assert [b.key for b in current] == ["casino"]
        assert all(b.label for b in view.children)


async def test_start_page_links_the_first_three_channels(mod):
    guild = FakeGuild([FakeChannel(1, "💌・intro"), FakeChannel(2, "🌈・role-select"), FakeChannel(3, "🧸・cuddle")])
    cog = make(mod, commands={"payday"})
    text = cog.render("start", guild, False, member=types.SimpleNamespace(id=5)).description
    assert "<#1>" in text and "<#2>" in text and "<#3>" in text
    assert text.index("<#1>") < text.index("<#2>") < text.index("<#3>")
    assert "`.payday`" in text and ".birthday" not in text and "{#" not in text


async def test_start_page_names_a_channel_it_cannot_link(mod):
    cog = make(mod)
    text = cog.render("start", FakeGuild([]), False, member=types.SimpleNamespace(id=5)).description
    assert "**#intro**" in text and "{#" not in text


async def test_casino_page_points_to_the_hub_button(mod):
    guild = FakeGuild([FakeChannel(9, "🎰・wondercasino")])
    cog = make(mod, commands={"gamble"})
    text = cog.render("casino", guild, False, member=types.SimpleNamespace(id=5)).description
    assert "<#9>" in text and "Open a Table" in text
    assert "team up" not in text


async def test_published_start_page_is_public_and_first(mod):
    guild = FakeGuild([FakeChannel(1, "💌・intro", hidden_from={"everyone"})])
    cog = make(mod)
    channel = FakeChannel(77, "server-guide")
    await cog.wonderland.subs["publish"].callback(cog, FakeCtx(guild=guild), channel)
    first = channel.sent[0].kw["embed"]
    assert "Start here" in first.title and "<#1>" not in first.description and "**#intro**" in first.description


async def test_retired_commands_never_appear_even_when_live(mod):
    live = {"lottery", "selfrole", "pockypolice drop", "pockypolice status", "pockypolice calendar", "pockypolice pto"}
    cog = make(mod, commands=live, mods={9})
    for key in [p.key for p in mod.PAGES]:
        text = cog.render(key, FakeGuild([]), True, member=types.SimpleNamespace(id=9)).description
        assert ".lottery" not in text and ".selfrole" not in text and ".pp " not in text
