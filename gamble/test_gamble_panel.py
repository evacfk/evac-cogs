"""Settings for photodrop: the `_apply_*` methods and the `.pp set` panel (real Red + discord.py)."""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)
try:
    from redbot.core import commands as _red_commands
    _real_red = hasattr(_red_commands.Cog, "listener")
except ImportError:
    _real_red = False
if not _real_red:
    pytest.skip("real Red not installed", allow_module_level=True)

import copy

from gamble import panel as panel_mod
from gamble.gamblethreads import COG_VERSION, GambleThreads
from gamble.settings_panel import PanelError, SettingsPanel


class Val:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.data)
        return _get().__await__()

    async def set(self, value):
        self.data = copy.deepcopy(value)


class Chan(SimpleNamespace):
    @property
    def mention(self):
        return f"<#{self.id}>"


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.chans = {10: Chan(id=10), 11: Chan(id=11)}

    def get_channel(self, cid):
        return self.chans.get(cid)


def make():
    cog = object.__new__(GambleThreads)
    group = SimpleNamespace(bot_channel_id=Val(None), post_channel_id=Val(None), timeout_minutes=Val(10),
                            shared_timeout_minutes=Val(15))
    cog.config = SimpleNamespace(guild=lambda g: group)
    return cog, group, Guild()


class Resp:
    def __init__(self):
        self.done, self.edited, self.modal = False, [], None

    def is_done(self):
        return self.done

    async def defer(self):
        self.done = True

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)

    async def send_message(self, *a, **k):
        self.done = True

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal


class Click:
    def __init__(self, user, values=None):
        self.user, self.data, self.response = user, {"values": values or []}, Resp()
        self.originals = []
        self.followup = SimpleNamespace(send=self._noop)

    async def _noop(self, *a, **k):
        pass

    async def edit_original_response(self, **kw):
        self.originals.append(kw)


def admin():
    return SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=False, manage_guild=True))


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


async def test_channel_and_timeouts_are_validated():
    cog, g, guild = make()
    with pytest.raises(PanelError):
        await cog._apply_channel(guild, None, None)
    await cog._apply_channel(guild, None, guild.chans[10])
    assert g.bot_channel_id.data == 10
    for bad in ({"personal": "0", "shared": "5"}, {"personal": "5", "shared": "0"}, {"personal": "x", "shared": "5"},
                {"personal": "5"}, {"personal": "2.5", "shared": "5"}):
        with pytest.raises(PanelError):
            await cog._apply_timeouts(guild, None, bad)
    assert (g.timeout_minutes.data, g.shared_timeout_minutes.data) == (10, 15)
    await cog._apply_timeouts(guild, None, {"personal": "20", "shared": "30"})
    assert (g.timeout_minutes.data, g.shared_timeout_minutes.data) == (20, 30)


async def test_post_hub_relays_the_cogs_error_or_success():
    cog, g, guild = make()
    results = ["No hub channel is set", None]

    async def refresh(gd):
        return results.pop(0)
    cog._refresh_hub_message = refresh
    with pytest.raises(PanelError, match="No hub channel"):
        await cog._apply_post_hub(guild, None)
    assert "posted" in await cog._apply_post_hub(guild, None)


