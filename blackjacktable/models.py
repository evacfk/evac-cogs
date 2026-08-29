"""
Pure data structures shared between engine.py (game math) and table.py
(the Discord-facing state machine, written next).

Nothing in this file imports discord.py. Seat.member_id is a plain int
(the Discord user ID) rather than a discord.Member object, so this module
-- and everything built on it -- stays testable without a running bot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class Suit(Enum):
    CLUBS = "clubs"
    DIAMONDS = "diamonds"
    HEARTS = "hearts"
    SPADES = "spades"


class Rank(Enum):
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"
    NINE = "9"
    TEN = "10"
    JACK = "J"
    QUEEN = "Q"
    KING = "K"
    ACE = "A"


# Base blackjack value per rank. Ace is scored as 11 here and hand_value()
# in engine.py knocks it down to 1 per ace as needed to avoid busting.
RANK_VALUES: dict[Rank, int] = {
    Rank.TWO: 2,
    Rank.THREE: 3,
    Rank.FOUR: 4,
    Rank.FIVE: 5,
    Rank.SIX: 6,
    Rank.SEVEN: 7,
    Rank.EIGHT: 8,
    Rank.NINE: 9,
    Rank.TEN: 10,
    Rank.JACK: 10,
    Rank.QUEEN: 10,
    Rank.KING: 10,
    Rank.ACE: 11,
}


@dataclass(frozen=True)
class Card:
    rank: Rank
    suit: Suit

    def __str__(self) -> str:
        return f"{self.rank.value}{_SUIT_GLYPH[self.suit]}"


_SUIT_GLYPH = {
    Suit.CLUBS: "♣",
    Suit.DIAMONDS: "♦",
    Suit.HEARTS: "♥",
    Suit.SPADES: "♠",
}


HandStatus = Literal["playing", "stood", "bust", "blackjack", "surrendered"]


@dataclass
class Hand:
    cards: list[Card] = field(default_factory=list)
    bet: int = 0
    status: HandStatus = "playing"
    # split_from / is_split reserved for v2 -- deliberately absent in v1 so
    # engine.py has nothing to accidentally branch on before splits exist.

    def add(self, card: Card) -> None:
        self.cards.append(card)


@dataclass
class Seat:
    member_id: int
    display_name: str
    hands: list[Hand] = field(default_factory=list)  # len 1 in v1

    @property
    def hand(self) -> Hand:
        """Convenience accessor while every seat has exactly one hand (v1)."""
        return self.hands[0]


TableState = Literal[
    "lobby", "betting", "dealing", "player_turns", "dealer_turn", "settling", "closed"
]
