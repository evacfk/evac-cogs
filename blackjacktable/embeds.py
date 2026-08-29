"""
Embed builders for the blackjack table. Pure rendering functions -- take a
Table (and sometimes a BankAdapter, for the balance displays decision #8
asked for) and return a discord.Embed. No state mutation happens here.

Discord embeds cannot render arbitrary colored text -- no markdown for it,
and the ANSI-code-block trick only works in plain message content, not
embeds, and doesn't work on mobile even there. So "showing green" here
means real color emoji (✅/❌/🟰) and the embed's own accent-color rail,
not colored numbers -- that's a hard platform limit, not a design choice.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord

from .constants import MAX_SEATS
from .engine import hand_value
from .models import RANK_DISPLAY, SUIT_EMOJI
from .table import Table

_OUTCOME_LABELS = {
    "blackjack_win": "🃏 Blackjack!",
    "win": "✅ Win",
    "push": "🟰 Push",
    "loss": "❌ Loss",
    "bust": "💥 Bust",
}

_COLOR_LOBBY = discord.Color.from_rgb(47, 133, 90)      # felt green
_COLOR_BETTING = discord.Color.from_rgb(212, 175, 55)    # gold
_COLOR_HAND = discord.Color.from_rgb(47, 133, 90)         # felt green
_COLOR_CLOSED = discord.Color.from_rgb(95, 99, 104)        # neutral grey

_DIVIDER = "─" * 32


def _money(amount: int) -> str:
    return f"{amount:,}"


def _card_display(card) -> str:
    """'Jack ♥️' rather than a crammed 'J♥' -- spelled-out rank, real
    color emoji suit. Matches how the Casino cog's blackjack shows hands,
    which is what this was redesigned to look more like."""
    return f"{RANK_DISPLAY[card.rank]} {SUIT_EMOJI[card.suit]}"


def _hand_display(cards) -> str:
    return ", ".join(_card_display(c) for c in cards)


def _shoe_footer(table: Table) -> str:
    total = table.deck_count * 52
    return f"Shoe: {table.cards_remaining}/{total} cards remaining"


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _add_divider(embed: discord.Embed) -> None:
    """A blank-named full-width field holding a line of dashes -- Discord
    has no native divider, this is the standard trick (what the Casino
    cog's reference screenshot is doing too). Separates the dealer's hand
    from the row of player hands below it."""
    embed.add_field(name="​", value=_DIVIDER, inline=False)


def render_lobby_embed(table: Table) -> discord.Embed:
    embed = discord.Embed(
        title="🃏 Blackjack Table",
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
        title="🃏 Blackjack — Place Your Bets",
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


def _dealer_hand_block(table: Table) -> str:
    cards = table.dealer_hand.cards
    if table.state == "player_turns":
        # Hole card stays hidden while players still have decisions to make.
        return f"{_card_display(cards[0])}, 🂠 = **?**"
    total, _ = hand_value(cards)
    bust_note = "  **(bust)**" if total > 21 else ""
    return f"{_hand_display(cards)} = **{total}**{bust_note}"


def render_hand_embed(table: Table) -> discord.Embed:
    embed = discord.Embed(title="🃏 Blackjack", color=_COLOR_HAND)
    embed.add_field(name="Dealer's Hand", value=_dealer_hand_block(table), inline=False)
    _add_divider(embed)

    current = table.current_seat()
    status_note = {
        "playing": "",
        "stood": " · _stood_",
        "bust": " · **bust**",
        "blackjack": " · **blackjack!**",
    }
    # One stacked row per seat (not the old 3-per-row inline grid) -- at a
    # full 7-seat table the inline columns got narrow enough that hands
    # wrapped mid-card. Full embed width means a long hand still fits on
    # one line, and it reads more like a real scoreboard. "=" instead of
    # a "Score:" label, per the leaner wording pass.
    for seat in table.seats:
        total, _ = hand_value(seat.hand.cards)
        marker = "▶️ " if current and seat.member_id == current.member_id else ""
        embed.add_field(
            name=f"{marker}{seat.display_name}",
            value=(
                f"{_hand_display(seat.hand.cards)} = **{total}**"
                f"{status_note.get(seat.hand.status, '')} · Bet: {_money(seat.hand.bet)}"
            ),
            inline=False,
        )
    embed.set_footer(text=_shoe_footer(table))
    return embed


async def render_results_embed(table: Table, bank) -> discord.Embed:
    """Balance is back, but moved to the footer as a compact one-line
    summary rather than repeated as its own clause on every player's row --
    keeps the per-player line down to hand/score/outcome/net while still
    surfacing balance right on this screen instead of making you flip back
    to the last betting embed to find it."""
    embed = discord.Embed(title="🃏 Blackjack — Results", color=_COLOR_CLOSED)
    embed.add_field(name="Dealer's Hand", value=_dealer_hand_block(table), inline=False)
    _add_divider(embed)

    # Stacked (one row per player, full embed width) rather than the old
    # 3-per-row inline grid -- same reasoning as render_hand_embed, and it
    # cuts what used to be five separately-labeled lines per player down
    # to hand=score, then outcome and net, on one line.
    balance_parts = []
    for result in table.last_results:
        seat = next((s for s in table.seats if s.member_id == result.member_id), None)
        net = result.payout - result.bet
        sign = "+" if net >= 0 else ""

        hand_part = ""
        if seat is not None:
            total, _ = hand_value(seat.hand.cards)
            # This is the part that matters for counting: every card that
            # was actually drawn stays visible here, including whichever
            # one pushed a busted hand over 21 -- nothing about how you
            # got to the final total is hidden after the fact.
            hand_part = f"{_hand_display(seat.hand.cards)} = **{total}** · "

        embed.add_field(
            name=result.display_name,
            value=(
                f"{hand_part}"
                f"{_OUTCOME_LABELS.get(result.outcome, result.outcome)} · "
                f"**{sign}{_money(net)}**"
            ),
            inline=False,
        )
        balance = await bank.get_balance(result.member_id)
        balance_parts.append(f"{result.display_name} {_money(balance)}")

    footer = _shoe_footer(table)
    if balance_parts:
        # Footer text has no markdown, so no bolding here -- just a plain
        # "name amount" per player, cheapest way to fit every balance on
        # one line without another labeled clause per row above.
        footer = f"💰 {' · '.join(balance_parts)}  •  {footer}"
    embed.set_footer(text=footer)
    return embed


def render_count_embed(table: Table) -> discord.Embed:
    """Only ever shown by `.wonderjack count`, and only when an admin has
    turned that on -- see the show_count gate in blackjacktable.py."""
    embed = discord.Embed(title="🃏 Card Count", color=_COLOR_BETTING)
    embed.add_field(name="Running count", value=f"{table.running_count:+d}", inline=True)
    embed.add_field(name="True count", value=f"{table.true_count:+.2f}", inline=True)
    embed.add_field(
        name="Decks remaining", value=f"{table.cards_remaining / 52:.1f}", inline=True
    )
    embed.set_footer(text=_shoe_footer(table))
    return embed


def render_session_summary_embed(table: Table, reason: str | None = None) -> discord.Embed:
    """Shown whenever the table closes for good -- host closed it, the
    host didn't respond to Next Round in time, nobody placed a bet, or
    nobody was seated to begin with. Summarizes the whole session (every
    round since the table opened, across however many reshuffles), not
    just the last round."""
    embed = discord.Embed(title="🃏 Table Closed", color=_COLOR_CLOSED)
    duration = _format_duration(table.session_elapsed_seconds)

    lines: list[str] = []
    if reason:
        lines.append(f"_{reason}_")
        lines.append("")

    if not table.session_net:
        lines.append("No rounds were completed.")
    else:
        # Biggest winner first -- reads like a scoreboard.
        ordered = sorted(table.session_net.items(), key=lambda kv: kv[1], reverse=True)
        for member_id, net in ordered:
            name = table.session_names.get(member_id, "Unknown")
            rounds = table.session_rounds.get(member_id, 0)
            round_word = "round" if rounds == 1 else "rounds"
            if net > 0:
                lines.append(f"✅ **{name}** won **{_money(net)}** over {rounds} {round_word}")
            elif net < 0:
                lines.append(
                    f"❌ **{name}** lost **{_money(abs(net))}** over {rounds} {round_word}"
                )
            else:
                lines.append(f"🟰 **{name}** broke even over {rounds} {round_word}")

    lines.append("")
    lines.append(f"🕐 Table was open for {duration}")
    embed.description = "\n".join(lines)
    return embed
