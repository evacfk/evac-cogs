"""
The Red cog class. Wires table.py's state machine and views.py's buttons
into actual Discord commands, plus a BankAdapter implementation backed by
Red's real bank API (redbot.core.bank).

Command group is `.wonderjack` (not `.blackjack`) -- the Casino cog you
already have loaded exposes its own `.blackjack` command, and the two
collided. `.load blackjacktable` (the cog/package name) is unchanged; only
the in-Discord command prefix moved.
"""

from __future__ import annotations

import discord
from redbot.core import Config, bank, checks, commands
from redbot.core.bot import Red

from .constants import (
    BET_TIMEOUT_SECONDS,
    DEFAULT_CHANNEL_ID,
    DEFAULT_DECK_COUNT,
    DEFAULT_PENETRATION_PCT,
    DEFAULT_SHOW_COUNT,
    LOBBY_TIMEOUT_SECONDS,
    MAX_DECK_COUNT,
    MAX_PENETRATION_PCT,
    MAX_SEATS,
    MIN_DECK_COUNT,
    MIN_PENETRATION_PCT,
    MIN_SEATS,
    TURN_TIMEOUT_SECONDS,
)
from .embeds import (
    render_betting_embed,
    render_count_embed,
    render_hand_embed,
    render_lobby_embed,
    render_results_embed,
    render_session_summary_embed,
)
from .table import Table, TableError
from .views import ActionView, BettingView, LobbyView, NextRoundView

CONFIG_SCHEMA_VERSION = 1


