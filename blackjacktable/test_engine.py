"""
Plain pytest coverage for engine.py -- no discord.py, no bot instance.
Run with: pytest test_engine.py -v

This is the "get it bulletproof before it touches money" step from the
build plan. Every payout rule in the v1 spec has a test here; add one
whenever a new rule (splits, insurance, soft-17 config) lands in v2.
"""

import random

import pytest

from .engine import (
    Shoe,
    can_double,
    dealer_should_hit,
    hand_value,
    is_blackjack,
    is_bust,
    play_dealer,
    settle_hand,
)
from .models import Card, Hand, Rank, Suit

C = Card  # shorthand for the fixtures below


# ---- hand_value -------------------------------------------------------

def test_hand_value_simple():
    cards = [C(Rank.KING, Suit.SPADES), C(Rank.SEVEN, Suit.HEARTS)]
    total, soft = hand_value(cards)
    assert (total, soft) == (17, False)


def test_hand_value_ace_soft():
    cards = [C(Rank.ACE, Suit.SPADES), C(Rank.SIX, Suit.HEARTS)]
    total, soft = hand_value(cards)
    assert (total, soft) == (17, True)


def test_hand_value_ace_demoted_to_avoid_bust():
    cards = [C(Rank.ACE, Suit.SPADES), C(Rank.SIX, Suit.HEARTS), C(Rank.KING, Suit.CLUBS)]
    total, soft = hand_value(cards)
    assert (total, soft) == (17, False)  # ace forced down to 1


def test_hand_value_multiple_aces():
    # A + A + 9 -> one ace at 11, one at 1 -> 21, still soft (one ace at 11)
    cards = [C(Rank.ACE, Suit.SPADES), C(Rank.ACE, Suit.HEARTS), C(Rank.NINE, Suit.CLUBS)]
    total, soft = hand_value(cards)
    assert (total, soft) == (21, True)


def test_bust_detection():
    cards = [C(Rank.KING, Suit.SPADES), C(Rank.QUEEN, Suit.HEARTS), C(Rank.TWO, Suit.CLUBS)]
    assert is_bust(cards) is True


# ---- blackjack detection -----------------------------------------------

def test_natural_blackjack():
    cards = [C(Rank.ACE, Suit.SPADES), C(Rank.KING, Suit.HEARTS)]
    assert is_blackjack(cards) is True


def test_21_via_three_cards_is_not_blackjack():
    cards = [C(Rank.SEVEN, Suit.SPADES), C(Rank.SEVEN, Suit.HEARTS), C(Rank.SEVEN, Suit.CLUBS)]
    total, _ = hand_value(cards)
    assert total == 21
    assert is_blackjack(cards) is False


# ---- dealer AI -----------------------------------------------------------

def test_dealer_hits_below_17():
    cards = [C(Rank.KING, Suit.SPADES), C(Rank.SIX, Suit.HEARTS)]
    assert dealer_should_hit(cards) is True


def test_dealer_stands_on_hard_17():
    cards = [C(Rank.KING, Suit.SPADES), C(Rank.SEVEN, Suit.HEARTS)]
    assert dealer_should_hit(cards) is False


def test_dealer_stands_on_soft_17():
    cards = [C(Rank.ACE, Suit.SPADES), C(Rank.SIX, Suit.HEARTS)]
    assert dealer_should_hit(cards) is False  # v1 rule: stand on ALL 17s


def test_play_dealer_draws_to_at_least_17():
    shoe = Shoe(deck_count=6, rng=random.Random(42))
    dealer_start = [C(Rank.KING, Suit.SPADES), C(Rank.THREE, Suit.HEARTS)]  # 13
    final = play_dealer(shoe, dealer_start)
    total, _ = hand_value(final)
    assert total >= 17 or total > 21  # stands at 17+, or busts trying to get there


# ---- settlement / payout math -------------------------------------------

def test_settle_player_bust_loses_regardless_of_dealer():
    player = [C(Rank.KING, Suit.SPADES), C(Rank.QUEEN, Suit.HEARTS), C(Rank.TWO, Suit.CLUBS)]  # 22
    dealer = [C(Rank.TWO, Suit.SPADES), C(Rank.TWO, Suit.HEARTS)]  # dealer also weak, doesn't matter
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "bust"
    assert result.payout == 0


