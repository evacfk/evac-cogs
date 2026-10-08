"""Config defaults and lookup tables for redditfeed. No discord/redbot imports."""

# -- Config defaults -----------------------------------------------------

DEFAULT_POLL_INTERVAL_SECONDS = 120
DEFAULT_STAGGER_SECONDS = 1.5
DEFAULT_FETCH_LIMIT = 25           # posts fetched per subreddit per poll
DEFAULT_DEDUP_TTL_DAYS = 7
# After failed polls the cursor stays put so missed posts are retried next cycle,
# but never looks further back than this (a long-dead subreddit shouldn't dump
# days-old posts when it recovers).
MAX_LOOKBACK_SECONDS = 3600

MIN_POLL_INTERVAL_SECONDS = 30      # floor so a typo can't hammer the source
MIN_STAGGER_SECONDS = 0.0
MAX_STAGGER_SECONDS = 30.0

# -- Keyword filter modes -------------------------------------------------

KEYWORD_MODE_REQUIRE = "require"
KEYWORD_MODE_BLOCK = "block"
KEYWORD_MODES = (KEYWORD_MODE_REQUIRE, KEYWORD_MODE_BLOCK)

# -- Dashboard page actions -----------------------------------------------

DASHBOARD_ACTION_PAUSE = "pause"
DASHBOARD_ACTION_RESUME = "resume"

# -- Media classification --------------------------------------------------

MEDIA_KIND_IMAGE = "image"              # direct image, posted as an embed/attachment
MEDIA_KIND_GALLERY_IMAGE = "gallery_image"
MEDIA_KIND_VIDEO_LINK = "video_link"    # reddit-hosted video, posted as a link
MEDIA_KIND_REDGIFS_LINK = "redgifs_link"

IMAGE_URL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

REDGIFS_DOMAINS = ("redgifs.com", "www.redgifs.com")

IMAGE_HOST_DOMAINS = ("i.redd.it", "i.imgur.com", "imgur.com")

# Discord allows at most 10 embeds per message. Embeds sharing the same
# embed.url (with no title set, so that url is never shown as visible text)
# get visually grouped into one tiled gallery by Discord's client -- this is
# what lets a multi-image gallery post land as one message instead of N.
MAX_EMBEDS_PER_MESSAGE = 10

# -- Arctic Shift data source ----------------------------------------------

ARCTIC_SHIFT_BASE_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCTIC_SHIFT_TIMEOUT_SECONDS = 15
ARCTIC_SHIFT_MAX_RETRIES = 2
ARCTIC_SHIFT_RETRY_BACKOFF_SECONDS = 2.0
ARCTIC_SHIFT_SUBREDDIT_SEARCH_URL = "https://arctic-shift.photon-reddit.com/api/subreddits/search"

# -- Subreddit discovery (`.redditfeed discover`) ----------------------------

DISCOVER_DEFAULT_MIN_SUBSCRIBERS = 5000
DISCOVER_SEARCH_LIMIT = 50           # subreddits requested per prefix
DISCOVER_MAX_PREFIXES = 5
DISCOVER_MAX_SUGGESTIONS = 8         # suggestion messages posted per run
DISCOVER_STAGGER_SECONDS = 1.5       # pause between Arctic Shift requests / suggestion posts
DISCOVER_VIEW_TIMEOUT_SECONDS = 6 * 3600   # Approve/Deny buttons expire after this

PREVIEW_WINDOW_SECONDS = 3 * 24 * 3600     # look at the last 3 days of posts
PREVIEW_FETCH_LIMIT = 100
PREVIEW_MAX_IMAGES = 4
PREVIEW_MAX_POST_LINKS = 3

# Coarse safety screen applied ONLY to discovery suggestions (subreddit name,
# description and recent post titles). It cannot see images -- the human
# preview + Approve/Deny step is the real control. Deliberately over-broad:
# a false positive just hides a suggestion (use `.redditfeed add` to override),
# a false negative would put something questionable in front of a mod.
SAFETY_BLOCKED_TERMS = (
    "teen", "young", "jailbait", "preteen", "pre-teen", "lolita", "loli", "shota",
    "child", "kid", "underage", "under-age", "minor", "schoolgirl", "school girl",
    "highschool", "high school", "toddler", "baby", "barely legal", "student",
    "cheerleader", "daughter", "niece", "little girl", "little boy",
)
