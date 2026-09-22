"""Static configuration defaults and lookup tables for the photodrop cog."""

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
POLL_LETTERS = "ABCDEFGHIJ"  # Discord native polls cap at 10 answers

STATUS_FULL = "full"
STATUS_TARDY = "tardy"
STATUS_NO_SHOW = "no_show"
STATUS_PTO = "pto"

STATUS_EMOJI = {
    STATUS_FULL: "\N{CAMERA WITH FLASH}",
    STATUS_TARDY: "\N{ALARM CLOCK}",
    STATUS_NO_SHOW: "\N{CROSS MARK}",
    STATUS_PTO: "\N{DECIDUOUS TREE}",
}

RATING_GOAT = "goat"
RATING_GOOD = "good"
RATING_MID = "mid"
RATING_BAD = "bad"
VALID_RATINGS = (RATING_GOAT, RATING_GOOD, RATING_MID, RATING_BAD)

RATING_LABELS = {
    RATING_GOAT: "\N{GOAT} GOAT",
    RATING_GOOD: "\N{WHITE HEAVY CHECK MARK} Good",
    RATING_MID: "\N{NEUTRAL FACE} Mid",
    RATING_BAD: "\N{THUMBS DOWN SIGN} Bad",
}

DEFAULT_QUOTA = 3
DEFAULT_STRIKE_WINDOW_DAYS = 30
DEFAULT_STRIKE_THRESHOLD = 1  # a single Tardy or no-show revokes the role
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_POLL_DURATION_HOURS = 24
POLL_CLOSE_GRACE_MINUTES = 5  # wait this long past expiry before reading results, so Discord has finalized tallies
DEFAULT_WEEKLY_POLL_WEEKDAY = 6  # Sunday, per date.weekday() (Mon=0 .. Sun=6)
DEFAULT_WEEKLY_POLL_HOUR = 18  # 6pm local, "Sunday evening"
DEFAULT_JOB_ROLE_ID = 1546643582633377832  # hardcoded default; changeable via `.pp set role`
DEFAULT_REMINDER_HOURS_BEFORE = 1  # ping the role this many hours before local midnight if anyone's still missing

DEFAULT_MEMBER = {
    "streak": 0,
    "strikes": [],  # list of ISO-8601 UTC timestamps, pruned to the rolling window on read
    "history": {
        # "YYYY-MM-DD" -> {"status": one of STATUS_*, "photo_paths": [...], "ratings": {"0": one of RATING_*, ...}}
    },
}

DEFAULT_GUILD = {
    "quota": DEFAULT_QUOTA,
    "channel_id": None,
    "job_role_id": DEFAULT_JOB_ROLE_ID,
    "strike_window_days": DEFAULT_STRIKE_WINDOW_DAYS,
    "strike_threshold": DEFAULT_STRIKE_THRESHOLD,
    "last_rollover_date": None,  # "YYYY-MM-DD", the last local date no-show detection ran for
    "last_weekly_poll_date": None,  # "YYYY-MM-DD" the automatic weekly poll last ran for
    "active_polls": {},  # message_id (str) -> poll bookkeeping dict, see PollRecord in models.py
    "rater_id": None,  # who can click rating buttons; falls back to the guild owner if unset
    "pending_ratings": {},  # message_id (str) -> {"member_id", "date_key", "photo_index"}
    "reminder_hours_before": DEFAULT_REMINDER_HOURS_BEFORE,
    "last_reminder_date": None,  # "YYYY-MM-DD" the end-of-day reminder last fired for
}

