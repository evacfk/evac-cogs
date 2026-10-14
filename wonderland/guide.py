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
    COG_VERSION, CONFIG_ID, PREFIX_REPLY_TTL, PUBLISH_FOOTER, STATS_COOLDOWN, VIEW_TIMEOUT,
)
from .content import HOME_KEY, MAP, MAP_KEY, PAGES

log = logging.getLogger("red.evac-cogs.wonderland")

Topic = Literal["economy", "casino", "games", "profile", "community", "music"]


class TopicSelect(discord.ui.Select):
    def __init__(self, cog: "Wonderland", is_mod: bool, current: str):
        options = [discord.SelectOption(label="Home", value=HOME_KEY, emoji="🐇", default=current == HOME_KEY)]
        for page in engine.visible_pages(is_mod):
            if page.mod_only:
                continue
            options.append(discord.SelectOption(
                label=page.title, value=page.key, emoji=page.emoji, default=current == page.key))
        options.append(discord.SelectOption(
            label="Server Map", value=MAP_KEY, emoji="🗺️", default=current == MAP_KEY))
        if is_mod:
            mod = engine.get_page("mod", True)
            options.append(discord.SelectOption(
                label=mod.title, value=mod.key, emoji=mod.emoji, default=current == mod.key))
        super().__init__(placeholder="Pick a topic…", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.is_mod = is_mod

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        embed = self.cog.render(key, interaction.guild, self.is_mod)
        new_view = GuideView(self.cog, self.view.author_id, self.is_mod, key)
        new_view.message = self.view.message
        self.view.stop()
        await interaction.response.edit_message(embed=embed, view=new_view)


class GuideView(discord.ui.View):
    """The dropdown. Only the person who asked can use it."""

    def __init__(self, cog: "Wonderland", author_id: int, is_mod: bool, current: str = HOME_KEY):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.add_item(TopicSelect(cog, is_mod, current))

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

    def _resolver(self, guild: Optional[discord.Guild]):
        names = {c.id: c.name for c in getattr(guild, "channels", [])} if guild else {}
        # Voice channels and categories are included: Room Creator is a voice channel.

        def resolve(slug: str) -> Optional[str]:
            channel_id = engine.match_channel(slug, names)
            return f"<#{channel_id}>" if channel_id else None

        return resolve

    def render(self, key: str, guild: Optional[discord.Guild], is_mod: bool, footer: Optional[str] = None):
        if key == HOME_KEY:
            return embeds.home_embed(engine.visible_pages(is_mod))
        if key == MAP_KEY:
            return embeds.map_embed(self._resolver(guild), footer) if footer else embeds.map_embed(self._resolver(guild))
        page = engine.get_page(key, is_mod)
        if page is None:
            return embeds.home_embed(engine.visible_pages(is_mod))
        lines = engine.page_lines(page, self._exists)
        return embeds.page_embed(page, lines, footer) if footer else embeds.page_embed(page, lines)

    async def _send_private(self, ctx: commands.Context, embed: discord.Embed, view: Optional[GuideView] = None):
        """Slash: ephemeral. Prefix: short-lived reply, and the invoking message is removed."""
        kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
        if view is not None:
            kwargs["view"] = view
        if ctx.interaction is not None:
            message = await ctx.send(ephemeral=True, **kwargs)
        else:
            message = await ctx.send(delete_after=PREFIX_REPLY_TTL, **kwargs)
            try:
                await ctx.message.delete()
            except Exception:
                pass  # no Manage Messages here; the reply still cleans itself up
        if view is not None:
            view.message = message

    async def _open(self, ctx: commands.Context, key: str):
        is_mod = await self._is_mod(ctx.author)
        if key == "mod" and not is_mod:
            key = HOME_KEY
        view = GuideView(self, ctx.author.id, is_mod, key)
        await self._send_private(ctx, self.render(key, ctx.guild, is_mod), view)

    # ------------------------------------------------------------------ commands

    @commands.hybrid_group(name="wonderland", aliases=["wld"], fallback="menu")
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
        if not await self._is_mod(ctx.author):
            return await self._send_private(ctx, embeds.home_embed(engine.visible_pages(False)))
        await self._open(ctx, "mod")

    @wonderland.command(name="search")
    async def wonderland_search(self, ctx: commands.Context, *, word: str):
        """Find a command by what it does, e.g. `.wld search card`."""
        is_mod = await self._is_mod(ctx.author)
        hits = engine.search(word, self._exists, is_mod)
        await self._send_private(ctx, embeds.search_embed(word, hits))

    @wonderland.command(name="stats")
    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    async def wonderland_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Your record across every game (or someone else's)."""
        member = member or ctx.author
        stats_cog = self.bot.get_cog("MyStats")
        if stats_cog is None:
            return await ctx.send("Stats aren't available right now.", ephemeral=True)
        async with ctx.typing():
            embed = await stats_cog.build_embed(ctx, member)
        await self._send_private(ctx, embed)

    @wonderland.command(name="version")
    async def wonderland_version(self, ctx: commands.Context):
        """Show the running build (deploy probe)."""
        await ctx.send(f"Wonderland guide v{COG_VERSION}")

    @wonderland.command(name="publish")
    @commands.mod_or_permissions(manage_guild=True)
    async def wonderland_publish(self, ctx: commands.Context, channel: discord.TextChannel):
        """Post the whole guide in a channel, or refresh it if it's already there."""
        async with ctx.typing():
            stored = await self.config.guild(ctx.guild).publish()
            old = stored.get("messages", {}) if stored.get("channel_id") == channel.id else {}
            new: dict = {}
            keys = [p.key for p in PAGES if not p.mod_only] + [MAP_KEY]
            for key in keys:
                embed = self.render(key, ctx.guild, False, footer=PUBLISH_FOOTER)
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
        await ctx.send(f"Guide published in {channel.mention} ({len(new)} messages). Run this again to refresh it.")
