"""`.wonderland` / `.wld`: the member-facing guidebook.

Read-only. A curated list of pages (content.py); each command line is shown only while that command
exists on the live bot. Replies are private: ephemeral for the slash version, and for the prefix
version the reply and the invoking message clean themselves up so nothing floods the channel.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

import discord
from redbot.core import Config, commands

from . import embeds, engine
from .constants import (
    COG_VERSION, CONFIG_ID, FOOTER, PUBLISH_FOOTER, STATS_COOLDOWN, STUB_TTL, VIEW_TIMEOUT,
)
from .content import HOME_KEY, MAP_KEY, PAGES

log = logging.getLogger("red.evac-cogs.wonderland")

Topic = Literal["start", "economy", "casino", "games", "profile", "community", "music"]


class TopicButton(discord.ui.Button):
    def __init__(self, cog: "Wonderland", key: str, label: str, emoji: str, row: int, current: bool):
        super().__init__(label=label, emoji=emoji, row=row,
                         style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary)
        self.cog = cog
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        embed = self.cog.render(self.key, interaction.guild, view.is_mod, member=interaction.user)
        new_view = GuideView(self.cog, view.author_id, view.is_mod, self.key)
        new_view.message = view.message
        view.stop()
        await interaction.response.edit_message(embed=embed, view=new_view)


class GuideView(discord.ui.View):
    """The topic buttons (two rows). Only the person who asked can use them."""

    def __init__(self, cog: "Wonderland", author_id: int, is_mod: bool, current: str = HOME_KEY):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.author_id = author_id
        self.is_mod = is_mod
        self.message: Optional[discord.Message] = None
        topics = [(HOME_KEY, "Home", "🐇")]
        topics += [(p.key, p.title.split(" &")[0], p.emoji) for p in engine.visible_pages(is_mod) if not p.mod_only]
        topics.append((MAP_KEY, "Channels", "🗺️"))
        if is_mod:
            mod = engine.get_page("mod", True)
            topics.append((mod.key, "Mod", mod.emoji))
        for index, (key, label, emoji) in enumerate(topics):
            self.add_item(TopicButton(cog, key, label, emoji, index // 5, key == current))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Run `.wld` to open your own copy.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message is None:
            return
        try:
            await self.message.delete()
        except Exception:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass


class OpenView(discord.ui.View):
    """The one-line public stub a `.wld` prefix command leaves in the channel.

    Prefix commands can't reply privately, so the real guide only appears when someone taps the
    button, and then as an ephemeral message that only that person sees (built for them: their
    visible channels, their mod status). Anyone can tap it and gets their own copy.
    """

    def __init__(self, make):
        super().__init__(timeout=STUB_TTL)
        self.make = make
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Open it (only you will see it)", emoji="🐇", style=discord.ButtonStyle.primary)
    async def open_button(self, interaction: discord.Interaction, button):
        await interaction.response.defer(ephemeral=True)
        try:
            embed, view = await self.make(interaction.user)
        except Exception:
            log.exception("wonderland: building the guide failed")
            await interaction.followup.send("Something went wrong. Try `/wonderland`.", ephemeral=True)
            return
        kwargs = {"embed": embed, "ephemeral": True, "wait": True}
        if view is not None:
            kwargs["view"] = view
        message = await interaction.followup.send(**kwargs)
        if view is not None:
            view.message = message

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass


class Wonderland(commands.Cog):
    """A compact guidebook to everything Wonderland's bots can do."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_ID, force_registration=True)
        self.config.register_guild(publish={"channel_id": None, "messages": {}})

    async def red_delete_data_for_user(self, **kwargs):
        return  # nothing stored per user

    # ------------------------------------------------------------------ helpers

    def _exists(self, name: str) -> bool:
        try:
            cmd = self.bot.get_command(name)
        except Exception:
            return False
        return cmd is not None and getattr(cmd, "enabled", True)

    async def _is_mod(self, member) -> bool:
        try:
            return bool(await self.bot.is_mod(member))
        except Exception:
            return False

    def _resolver(self, guild: Optional[discord.Guild], member=None, public: bool = False):
        """channel -> mention / None (name only) / HIDDEN.

        member: what that member can open. A channel they can't open vanishes, unless it has an
        `unlock` hint (verified-chat, pillow-talk): then it stays as a bold name plus the hint.
        public: the copy everybody reads. Channels with public_link=False, and any @everyone can't
        open, are a bold name only, never a link that would show "no access".
        """
        channels = list(getattr(guild, "channels", [])) if guild else []
        by_id = {c.id: c for c in channels}
        names = {c.id: c.name for c in channels}
        # Voice channels and forums are included on purpose: Room Creator is a voice channel.

        def can_view(channel_id, who) -> Optional[bool]:
            try:
                return bool(by_id[channel_id].permissions_for(who).view_channel)
            except Exception:
                return None  # can't tell

        def resolve(ch):
            channel_id = engine.match_channel(ch.slug, names)
            if channel_id is None:
                return None
            if public:
                if not ch.public_link or can_view(channel_id, guild.default_role) is False:
                    return None
            elif member is not None and can_view(channel_id, member) is False:
                return None if ch.unlock else engine.HIDDEN
            return f"<#{channel_id}>"

        return resolve

    def render(self, key: str, guild: Optional[discord.Guild], is_mod: bool, footer: Optional[str] = None, member=None,
               public: bool = False):
        if key == HOME_KEY:
            return embeds.home_embed(engine.visible_pages(is_mod))
        if key == MAP_KEY:
            resolver = self._resolver(guild, member, public)
            return embeds.map_embed(resolver, footer) if footer else embeds.map_embed(resolver)
        page = engine.get_page(key, is_mod)
        if page is None:
            return embeds.home_embed(engine.visible_pages(is_mod))
        lines = engine.page_lines(page, self._exists)
        resolver = self._resolver(guild, member, public)
        return embeds.page_embed(page, lines, footer or FOOTER, resolver)

    async def _deliver(self, ctx: commands.Context, make):
        """Show `make(user) -> (embed, view or None)` to the person who asked, and only them.

        Slash: an ephemeral reply, straight away. Prefix: a one-line stub with a button (see
        OpenView), because a prefix reply is visible to the whole channel."""
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)  # also keeps slow builders (stats) inside Discord's 3s window
            embed, view = await make(ctx.author)
            kwargs = {"embed": embed, "ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
            if view is not None:
                kwargs["view"] = view
            message = await ctx.send(**kwargs)
            if view is not None:
                view.message = message
            return
        stub = OpenView(make)
        stub.message = await ctx.send(
            "🐇 **Wonderland guide**: tap the button. Only you will see it.",
            view=stub, delete_after=STUB_TTL, allowed_mentions=discord.AllowedMentions.none())
        try:
            await ctx.message.delete()
        except Exception:
            pass  # no Manage Messages here; the stub still cleans itself up

    def _guide_maker(self, ctx: commands.Context, key: str):
        async def make(user):
            is_mod = await self._is_mod(user)
            shown = HOME_KEY if key == "mod" and not is_mod else key
            view = GuideView(self, user.id, is_mod, shown)
            return self.render(shown, ctx.guild, is_mod, member=user), view
        return make

    async def _open(self, ctx: commands.Context, key: str):
        await self._deliver(ctx, self._guide_maker(ctx, key))

    # ------------------------------------------------------------------ commands

    @commands.hybrid_group(name="wonderland", aliases=["wld"], fallback="menu", invoke_without_command=True)
    @commands.guild_only()
    async def wonderland(self, ctx: commands.Context):
        """Your guide to everything Wonderland's bots can do."""
        await self._open(ctx, HOME_KEY)

    @wonderland.command(name="page")
    async def wonderland_page(self, ctx: commands.Context, topic: Topic):
        """Jump straight to one topic."""
        await self._open(ctx, topic)

    @wonderland.command(name="map")
    async def wonderland_map(self, ctx: commands.Context):
        """What every channel is for."""
        await self._open(ctx, MAP_KEY)

    @wonderland.command(name="mod")
    async def wonderland_mod(self, ctx: commands.Context):
        """The mod cheat sheet (mods only)."""
        await self._open(ctx, "mod")  # a non-mod just gets the normal home page

    @wonderland.command(name="search")
    async def wonderland_search(self, ctx: commands.Context, *, word: str):
        """Find a command by what it does, e.g. `.wld search card`."""
        async def make(user):
            hits = engine.search(word, self._exists, await self._is_mod(user))
            return embeds.search_embed(word, hits), None
        await self._deliver(ctx, make)

    @wonderland.command(name="stats")
    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    async def wonderland_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Your record across every game (or someone else's)."""
        stats_cog = self.bot.get_cog("MyStats")
        if stats_cog is None:
            return await ctx.send("Stats aren't available right now.", ephemeral=True)

        async def make(user):
            return await stats_cog.build_embed(ctx, member or user), None
        await self._deliver(ctx, make)

    @wonderland.command(name="version")
    async def wonderland_version(self, ctx: commands.Context):
        """Show the running build (deploy probe)."""
        await ctx.send(f"Wonderland guide v{COG_VERSION}")

    @wonderland.command(name="publish")
    @commands.mod_or_permissions(manage_guild=True)
    async def wonderland_publish(self, ctx: commands.Context, channel: discord.TextChannel,
                                 what: Literal["all", "map"] = "all"):
        """Post the guide (`all`) or just the server map (`map`) in a channel. Run again to refresh."""
        async with ctx.typing():
            stored = await self.config.guild(ctx.guild).publish()
            old = stored.get("messages", {}) if stored.get("channel_id") == channel.id else {}
            new: dict = dict(old)  # keep earlier posts so a map-only run never orphans the other pages
            keys = [MAP_KEY] if what == "map" else [p.key for p in PAGES if not p.mod_only] + [MAP_KEY]
            for key in keys:
                embed = self.render(key, ctx.guild, False, footer=PUBLISH_FOOTER, public=True)
                message = None
                if str(key) in old:
                    try:
                        message = await channel.fetch_message(old[str(key)])
                        await message.edit(embed=embed)
                    except discord.NotFound:
                        message = None
                if message is None:
                    message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                new[str(key)] = message.id
            await self.config.guild(ctx.guild).publish.set({"channel_id": channel.id, "messages": new})
        posted = len(keys)
        await ctx.send(f"Published {posted} message{'s' if posted != 1 else ''} in {channel.mention}. Run the same command again to refresh them.")
