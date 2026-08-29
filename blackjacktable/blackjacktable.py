"""
The actual Red cog class. This is the piece that was missing before --
engine.py/models.py/constants.py are plain Python modules with no discord.py
or redbot.core imports, so Red had nothing to register as a cog. This file
is what setup() in __init__.py hands to bot.add_cog().

Current state: loads cleanly, has the admin kill switch (.blackjackset
enabled) working end to end against Config, and a `.blackjack table`
command that enforces the channel lock + kill switch and tells you the
game itself isn't wired up yet. table.py (the state machine) and views.py
(the Join/Hit/Stand/Double buttons) are the next build steps -- once those
exist, `_open_table()` below is where they plug in.
"""

from __future__ import annotations

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

from .constants import (
    BET_TIMEOUT_SECONDS,
    DEFAULT_CHANNEL_ID,
    LOBBY_TIMEOUT_SECONDS,
    MAX_SEATS,
    MIN_SEATS,
    TURN_TIMEOUT_SECONDS,
)

# Bump this if the Config schema below ever changes shape.
CONFIG_SCHEMA_VERSION = 1


class BlackjackTable(commands.Cog):
    """A multiplayer blackjack table with a shared shoe, seated players,
    and Discord-button hit/stand/double actions."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xB1AC4AC7, force_registration=True
        )
        self.config.register_guild(
            enabled=True,
            min_bet=10,
            max_bet=1000,
            lobby_timeout=LOBBY_TIMEOUT_SECONDS,
            bet_timeout=BET_TIMEOUT_SECONDS,
            turn_timeout=TURN_TIMEOUT_SECONDS,
        )
        # Keyed by channel_id once table.py exists. Empty for now since
        # there's nothing yet that can populate it.
        self.active_tables: dict[int, object] = {}

    def cog_unload(self) -> None:
        # Once table.py exists: force-close/refund any live tables here so
        # a cog reload doesn't strand withdrawn bets. Nothing to clean up
        # yet since no table can actually start.
        pass

    # ------------------------------------------------------------------
    # Player-facing commands
    # ------------------------------------------------------------------

    @commands.group(name="blackjack", invoke_without_command=True)
    @commands.guild_only()
    async def blackjack(self, ctx: commands.Context) -> None:
        """Blackjack table commands. Run `.blackjack table` to open a lobby."""
        await ctx.send_help(ctx.command)

    @blackjack.command(name="table")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def blackjack_table(self, ctx: commands.Context) -> None:
        """Open a blackjack table lobby in the designated channel."""
        if ctx.channel.id != DEFAULT_CHANNEL_ID:
            channel_mention = f"<#{DEFAULT_CHANNEL_ID}>"
            await ctx.send(f"Blackjack can only be played in {channel_mention}.")
            return

        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send("Blackjack is currently disabled on this server.")
            return

        if ctx.channel.id in self.active_tables:
            await ctx.send("A table is already open in this channel.")
            return

        # table.py / views.py don't exist yet -- this is the wiring point
        # for _open_table() once they do. Left explicit rather than silently
        # no-op-ing so it's obvious in testing that this is the unfinished part.
        await ctx.send(
            "Table lobby, seating, and the actual game aren't wired up yet "
            "(engine only, so far) -- this command is a placeholder. "
            "Channel lock and the enabled/disabled check above ARE live."
        )

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    @commands.group(name="blackjackset")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def blackjackset(self, ctx: commands.Context) -> None:
        """Admin configuration for the blackjack table cog."""

    @blackjackset.command(name="enabled")
    async def blackjackset_enabled(self, ctx: commands.Context, on_off: bool) -> None:
        """Turn the blackjack table on or off for this server.

        A table already in progress finishes normally even if this is
        flipped off mid-round -- this only blocks new lobbies from opening.
        """
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = "enabled" if on_off else "disabled"
        await ctx.send(f"Blackjack table {state}.")

    @blackjackset.command(name="minbet")
    async def blackjackset_minbet(self, ctx: commands.Context, amount: int) -> None:
        """Set the minimum bet for this server's blackjack table."""
        if amount < 1:
            await ctx.send("Minimum bet must be at least 1.")
            return
        await self.config.guild(ctx.guild).min_bet.set(amount)
        await ctx.send(f"Minimum bet set to {amount}.")

    @blackjackset.command(name="maxbet")
    async def blackjackset_maxbet(self, ctx: commands.Context, amount: int) -> None:
        """Set the maximum bet for this server's blackjack table."""
        min_bet = await self.config.guild(ctx.guild).min_bet()
        if amount < min_bet:
            await ctx.send(f"Maximum bet must be at least the minimum bet ({min_bet}).")
            return
        await self.config.guild(ctx.guild).max_bet.set(amount)
        await ctx.send(f"Maximum bet set to {amount}.")

    @blackjackset.command(name="settings")
    async def blackjackset_settings(self, ctx: commands.Context) -> None:
        """Show the current blackjack table settings for this server."""
        settings = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="Blackjack Table Settings", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value=str(settings["enabled"]))
        embed.add_field(name="Min bet", value=str(settings["min_bet"]))
        embed.add_field(name="Max bet", value=str(settings["max_bet"]))
        embed.add_field(name="Channel", value=f"<#{DEFAULT_CHANNEL_ID}>")
        embed.add_field(name="Seats", value=f"{MIN_SEATS}-{MAX_SEATS}")
        await ctx.send(embed=embed)
