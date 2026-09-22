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

DEFAULT_CLAIM_WINDOW_SECONDS = 1.0
DEFAULT_CLAIM_COOLDOWN_SECONDS = 300  # 5 minutes

DEFAULT_DECOYS_ENABLED = True
DEFAULT_DECOY_COUNT = 5  # extra decoy reactions added alongside the 3 real ones

# A reasonably large, visually distinct pool of standard emoji used as claim
# reactions. Kept deliberately varied (food/animals/objects/symbols) so decoys
# and real emoji don't cluster into one obvious visual category. Renderable
# via the bundled Twemoji PNG set (see imagegen.py) and addable as a Discord
# message reaction (all are standard Unicode, no custom-emoji dependency).
EMOJI_POOL = [
    "🍉", "🍇", "🍊", "🍋", "🍓", "🍒", "🍑", "🍍", "🥝", "🍌",
    "🐶", "🐱", "🦊", "🐼", "🐨", "🐯", "🦁", "🐸", "🐙", "🦉",
    "⭐", "🌙", "☀️", "❄️", "🔥", "💧", "🌈", "⚡", "🍀", "🌸",
    "⚽", "🎸", "🎲", "🎯", "🎨", "🧩", "🔑", "💎", "🎁", "🪄",
    "🚀", "⛵", "🚲", "🛸", "🧸", "🍩", "🧁", "🍪", "🍦", "🍰",
]

# hour-of-day activity tracking, mirrors minigamehub's diagnostics shape
ACTIVITY_TIMEZONE = "America/Los_Angeles"

CARD_IMAGE_SIZE = (400, 560)  # portrait card aspect, consistent tile size for drops + gallery
GALLERY_TILE_SIZE = (140, 196)
GALLERY_FAVORITE_TILE_SIZE = (240, 336)
GALLERY_COLUMNS = 4
