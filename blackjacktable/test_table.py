"""
pytest coverage for table.py -- the state machine, driven directly
without any Discord bot running, via a FakeBank in-memory ledger. This is
the "table.py driven from a test script, no UI yet" step from the build
plan.

Where a test needs deterministic cards, it seeds Table's rng. Full-round
tests don't hardcode expected cards -- instead they re-run engine.settle_hand
independently against whatever the Table actually dealt and assert Table's
own settlement matches, which is the real thing worth checking: that the
state machine calls the engine correctly, not that a specific seed deals
a specific hand.
"""

import random

import pytest

from .engine import settle_hand
from .table import Table, TableError


class FakeBank:
    """In-memory stand-in for redbot.core.bank, implementing the same
    shape as table.BankAdapter. Starts every member with a configurable
    balance; raises if a withdrawal would go negative, mirroring how a
    real bank.withdraw_credits() call behaves."""

    def __init__(self, starting_balance: int = 1000) -> None:
        self._starting_balance = starting_balance
        self._balances: dict[int, int] = {}

    def _balance(self, member_id: int) -> int:
        return self._balances.setdefault(member_id, self._starting_balance)

    async def can_spend(self, member_id: int, amount: int) -> bool:
        return self._balance(member_id) >= amount

    async def withdraw(self, member_id: int, amount: int) -> None:
        if self._balance(member_id) < amount:
            raise RuntimeError("insufficient funds")  # should never hit in practice --
            # Table always checks can_spend first
        self._balances[member_id] -= amount

    async def deposit(self, member_id: int, amount: int) -> None:
        self._balances[member_id] = self._balance(member_id) + amount

    def balance(self, member_id: int) -> int:
        return self._balance(member_id)


def make_table(rng_seed: int = 0, **kwargs) -> tuple[Table, FakeBank]:
    bank = FakeBank(starting_balance=kwargs.pop("starting_balance", 1000))
    table = Table(
        guild_id=1,
        channel_id=1530348323330986005,
        host_id=100,
        bank=bank,
        min_bet=kwargs.pop("min_bet", 10),
        max_bet=kwargs.pop("max_bet", 1000),
        rng=random.Random(rng_seed),
    )
    return table, bank


# ---- lobby ------------------------------------------------------------

def test_join_and_leave_lobby():
    table, _ = make_table()
    table.add_seat(100, "evac")
    assert table.seat_count if hasattr(table, "seat_count") else len(table.seats) == 1
    table.remove_seat(100)
    assert len(table.seats) == 0


def test_cannot_join_twice():
    table, _ = make_table()
    table.add_seat(100, "evac")
    with pytest.raises(TableError):
        table.add_seat(100, "evac")


def test_table_full_rejects_extra_seat():
    table, _ = make_table()
    for i in range(7):
        table.add_seat(i, f"player{i}")
    with pytest.raises(TableError):
        table.add_seat(999, "one-too-many")


def test_solo_table_can_start():
    table, _ = make_table()
    table.add_seat(100, "evac")
    assert table.can_start() is True


def test_empty_table_cannot_start():
    table, _ = make_table()
    assert table.can_start() is False


def test_cannot_join_after_betting_opens():
    table, _ = make_table()
    table.add_seat(100, "evac")
    table.open_betting()
    with pytest.raises(TableError):
        table.add_seat(200, "latecomer")


def test_cannot_leave_after_betting_opens():
    table, _ = make_table()
    table.add_seat(100, "evac")
    table.open_betting()
    with pytest.raises(TableError):
        table.remove_seat(100)


# ---- betting ------------------------------------------------------------

@pytest.mark.asyncio
async def test_bet_within_range_accepted():
    table, bank = make_table(min_bet=10, max_bet=500)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    assert table.all_bets_placed() is True


@pytest.mark.asyncio
async def test_bet_below_minimum_rejected():
    table, _ = make_table(min_bet=10, max_bet=500)
    table.add_seat(100, "evac")
    table.open_betting()
    with pytest.raises(TableError):
        await table.place_bet(100, 5)


@pytest.mark.asyncio
async def test_bet_above_maximum_rejected():
    table, _ = make_table(min_bet=10, max_bet=500)
    table.add_seat(100, "evac")
    table.open_betting()
    with pytest.raises(TableError):
        await table.place_bet(100, 501)


@pytest.mark.asyncio
async def test_bet_exceeding_balance_rejected():
    table, _ = make_table(min_bet=10, max_bet=5000, starting_balance=100)
    table.add_seat(100, "evac")
    table.open_betting()
    with pytest.raises(TableError):
        await table.place_bet(100, 200)


