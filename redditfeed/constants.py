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
REDGIFS_UPLOAD = "upload"   # download the clip and attach it so Discord plays it
REDGIFS_LINK = "link"       # post the bare link (Discord shows no preview for these)

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

# -- Fallback sources (used when Arctic Shift is down) ------------------------

REDDIT_RSS_BASE_URL = "https://www.reddit.com"
PULLPUSH_BASE_URL = "https://api.pullpush.io/reddit/search/submission/"
# Browser-style on purpose: from the evacOVH datacenter IP, PullPush answers 403 to a custom
# bot UA and 429 (reachable) to this one, and Reddit RSS was only verified with this one.
FALLBACK_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
FALLBACK_TIMEOUT_SECONDS = 20
FALLBACK_MAX_RETRIES = 1
FALLBACK_RETRY_BACKOFF_SECONDS = 3.0
REDDIT_RSS_MIN_GAP_SECONDS = 7.0       # unauthenticated Reddit allows ~10 requests/minute
PRIMARY_COOLDOWN_SECONDS = 300          # after repeated primary failures, skip it this long

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


# -- Approval queue (manual mode) ---------------------------------------------------

APPROVAL_MANUAL = "manual"     # posts wait in the mod queue for Approve / Reject
APPROVAL_AUTO = "auto"         # posts go straight to the destination channel
APPROVAL_MODES = (APPROVAL_MANUAL, APPROVAL_AUTO)
DEFAULT_APPROVAL = APPROVAL_MANUAL

QUEUE_TTL_HOURS = 24           # an undecided item is discarded after this
QUEUE_KEEP_RESOLVED_HOURS = 48 # decided items stay in Config this long (then pruned)
DEFAULT_QUEUE_MAX_PENDING = 40 # stop queueing when this many are waiting
DEFAULT_QUEUE_MIN_SCORE = 3    # queue a post only once it has at least this score...
DEFAULT_QUEUE_MIN_AGE_MINUTES = 60   # ...and is at least this old (scores need time to settle)
QUEUE_LOOKBACK_HOURS = 12      # manual mappings look at posts from this far back
QUEUE_FETCH_LIMIT = 100
QUEUE_MAX_PER_SUB_PER_CYCLE = 5
QUEUE_MAX_PREVIEW_IMAGES = 4
QUEUE_POST_DELAY_SECONDS = 1.0

QUEUE_APPROVE_ID = "redditfeed:q:approve"
QUEUE_REJECT_ID = "redditfeed:q:reject"
QUEUE_PAUSE_ID = "redditfeed:q:pause"
FEED_X_ID = "redditfeed:x"

QUEUE_PENDING = "pending"
QUEUE_APPROVED = "approved"
QUEUE_REJECTED = "rejected"
QUEUE_EXPIRED = "expired"

DEFAULT_LOG_CHANNEL_ID = 416660303741452299   # #mod-commands
POSTED_MAP_TTL_DAYS = 7
POSTED_MAP_MAX = 5000

# -- Destination proposals for `discover` -------------------------------------------

# Words that say nothing about the topic; skipped when deriving a topic from a
# subreddit name. Deliberately NOT here: fetish, hentai, bdsm (those are topics).
TOPIC_NOISE = frozenset({
    "pics", "pic", "pictures", "porn", "nsfw", "gonewild", "gw", "xxx", "sex", "sexy",
    "hot", "gifs", "gif", "only", "real", "amateur", "best", "the", "and", "for", "nude",
    "nudes", "adult", "hd", "girls", "girl", "women", "woman", "babes", "club", "lovers",
    "love", "porno", "content", "daily", "place", "paradise", "heaven", "gone", "wild",
    "amateurs", "my", "your", "our", "in", "of", "on",
})
# Same topic, different spelling -> one canonical word to compare channel names with.
TOPIC_ALIASES = {
    "foot": "feet", "soles": "feet", "sole": "feet", "toes": "feet", "toe": "feet",
    "feetish": "feet", "footfetish": "feet", "feetpics": "feet",
    "anime": "2d", "ecchi": "2d", "hentai": "2d", "manga": "2d",
}
