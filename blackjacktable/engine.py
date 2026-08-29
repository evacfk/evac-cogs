"""
Pure blackjack game logic: shoe construction, hand scoring, dealer AI,
and payout math. No discord.py imports on purpose -- this is the module
that handles money math and needs to be trustworthy on its own, testable
with plain pytest and no running bot (see test_engine.py).

v1 ruleset (locked):
- Dealer stands on all 17s, hard or soft.
- Natural blackjack (21 on the first two cards) pays 3:2.
- Regular win pays 1:1. Push returns the original bet.
- Dealer blackjack beats any non-blackjack player hand.
- No split / insurance / surrender (v2).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from .constants import (
    BLACKJACK_PAYOUT_DENOMINATOR,
    BLACKJACK_PAYOUT_NUMERATOR,
    DEALER_STAND_TOTAL,
    DECK_COUNT,
)
from .models import RANK_VALUES, Card, Hand, Rank, Suit


class Shoe:
    """A shuffled multi-deck shoe. Reshuffled fresh per round in v1 --
    no cut-card/penetration tracking yet (see design doc, v2 candidates)."""

    def __init__(self, deck_count: int = DECK_COUNT, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._cards: list[Card] = self._build(deck_count)
        self._rng.shuffle(self._cards)

    @staticmethod
    def _build(deck_count: int) -> list[Card]:
        return [
            Card(rank, suit)
            for _ in range(deck_count)
            for suit in Suit
            for rank in Rank
        ]

    def draw(self) -> Card:
        if not self._cards:
            raise RuntimeError("Shoe is empty -- this should never happen within one round")
        return self._cards.pop()

    def __len__(self) -> int:
        return len(self._cards)


def hand_value(cards: list[Card]) -> tuple[int, bool]:
    """Returns (best_total, is_soft). is_soft is True if an ace is currently
    counted as 11 in that best total (relevant for dealer soft-17 logic,
    even though v1 treats soft and hard 17 the same for the stand rule)."""
    total = sum(RANK_VALUES[card.rank] for card in cards)
    aces_as_eleven = sum(1 for card in cards if card.rank is Rank.ACE)

    while total > 21 and aces_as_eleven > 0:
        total -= 10  # demote one ace from 11 to 1
        aces_as_eleven -= 1

    # If the loop exits with an ace still counted as 11, total is
    # guaranteed <= 21 (otherwise the loop would have kept demoting).
    soft = aces_as_eleven > 0
    return total, soft


def is_bust(cards: list[Card]) -> bool:
    total, _ = hand_value(cards)
    return total > 21


def is_blackjack(cards: list[Card]) -> bool:
    """Natural blackjack: exactly two cards totaling 21. A 21 reached via
    hit (e.g. after a hypothetical split, or 7+7+7) is NOT a blackjack --
    it's just a 21, and pays even money, not 3:2."""
    return len(cards) == 2 and hand_value(cards)[0] == 21


def dealer_should_hit(dealer_cards: list[Card]) -> bool:
    total, _ = hand_value(dealer_cards)
    return total < DEALER_STAND_TOTAL


def play_dealer(shoe: Shoe, dealer_cards: list[Card]) -> list[Card]:
    """Draws for the dealer per the stand-on-17 rule, returns the final hand.
    Mutates nothing outside its own return value; caller owns dealer_cards."""
    cards = list(dealer_cards)
    while dealer_should_hit(cards):
        cards.append(shoe.draw())
    return cards


Outcome = Literal["blackjack_win", "win", "push", "loss", "bust"]


@dataclass(frozen=True)
class Settlement:
    outcome: Outcome
    payout: int  # total credits returned to the player, INCLUDING their original bet
                  # (0 means the bet is fully lost; equal to bet means a push)


def settle_hand(player_cards: list[Card], dealer_cards: list[Card], bet: int) -> Settlement:
    """Resolves one player hand against the finished dealer hand and returns
    what the player is owed. Caller is responsible for the actual bank
    withdraw/deposit calls -- this function is pure math, no side effects."""
    player_total, _ = hand_value(player_cards)
    dealer_total, _ = hand_value(dealer_cards)

    player_bust = player_total > 21
    dealer_bust = dealer_total > 21
    player_bj = is_blackjack(player_cards)
    dealer_bj = is_blackjack(dealer_cards)

    if player_bust:
        # Player already lost the moment they busted, regardless of what the
        # dealer ends up with -- this is why player_turns fully resolves
        # busted hands before the dealer even plays.
        return Settlement("bust", 0)

    if player_bj and dealer_bj:
        return Settlement("push", bet)

    if player_bj:
        winnings = (bet * BLACKJACK_PAYOUT_NUMERATOR) // BLACKJACK_PAYOUT_DENOMINATOR
        return Settlement("blackjack_win", bet + winnings)

    if dealer_bj:
        return Settlement("loss", 0)

    if dealer_bust:
        return Settlement("win", bet * 2)

    if player_total > dealer_total:
        return Settlement("win", bet * 2)

    if player_total < dealer_total:
        return Settlement("loss", 0)

    return Settlement("push", bet)


def can_double(hand: Hand) -> bool:
    """Double down is only offered on a fresh two-card hand in v1 (no
    double-after-split since splits don't exist yet)."""
    return len(hand.cards) == 2 and hand.status == "playing"
