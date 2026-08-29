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
        deck_count=kwargs.pop("deck_count", 2),
        penetration_pct=kwargs.pop("penetration_pct", 0.75),
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


def test_can_join_during_betting():
    """Relaxed on purpose: once the table is dealing continuously (the
    round loop reopens straight into betting between hands, see
    blackjacktable.py), a new player showing up shouldn't have to wait for
    a full lobby window -- they can seat themselves in time for the next
    bet. Their bet starts at 0 like everyone else's."""
    table, _ = make_table()
    table.add_seat(100, "evac")
    table.open_betting()
    table.add_seat(200, "latecomer")
    assert len(table.seats) == 2
    assert table.seats[1].hand.bet == 0


def test_can_leave_during_betting():
    """Also relaxed: no money has left anyone's balance yet at this point
    (withdrawal happens in deal()), so leaving mid-betting costs nothing."""
    table, _ = make_table()
    table.add_seat(100, "evac")
    table.open_betting()
    table.remove_seat(100)
    assert len(table.seats) == 0


@pytest.mark.asyncio
async def test_cannot_join_after_deal():
    table, _ = make_table(rng_seed=1)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()
    with pytest.raises(TableError):
        table.add_seat(200, "toolate")


@pytest.mark.asyncio
async def test_cannot_leave_after_deal():
    table, _ = make_table(rng_seed=1)
    table.add_seat(100, "evac")
    table.open_betting()
    await table.place_bet(100, 50)
    await table.deal()
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


# ---- shoe persistence / reshuffle / card counting -------------------------

async def _play_one_round(table: Table, member_id: int, bet: int = 10) -> None:
    """Test helper: bet, deal, then Stand as soon as it's this player's
    turn (keeps rounds short and deterministic for shoe-lifecycle tests
    that don't care about the specific outcome)."""
    table.open_betting()
    await table.place_bet(member_id, bet)
    await table.deal()
    if table.state == "player_turns":
        await table.stand(member_id)
    table.reopen_lobby()


@pytest.mark.asyncio
async def test_shoe_persists_across_rounds_without_reshuffling():
    """The whole point of card counting: the shoe should NOT reset every
    hand. With a 2-deck shoe (104 cards) and one solo player drawing a
    handful of cards a round, several rounds in a row should all draw from
    the same shoe -- cards_remaining should just keep dropping, never
    jump back up to 104 between hands."""
    table, _ = make_table(rng_seed=3, deck_count=2, penetration_pct=0.75)
    table.add_seat(100, "evac")

    remaining_after_each_round = []
    for _ in range(5):
        await _play_one_round(table, 100)
        remaining_after_each_round.append(table.cards_remaining)

    # Strictly non-increasing -- never jumps back up mid-sequence, which
    # would mean a reshuffle happened when it shouldn't have.
    for earlier, later in zip(remaining_after_each_round, remaining_after_each_round[1:]):
        assert later <= earlier, remaining_after_each_round
    # And it did actually go down at least once -- proves cards were drawn
    # from a persistent shoe rather than a fresh one appearing each round.
    assert remaining_after_each_round[-1] < remaining_after_each_round[0]


@pytest.mark.asyncio
async def test_running_count_accumulates_across_rounds():
    """seen_cards, and therefore running_count, should keep growing round
    over round within the same shoe -- not reset to whatever one hand's
    cards add up to."""
    table, _ = make_table(rng_seed=11, deck_count=2, penetration_pct=0.75)
    table.add_seat(100, "evac")

    seen_counts = []
    for _ in range(4):
        await _play_one_round(table, 100)
        seen_counts.append(len(table.seen_cards))

    for earlier, later in zip(seen_counts, seen_counts[1:]):
        assert later > earlier, seen_counts  # strictly grows every round


@pytest.mark.asyncio
async def test_reshuffle_resets_count_and_is_reported():
    """Force a reshuffle by using a tiny 1-deck shoe with shallow (10%)
    penetration -- a handful of rounds will blow past that fast. deal()
    should report reshuffled=True on the round that triggers it, and
    seen_cards/running_count should reset to empty/zero at that point."""
    table, _ = make_table(rng_seed=17, deck_count=1, penetration_pct=0.10)
    table.add_seat(100, "evac")

    reshuffled_flags = []
    for _ in range(8):
        table.open_betting()
        await table.place_bet(100, 10)
        reshuffled = await table.deal()
        reshuffled_flags.append(reshuffled)
        if table.state == "player_turns":
            await table.stand(100)
        table.reopen_lobby()

    # First deal always "reshuffles" (shoe starts out empty/None).
    assert reshuffled_flags[0] is True
    # With only 10% penetration on a single deck, at least one later round
    # should also have triggered a reshuffle.
    assert any(reshuffled_flags[1:]), reshuffled_flags


@pytest.mark.asyncio
async def test_count_properties_available_before_any_round():
    """A freshly created table (no deal() called yet) shouldn't blow up if
    something asks for its count -- should read as a full, untouched shoe."""
    table, _ = make_table(deck_count=2)
    assert table.cards_remaining == 104
    assert table.running_count == 0
    assert table.true_count == 0.0
