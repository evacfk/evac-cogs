"""Config defaults and fixed values for contest (Wonderween 2026, the Halloween costume contest)."""
from datetime import datetime
from zoneinfo import ZoneInfo

COG_VERSION = "1.0.0"
CONFIG_IDENTIFIER = 0x5C0A7E57
HOME_TZ = ZoneInfo("America/Los_Angeles")

CONTEST_NAME = "Wonderween 2026"
CHANNEL_NAME = "wonderween-costumes"
BRONZE_ROLE_NAME = "Bronze Pumpkin 2026"
GOLD_ROLE_NAME = "Golden Pumpkin 2026"
BRONZE_COLOR = 0xCD7F32
GOLD_COLOR = 0xFFD700

STAFF_ROLE_IDS = (            # may approve entries (the bot owner always may)
    1556159237451681803,      # Staff
    426696709780013066,       # Moderators
    723377047950590033,       # superpowers
)

TICK_SECONDS = 60
MAX_PHOTOS = 2
MAX_TITLE_LEN = 60
SESSION_SECONDS = 1800        # how long a DM entry stays open before the member must press Enter again
PHOTO_MAX_SIDE = 1600
PHOTO_QUALITY = 88
MIN_VOTE_LEVEL = 1
PLACES = 3                    # 1st, 2nd, 3rd in each of the two awards
TIER_GOLD, TIER_SECOND, TIER_THIRD, TIER_PARTICIPANT = 1, 2, 3, 4
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

REJECT_REASONS = (
    ("no_note", "No username + Wonderland note in the photos"),
    ("not_costume", "Not a costume"),
    ("not_you", "Not you / not your photo"),
    ("nsfw", "Not safe for work"),
    ("other", "Other (a mod will explain)"),
)


def _pt(y, m, d, h=0, mi=0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=HOME_TZ).timestamp()


DEFAULT_SCHEDULE = {
    "open_ts": _pt(2026, 10, 26),                 # channel becomes visible, voting starts
    "entries_close_ts": _pt(2026, 10, 31, 23, 0),
    "voting_close_ts": _pt(2026, 11, 1, 0, 0),    # end of Oct 31
    "reveal_ts": _pt(2026, 11, 1, 12, 0),
    "purge_ts": _pt(2026, 11, 10, 12, 0),
}

DEFAULT_GUILD = {
    "launched": False,
    "opened": False,              # the channel has been made visible
    "revealed": False,
    "purged": False,
    "hold_purge": False,
    "approval_channel_id": None,  # private channel where entries wait for a mod
    "announce_channel_id": None,  # where the announcement + Enter button is posted
    "results_channel_id": None,   # where the text-only winners post goes
    "category_id": None,          # category for the contest channel (blank = the announcement channel's category)
    "contest_channel_id": None,
    "announce_message_id": None,
    "status_message_id": None,
    "status_date": "",
    "excluded_role_ids": [],      # roles that cannot see the channel, enter or vote (Lurker, 13-17, Muted)
    "bronze_role_id": None,
    "gold_role_id": None,
    "coins_participant": 0,
    "coins_third": 0,
    "coins_second": 0,
    "coins_gold": 0,
    "shop_item": "",
    **DEFAULT_SCHEDULE,
    "entries": {},                # str(user_id) -> entry (see contest.py)
    "votes": {},                  # str(voter_id) -> {"YYYY-MM-DD": costume number}
    "mods_picks": [None, None, None],   # Mods' Choice: costume numbers for 1st, 2nd, 3rd
    "ledger": [],                 # paid out at the reveal: {user_id, tier, coins, ts, skipped}
    "notified": [],               # one-shot alerts already sent
}
