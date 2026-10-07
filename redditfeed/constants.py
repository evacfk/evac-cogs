"""Config defaults and lookup tables for redditfeed. No discord/redbot imports."""

# -- Config defaults -----------------------------------------------------

DEFAULT_POLL_INTERVAL_SECONDS = 120
DEFAULT_STAGGER_SECONDS = 1.5
DEFAULT_FETCH_LIMIT = 25           # posts fetched per subreddit per poll
DEFAULT_DEDUP_TTL_DAYS = 7

MIN_POLL_INTERVAL_SECONDS = 30      # floor so a typo can't hammer the source
MIN_STAGGER_SECONDS = 0.0
MAX_STAGGER_SECONDS = 30.0

# -- Keyword filter modes -------------------------------------------------

KEYWORD_MODE_REQUIRE = "require"
KEYWORD_MODE_BLOCK = "block"
KEYWORD_MODES = (KEYWORD_MODE_REQUIRE, KEYWORD_MODE_BLOCK)

# -- Media classification --------------------------------------------------

MEDIA_KIND_IMAGE = "image"              # direct image, posted as an embed/attachment
MEDIA_KIND_GALLERY_IMAGE = "gallery_image"
MEDIA_KIND_VIDEO_LINK = "video_link"    # reddit-hosted video, posted as a link
MEDIA_KIND_REDGIFS_LINK = "redgifs_link"

IMAGE_URL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

REDGIFS_DOMAINS = ("redgifs.com", "www.redgifs.com")

IMAGE_HOST_DOMAINS = ("i.redd.it", "i.imgur.com", "imgur.com")

# -- Arctic Shift data source ----------------------------------------------

ARCTIC_SHIFT_BASE_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCTIC_SHIFT_TIMEOUT_SECONDS = 15
ARCTIC_SHIFT_MAX_RETRIES = 2
ARCTIC_SHIFT_RETRY_BACKOFF_SECONDS = 2.0
