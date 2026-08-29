"""
The Discord-facing interactive pieces: the Join/Leave/Start lobby buttons,
the bet-entry modal, and the Hit/Stand/Double turn buttons.

Every view here calls straight into Table's methods and lets TableError
propagate into an ephemeral reply -- the views are intentionally thin,
all the actual game rules live in table.py/engine.py.
"""

from __future__ import annotations

import discord

from .constants import MAX_SEATS
from .embeds import render_betting_embed, render_hand_embed, render_lobby_embed
from .engine import can_double
from .table import Table, TableError


class LobbyView(discord.ui.View):
    """Join / Leave / Start Now. Stops itself (ending the wait() the cog
    is awaiting on) either when the host clicks Start Now or the table
    fills up -- otherwise it runs out via its own timeout."""

    def __init__(self, table: Table, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.table = table

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            self.table.add_seat(interaction.user.id, interaction.user.display_name)
        except TableError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(embed=render_lobby_embed(self.table), view=self)
        if len(self.table.seats) >= MAX_SEATS:
            self.stop()

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            self.table.remove_seat(interaction.user.id)
        except TableError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(embed=render_lobby_embed(self.table), view=self)

    @discord.ui.button(label="Start Now", style=discord.ButtonStyle.primary)
    async def start_now(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.table.host_id:
            await interaction.response.send_message(
                "Only the host who opened this table can start it early.", ephemeral=True
            )
            return
        if not self.table.can_start():
            await interaction.response.send_message(
                "Need at least one seated player first.", ephemeral=True
            )
            return
        await interaction.response.defer()
        self.stop()


class BetModal(discord.ui.Modal, title="Place Your Bet"):
    """Opened by BettingView's Place Bet button. Editing the underlying
    table message on submit -- and re-checking all_bets_placed() -- is
    what lets the round move on the moment everyone's in, rather than
    always waiting out the full bet timeout."""

    def __init__(self, table: Table, view: "BettingView", bank) -> None:
        super().__init__()
        self.table = table
        self.view = view
        self.bank = bank
        self.amount = discord.ui.TextInput(
            label=f"Amount ({table.min_bet}-{table.max_bet})",
            placeholder=str(table.min_bet),
            max_length=10,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "Enter a whole number.", ephemeral=True
            )
            return
        try:
            await self.table.place_bet(interaction.user.id, int(raw))
        except TableError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        embed = await render_betting_embed(self.table, self.bank)
        await interaction.response.edit_message(embed=embed, view=self.view)
        if self.table.all_bets_placed():
            self.view.stop()


class BettingView(discord.ui.View):
    """One shared 'Place Bet' button -- every seated player can click it
    (each gets their own private modal), so bets are entered in parallel
    rather than turn by turn."""

    def __init__(self, table: Table, bank, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.table = table
        self.bank = bank

    @discord.ui.button(label="Place Bet", style=discord.ButtonStyle.primary)
    async def place_bet(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        seated = any(s.member_id == interaction.user.id for s in self.table.seats)
        if not seated:
            await interaction.response.send_message(
                "You're not seated at this table.", ephemeral=True
            )
            return
        await interaction.response.send_modal(BetModal(self.table, self, self.bank))


class ActionView(discord.ui.View):
    """Hit / Stand / Double for whichever seat is currently up. A fresh
    ActionView is created for every turn (including a player's own
    consecutive hits) by the cog's round loop -- this view itself never
    advances past one action before stopping."""

    def __init__(self, table: Table, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.table = table
        self.seat = table.current_seat()
        if self.seat is None or not can_double(self.seat.hand):
            self.double.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.seat is None or interaction.user.id != self.seat.member_id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.success)
    async def hit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.table.hit(self.seat.member_id)
        await interaction.response.edit_message(embed=render_hand_embed(self.table), view=None)
        self.stop()

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.table.stand(self.seat.member_id)
        await interaction.response.edit_message(embed=render_hand_embed(self.table), view=None)
        self.stop()

    @discord.ui.button(label="Double", style=discord.ButtonStyle.danger)
    async def double(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            await self.table.double(self.seat.member_id)
        except TableError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(embed=render_hand_embed(self.table), view=None)
        self.stop()
