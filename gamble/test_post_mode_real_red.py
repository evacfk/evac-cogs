"""Hub `post` mode against real discord.py objects and a real Red bot.

test_gamble_panel.py covers post mode with fakes. This file runs the same code path (`_invoke_post`) with a real
discord.Message copied and re-aimed, a real Guild/Member/TextChannel (so permissions_for, including a timed-out
member, is discord.py's own), and Red's own get_prefix / get_context / invoke with a real `card daily` command
registered, so the pieces the fakes can't prove (the Message copy trick, Red's before-invoke hooks and checks,
ctx.reply_privately on a real Context) are exercised for real. Only Discord's HTTP send is replaced.
"""
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

import discord.ext.commands
from redbot.core import commands

from gamble.gamblethreads import GambleThreads

GUILD_ID, HUB_ID, REPLY_ID, USER_ID, BOT_ID = 1, 10, 11, 42, 999
EVERYONE = 0x400 | 0x800 | 0x10000 | 0x40000  # view channel, send messages, read history, use external emoji


def _user(uid, name, bot=False):
    return {"id": str(uid), "username": name, "discriminator": "0", "avatar": None, "bot": bot, "global_name": name}


def _channel(cid, name):
    return {"id": str(cid), "name": name, "type": 0, "position": 0, "permission_overwrites": [],
            "guild_id": str(GUILD_ID), "nsfw": False, "topic": None, "last_message_id": None, "rate_limit_per_user": 0}


def _member(uid, name, bot=False, timed_out=False):
    data = {"user": _user(uid, name, bot), "roles": [], "joined_at": "2026-01-01T00:00:00+00:00", "deaf": False,
            "mute": False, "flags": 0}
    if timed_out:
        data["communication_disabled_until"] = "2999-01-01T00:00:00+00:00"
    return data


def make_world(red, timed_out=False):
    """A real guild with #wondercasino (the hub), #bot-commands, the clicker and the bot, plus the hub's own message."""
    state = red._connection
    payload = {
        "id": str(GUILD_ID), "name": "Wonderland", "owner_id": "5", "afk_timeout": 0, "verification_level": 0,
        "default_message_notifications": 0, "explicit_content_filter": 0, "mfa_level": 0, "system_channel_flags": 0,
        "premium_tier": 0, "nsfw_level": 0, "preferred_locale": "en-US", "features": [], "emojis": [], "stickers": [],
        "roles": [{"id": str(GUILD_ID), "name": "@everyone", "permissions": str(EVERYONE), "position": 0, "color": 0,
                   "hoist": False, "managed": False, "mentionable": False, "flags": 0}],
        "channels": [_channel(HUB_ID, "wondercasino"), _channel(REPLY_ID, "bot-commands")],
        "members": [_member(USER_ID, "clicker", timed_out=timed_out), _member(BOT_ID, "Hana", bot=True)],
        "threads": [], "voice_states": [], "presences": [], "member_count": 2,
    }
    guild = discord.Guild(data=payload, state=state)
    state._add_guild(guild)
    hub = guild.get_channel(HUB_ID)
    hub_message = discord.Message(state=state, channel=hub, data={
        "id": "5000", "channel_id": str(HUB_ID), "author": _user(BOT_ID, "Hana", bot=True), "content": "",
        "timestamp": "2026-10-01T00:00:00+00:00", "edited_timestamp": None, "tts": False, "mention_everyone": False,
        "mentions": [], "mention_roles": [], "attachments": [], "embeds": [], "pinned": False, "type": 0})
    return guild, guild.get_member(USER_ID), hub_message


