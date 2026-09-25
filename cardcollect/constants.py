"""Config defaults and lookup tables for cardcollect. No discord/redbot imports here."""

DEFAULT_GRID = (3, 3)  # unused placeholder for future card-back layout tweaks

TIERS = ["common", "rare", "epic", "legendary"]

DEFAULT_DROP_WEIGHTS = {
    "common": 60,
    "rare": 27,
    "epic": 10,
    "legendary": 3,
}

DEFAULT_SELL_PRICES = {
    "common": 25,
    "rare": 100,
    "epic": 400,
    "legendary": 1500,
}

# Favourites-count cutoffs used at import time to bucket a character into a tier.
# A character's favourites count must be >= the threshold to land in that tier;
# checked highest-to-lowest so ties resolve to the rarer tier.
DEFAULT_TIER_CUTOFFS = {
    "legendary": 20000,
    "epic": 8000,
    "rare": 2000,
    "common": 0,
}

DEFAULT_DROP_SIZE = 3

DEFAULT_DROP_CHANCE = 0.02  # 2% per qualifying message, retune via .card diagnostics
DEFAULT_DROP_COOLDOWN_SECONDS = 900  # 15 minutes

DEFAULT_CLAIM_COOLDOWN_SECONDS = 300  # 5 minutes

# Claiming is done by pressing the drop's Claim button and typing the CAPTCHA
# code shown on a card into the pop-up (see captcha.py / views.py /
# cardcollect.submit_code). Each member gets a small, fixed number of *wrong
# but code-shaped* guesses per drop; using them all locks them out of that
# drop and, on a real (non-test) drop, out of *winning* any drop for
# DEFAULT_WRONG_GUESS_PENALTY_SECONDS -- a real cost to spamming guesses.
# Submissions that aren't even code-shaped (a typo) get a hint and cost
# nothing. 0 = unlimited guesses, no lockout.
DEFAULT_MAX_WRONG_GUESSES = 2
DEFAULT_WRONG_GUESS_PENALTY_SECONDS = 120  # 2 minutes

# An unclaimed drop stops accepting codes after this long (and its state is
# dropped from memory) -- without it, a drop nobody finished would stay
# claimable, and keep growing in-memory state, forever.
DEFAULT_DROP_EXPIRY_SECONDS = 600  # 10 minutes

DEFAULT_CLAIM_QUOTA = 10  # max real claims per member per day; 0 = unlimited
MAX_SHOWCASE_SLOTS = 3  # how many cards a member can pin in their gallery header

# 1 "original" copy + 1 tradeable spare; a 3rd+ claim of the same character
# auto-converts to a sell token instead of piling up more duplicates, so
# members can still trade a spare with `.card give` without being able to
# hoard an unlimited stack of the same character.
MAX_COPIES_KEPT = 2

# CAPTCHA code alphabet -- see captcha.py for the reasoning behind every
# exclusion. Confusable glyphs left out on purpose: 0 O Q D / 1 I L / 5 S /
# 2 Z / 8 B / U V / G 6.
CODE_LENGTH = 5
CODE_LETTERS = "ACEFHJKMNPRTWXY"
CODE_DIGITS = "3479"
CODE_SYMBOLS = "@#$%&?+"
MIN_CODE_DISTANCE = 2  # min edits between any two codes within one drop

# hour-of-day activity tracking, mirrors minigamehub's diagnostics shape
ACTIVITY_TIMEZONE = "America/Los_Angeles"

CARD_IMAGE_SIZE = (400, 560)  # portrait card aspect, consistent tile size for drops + gallery
GALLERY_TILE_SIZE = (140, 196)
GALLERY_FAVORITE_TILE_SIZE = (240, 336)
GALLERY_COLUMNS = 4
