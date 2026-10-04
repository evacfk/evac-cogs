"""MyStats: `.mystats [member]` shows a member's record across every game the server hosts.

Read-only. Keeps no data of its own; reads what the other cogs already store and skips any
game whose cog is not loaded (or whose reader fails), so it can never break the games themselves.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from redbot.core import bank, commands

from . import embeds, engine, sources
from .constants import COG_VERSION, COOLDOWN_SECONDS

log = logging.getLogger("red.evac-cogs.mystats")


class MyStats(commands.Cog):
    """Member-facing stats for every hosted game."""

    def __init__(self, bot):
        self.bot = bot

    async def red_delete_data_for_user(self, **kwargs):
        return  # nothing stored here

    @commands.group(name="mystats", invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def mystats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Your record across every game: minigames, duels, heists, casino, cards, puzzle, Verdict and more."""
        member = member or ctx.author
        async with ctx.typing():
            raw = await sources.gather(self.bot, member)
            try:
                currency = await bank.get_currency_name(ctx.guild)
                balance = await bank.get_balance(member)
                balance_text = f"**Balance:** {balance:,} {currency}"
            except Exception:
                currency, balance_text = "coins", None
            sections = engine.build_sections(raw, currency)
        await ctx.send(embed=embeds.stats_embed(member, sections, balance_text),
                       allowed_mentions=discord.AllowedMentions.none())

    @mystats.command(name="version")
    async def mystats_version(self, ctx: commands.Context):
        """Show the running build (deploy probe)."""
        await ctx.send(f"MyStats v{COG_VERSION}")