def test_settle_both_blackjack_is_push():
    player = [C(Rank.ACE, Suit.SPADES), C(Rank.KING, Suit.HEARTS)]
    dealer = [C(Rank.ACE, Suit.CLUBS), C(Rank.QUEEN, Suit.DIAMONDS)]
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "push"
    assert result.payout == 100


def test_settle_player_blackjack_pays_three_to_two():
    player = [C(Rank.ACE, Suit.SPADES), C(Rank.KING, Suit.HEARTS)]
    dealer = [C(Rank.NINE, Suit.CLUBS), C(Rank.SEVEN, Suit.DIAMONDS)]  # 16, not blackjack
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "blackjack_win"
    assert result.payout == 250  # 100 bet back + 150 winnings (3:2)


def test_settle_dealer_blackjack_beats_player_21_via_hit():
    player = [C(Rank.SEVEN, Suit.SPADES), C(Rank.SEVEN, Suit.HEARTS), C(Rank.SEVEN, Suit.CLUBS)]  # 21, not natural
    dealer = [C(Rank.ACE, Suit.CLUBS), C(Rank.KING, Suit.DIAMONDS)]  # natural blackjack
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "loss"
    assert result.payout == 0


def test_settle_dealer_bust_player_wins_even_money():
    player = [C(Rank.KING, Suit.SPADES), C(Rank.NINE, Suit.HEARTS)]  # 19
    dealer = [C(Rank.KING, Suit.CLUBS), C(Rank.QUEEN, Suit.DIAMONDS), C(Rank.FIVE, Suit.SPADES)]  # 25, bust
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "win"
    assert result.payout == 200


def test_settle_higher_total_wins():
    player = [C(Rank.KING, Suit.SPADES), C(Rank.NINE, Suit.HEARTS)]  # 19
    dealer = [C(Rank.KING, Suit.CLUBS), C(Rank.EIGHT, Suit.DIAMONDS)]  # 18
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "win"
    assert result.payout == 200


def test_settle_lower_total_loses():
    player = [C(Rank.KING, Suit.SPADES), C(Rank.EIGHT, Suit.HEARTS)]  # 18
    dealer = [C(Rank.KING, Suit.CLUBS), C(Rank.NINE, Suit.DIAMONDS)]  # 19
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "loss"
    assert result.payout == 0


def test_settle_equal_totals_push():
    player = [C(Rank.KING, Suit.SPADES), C(Rank.EIGHT, Suit.HEARTS)]  # 18
    dealer = [C(Rank.QUEEN, Suit.CLUBS), C(Rank.EIGHT, Suit.DIAMONDS)]  # 18
    result = settle_hand(player, dealer, bet=100)
    assert result.outcome == "push"
    assert result.payout == 100


# ---- double down eligibility ---------------------------------------------

def test_can_double_on_fresh_two_card_hand():
    hand = Hand(cards=[C(Rank.FIVE, Suit.SPADES), C(Rank.SIX, Suit.HEARTS)], bet=100)
    assert can_double(hand) is True


def test_cannot_double_after_hitting():
    hand = Hand(
        cards=[C(Rank.FIVE, Suit.SPADES), C(Rank.SIX, Suit.HEARTS), C(Rank.TWO, Suit.CLUBS)],
        bet=100,
    )
    assert can_double(hand) is False


# ---- shoe sanity -----------------------------------------------------------

def test_shoe_size_and_draw():
    shoe = Shoe(deck_count=6, rng=random.Random(1))
    assert len(shoe) == 52 * 6
    drawn = shoe.draw()
    assert isinstance(drawn, Card)
    assert len(shoe) == 52 * 6 - 1


def test_shoe_raises_when_empty():
    shoe = Shoe(deck_count=1, rng=random.Random(2))
    for _ in range(52):
        shoe.draw()
    with pytest.raises(RuntimeError):
        shoe.draw()
