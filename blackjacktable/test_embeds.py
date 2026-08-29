"""
Render-level checks for embeds.py, run against real discord.py (installed
in this dev environment specifically so these exercise the actual
discord.Embed API, not a hand-written stub).

Two things matter enough to lock in with tests:
1. The bust-visibility fix -- render_results_embed has to show each
   player's full hand (including whatever card caused a bust), not just
   the outcome/bet/balance line it showed before.
2. render_session_summary_embed's win/loss/push wording and duration
   formatting, since that's the whole point of the "table closed" fix.
"""

import pytest

from .embeds import render_results_embed, render_session_summary_embed
from .engine import hand_value
from .test_table import make_table


class _GetBalanceBank:
    """render_results_embed calls bank.get_balance(...) (the real
    RedBankAdapter's method name); FakeBank exposes a sync .balance()
    instead, so this adapts one to the other for these render tests."""

    def __init__(self, fake_bank) -> None:
        self._fake_bank = fake_bank

    async def get_balance(self, member_id: int) -> int:
        return self._fake_bank.balance(member_id)


@pytest.mark.asyncio
async def test_results_embed_shows_full_hand_including_bust_card():
    table, bank = make_table(rng_seed=99, starting_balance=1000)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()

    if table.state == "player_turns":
        for _ in range(20):
            if table.state != "player_turns":
                break
            await table.hit(100)

    embed = await render_results_embed(table, _GetBalanceBank(bank))
    seat = table.seats[0]
    field = next(f for f in embed.fields if f.name == "evac")

    # Every card actually in the final hand has to appear in the field --
    # this is the part that was missing entirely before the fix, making
    # it impossible to see which card busted you.
    from .models import RANK_DISPLAY, SUIT_EMOJI

    for card in seat.hand.cards:
        assert RANK_DISPLAY[card.rank] in field.value
        assert SUIT_EMOJI[card.suit] in field.value
    # Score is shown as "= N" now (leaner wording pass), not a "Score:" label.
    total, _ = hand_value(seat.hand.cards)
    assert f"= **{total}**" in field.value
    # Balance stays out of the per-player field -- it's in the footer
    # instead now, not repeated as its own clause on every row.
    assert "Balance" not in field.value
    # Stacked layout: each player's field is full-width, not one of a
    # 3-per-row inline grid -- narrow inline columns were wrapping longer
    # hands mid-card at a full table.
    assert field.inline is False


@pytest.mark.asyncio
async def test_results_embed_footer_shows_balances():
    table, bank = make_table(rng_seed=99, starting_balance=1000)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()
    if table.state == "player_turns":
        await table.stand(100)

    embed = await render_results_embed(table, _GetBalanceBank(bank))
    assert "💰" in embed.footer.text
    assert "evac" in embed.footer.text
    assert str(bank.balance(100)) in embed.footer.text
    # Shoe count still shares the footer, not replaced by the balance summary.
    assert "Shoe:" in embed.footer.text


@pytest.mark.asyncio
async def test_results_embed_has_divider_between_dealer_and_players():
    table, bank = make_table(rng_seed=1, starting_balance=1000)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()
    if table.state == "player_turns":
        await table.stand(100)

    embed = await render_results_embed(table, _GetBalanceBank(bank))
    names = [f.name for f in embed.fields]
    assert "Dealer's Hand" in names
    # A blank-named (zero-width space) full-width field sits right after
    # the dealer's hand and before the first player row -- the divider
    # line matching the Casino cog reference's layout.
    divider_index = names.index("Dealer's Hand") + 1
    assert embed.fields[divider_index].name == "​"
    assert embed.fields[divider_index].inline is False
    assert "─" in embed.fields[divider_index].value


@pytest.mark.asyncio
async def test_session_summary_reports_win_loss_and_push():
    table, bank = make_table(rng_seed=3, starting_balance=1000)
    table.session_names = {1: "evac", 2: "zayla", 3: "spooder"}
    table.session_net = {1: 500, 2: -200, 3: 0}
    table.session_rounds = {1: 4, 2: 2, 3: 1}

    embed = render_session_summary_embed(table, reason="Table closed by the host.")
    desc = embed.description

    assert "_Table closed by the host._" in desc
    assert "✅ **evac** won **500** over 4 rounds" in desc
    assert "❌ **zayla** lost **200** over 2 rounds" in desc
    assert "🟰 **spooder** broke even over 1 round" in desc
    # Sorted net-descending: evac (winner) has to appear before zayla (loser).
    assert desc.index("evac") < desc.index("zayla")


def test_session_summary_no_rounds_played():
    table, _ = make_table()
    embed = render_session_summary_embed(table, reason="No players seated — table closed.")
    assert "No rounds were completed." in embed.description
    assert "table was open for" not in embed.description.lower() or "🕐" in embed.description


def test_session_summary_includes_duration():
    table, _ = make_table()
    embed = render_session_summary_embed(table)
    assert "🕐 Table was open for" in embed.description