async def test_panel_widgets_save_the_timeouts_and_gate_by_permission():
    cog, g, guild = make()
    a = admin()
    p = SettingsPanel(cog, guild, a, panel_mod.build_specs(), title="Wondercasino", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    for spec in p.tab_specs():
        await p._on_setting(Click(a, [spec.key]))
        assert len(p.to_components()) <= 5 and p.build_embed().description
    await p._on_setting(Click(a, ["timeouts"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert (modal.boxes["personal"].default, modal.boxes["shared"].default) == ("10", "15")
    modal.boxes["personal"]._value = "12"
    done = Click(a)
    await modal.on_submit(done)
    assert "\u2705" in note(done) and g.timeout_minutes.data == 12
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False


async def test_group_opens_the_panel_for_admins_and_help_for_everyone_else():
    cog, g, guild = make()
    sent = {}

    class Ctx:
        def __init__(self, member):
            self.guild, self.author, self.command = guild, member, GambleThreads.gambleset

        async def send(self, content=None, **kw):
            sent.update(kw)
            return SimpleNamespace(id=1)

        async def send_help(self, command=None):
            sent["help"] = True
    fn = GambleThreads.gambleset.callback
    await fn(cog, Ctx(admin()))
    assert isinstance(sent["view"], SettingsPanel)
    sent.clear()
    plain = SimpleNamespace(id=3, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False))
    await fn(cog, Ctx(plain))
    assert sent == {"help": True}


def test_removed_setters_are_gone_and_the_probe_exists():
    names = {c.name for c in GambleThreads.gambleset.commands}
    assert names == {"version", "settings"}
    assert COG_VERSION == "1.3.0"
    assert {"setup", "listgames", "lockdown"} <= {c.name for c in GambleThreads.gamblehub.commands}


# ---------------------------------------------------------------------------
# "post" mode: run a hub game's command in the configured reply channel
# ---------------------------------------------------------------------------

POST_CHANNEL_ID = 11


class Perms(SimpleNamespace):
    pass


class PostChannel(Chan):
    """A reply channel; `can_send` / `can_view` flip what permissions_for says."""

    can_send = True  # the bot
    can_view = True  # a member
    member_can_send = True  # a member (False = muted or timed out: sees the channel, can't post)

    def permissions_for(self, who):
        if who is ME:
            return Perms(send_messages=self.can_send, view_channel=True)
        return Perms(send_messages=self.member_can_send, view_channel=self.can_view)


ME = SimpleNamespace(id=999)


class PostGuild:
    id, name, me = 1, "Wonderland", ME

    def __init__(self):
        self.chans = {10: Chan(id=10), POST_CHANNEL_ID: PostChannel(id=POST_CHANNEL_ID)}

    def get_channel(self, cid):
        return self.chans.get(cid)


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


class PostResponse:
    def __init__(self):
        self.deferred = []

    def is_done(self):
        return bool(self.deferred)

    async def defer(self, **kw):
        self.deferred.append(kw)

    async def send_message(self, *a, **k):
        raise AssertionError("post mode must not answer the interaction directly")


class PostInteraction:
    def __init__(self, guild, user, message):
        self.guild, self.user, self.message = guild, user, message
        self.response, self.followup = PostResponse(), Followup()
        self.channel = guild.get_channel(10)
        self.channel_id = 10


class GamesVal:
    """A Config-like value that supports both `await x()` and `async with x() as d`."""

    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.data)
        return _get().__await__()

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *exc):
        return False


def make_post(post_channel=POST_CHANNEL_ID, ctx_valid=True):
    cog = object.__new__(GambleThreads)
    games = {"dailycard": {"emoji": "\U0001f3b4", "label": "Daily Cards", "command": "card daily", "mode": "post"}}
    group = SimpleNamespace(bot_channel_id=Val(10), post_channel_id=Val(post_channel), games=GamesVal(games))
    cog.config = SimpleNamespace(guild=lambda g: group)
    calls = SimpleNamespace(invoked=[], contexts=[], while_running=None)  # while_running: what the command "does"
    command = SimpleNamespace(qualified_name="card daily")

    async def get_prefix(_channel):
        return ["."]

    async def get_context(message):
        ctx = SimpleNamespace(message=message, valid=ctx_valid)
        calls.contexts.append(ctx)
        return ctx

    async def invoke(ctx):
        calls.invoked.append(ctx)
        if calls.while_running is not None:
            await calls.while_running(ctx)

    cog.bot = SimpleNamespace(get_prefix=get_prefix, get_context=get_context, invoke=invoke,
                              get_command=lambda name: command if name == "card daily" else None)
    cog._bg_tasks = set()
    return cog, group, calls, command


def hub_click(guild):
    member = SimpleNamespace(id=42, display_name="Clicker")
    hub_message = SimpleNamespace(id=1, author=ME, channel=guild.get_channel(10), guild=guild, content="", reference=None)
    return member, hub_message, PostInteraction(guild, member, hub_message)


async def test_post_mode_runs_the_command_in_the_reply_channel_as_the_clicker():
    cog, group, calls, command = make_post()
    guild = PostGuild()
    member, hub_message, interaction = hub_click(guild)
    await cog._invoke_post(interaction, member, command)
    assert len(calls.invoked) == 1
    fake = calls.invoked[0].message
    assert fake.channel is guild.chans[POST_CHANNEL_ID]
    assert fake.author is member
    assert fake.content == ".card daily"
    assert fake.id != hub_message.id  # a fresh snowflake, so created_at is "now"
    # the hub message used as a template is untouched
    assert hub_message.author is ME and hub_message.channel is guild.chans[10] and hub_message.content == ""
    assert interaction.response.deferred == [{"thinking": False}]
    (text, kw), = interaction.followup.sent
    assert "<#11>" in text and kw == {"ephemeral": True}


async def test_a_command_can_answer_just_the_clicker_and_then_no_pointer_note_is_sent():
    cog, group, calls, command = make_post()
    guild = PostGuild()
    member, _hub, interaction = hub_click(guild)
    view = object()

    async def command_body(ctx):
        # what cardcollect does for "already pulled today": a private reply carrying the pack button
        sent = await ctx.reply_privately("Already pulled.", view=view, delete_after=5, reference=object())
        assert sent is None  # the fake followup returns nothing; the real one returns the message

    calls.while_running = command_body
    await cog._invoke_post(interaction, member, command)
    (text, kw), = interaction.followup.sent  # exactly one thing was said, and it was private
    assert text == "Already pulled." and kw == {"view": view, "ephemeral": True}  # unsupported kwargs dropped
    assert not any("head to" in t for t, _k in interaction.followup.sent)


async def test_when_the_command_posts_publicly_the_clicker_still_gets_the_pointer_note():
    cog, group, calls, command = make_post()
    guild = PostGuild()
    member, _hub, interaction = hub_click(guild)
    calls.while_running = None  # the command just ctx.send()s to the channel; it never calls reply_privately
    await cog._invoke_post(interaction, member, command)
    (text, kw), = interaction.followup.sent
    assert "<#11>" in text and kw == {"ephemeral": True}
    assert callable(calls.invoked[0].reply_privately)  # offered to every post-mode command


async def test_post_mode_refuses_cleanly_without_running_anything():
    guild = PostGuild()
    # no reply channel set
    cog, group, calls, command = make_post(post_channel=None)
    member, _hub, interaction = hub_click(guild)
    await cog._invoke_post(interaction, member, command)
    assert not calls.invoked and "No reply channel" in interaction.followup.sent[0][0]
    # channel was deleted
    cog, group, calls, command = make_post(post_channel=555)
    member, _hub, interaction = hub_click(guild)
    await cog._invoke_post(interaction, member, command)
    assert not calls.invoked and "No reply channel" in interaction.followup.sent[0][0]
    # bot can't send there
    cog, group, calls, command = make_post()
    guild = PostGuild()
    guild.chans[POST_CHANNEL_ID].can_send = False
    member, _hub, interaction = hub_click(guild)
    await cog._invoke_post(interaction, member, command)
    assert not calls.invoked and "can't post" in interaction.followup.sent[0][0]
    # clicker can't see it
    cog, group, calls, command = make_post()
    guild = PostGuild()
    guild.chans[POST_CHANNEL_ID].can_view = False
    member, _hub, interaction = hub_click(guild)
    await cog._invoke_post(interaction, member, command)
    assert not calls.invoked and "can't post" in interaction.followup.sent[0][0]
    # clicker can see it but is muted / timed out: the bot must not post for them
    cog, group, calls, command = make_post()
    guild = PostGuild()
    guild.chans[POST_CHANNEL_ID].member_can_send = False
    member, _hub, interaction = hub_click(guild)
    await cog._invoke_post(interaction, member, command)
    assert not calls.invoked and "can't post" in interaction.followup.sent[0][0]
    assert interaction.followup.sent[0][1] == {"ephemeral": True}
    # the command doesn't resolve to a valid invocation
    cog, group, calls, command = make_post(ctx_valid=False)
    guild = PostGuild()
    member, _hub, interaction = hub_click(guild)
    await cog._invoke_post(interaction, member, command)
    assert not calls.invoked and "Couldn't run" in interaction.followup.sent[0][0]


async def test_hub_click_routes_post_entries_to_post_mode_only():
    cog, group, calls, command = make_post()
    guild = PostGuild()
    member, _hub, interaction = hub_click(guild)
    routed, reset = [], []

    async def fake_post(i, m, c):
        routed.append((i, m, c))

    async def no_thread(*a, **k):
        raise AssertionError("post mode must never create a thread")
    cog._invoke_post = fake_post
    cog._get_or_create_personal_thread = no_thread
    cog._get_or_create_shared_thread = no_thread
    cog._schedule_select_reset = lambda g: reset.append(g)
    await cog.handle_hub_click(interaction, "dailycard", from_select=True)
    assert routed == [(interaction, member, command)] and reset == [guild]
    assert interaction.response.deferred == []  # _invoke_post owns the defer


async def test_addgame_accepts_post_only_once_a_reply_channel_is_set():
    sent = []

    class Ctx:
        def __init__(self, guild):
            self.guild = guild

        async def send(self, content=None, **kw):
            sent.append(content)

    async def refresh(_guild):
        return None
    fn = GambleThreads.gamblehub_addgame.callback
    guild = PostGuild()
    cog, group, calls, command = make_post(post_channel=None)
    cog._refresh_hub_message = refresh
    group.games.data.clear()
    await fn(cog, Ctx(guild), "dailycard", "\U0001f3b4", "post", "card daily", label="Daily Cards")
    assert "none is set" in sent[-1] and group.games.data == {}
    await group.post_channel_id.set(POST_CHANNEL_ID)
    await fn(cog, Ctx(guild), "dailycard", "\U0001f3b4", "post", "card daily", label="Daily Cards")
    assert group.games.data["dailycard"] == {"emoji": "\U0001f3b4", "label": "Daily Cards", "command": "card daily",
                                             "mode": "post"}
    assert "Added" in sent[-1] and "post mode" in sent[-1]
    await fn(cog, Ctx(guild), "x", "\U0001f3b4", "bogus", "card daily", label="X")
    assert "`post`" in sent[-1] and "x" not in group.games.data


async def test_reply_channel_setting_is_validated_and_shown_in_the_panel():
    cog, g, guild = make()
    with pytest.raises(PanelError):
        await cog._apply_post_channel(guild, None, None)
    assert "<#11>" in await cog._apply_post_channel(guild, None, guild.chans[11])
    assert g.post_channel_id.data == 11
    keys = [s.key for s in panel_mod.build_specs()]
    assert keys.count("post_channel") == 1 and keys.index("channel") < keys.index("hub_menu")


async def test_gambleset_settings_shows_both_channels():
    cog, g, guild = make()
    sent = []

    class Ctx:
        def __init__(self):
            self.guild = guild

        async def send(self, content=None, **kw):
            sent.append(content)
    fn = GambleThreads.gambleset_settings.callback
    await fn(cog, Ctx())
    assert "Hub channel: not set" in sent[-1] and "Command reply channel: not set" in sent[-1]
    await cog._apply_channel(guild, None, guild.chans[10])
    await cog._apply_post_channel(guild, None, guild.chans[11])
    await fn(cog, Ctx())
    assert "Hub channel: <#10>" in sent[-1] and "Command reply channel: <#11>" in sent[-1]


# ---------------------------------------------------------------------------
# Close table button (persistent, on the personal table's welcome message)
# ---------------------------------------------------------------------------

class _Map:
    """Config group stand-in usable both as `await group()` and `async with group() as d`."""

    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.data)
        return _get().__await__()

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *exc):
        return False


