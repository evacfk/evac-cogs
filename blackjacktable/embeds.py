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
    "blackjack_win": "🃏 Blackjack!",
    "win": "✅ Win",
    "push": "🟰 Push",
    "loss": "❌ Loss",
    "bust": "💥 Bust",
}

_STATUS_SUFFIX = {
    "playing": "",
    "stood": " · stood",
    "bust": " · **bust**",
    "blackjack": " · **blackjack!**",
}

_COLOR_LOBBY = discord.Color.from_rgb(47, 133, 90)      # felt green
_COLOR_BETTING = discord.Color.from_rgb(212, 175, 55)    # gold
_COLOR_HAND = discord.Color.from_rgb(47, 133, 90)         # felt green
_COLOR_CLOSED = discord.Color.from_rgb(95, 99, 104)        # neutral grey


def _money(amount: int) -> str:
    return f"{amount:,}"


def _card_row(cards) -> str:
    """Cards rendered in a code span so they line up and read as a hand
    rather than a run of plain text."""
    return "`" + " ".join(str(c) for c in cards) + "`"


def _shoe_footer(table: Table) -> str:
    total = table.deck_count * 52
    return f"Shoe: {table.cards_remaining}/{total} cards remaining"


def render_lobby_embed(table: Table) -> discord.Embed:
    embed = discord.Embed(
        title="🂡 Blackjack Table",
        description=(
            f"Bet range **{_money(table.min_bet)}-{_money(table.max_bet)}**  •  "
            f"Seats **{len(table.seats)}/{MAX_SEATS}**\n"
            "Click **Join** to sit down. The host can **Start Now**, "
            "or the table deals automatically once the lobby timer runs out."
        ),
        color=_COLOR_LOBBY,
    )
    if table.seats:
        seated = "\n".join(
            f"{'👑 ' if s.member_id == table.host_id else '• '}{s.display_name}"
            for s in table.seats
        )
        embed.add_field(name="Seated", value=seated, inline=False)
    else:
        embed.add_field(name="No one seated yet", value="Be the first to Join.", inline=False)
    embed.set_footer(text=f"{table.deck_count}-deck shoe")
    return embed


async def render_betting_embed(table: Table, bank) -> discord.Embed:
    embed = discord.Embed(
        title="🂡 Blackjack — Place Your Bets",
        description=f"Bet range **{_money(table.min_bet)}-{_money(table.max_bet)}**",
        color=_COLOR_BETTING,
    )
    if table.seats:
        lines = []
        for seat in table.seats:
            balance = await bank.get_balance(seat.member_id)
            status = f"bet **{_money(seat.hand.bet)}**" if seat.hand.bet > 0 else "_waiting..._"
            lines.append(f"**{seat.display_name}** — {status}  ·  balance {_money(balance)}")
        embed.add_field(name="Table", value="\n".join(lines), inline=False)
    embed.set_footer(text=_shoe_footer(table))
    return embed


def _dealer_display(table: Table) -> str:
    cards = table.dealer_hand.cards
    if table.state == "player_turns":
        # Hole card stays hidden while players still have decisions to make.
        return _card_row(cards[:1]) + "  🂠"
    total, _ = hand_value(cards)
    return f"{_card_row(cards)}  **{total}**"


def render_hand_embed(table: Table) -> discord.Embed:
    embed = discord.Embed(title="🂡 Blackjack", color=_COLOR_HAND)
    embed.add_field(name="Dealer", value=_dealer_display(table), inline=False)

    current = table.current_seat()
    for seat in table.seats:
        total, _ = hand_value(seat.hand.cards)
        marker = "▶️ " if current and seat.member_id == current.member_id else ""
        suffix = _STATUS_SUFFIX.get(seat.hand.status, "")
        embed.add_field(
            name=f"{marker}{seat.display_name}",
            value=f"{_card_row(seat.hand.cards)}  **{total}**{suffix}\nBet: {_money(seat.hand.bet)}",
            inline=True,
        )
    embed.set_footer(text=_shoe_footer(table))
    return embed


async def render_results_embed(table: Table, bank) -> discord.Embed:
    dealer_total, _ = hand_value(table.dealer_hand.cards)
    embed = discord.Embed(
        title="🂡 Blackjack — Results",
        description=f"Dealer: {_dealer_display(table)}" + (
            "  **(bust)**" if dealer_total > 21 else ""
        ),
        color=_COLOR_CLOSED,
    )

    for result in table.last_results:
        balance = await bank.get_balance(result.member_id)
        net = result.payout - result.bet
        sign = "+" if net >= 0 else ""
        embed.add_field(
            name=result.display_name,
            value=(
                f"{_OUTCOME_LABELS.get(result.outcome, result.outcome)}\n"
                f"Bet {_money(result.bet)} → **{sign}{_money(net)}**\n"
                f"Balance: {_money(balance)}"
            ),
            inline=True,
        )
    embed.set_footer(text=_shoe_footer(table))
    return embed


def render_count_embed(table: Table) -> discord.Embed:
    """Only ever shown by `.blackjack count`, and only when an admin has
    turned that on -- see the show_count gate in blackjacktable.py."""
    embed = discord.Embed(title="🂡 Card Count", color=_COLOR_BETTING)
    embed.add_field(name="Running count", value=f"{table.running_count:+d}", inline=True)
    embed.add_field(name="True count", value=f"{table.true_count:+.2f}", inline=True)
    embed.add_field(
        name="Decks remaining", value=f"{table.cards_remaining / 52:.1f}", inline=True
    )
    embed.set_footer(text=_shoe_footer(table))
    return embed
