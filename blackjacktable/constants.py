"""
Tunable constants for the blackjack table cog.

These are the v1 defaults. Anything here that should be per-guild
configurable (channel restriction, bet limits, timeouts) gets mirrored
into the cog's Config schema in blackjacktable.py — this module is just
the fallback/default values and the things that aren't worth making
configurable yet (deck count, dealer stand rule, payout ratio).
"""

# Locked to this channel for v1 (per your answer: one table, this channel only).
# Exposed as a Config-overridable guild setting later if you ever want a second
# channel — hardcoded here for now since that's the actual requirement.
DEFAULT_CHANNEL_ID: int = 1530348323330986005

# A table can start with just the host (solo vs. dealer is allowed).
MIN_SEATS: int = 1
# Matches a real blackjack table's physical seat count.
MAX_SEATS: int = 7

# Real casino tables deal from a multi-deck shoe rather than a single reshuffled
# deck -- flattens card-counting edges and matches the "real casino" ruleset call.
# Reshuffled fresh every round in v1 (no penetration/cut-card tracking yet).
DECK_COUNT: int = 6

LOBBY_TIMEOUT_SECONDS: int = 45
BET_TIMEOUT_SECONDS: int = 30
TURN_TIMEOUT_SECONDS: int = 30

# Dealer stands on all 17s, hard or soft (v1 ruleset; soft-17-hit is a v2 config flag).
DEALER_STAND_TOTAL: int = 17

# Natural blackjack (21 on the first two cards) pays 3:2.
BLACKJACK_PAYOUT_NUMERATOR: int = 3
BLACKJACK_PAYOUT_DENOMINATOR: int = 2

# Regular win pays 1:1 (bet returned + equal winnings) -- no magic number needed
# in engine.py beyond "bet * 2 back", but named here for clarity/searchability.
REGULAR_WIN_MULTIPLIER: int = 2