class _Thread:
    def __init__(self, tid, manage=False):
        self.id, self.deleted, self.sent, self._manage = tid, [], [], manage

    def permissions_for(self, user):
        return SimpleNamespace(manage_threads=self._manage and getattr(user, "is_mod", False))

    async def send(self, text, **kw):
        self.sent.append(text)

    async def delete(self, reason=None):
        self.deleted.append(reason)


class _CloseClick:
    def __init__(self, user, channel, guild):
        self.user, self.channel, self.guild = user, channel, guild
        self.replies, self.deferred = [], False
        self.response = SimpleNamespace(send_message=self._reply, defer=self._defer)

    async def _reply(self, text, **kw):
        self.replies.append(text)

    async def _defer(self, **kw):
        self.deferred = True


def _close_world(owner_id=5, thread_id=70):
    cog = object.__new__(GambleThreads)
    active = _Map({str(owner_id): {"thread_id": thread_id, "last_activity": "x"}})
    cog.config = SimpleNamespace(guild=lambda g: SimpleNamespace(active_threads=active))
    return cog, active, _Thread(thread_id, manage=True), SimpleNamespace(id=1)


async def test_close_button_lets_the_owner_close_their_own_table():
    cog, active, thread, guild = _close_world()
    click = _CloseClick(SimpleNamespace(id=5), thread, guild)
    await cog.handle_close_click(click)
    assert click.deferred and thread.deleted and active.data == {}


async def test_close_button_refuses_other_members_and_keeps_the_table():
    cog, active, thread, guild = _close_world()
    click = _CloseClick(SimpleNamespace(id=6), thread, guild)
    await cog.handle_close_click(click)
    assert click.replies == ["Only the table's owner can close it."]
    assert not thread.deleted and "5" in active.data


async def test_close_button_lets_a_mod_with_manage_threads_close_it():
    cog, active, thread, guild = _close_world()
    click = _CloseClick(SimpleNamespace(id=9, is_mod=True), thread, guild)
    await cog.handle_close_click(click)
    assert thread.deleted and active.data == {}


async def test_close_button_on_an_already_closed_table_just_says_so():
    cog, active, thread, guild = _close_world()
    active.data.clear()
    click = _CloseClick(SimpleNamespace(id=5), thread, guild)
    await cog.handle_close_click(click)
    assert "already closed" in click.replies[0] and not thread.deleted


def test_close_view_is_persistent_with_a_fixed_custom_id():
    from gamble.gamblethreads import CloseTableView
    view = CloseTableView(object())
    assert view.timeout is None
    assert [c.custom_id for c in view.children] == ["gamblehub:close"]
