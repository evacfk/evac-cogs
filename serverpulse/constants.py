"""All defaults, thresholds and lookup tables for serverpulse."""
from __future__ import annotations

from zoneinfo import ZoneInfo

COG_VERSION = "1.4.0"
COG_BUILD = "2026-10-13"

TIMEZONE_NAME = "America/Los_Angeles"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

CONFIG_IDENTIFIER = 0x5E4B5075  # arbitrary, unique to this cog

# -- hardcoded defaults (confirm / change with `.pulse set ...`) ----------------
DEFAULT_MOD_CHANNEL_ID = 912520841613418596
DEFAULT_MOD_ROLE_ID = 426696709780013066
DEFAULT_IGNORED_CHANNEL_IDS = [1546909458582474772, 1530348323330986005]  # bot channels

# -- tracking ---------------------------------------------------------------
CONCURRENCY_WINDOW_SECONDS = 300  # "peak concurrent chatters" = distinct authors in a rolling 5 minutes
VALID_MESSAGE_TYPES = frozenset({"default", "reply"})
USER_RETENTION_DAYS = 90  # per-user daily counts (top chatters board) are dropped after this
FLUSH_INTERVAL_SECONDS = 120
BOARD_INTERVAL_MINUTES = 5
DIGEST_CHECK_MINUTES = 5
DIGEST_HOUR = 9  # local hour weekly/monthly digests are due (Mon / 1st)

# -- reporting --------------------------------------------------------------
DEFAULT_WINDOW_DAYS = 28  # default look-back for hour-of-day averages / heatmap
BOARD_HISTORY_DAYS = 56  # look-back used by the live board for "usual" levels
BEST_WINDOW_HOURS = 2
BEST_WINDOW_TOP_N = 3
# weights for the "how alive is this hour" score (each metric normalised 0..1 first)
SCORE_WEIGHT_PEAK = 0.5
SCORE_WEIGHT_CHATTERS = 0.3
SCORE_WEIGHT_MESSAGES = 0.2

# -- anomalies --------------------------------------------------------------
ANOMALY_BASELINE_WEEKS = 8
ANOMALY_MIN_SAMPLES = 3
DAY_HIGH_RATIO = 1.5
DAY_LOW_RATIO = 0.5
DAY_MIN_DELTA = 50  # messages; ignore swings smaller than this
HOUR_HIGH_RATIO = 2.5
HOUR_HIGH_MIN_DELTA = 20
HOUR_LOW_RATIO = 0.25
HOUR_LOW_MIN_EXPECTED = 20  # only call an hour "dead" if it is normally at least this busy
ANOMALY_REPORT_LIMIT = 4

# -- rendering --------------------------------------------------------------
COLOR_PRIMARY = 0x5865F2
COLOR_GOOD = 0x2ECC71
COLOR_WARN = 0xF1C40F
COLOR_QUIET = 0x3498DB
COLOR_BAD = 0xE74C3C

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
SPARK_CHARS = "▁▂▃▄▅▆▇█"

# -- weekly recap (1.4.0) ----------------------------------------------------
RECAP_MODEL = "claude-sonnet-5-5"
RECAP_MAX_TOKENS = 4000  # a ceiling, not a target: a draft is ~700 tokens; the rest is headroom for model thinking
RECAP_MAX_DRAFTS_WEEK = 6  # drafts + regenerations in a rolling 7 days; the cost cap
RECAP_MIN_REACTIONS = 3
RECAP_HIGHLIGHTS = 8  # most-reacted messages handed to Claude
RECAP_PER_AUTHOR = 2
RECAP_SNIPPET_CHARS = 280  # per highlight, what Claude sees
RECAP_MOMENT_CHARS = 110  # per highlight, what is quoted in the public post
RECAP_MOMENTS = 3  # "Top moments" lines in the public post
RECAP_MAX_SCAN = 60000  # messages read per draft; a safety stop, not an expected size
RECAP_VOICE_MAX = 6000
RECAP_PUBLIC_MAX = 2500  # the public text; keeps the whole draft embed under Discord's 6000-character limit
RECAP_MODS_MAX = 1000  # one embed field
RECAP_PENDING_MAX = 5
RECAP_API_URL = "https://api.anthropic.com/v1/messages"
RECAP_API_VERSION = "2023-06-01"
RECAP_API_TIMEOUT = 150
RECAP_DEFAULT_VOICE = (
    "Warm, playful and community-first. Short sentences, a little humour, a few emoji. "
    "Speaks as the owner (\"we\", \"our\"), never like a press release."
)

# -- Config defaults --------------------------------------------------------
DEFAULT_GUILD = {
    "ignored_channels": list(DEFAULT_IGNORED_CHANNEL_IDS),
    "mod_channel_id": DEFAULT_MOD_CHANNEL_ID,
    "mod_role_id": DEFAULT_MOD_ROLE_ID,
    "exclude_commands": True,
    "live_since": None,  # epoch seconds, hour-aligned: first moment live tracking counts messages
    "coverage_start": None,  # epoch seconds: earliest moment with complete data (live or backfilled)
    "board": {"channel_id": None, "message_id": None},
    "digest_weekly": {"enabled": True, "last": ""},
    "digest_monthly": {"enabled": True, "last": ""},
    "last_purge": "",
    "backfill": {},
    # -- new-member retention (1.3.0) --
    # invite code -> label; joins through any other invite are labelled by its creator
    "invite_sources": {"etywguQBWq": "Disboard", "829baEszgg": "discord.me"},
    "joinlog": {"join_channel": None, "leave_channel": None},
    # -- weekly recap drafts (1.4.0), see recap.py ----------------------------
    "recap": {
        "source_channel": None,   # where the week's highlights come from; None = a channel named "cuddle"
        "post_channel": None,     # where an approved recap is posted (must be set before Approve works)
        "voice": "",              # the owner's style notes + past announcements, used to write in their voice
        "notes": "",              # what happened this week (events etc.), used once by the next draft then cleared
        "model": RECAP_MODEL,
        "min_reactions": RECAP_MIN_REACTIONS,
        "max_drafts_week": RECAP_MAX_DRAFTS_WEEK,
        "excluded_users": [],     # members whose messages are never read or highlighted
        "auto": False,            # draft one every Monday after the weekly digest
        "last": "",               # week key of the last automatic draft (at-most-once)
        "usage": [],              # [[timestamp, input tokens, output tokens], ...] last 30 days
        "pending": {},            # draft message id -> draft awaiting a decision
    },
}

COHORT_FLUSH_SECONDS = 600  # join records are rewritten at most this often (plus on unload)
RETENTION_DEFAULT_DAYS = 120
