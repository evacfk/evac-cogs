"""Retired Secret Hunt. This build only holds the one-time pin cleanup; the hunt itself is gone."""
from __future__ import annotations

import discord
from redbot.core import Config, commands
from redbot.core.utils.chat_formatting import pagify

from . import cleanup
from .constants import COG_VERSION, CONFIG_IDENTIFIER, DEFAULT_GUILD, DEFAULT_MEMBER


class SecretHunt(commands.Cog):
    """Retired. Unpins what the Secret Hunt pinned, then it can be uninstalled."""

    __version__ = COG_VERSION

    def __init__(self, bot):
        self.bot = bot
        # Same class name and identifier as the retired cog, so this reads the data it left behind.
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()
            async with self.config.guild(guild).finishers() as fin:
                for ids in fin.values():
                    while user_id in ids:
                        ids.remove(user_id)

    @commands.group(name="secrethunt", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def secrethunt(self, ctx: commands.Context):
        """The Secret Hunt is retired. `.secrethunt cleanup` unpins what it left pinned."""
        await ctx.send("The Secret Hunt is retired. Run `.secrethunt cleanup` for a dry run of the unpin.")

    @secrethunt.command(name="cleanup")
    async def secrethunt_cleanup(self, ctx: commands.Context, confirm: str = ""):
        """Dry run by default. `.secrethunt cleanup confirm` unpins every hunt poem and object."""
        if confirm not in ("", "confirm"):
            return await ctx.send("Use `.secrethunt cleanup` for the dry run or `.secrethunt cleanup confirm` to unpin.")
        go = confirm == "confirm"
        data = await self.config.guild(ctx.guild).all()
        async with ctx.typing():
            report = await cleanup.run(ctx.guild, self.bot.user.id, data, go)
        text = "\n".join([*report.lines, "", report.summary(go)])
        if not go and report.found:
            text += "\nTo unpin them: `.secrethunt cleanup confirm`"
        for page in pagify(text, delims=["\n"], page_length=1900):
            await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())

    @secrethunt.command(name="version")
    async def secrethunt_version(self, ctx: commands.Context):
        """Which build is running."""
        await ctx.send(f"secrethunt {COG_VERSION}")
