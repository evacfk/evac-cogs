"""Config defaults and lookup tables for tgfeed. No discord/redbot/telethon imports."""

BUILD = "tg-v1 (public forum media mirror)"

# -- Environment / files ---------------------------------------------------------
ENV_API_ID = "TG_API_ID"
ENV_API_HASH = "TG_API_HASH"
ENV_DATA_DIR = "TG_DATA_DIR"
DEFAULT_DATA_DIR = "/data/tgfeed"          # inside the red container; /data is the host bind mount
SESSION_BASENAME = "tgfeed"                # Telethon appends .session
POSTED_DB_NAME = "posted.db"
TMP_DIR_NAME = "tmp"

# -- Polling and Telegram-account safety -----------------------------------------
# Everything here is deliberately slow. Nothing is ever sent TO Telegram: the client
# only reads, never joins, never posts, never opens a login flow on its own.
DEFAULT_POLL_INTERVAL_SECONDS = 300
MIN_POLL_INTERVAL_SECONDS = 120
POLL_JITTER_FRACTION = 0.25                # each wait is interval +/- 25%
STARTUP_DELAY_RANGE = (20.0, 60.0)
TOPIC_STAGGER_RANGE = (3.0, 8.0)           # random pause between topics in a cycle
DEFAULT_FILE_GAP_MIN = 5.0                 # random pause between two downloads
DEFAULT_FILE_GAP_MAX = 12.0
MIN_FILE_GAP_SECONDS = 2.0
DEFAULT_MAX_FILES_PER_HOUR = 60
DEFAULT_MAX_FILES_PER_DAY = 500
MIN_FILES_PER_HOUR = 10                    # an album is up to 10 files; a lower cap could deadlock
MAX_UNITS_PER_TOPIC_PER_CYCLE = 10         # a unit is one photo/video or one whole album
SCAN_CEILING = 3000                        # most messages read in one scan (catch-up bound)
SCAN_WAIT_SECONDS = 1.5                    # Telethon's pause between history pages
ALBUM_SETTLE_SECONDS = 45                  # wait for an album's last file before posting it
MAX_UNIT_RETRIES = 3                       # then a poisoned unit is skipped, not retried forever

# Flood handling: any FloodWait pauses everything for its full length plus a margin.
# Three of them in a day switches the feed off until a mod resumes it.
FLOOD_MARGIN_SECONDS = 5
FLOOD_NOTIFY_THRESHOLD = 300
FLOOD_PAUSE_COUNT = 3
FLOOD_WINDOW_SECONDS = 24 * 3600

# -- Media -------------------------------------------------------------------------
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
GENERAL_TOPIC_ID = 1
MAX_FILES_PER_MESSAGE = 10
MAX_SOURCE_VIDEO_BYTES = 300 * 1024 * 1024  # never even download a source bigger than this
UPLOAD_MARGIN_BYTES = 512 * 1024
HARD_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_GUILD_UPLOAD_BYTES = 10 * 1024 * 1024
PLAYABLE_VIDEO_EXTS = (".mp4", ".mov", ".webm")   # Discord plays these inline; anything else is re-encoded
MIME_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpg",
    "video/3gpp": ".3gp",
}
DEFAULT_VIDEO_EXT = ".mp4"
PHOTO_EXT = ".jpg"

# -- ffmpeg shrinking ----------------------------------------------------------------
AUDIO_KBPS = 96
MIN_VIDEO_KBPS = 250                       # below this the result is unwatchable: skip instead
MAX_VIDEO_KBPS = 6000
SIZE_SAFETY_FACTORS = (0.88, 0.65)         # first attempt, then one tighter retry
TRANSCODE_TIMEOUT_SECONDS = 900

# -- Retention / UI ------------------------------------------------------------------
POSTED_RETENTION_DAYS = 180                # how long the Discord->Telegram takedown map is kept
FEED_X_ID = "tgfeed:x"
DEFAULT_LOG_CHANNEL_ID = 416660303741452299      # #mod-commands
CHANNEL_NAME_MAX = 90
