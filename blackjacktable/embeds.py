"""
Embed builders for the blackjack table. Pure rendering functions -- take a
Table (and sometimes a BankAdapter, for the balance displays decision #8
asked for) and return a discord.Embed. No state mutation happens here.
"""

from __future__ import annotations

import discord

from .constants import MAX_SEATS
from .engine import hand_value
from .table import Table

_OUTCOME_LABELS = {
    "blackjack_win": "Blackjack! 🎉",
    "win": "Win",
    "push": "Push",
    "loss": "Loss",
    "bust": "Bust",
}

_STATUS_SUFFIX = {
    "playing": "",
    "stood": " (stood)",
    "bust": " (bust)",
    "blackjack": " (blackjack!)",
}


def render_lobby_embed(table: Table) -> discord.Embed:
    embed = discord.Embed(
        title="🃏 Blackjack Table",
        description=(
            f"Bet range: **{table.min_bet}-{table.max_bet}**\n"
            f"Seats: **{len(table.seats)}/{MAX_SEATS}**\n\n"
            "Click **Join** to sit down. The host can **Start Now**, "
            "or the table deals automatically once the lobby timer runs out."
        ),
        color=discord.Color.blurple(),
    )
    if table.seats:
        for seat in table.seats:
            host_tag = " (host)" if seat.member_id == table.host_id else ""
            embed.add_field(name=f"{seat.display_name}{host_tag}", value="Seated", inline=True)
    else:
        embed.add_field(name="No one seated yet", value="Be the first to Join.", inline=False)
    return embed


async def render_betting_embed(table: Table, bank) -> discord.Embed:
    embed = discord.Embed(
        title="🃏 Blackjack — Place Your Bets",
        description=f"Bet range: **{table.min_bet}-{table.max_bet}**. Click **Place Bet** below.",
        color=discord.Color.gold(),
    )
    for seat in table.seats:
        balance = await bank.get_balance(seat.member_id)
        status = f"Bet: **{seat.hand.bet}**" if seat.hand.bet > 0 else "Waiting for bet..."
        embed.add_field(
            name=seat.display_name, value=f"{status}\nBalance: {balance}", inline=True
        )
    return embed


def _dealer_display(table: Table) -> str:
    cards = table.dealer_hand.cards
    if table.state == "player_turns":
        # Hole card stays hidden while players still have decisions to make.
        shown = cards[:1]
        return " ".join(str(c) for c in shown) + "  🂠"
    total, _ = hand_value(cards)
    return " ".join(str(c) for c in cards) + f"  (**{total}**)"


def render_hand_embed(table: Table) -> discord.Embed:
    embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.blurple())
    embed.add_field(name="Dealer", value=_dealer_display(table), inline=False)

    current = table.current_seat()
    for seat in table.seats:
        total, _ = hand_value(seat.hand.cards)
        cards = " ".join(str(c) for c in seat.hand.cards)
        marker = " ⬅️" if current and seat.member_id == current.member_id else ""
        suffix = _STATUS_SUFFIX.get(seat.hand.status, "")
        embed.add_field(
            name=f"{seat.display_name}{marker}",
            value=f"{cards}  (**{total}**){suffix}\nBet: {seat.hand.bet}",
            inline=True,
        )
    return embed


async def render_results_embed(table: Table, bank) -> discord.Embed:
    embed = discord.Embed(title="🃏 Blackjack — Results", color=discord.Color.green())
    embed.add_field(name="Dealer", value=_dealer_display(table), inline=False)

    for result in table.last_results:
        balance = await bank.get_balance(result.member_id)
        net = result.payout - result.bet
        sign = "+" if net >= 0 else ""
        embed.add_field(
            name=result.display_name,
            value=(
                f"{_OUTCOME_LABELS.get(result.outcome, result.outcome)}\n"
                f"Bet {result.bet} → {sign}{net}\n"
                f"Balance: {balance}"
            ),
            inline=True,
        )
    return embed
