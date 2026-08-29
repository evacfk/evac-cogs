"""
Coverage-completeness checks for the display-lookup dicts in models.py.
These aren't game-logic tests -- they exist purely to catch a KeyError
waiting to happen at runtime: if Rank or Suit ever gains a member and
RANK_DISPLAY/SUIT_EMOJI isn't updated to match, embeds.py's _card_display
would blow up mid-round instead of failing here at collection time.
"""

from .models import RANK_DISPLAY, SUIT_EMOJI, Rank, Suit


def test_rank_display_covers_every_rank():
    assert set(RANK_DISPLAY.keys()) == set(Rank)


def test_suit_emoji_covers_every_suit():
    assert set(SUIT_EMOJI.keys()) == set(Suit)


def test_suit_emoji_uses_variation_selector():
    """The plain glyphs (Card.__str__'s _SUIT_GLYPH) render as flat
    monochrome dingbats on Discord; only the U+FE0F-suffixed form renders
    as the colored suit icon. Lock that in so a future edit can't
    silently strip the selector back out."""
    for glyph in SUIT_EMOJI.values():
        assert ord(glyph[-1]) == 0xFE0F


def test_rank_display_ten_is_two_chars_not_letter():
    # Locked-in choice: Option A from the mockup -- "10", not "T".
    assert RANK_DISPLAY[Rank.TEN] == "10"
