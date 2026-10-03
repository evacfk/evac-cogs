"""All defaults, thresholds and lookup tables for serverpulse."""
from __future__ import annotations

from zoneinfo import ZoneInfo

COG_VERSION = "1.2.0"
COG_BUILD = "2026-10-02"

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
}
