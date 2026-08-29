"""
The Red cog class. Wires table.py's state machine and views.py's buttons
into actual Discord commands, plus a BankAdapter implementation backed by
Red's real bank API (redbot.core.bank).
"""

from __future__ import annotations

import discord
from redbot.core import Config, bank, checks, commands
from redbot.core.bot import Red

from .constants import (
    BET_TIMEOUT_SECONDS,
    DEFAULT_CHANNEL_ID,
    LOBBY_TIMEOUT_SECONDS,
    MAX_SEATS,
    MIN_SEATS,
    TURN_TIMEOUT_SECONDS,
)
from .embeds import render_betting_embed, render_hand_embed, render_lobby_embed, render_results_embed
from .table import Table, TableError
from .views import ActionView, BettingView, LobbyView

CONFIG_SCHEMA_VERSION = 1


class RedBankAdapter:
    """table.py's BankAdapter protocol, implemented against Red's real
    bank API. Table only ever deals in plain member IDs (so it stays
    testable without a bot) -- this is where an ID gets turned back into
    the discord.Member object bank.* actually wants."""

    def __init__(self, guild: discord.Guild) -> None:
        self.guild = guild

    def _member(self, member_id: int) -> discord.Member:
        member = self.guild.get_member(member_id)
        if member is None:
            # Only realistically happens if someone leaves the guild
            # mid-round. Not specifically handled beyond surfacing the
            # error -- caught by the cog's round-level try/except.
            raise RuntimeError(f"Member {member_id} is no longer in this server.")
        return member

    async def can_spend(self, member_id: int, amount: int) -> bool:
        return await bank.can_spend(self._member(member_id), amount)

    async def withdraw(self, member_id: int, amount: int) -> None:
        await bank.withdraw_credits(self._member(member_id), amount)

    async def deposit(self, member_id: int, amount: int) -> None:
        await bank.deposit_credits(self._member(member_id), amount)

    async def get_balance(self, member_id: int) -> int:
        return await bank.get_balance(self._member(member_id))


class BlackjackTable(commands.Cog):
    """A multiplayer blackjack table with a shared shoe, seated players,
    and button-based hit/stand/double play."""

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
        # channel_id -> Table, only ever one entry given the single hardcoded channel.
        self.active_tables: dict[int, Table] = {}

    def cog_unload(self) -> None:
        # In-memory state, no persistence (accepted risk per the design doc --
        # this bot restarts rarely enough that it wasn't worth the extra
        # complexity). A reload/unload mid-round strands any bets already
        # withdrawn for that round; clearing the dict here at least stops
        # the channel from being permanently stuck reporting "a table is
        # already open" after a reload.
        self.active_tables.clear()

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
        """Open a blackjack table lobby. Click Join to sit down."""
        if ctx.channel.id != DEFAULT_CHANNEL_ID:
            await ctx.send(f"Blackjack can only be played in <#{DEFAULT_CHANNEL_ID}>.")
            return

        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send("Blackjack is currently disabled on this server.")
            return

        if ctx.channel.id in self.active_tables:
            await ctx.send("A table is already open in this channel.")
            return

        settings = await self.config.guild(ctx.guild).all()
        bank_adapter = RedBankAdapter(ctx.guild)
        table = Table(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            host_id=ctx.author.id,
            bank=bank_adapter,
            min_bet=settings["min_bet"],
            max_bet=settings["max_bet"],
        )
        table.add_seat(ctx.author.id, ctx.author.display_name)  # host sits down automatically

        self.active_tables[ctx.channel.id] = table
        message = await ctx.send(embed=render_lobby_embed(table))

        try:
            await self._run_round(
                message,
                table,
                bank_adapter,
                lobby_timeout=settings["lobby_timeout"],
                bet_timeout=settings["bet_timeout"],
                turn_timeout=settings["turn_timeout"],
            )
        except Exception:
            await ctx.send(
                "Something went wrong running that table -- it's closed now. "
                "Any bets already withdrawn for the round in progress were NOT "
                "automatically refunded (see the design doc's restart-durability note)."
            )
            raise
        finally:
            self.active_tables.pop(ctx.channel.id, None)

    async def _run_round(
        self,
        message: discord.Message,
        table: Table,
        bank_adapter: RedBankAdapter,
        lobby_timeout: int,
        bet_timeout: int,
        turn_timeout: int,
    ) -> None:
        # --- Lobby ---
        lobby_view = LobbyView(table, timeout=lobby_timeout)
        await message.edit(embed=render_lobby_embed(table), view=lobby_view)
        await lobby_view.wait()

        if not table.can_start():
            await message.edit(
                embed=discord.Embed(
                    description="Table closed — no players seated.",
                    color=discord.Color.greyple(),
                ),
                view=None,
            )
            return

        # --- Betting ---
        table.open_betting()
        betting_view = BettingView(table, bank_adapter, timeout=bet_timeout)
        await message.edit(embed=await render_betting_embed(table, bank_adapter), view=betting_view)
        await betting_view.wait()

        table.drop_unbet_seats()
        if not table.seats:
            await message.edit(
                embed=discord.Embed(
                    description="No bets placed in time — table closed.",
                    color=discord.Color.greyple(),
                ),
                view=None,
            )
            return

        # --- Dealing + player turns ---
        await table.deal()

        while table.state == "player_turns":
            seat = table.current_seat()
            action_view = ActionView(table, timeout=turn_timeout)
            await message.edit(embed=render_hand_embed(table), view=action_view)
            timed_out = await action_view.wait()
            if timed_out and table.state == "player_turns" and table.current_seat() is seat:
                # AFK player: forced stand rather than freezing the whole table
                # (locked decision #7).
                await table.stand(seat.member_id)

        # --- Results ---
        await message.edit(embed=await render_results_embed(table, bank_adapter), view=None)

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
        embed.add_field(name="Lobby timeout", value=f"{settings['lobby_timeout']}s")
        embed.add_field(name="Bet timeout", value=f"{settings['bet_timeout']}s")
        embed.add_field(name="Turn timeout", value=f"{settings['turn_timeout']}s")
        await ctx.send(embed=embed)
