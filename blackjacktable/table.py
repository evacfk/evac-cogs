"""
The Table state machine: lobby -> betting -> dealing -> player_turns ->
dealer_turn -> settling -> closed.

Deliberately has zero discord.py imports, same philosophy as engine.py --
this is the piece with money and turn-order bugs in it, so it needs to be
testable with plain pytest (see test_table.py) without a running bot. The
Discord side (embeds, buttons, the live message) lives in views.py and
blackjacktable.py, which hold a Table instance and call these methods.

Bank access goes through the BankAdapter protocol below rather than
importing redbot.core.bank directly -- blackjacktable.py wires the real
bank calls in; test_table.py wires in a fake in-memory ledger. This is
the same "keep the money logic testable in isolation" idea as engine.py,
one layer up.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Protocol

from .constants import DEFAULT_DECK_COUNT, DEFAULT_PENETRATION_PCT, MAX_SEATS, MIN_SEATS
from .engine import Shoe, can_double, hand_value, is_blackjack, play_dealer, settle_hand
from .engine import running_count as _calc_running_count
from .engine import true_count as _calc_true_count
from .models import Card, Hand, Seat, TableState


class BankAdapter(Protocol):
    """Thin interface over Red's bank API. blackjacktable.py implements
    this against redbot.core.bank; test_table.py implements it against an
    in-memory dict. Table never touches a real bank module directly."""

    async def can_spend(self, member_id: int, amount: int) -> bool: ...
    async def withdraw(self, member_id: int, amount: int) -> None: ...
    async def deposit(self, member_id: int, amount: int) -> None: ...


class TableError(Exception):
    """Raised for any invalid action against a Table: wrong state, not
    your turn, bad bet amount, insufficient funds, etc. Callers (the
    Discord command/button layer) catch this and show the message text
    directly to the user -- messages here are written to be user-facing."""


@dataclass
class RoundResult:
    member_id: int
    display_name: str
    outcome: str  # one of engine.Outcome's values, or "bust"
    payout: int
    bet: int


class Table:
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        host_id: int,
        bank: BankAdapter,
        min_bet: int,
        max_bet: int,
        deck_count: int = DEFAULT_DECK_COUNT,
        penetration_pct: float = DEFAULT_PENETRATION_PCT,
        rng: random.Random | None = None,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.host_id = host_id
        self.bank = bank
        self.min_bet = min_bet
        self.max_bet = max_bet
        self.deck_count = deck_count
        self.penetration_pct = penetration_pct
        self._rng = rng or random.Random()

        self.state: TableState = "lobby"
        self.seats: list[Seat] = []
        self.dealer_hand: Hand = Hand()
        self.shoe: Optional[Shoe] = None
        # Every card that's actually been exposed face-up so far in the
        # current shoe's life (NOT every card drawn -- a dealt-but-hidden
        # dealer hole card isn't added until it's revealed). This is what
        # the Hi-Lo running count is computed from. Persists across many
        # rounds/reopen_lobby() calls -- only reset when the shoe itself
        # gets reshuffled (see _ensure_shoe below), same as a real count.
        self.seen_cards: list[Card] = []
        self.current_seat_index: Optional[int] = None
        self.last_results: list[RoundResult] = []

    # ------------------------------------------------------------------
    # Lobby
    # ------------------------------------------------------------------

    def add_seat(self, member_id: int, display_name: str) -> None:
        # Joining is allowed during "betting" too, not just "lobby" -- once
        # the table is dealing continuously (see reopen_lobby/blackjacktable.py's
        # round loop), a new player showing up between hands shouldn't have
        # to wait for a full lobby window; they just get seated in time for
        # the next bet.
        if self.state not in ("lobby", "betting"):
            raise TableError("Table isn't accepting new players right now.")
        if len(self.seats) >= MAX_SEATS:
            raise TableError("Table is full.")
        if any(s.member_id == member_id for s in self.seats):
            raise TableError("You're already seated.")
        seat = Seat(member_id=member_id, display_name=display_name, hands=[])
        if self.state == "betting":
            seat.hands = [Hand(bet=0)]  # matches what open_betting() gives existing seats
        self.seats.append(seat)

    def remove_seat(self, member_id: int) -> None:
        # Leaving is safe during betting too -- no money has actually left
        # anyone's balance yet at this point (withdrawal happens in deal()),
        # so dropping a bet-in-progress seat costs nothing.
        if self.state not in ("lobby", "betting"):
            raise TableError("Can't leave once the round has started.")
        self.seats = [s for s in self.seats if s.member_id != member_id]

    def can_start(self) -> bool:
        return self.state == "lobby" and len(self.seats) >= MIN_SEATS

    # ------------------------------------------------------------------
    # Betting
    # ------------------------------------------------------------------

    def open_betting(self) -> None:
        if not self.can_start():
            raise TableError(f"Need at least {MIN_SEATS} player(s) to start.")
        self.state = "betting"
        for seat in self.seats:
            seat.hands = [Hand(bet=0)]

    async def place_bet(self, member_id: int, amount: int) -> None:
        if self.state != "betting":
            raise TableError("Not taking bets right now.")
        seat = self._seat_for(member_id)
        if amount < self.min_bet or amount > self.max_bet:
            raise TableError(f"Bet must be between {self.min_bet} and {self.max_bet}.")
        if not await self.bank.can_spend(member_id, amount):
            raise TableError("You don't have enough credits for that bet.")
        seat.hand.bet = amount

    def all_bets_placed(self) -> bool:
        return bool(self.seats) and all(seat.hand.bet > 0 for seat in self.seats)

    def drop_unbet_seats(self) -> list[Seat]:
        """Removes any seat that never placed a bet before the bet timeout
        expired. Returns the dropped seats so the caller can notify them.
        They're dropped from the ROUND, not banned from the table --
        nothing stops them rejoining next time the table reopens."""
        dropped = [s for s in self.seats if s.hand.bet <= 0]
        self.seats = [s for s in self.seats if s.hand.bet > 0]
        return dropped

    # ------------------------------------------------------------------
    # Dealing
    # ------------------------------------------------------------------

    def _ensure_shoe(self) -> bool:
        """Creates a fresh shoe if none exists yet, or if the current one
        has been dealt down past the configured penetration depth (checked
        BETWEEN rounds, here, never mid-hand -- matches a real dealer
        noticing the cut card, finishing the hand, then shuffling before
        the next one). Returns True if a new shoe was just shuffled in, so
        the caller can announce it -- anyone counting cards needs to know
        their running count just reset to zero."""
        total_cards = self.deck_count * 52
        threshold = total_cards * (1 - self.penetration_pct)
        if self.shoe is None or len(self.shoe) <= threshold:
            self.shoe = Shoe(deck_count=self.deck_count, rng=self._rng)
            self.seen_cards = []
            return True
        return False

    async def deal(self) -> bool:
        """Deals a new round. Returns True if this deal triggered a shoe
        reshuffle (see _ensure_shoe)."""
        if self.state != "betting":
            raise TableError("Can't deal outside the betting phase.")
        if not self.seats:
            raise TableError("No players with bets placed.")

        # Withdraw every bet up front, same as buying chips before cards
        # touch the table -- means a mid-round disconnect can't leave a
        # player owing without having actually staked anything.
        for seat in self.seats:
            await self.bank.withdraw(seat.member_id, seat.hand.bet)

        reshuffled = self._ensure_shoe()
        self.dealer_hand = Hand()

        for _ in range(2):
            for seat in self.seats:
                card = self.shoe.draw()
                seat.hand.add(card)
                self.seen_cards.append(card)  # player cards are always face-up
            self.dealer_hand.add(self.shoe.draw())

        # Dealer's first card is the up-card (visible); the second stays
        # hidden until _advance_to_dealer reveals it -- so only the first
        # goes into seen_cards here.
        self.seen_cards.append(self.dealer_hand.cards[0])

        for seat in self.seats:
            if is_blackjack(seat.hand.cards):
                seat.hand.status = "blackjack"

        self.state = "player_turns"
        self.current_seat_index = 0
        self._skip_resolved_seats()
        if self.current_seat_index is None:
            await self._advance_to_dealer()

        return reshuffled

    # ------------------------------------------------------------------
    # Player turns
    # ------------------------------------------------------------------

    def current_seat(self) -> Optional[Seat]:
        if self.state != "player_turns" or self.current_seat_index is None:
            return None
        return self.seats[self.current_seat_index]

    def _seat_for(self, member_id: int) -> Seat:
        for seat in self.seats:
            if seat.member_id == member_id:
                return seat
        raise TableError("You're not seated at this table.")

    def _require_current_turn(self, member_id: int) -> Seat:
        seat = self.current_seat()
        if seat is None or seat.member_id != member_id:
            raise TableError("It's not your turn.")
        return seat

    async def hit(self, member_id: int) -> None:
        seat = self._require_current_turn(member_id)
        card = self.shoe.draw()
        seat.hand.add(card)
        self.seen_cards.append(card)
        total, _ = hand_value(seat.hand.cards)
        if total > 21:
            seat.hand.status = "bust"
            await self._advance_turn()
        elif total == 21:
            # No decision left to make at 21 -- auto-stand rather than
            # making the player click Stand on a hand that can't improve.
            seat.hand.status = "stood"
            await self._advance_turn()

    async def stand(self, member_id: int) -> None:
        seat = self._require_current_turn(member_id)
        seat.hand.status = "stood"
        await self._advance_turn()

    async def double(self, member_id: int) -> None:
        seat = self._require_current_turn(member_id)
        if not can_double(seat.hand):
            raise TableError("Can only double down on your first two cards.")
        if not await self.bank.can_spend(member_id, seat.hand.bet):
            raise TableError("Not enough credits to double down.")
        await self.bank.withdraw(member_id, seat.hand.bet)
        seat.hand.bet *= 2
        card = self.shoe.draw()
        seat.hand.add(card)
        self.seen_cards.append(card)
        total, _ = hand_value(seat.hand.cards)
        seat.hand.status = "bust" if total > 21 else "stood"
        await self._advance_turn()

    def _skip_resolved_seats(self) -> None:
        """Advances current_seat_index past any seat whose hand is already
        decided (blackjack dealt, or resolved this round), landing on the
        next seat still awaiting a decision -- or None if everyone's done."""
        idx = self.current_seat_index if self.current_seat_index is not None else 0
        while idx < len(self.seats) and self.seats[idx].hand.status != "playing":
            idx += 1
        self.current_seat_index = idx if idx < len(self.seats) else None

    async def _advance_turn(self) -> None:
        assert self.current_seat_index is not None
        self.current_seat_index += 1
        self._skip_resolved_seats()
        if self.current_seat_index is None:
            await self._advance_to_dealer()

    # ------------------------------------------------------------------
    # Dealer turn
    # ------------------------------------------------------------------

    async def _advance_to_dealer(self) -> None:
        self.state = "dealer_turn"
        # Reveal the hole card now -- it becomes visible (and countable)
        # regardless of whether the dealer ends up drawing further.
        self.seen_cards.append(self.dealer_hand.cards[1])

        # If every seat busted, the dealer's hand can't matter to the
        # outcome -- skip drawing for it entirely (matches the design doc:
        # "all-bust tables skip dealer play, no reason to draw").
        anyone_still_live = any(s.hand.status in ("stood", "blackjack") for s in self.seats)
        if anyone_still_live:
            already_dealt = len(self.dealer_hand.cards)
            self.dealer_hand.cards = play_dealer(self.shoe, self.dealer_hand.cards)
            self.seen_cards.extend(self.dealer_hand.cards[already_dealt:])
        await self.settle()

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def settle(self) -> list[RoundResult]:
        self.state = "settling"
        results: list[RoundResult] = []
        for seat in self.seats:
            hand = seat.hand
            if hand.status == "bust":
                outcome, payout = "bust", 0
            else:
                settlement = settle_hand(hand.cards, self.dealer_hand.cards, hand.bet)
                outcome, payout = settlement.outcome, settlement.payout
            if payout > 0:
                await self.bank.deposit(seat.member_id, payout)
            results.append(
                RoundResult(seat.member_id, seat.display_name, outcome, payout, hand.bet)
            )
        self.last_results = results
        self.state = "closed"
        return results

    # ------------------------------------------------------------------
    # Next round
    # ------------------------------------------------------------------

    def reopen_lobby(self) -> None:
        """Loops the table back to an open lobby with the same seated
        players (a seat isn't auto-dropped just because a round ended --
        matches sitting at a real table between hands). Only valid once
        a round has fully settled.

        Deliberately does NOT touch self.shoe or self.seen_cards -- the
        whole point of a shoe is that it persists across many rounds
        without reshuffling (see _ensure_shoe). Wiping it here would reset
        everyone's count every single hand, which defeats card counting
        entirely."""
        if self.state != "closed":
            raise TableError("Can't reopen a table mid-round.")
        self.state = "lobby"
        for seat in self.seats:
            seat.hands = []
        self.dealer_hand = Hand()
        self.current_seat_index = None

    # ------------------------------------------------------------------
    # Card counting
    # ------------------------------------------------------------------

    @property
    def cards_remaining(self) -> int:
        """Cards left in the current shoe (or a fresh shoe's full size if
        none has been dealt from yet)."""
        return len(self.shoe) if self.shoe is not None else self.deck_count * 52

    @property
    def running_count(self) -> int:
        return _calc_running_count(self.seen_cards)

    @property
    def true_count(self) -> float:
        return _calc_true_count(self.running_count, self.cards_remaining)