class Click:
    """The slice of discord.Interaction that _invoke_post uses."""

    def __init__(self, guild, member, message):
        self.guild, self.user, self.message, self.channel = guild, member, message, message.channel
        self.sent = []
        self.response = SimpleNamespace(defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _defer(self, **kwargs):
        pass

    async def _send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class Val:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return self.value
        return _get().__await__()


@pytest.fixture
async def world(red, monkeypatch):
    posted = []

    async def fake_send(self, content=None, **kwargs):  # the real one would call Discord's HTTP API
        posted.append((getattr(getattr(self, "channel", self), "id"), content, kwargs))
        return SimpleNamespace(id=1)

    monkeypatch.setattr(discord.abc.Messageable, "send", fake_send)
    monkeypatch.setattr(discord.ext.commands.Context, "send", fake_send, raising=False)
    monkeypatch.setattr(commands.Context, "send", fake_send, raising=False)
    await red.set_prefixes(prefixes=["."])  # like the real bot
    red._red_ready.set()  # a bot that never connected must still look "ready" to Red's before-invoke hook
    red._connection.user = discord.ClientUser(state=red._connection, data=_user(BOT_ID, "Hana", bot=True))
    seen = {}

    class CardStub(commands.Cog):
        """Stands in for cardcollect's `.card daily`: records the Context it was run with."""

        @commands.group(name="card", invoke_without_command=True)
        async def card(self, ctx):
            pass

        @card.command(name="daily")
        @commands.guild_only()
        async def daily(self, ctx):
            seen["ctx"] = ctx
            seen["has_hook"] = callable(getattr(ctx, "reply_privately", None))
            if seen.get("go_private"):
                await ctx.reply_privately("Already pulled.")
            else:
                await ctx.send(f"{ctx.author.mention} pulled a card")

    await red.add_cog(CardStub())
    cog = object.__new__(GambleThreads)
    cog.bot = red
    cog._bg_tasks = set()
    cog.config = SimpleNamespace(guild=lambda g: SimpleNamespace(post_channel_id=Val(REPLY_ID)))
    return SimpleNamespace(cog=cog, red=red, posted=posted, seen=seen)


async def test_a_real_message_copy_runs_a_real_red_command_as_the_clicker_in_the_reply_channel(world):
    guild, member, hub_message = make_world(world.red)
    click = Click(guild, member, hub_message)
    command = world.red.get_command("card daily")
    assert command is not None
    await world.cog._invoke_post(click, member, command)
    ctx = world.seen["ctx"]
    assert world.seen["has_hook"] is True
    assert ctx.author.id == USER_ID and ctx.channel.id == REPLY_ID and ctx.guild.id == GUILD_ID
    assert ctx.message.id != hub_message.id and ctx.message.content == ".card daily"
    assert len(world.posted) == 1 and world.posted[0][0] == REPLY_ID  # public post: #bot-commands, never the hub
    assert f"<@{USER_ID}>" in world.posted[0][1] and "pulled a card" in world.posted[0][1]
    assert hub_message.channel.id == HUB_ID and hub_message.author.id == BOT_ID  # the template message is untouched
    (text, kwargs), = click.sent
    assert "<#11>" in text and kwargs == {"ephemeral": True}


async def test_a_private_reply_from_the_command_posts_nothing_publicly(world):
    guild, member, hub_message = make_world(world.red)
    world.seen["go_private"] = True
    click = Click(guild, member, hub_message)
    await world.cog._invoke_post(click, member, world.red.get_command("card daily"))
    assert world.posted == []
    (text, kwargs), = click.sent  # exactly one thing was said: the private reply, with no "head to" note after it
    assert text == "Already pulled." and kwargs["ephemeral"] is True


async def test_a_timed_out_member_is_refused_before_anything_runs(world):
    guild, member, hub_message = make_world(world.red, timed_out=True)
    assert member.is_timed_out()  # discord.py's own permissions_for strips send_messages for them
    click = Click(guild, member, hub_message)
    await world.cog._invoke_post(click, member, world.red.get_command("card daily"))
    assert "ctx" not in world.seen and world.posted == []
    (text, kwargs), = click.sent
    assert "can't post" in text and kwargs == {"ephemeral": True}
