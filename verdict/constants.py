"""Config defaults and fixed values for verdict (Daily Verdict)."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.6.0"
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
THREAD_ARCHIVE_MINUTES = 10080   # 7 days: Discord must never archive a thread before we lock it ourselves
THREAD_NAME_LIMIT = 100

TICK, CROSS = "\N{WHITE HEAVY CHECK MARK}", "\N{CROSS MARK}"

# ---- Acrophobia (the second round type) ----
ACRO_SUBMIT_SECONDS = 90
ACRO_VOTE_SECONDS = 45
ACRO_MIN_ENTRIES = 3          # fewer entries and the round is cancelled before voting
ACRO_MAX_ENTRIES = 20         # 4 rows of 5 vote buttons; row 5 holds the mod button
ACRO_MAX_ENTRY_LEN = 120
ACRO_LETTER_COUNTS = (3, 4, 5)
ACRO_THEME_CHANCE = 0.7       # share of rounds that get a theme (when the list isn't empty)
ACRO_EARLIEST_HOUR = 9        # Pacific start hours an automatic round may use...
ACRO_LATEST_HOUR = 18         # ...a round starting at 6pm is over well before 7pm
ACRO_MIN_GAP_HOURS = 3        # automatic rounds are spread out
ACRO_FALLBACK_HOURS = (12, 17)    # used when ServerPulse has no data yet
ACRO_LATE_GRACE_SECONDS = 25 * 60   # a planned slot missed by this much (bot down, round busy) is skipped
ACRO_PULSE_DAYS = 28
ACRO_PULSE_MIN_SAMPLES = 3    # same-weekday samples needed before a weekday's own hour profile is trusted
ACRO_HISTORY = 60
ACRO_REVIEW_DAYS = 21
ACRO_REVIEW_MIN_AVG = 4.0     # fewer entries per round than this at review time: propose retiring it
ACRO_MAX_RATE = 4             # rounds per day, upper bound for the setting
ACRO_MAX_PRIZE = 100_000
DEFAULT_THEMES = [
    "a pickup line", "a server rule we need", "a terrible excuse for being late", "a movie nobody asked for",
    "what the mods are secretly thinking", "a bad slogan for Wonderland", "a villain's dating profile",
    "the worst thing to say at a funeral", "a new game show", "the title of my autobiography",
    "a warning label", "what the cat is plotting", "an unhelpful tip", "a headline from 2050",
    "what my dog would say", "a conspiracy theory", "the secret ingredient", "a terrible band name",
    "a question for the group", "my superpower",
]

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
    "threads_enabled": True,      # a voters-only private discussion thread per question
    "thread_cleanup": [],         # thread ids we failed to lock; retried on the next post
    "history": [],                # closed questions, newest last: who played, who read the crowd, lone wolves
    # Acrophobia
    "acro_enabled": False,
    "acro_rounds_per_day": 2,
    "acro_prize": 2000,           # wondercoin to the winner (shared on a tie)
    "acro_daily_cap": 4000,       # most wondercoin the faucet pays out per Pacific day, manual rounds included
    "acro_themes": list(DEFAULT_THEMES),
    "acro_current": {},           # the open round; {} = none
    "acro_plan": {},              # today's automatic slots: {date, slots: [ts], done: [ts], hours, source}
    "acro_paid": {"date": "", "amount": 0},
    "acro_seq": 0,
    "acro_launch_ts": 0.0,        # first time it was switched on; the 3-week review counts from here
    "acro_history": [],           # one counts-only record per round (no entries, no authors)
    "acro_months": {},            # "YYYY-MM" -> {uid: wins}
    "acro_total_paid": 0,
}

DEFAULT_MEMBER = {
    "streak": 0,
    "best_streak": 0,
    "last_seq": 0,
    "played": 0,
    "correct": 0,
    "last_suggest_ts": 0.0,
    "acro_wins": 0,
    "acro_rounds": 0,             # rounds they put an entry in
}