def _channel_allowed(channel: discord.abc.Messageable) -> bool:
    """True in the home channel itself, or in any thread hanging off of
    it -- the same shape as the .gamble command's per-session threads.
    A thread's `.parent_id` points at the channel it was created under,
    so this covers both a thread made directly under the home channel and
    (since Discord only allows one level of nesting) any thread a player
    spins up from within it."""
    if channel.id == DEFAULT_CHANNEL_ID:
        return True
    return getattr(channel, "parent_id", None) == DEFAULT_CHANNEL_ID


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
    and button-based hit/stand/double play. Commands live under
    `.wonderjack` / `.wonderjackset`."""

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
            deck_count=DEFAULT_DECK_COUNT,
            # Stored as a whole-number percent (1-99) rather than a float --
            # Red/Discord command args are cleaner as plain ints, and it
            # avoids float-precision noise round-tripping through Config.
            penetration_pct=int(DEFAULT_PENETRATION_PCT * 100),
            show_count=DEFAULT_SHOW_COUNT,
        )
        # channel_id -> Table (a thread has its own ID here too, distinct
        # from its parent channel's). The home channel and any thread
        # under it can each run one table concurrently -- this dict is
        # what makes "one table per channel/thread" hold.
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

    @commands.group(name="wonderjack", invoke_without_command=True)
    @commands.guild_only()
    async def wonderjack(self, ctx: commands.Context) -> None:
        """Blackjack table commands. Run `.wonderjack table` to open a lobby."""
        await ctx.send_help(ctx.command)

    @wonderjack.command(name="table")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def wonderjack_table(self, ctx: commands.Context) -> None:
        """Open a blackjack table lobby. Click Join to sit down.

        Allowed in the home channel (`constants.DEFAULT_CHANNEL_ID`) and in
        any thread created under it -- same shape as the .gamble command's
        per-session threads. `self.active_tables` is keyed by channel ID
        (a thread has its own ID, distinct from its parent), so this
        naturally means one table per channel/thread: several threads can
        each run their own table at the same time, but two tables can't
        stack in the same one (the check just below still catches that).
        """
        if not _channel_allowed(ctx.channel):
            await ctx.send(
                f"Blackjack can only be played in <#{DEFAULT_CHANNEL_ID}> or a thread under it."
            )
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
            deck_count=settings["deck_count"],
            penetration_pct=settings["penetration_pct"] / 100,
        )
        table.add_seat(ctx.author.id, ctx.author.display_name)  # host sits down automatically

        self.active_tables[ctx.channel.id] = table
        message = await ctx.send(embed=render_lobby_embed(table))

        try:
            await self._run_table_session(ctx, message, table, bank_adapter, settings)
        except Exception:
            await ctx.send(
                "Something went wrong running that table -- it's closed now. "
                "Any bets already withdrawn for the round in progress were NOT "
                "automatically refunded (see the design doc's restart-durability note)."
            )
            raise
        finally:
            self.active_tables.pop(ctx.channel.id, None)

    async def _run_table_session(
        self,
        ctx: commands.Context,
        message: discord.Message,
        table: Table,
        bank_adapter: RedBankAdapter,
        settings: dict,
    ) -> None:
        """Runs the table for as long as anyone keeps playing: one initial
        lobby window, then betting -> deal -> turns -> dealer -> results
        rounds, dealing from the SAME shoe (only reshuffling once it's
        dealt past the configured penetration depth -- see
        Table._ensure_shoe). This persistence across rounds is what makes
        card counting mean anything.

        Between rounds, the results stay on screen with a Next Round /
        Close Table view -- the host has to explicitly advance it. This is
        deliberate: auto-continuing straight into the next bet meant
        results flashed by before anyone could actually see who won or
        lost. Ends when the host closes it, nobody's left seated, nobody
        places a bet before the timeout, or the host doesn't respond to
        the Next Round prompt in time.
        """
        lobby_view = LobbyView(table, timeout=settings["lobby_timeout"])
        await message.edit(embed=render_lobby_embed(table), view=lobby_view)
        await lobby_view.wait()

        if not table.can_start():
            await message.edit(
                embed=render_session_summary_embed(table, reason="No players seated — table closed."),
                view=None,
            )
            return

        while True:
            table.open_betting()
            betting_view = BettingView(table, bank_adapter, timeout=settings["bet_timeout"])
            await message.edit(
                embed=await render_betting_embed(table, bank_adapter), view=betting_view
            )
            await betting_view.wait()

            table.drop_unbet_seats()
            if not table.seats:
                await message.edit(
                    embed=render_session_summary_embed(table, reason="No bets placed — table closed."),
                    view=None,
                )
                return

            reshuffled = await table.deal()
            if reshuffled:
                await ctx.send(
                    f"🔀 New {table.deck_count}-deck shoe shuffled in "
                    f"(previous shoe reached {table.penetration_pct:.0%} penetration)."
                )

            while table.state == "player_turns":
                seat = table.current_seat()
                action_view = ActionView(table, timeout=settings["turn_timeout"])
                await message.edit(embed=render_hand_embed(table), view=action_view)
                timed_out = await action_view.wait()
                if timed_out and table.state == "player_turns" and table.current_seat() is seat:
                    # AFK player: forced stand rather than freezing the whole
                    # table (locked decision #7).
                    await table.stand(seat.member_id)

            # --- Results: wait for the host, don't auto-continue ---
            results_embed = await render_results_embed(table, bank_adapter)
            next_view = NextRoundView(table, timeout=settings["lobby_timeout"])
            await message.edit(embed=results_embed, view=next_view)
            timed_out = await next_view.wait()

            if timed_out:
                await message.edit(
                    embed=render_session_summary_embed(
                        table, reason="Table closed — no response from the host."
                    ),
                    view=None,
                )
                return

            if not table.seats:
                # Host clicked Close Table (NextRoundView clears seats to signal this).
                await message.edit(
                    embed=render_session_summary_embed(table, reason="Table closed by the host."),
                    view=None,
                )
                return

            table.reopen_lobby()

    @wonderjack.command(name="count")
    @commands.guild_only()
    async def wonderjack_count(self, ctx: commands.Context) -> None:
        """Check the current running/true count -- only works if an admin
        has turned this on with `.wonderjackset showcount true`."""
        if not _channel_allowed(ctx.channel):
            await ctx.send(
                f"Blackjack can only be played in <#{DEFAULT_CHANNEL_ID}> or a thread under it."
            )
            return
        if not await self.config.guild(ctx.guild).show_count():
            await ctx.send("Count checking isn't enabled on this server.")
            return
        table = self.active_tables.get(ctx.channel.id)
        if table is None:
            await ctx.send("No table is currently in progress.")
            return
        await ctx.send(embed=render_count_embed(table))

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    @commands.group(name="wonderjackset")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def wonderjackset(self, ctx: commands.Context) -> None:
        """Admin configuration for the blackjack table cog."""

    @wonderjackset.command(name="enabled")
    async def wonderjackset_enabled(self, ctx: commands.Context, on_off: bool) -> None:
        """Turn the blackjack table on or off for this server.

        A table already in progress finishes normally even if this is
        flipped off mid-round -- this only blocks new lobbies from opening.
        """
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = "enabled" if on_off else "disabled"
        await ctx.send(f"Blackjack table {state}.")

    @wonderjackset.command(name="minbet")
    async def wonderjackset_minbet(self, ctx: commands.Context, amount: int) -> None:
        """Set the minimum bet for this server's blackjack table."""
        if amount < 1:
            await ctx.send("Minimum bet must be at least 1.")
            return
        await self.config.guild(ctx.guild).min_bet.set(amount)
        await ctx.send(f"Minimum bet set to {amount}.")

    @wonderjackset.command(name="maxbet")
    async def wonderjackset_maxbet(self, ctx: commands.Context, amount: int) -> None:
        """Set the maximum bet for this server's blackjack table."""
        min_bet = await self.config.guild(ctx.guild).min_bet()
        if amount < min_bet:
            await ctx.send(f"Maximum bet must be at least the minimum bet ({min_bet}).")
            return
        await self.config.guild(ctx.guild).max_bet.set(amount)
        await ctx.send(f"Maximum bet set to {amount}.")

    @wonderjackset.command(name="decks")
    async def wonderjackset_decks(self, ctx: commands.Context, count: int) -> None:
        """Set how many decks the shoe uses (1-8).

        Fewer decks = a faster, more obvious count -- easier to learn on.
        More decks = closer to a real casino floor, harder to track. This
        only takes effect on the NEXT shoe shuffle (an in-progress shoe
        keeps dealing at its current size until it hits penetration).
        """
        if not (MIN_DECK_COUNT <= count <= MAX_DECK_COUNT):
            await ctx.send(f"Deck count must be between {MIN_DECK_COUNT} and {MAX_DECK_COUNT}.")
            return
        await self.config.guild(ctx.guild).deck_count.set(count)
        await ctx.send(
            f"Shoe size set to {count} deck(s). Takes effect next time the shoe reshuffles."
        )

    @wonderjackset.command(name="penetration")
    async def wonderjackset_penetration(self, ctx: commands.Context, percent: int) -> None:
        """Set how deep into the shoe to deal before reshuffling (1-99, as a percent).

        75 (the default) matches standard casino cut-card depth. Lower
        means shorter counting runs that reset more often; higher means
        longer runs before a reshuffle. Also only takes effect on the next
        shuffle, same as deck count.
        """
        min_pct, max_pct = int(MIN_PENETRATION_PCT * 100), int(MAX_PENETRATION_PCT * 100)
        if not (min_pct <= percent <= max_pct):
            await ctx.send(f"Penetration must be between {min_pct} and {max_pct} percent.")
            return
        await self.config.guild(ctx.guild).penetration_pct.set(percent)
        await ctx.send(
            f"Penetration set to {percent}%. Takes effect next time the shoe reshuffles."
        )

    @wonderjackset.command(name="showcount")
    async def wonderjackset_showcount(self, ctx: commands.Context, on_off: bool) -> None:
        """Turn `.wonderjack count` on or off for this server.

        When on, anyone can run `.wonderjack count` during an active table
        to see the current running/true count -- meant as a way to check
        your own manual count while learning, not something shown
        automatically or publicly on the table itself.
        """
        await self.config.guild(ctx.guild).show_count.set(on_off)
        state = "enabled" if on_off else "disabled"
        await ctx.send(f"Count checking (`.wonderjack count`) {state}.")

    @wonderjackset.command(name="settings")
    async def wonderjackset_settings(self, ctx: commands.Context) -> None:
        """Show the current blackjack table settings for this server."""
        settings = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="Blackjack Table Settings", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value=str(settings["enabled"]))
        embed.add_field(name="Min bet", value=str(settings["min_bet"]))
        embed.add_field(name="Max bet", value=str(settings["max_bet"]))
        embed.add_field(name="Channel", value=f"<#{DEFAULT_CHANNEL_ID}> + its threads")
        embed.add_field(name="Seats", value=f"{MIN_SEATS}-{MAX_SEATS}")
        embed.add_field(name="Decks", value=str(settings["deck_count"]))
        embed.add_field(name="Penetration", value=f"{settings['penetration_pct']}%")
        embed.add_field(name="Count checking", value=str(settings["show_count"]))
        embed.add_field(name="Lobby timeout", value=f"{settings['lobby_timeout']}s")
        embed.add_field(name="Bet timeout", value=f"{settings['bet_timeout']}s")
        embed.add_field(name="Turn timeout", value=f"{settings['turn_timeout']}s")
        await ctx.send(embed=embed)
