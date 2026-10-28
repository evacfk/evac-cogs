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
    "common": 1000,
    "rare": 5000,
    "epic": 10000,
    "legendary": 25000,
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

# `.card daily`: one free pull per member per Pacific day, rarer than ambient drops
DEFAULT_DAILY_WEIGHTS = {
    "common": 75,
    "rare": 20,
    "epic": 4.5,
    "legendary": 0.5,
}
DAILY_STREAK_BONUS_EVERY = 7  # every 7th day in a row, that day's pull is bumped up one tier

DEFAULT_DROP_SIZE = 3

DEFAULT_DROP_CHANCE = 0.02  # 2% per qualifying message, retune via .card diagnostics
DEFAULT_DROP_COOLDOWN_SECONDS = 900  # 15 minutes

# Cards drop in the drop channel with one button per card. Everyone who presses
# a card's button before the draw closes goes into a random draw for it (see
# engine.resolve_drop); one card per member per drop, and a member can change
# their pick until the draw closes.
DEFAULT_DRAW_WINDOW_SECONDS = 90
MIN_DRAW_WINDOW_SECONDS = 30
MAX_DRAW_WINDOW_SECONDS = 600
MAX_DROP_SIZE = 5  # one button per card, and Discord allows five buttons in a row
PICK_BUTTON_PREFIX = "cardcollect:pick:"  # + the card's position in the drop (0-4)

# Pity: each consecutive lost draw against an equal rival adds PITY_STEP to the
# member's draw weight, up to PITY_CAP times a normal entry; winning resets it.
PITY_STEP = 0.25
PITY_CAP = 2.0

# Wishlist: how many cards a member can wish for, how many members one drop's
# ping line names per card (the rest become "+N more"), and the ping line's reply.
WISHLIST_MAX = 20
WISH_PING_MAX = 10

# Series sets: a series needs at least this many (active) cards to count as a
# set; completing one pays SET_BONUS_BASE + SET_BONUS_PER_CARD per card, once.
SET_MIN_CARDS = 3
SET_BONUS_BASE = 5000
SET_BONUS_PER_CARD = 500

# Requests for characters that aren't in the pool yet: how many a member can have
# waiting at once, and the longest request text.
REQUEST_MAX_PENDING = 3
REQUEST_MAX_LENGTH = 100
REQUEST_APPROVE_ID = "cardcollect:request:approve"
REQUEST_DECLINE_ID = "cardcollect:request:decline"
REQUEST_ADD_ID = "cardcollect:request:add"

MAX_SHOWCASE_SLOTS = 3  # how many cards a member can pin in their gallery header

# 1 "original" copy + 1 tradeable spare; a 3rd+ win of the same character
# auto-converts to a sell token instead of piling up more duplicates, so
# members can still trade a spare with `.card give` without being able to
# hoard an unlimited stack of the same character.
MAX_COPIES_KEPT = 2

# `.card importseries`: how many of a series' characters (by AniList favourites)
# the default button imports, the hard ceiling for "import all", and how many
# AniList search matches the pick-list shows.
SERIES_IMPORT_DEFAULT = 15
SERIES_IMPORT_MAX = 100
SERIES_SEARCH_RESULTS = 5
SERIES_VIEW_TIMEOUT_SECONDS = 600

# `.card importpool <count> [gender]`
ART_MIN_SIDE = 150  # AniList art with a shorter side than this is flagged as probably too small
ART_MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # largest replacement image an admin can upload
ART_UPLOAD_WAIT_SECONDS = 90  # how long the bot waits for each uploaded image
SHEET_COLUMNS = 5
IMPORT_GENDERS = ("any", "female", "male")

# hour-of-day activity tracking, mirrors minigamehub's diagnostics shape
ACTIVITY_TIMEZONE = "America/Los_Angeles"

CARD_IMAGE_SIZE = (400, 560)  # portrait card aspect, consistent tile size for drops + gallery
GALLERY_TILE_SIZE = (140, 196)
GALLERY_FAVORITE_TILE_SIZE = (240, 336)
GALLERY_COLUMNS = 4
GALLERY_ROWS_PER_PAGE = 3  # keeps a page's image small enough to read on mobile
GALLERY_PAGE_SIZE = GALLERY_COLUMNS * GALLERY_ROWS_PER_PAGE
GALLERY_VIEW_TIMEOUT_SECONDS = 180  # a stale pager after this just stops responding

# bumped on every deploy so `.card version` can confirm the new build is live
COG_VERSION = "2.1.1"