@pytest.mark.asyncio
async def test_drop_unbet_seats_on_timeout():
    table, _ = make_table()
    table.add_seat(100, "evac")
    table.add_seat(200, "afk-player")
    table.open_betting()
    await table.place_bet(100, 50)
    dropped = table.drop_unbet_seats()
    assert [s.member_id for s in dropped] == [200]
    assert len(table.seats) == 1


# ---- dealing / turn order -----------------------------------------------

@pytest.mark.asyncio
async def test_deal_withdraws_bets():
    table, bank = make_table(rng_seed=1, starting_balance=1000)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 100)
    await table.deal()
    assert bank.balance(100) == 900  # bet withdrawn up front
    assert len(table.seats[0].hand.cards) == 2
    assert len(table.dealer_hand.cards) == 2


@pytest.mark.asyncio
async def test_only_current_player_can_act():
    table, _ = make_table(rng_seed=2)
    table.add_seat(100, "first")
    table.add_seat(200, "second")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.place_bet(200, 50)
    await table.deal()

    current = table.current_seat()
    # find the seat NOT currently up (deal order preserved as join order,
    # unless one of them got a natural blackjack and was auto-skipped)
    other_id = 200 if current.member_id == 100 else 100
    with pytest.raises(TableError):
        await table.stand(other_id)


@pytest.mark.asyncio
async def test_hit_until_bust_advances_turn():
    table, _ = make_table(rng_seed=3)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()

    if table.state == "player_turns":  # not an instant natural blackjack
        seat = table.current_seat()
        member_id = seat.member_id
        # Hit repeatedly until the engine resolves the hand one way or
        # another (bust, 21, or the caller stands) -- bounded loop so a
        # bug can't hang the test suite.
        for _ in range(20):
            if table.state != "player_turns":
                break
            await table.hit(member_id)
        assert table.state != "player_turns" or table.current_seat().member_id != member_id


@pytest.mark.asyncio
async def test_double_requires_two_card_hand():
    table, _ = make_table(rng_seed=4)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()

    if table.state == "player_turns":
        member_id = table.current_seat().member_id
        await table.hit(member_id)  # now 3 cards, unless that busted/21'd already
        if table.state == "player_turns" and table.current_seat().member_id == member_id:
            with pytest.raises(TableError):
                await table.double(member_id)


# ---- full round, wiring-correctness check --------------------------------

@pytest.mark.asyncio
async def test_full_round_settlement_matches_engine():
    """Runs a complete solo round (always Stand as soon as it's this
    player's turn, to keep the test deterministic regardless of what the
    seeded shoe deals) and verifies Table.settle()'s result for every seat
    exactly matches what engine.settle_hand computes independently from
    the same final cards. This is the actual contract table.py needs to
    honor -- that it's wiring the engine correctly, not any specific
    outcome for this seed."""
    table, bank = make_table(rng_seed=7, starting_balance=1000)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 100)

    starting_balance = bank.balance(100)
    await table.deal()

    if table.state == "player_turns":
        await table.stand(100)

    assert table.state == "closed"
    assert len(table.last_results) == 1

    result = table.last_results[0]
    seat = table.seats[0]
    expected = settle_hand(seat.hand.cards, table.dealer_hand.cards, result.bet)

    if seat.hand.status == "bust":
        assert result.outcome == "bust"
        assert result.payout == 0
    else:
        assert result.outcome == expected.outcome
        assert result.payout == expected.payout

    # Balance sanity: started with `starting_balance`, bet was withdrawn
    # at deal time, payout (if any) deposited at settle time.
    assert bank.balance(100) == starting_balance - result.bet + result.payout


@pytest.mark.asyncio
async def test_all_bust_table_dealer_does_not_draw():
    """When every seated player busts, the dealer shouldn't draw at all
    (nothing left to compare against) -- this locks in that optimization
    so it can't silently regress into always playing the dealer out."""
    table, bank = make_table(rng_seed=99, starting_balance=1000)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()

    if table.state == "player_turns":
        dealer_cards_before = list(table.dealer_hand.cards)
        # Force a bust: hit until we go over, however many cards that takes.
        for _ in range(20):
            if table.state != "player_turns":
                break
            await table.hit(100)
        if table.seats[0].hand.status == "bust":
            assert table.dealer_hand.cards == dealer_cards_before


@pytest.mark.asyncio
async def test_reopen_lobby_after_round_closes():
    table, bank = make_table(rng_seed=5)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()
    if table.state == "player_turns":
        await table.stand(100)

    assert table.state == "closed"
    table.reopen_lobby()
    assert table.state == "lobby"
    assert len(table.seats) == 1  # still seated, just no hand yet
    with pytest.raises(IndexError):
        _ = table.seats[0].hand  # no hand dealt until betting reopens
