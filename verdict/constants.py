"""Config defaults and fixed values for verdict (Daily Verdict)."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.1.0"
CONFIG_IDENTIFIER = 0x7E4D1C70
HOME_TZ = ZoneInfo("America/Los_Angeles")

STAFF_ROLE_IDS = (            # may add/queue/remove questions (config stays admin-only)
    1556159237451681803,      # Staff
    426696709780013066,       # Moderators
    723377047950590033,       # superpowers
)

TICK_SECONDS = 60
EARLY_MANUAL_HOURS = 6        # a manual post this many hours (or more) before the posting hour does not use up the day
DEFAULT_POST_HOUR = 10        # Pacific; before chat dies for Europe, after the US wakes up
MIN_OPTIONS, MAX_OPTIONS = 2, 4
MAX_QUESTION_LEN, MAX_OPTION_LEN = 200, 40
MAX_PENDING_PER_USER = 3
SUGGEST_COOLDOWN_SECONDS = 600
LONE_WOLF_MAX_SHARE = 0.20    # picked by 20% or fewer...
LONE_WOLF_MIN_VOTES = 5       # ...and at least this many people voted
MIND_READER_MIN_PLAYED = 5    # to be eligible for the monthly title
STREAK_MILESTONES = (7, 30, 100, 365)
KEEP_MONTHS = 3
KEEP_HISTORY = 60             # closed questions whose participant lists are kept
BAR_WIDTH = 10

TICK, CROSS = "\N{WHITE HEAVY CHECK MARK}", "\N{CROSS MARK}"

DEFAULT_GUILD = {
    "enabled": False,
    "channel_id": None,           # where the daily question is posted (#cuddle)
    "review_channel_id": None,    # private channel where member submissions wait for approval
    "approver_ids": [],           # extra users whose tick/cross counts (the bot owner always counts)
    "post_hour": DEFAULT_POST_HOUR,
    "ping_role_id": None,         # optional role pinged on the daily post
    "mind_reader_role_id": None,  # monthly title
    "queue": [],                  # approved questions waiting their turn: {question, options, submitter_id}
    "pending": {},                # review message id -> {question, options, submitter_id}
    "current": {},                # the open question (see verdict.py); {} = none. Not None: Red's Config
                                  # cannot merge a stored dict into a None default
    "seq": 0,                     # how many questions have been posted
    "last_post_date": "",         # Pacific date of the last post
    "used_seeds": [],
    "months": {},                 # "YYYY-MM" -> {uid: [correct, played]}
    "last_award_month": "",
    "mind_reader_holder": None,
    "history": [],                # closed questions, newest last: who played, who read the crowd, lone wolves
}

DEFAULT_MEMBER = {
    "streak": 0,
    "best_streak": 0,
    "last_seq": 0,
    "played": 0,
    "correct": 0,
    "last_suggest_ts": 0.0,
}
