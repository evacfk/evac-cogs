"""Config defaults and fixed values for secrethunt (Secrets of Wonderland)."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.8.2"
CONFIG_IDENTIFIER = 0x5EC8E7A1
HOME_TZ = ZoneInfo("America/Los_Angeles")

TICK_SECONDS = 60                  # how often the delivery loop looks for clues that have come due
WATCH_EMOJI = "\N{MANTELPIECE CLOCK}"   # the reaction that "clicks" a hunt object (compared without the VS16)

MIN_DELAY_HOURS = 4                # a clue never arrives sooner than this after the previous step...
UNLOCK_HOUR = 9                    # ...and always lands on the next 9:00 Pacific after that
TEST_DELAY_SECONDS = 30            # test mode: skip the wait
DM_RETRY_SECONDS = 3600            # a closed-DM clue is retried hourly...
MAX_DM_FAILS = 72                  # ...for about three days, then waits for the member to retrigger
MAX_WRONG_PER_DAY = 10
MAX_WRONG_REACTS = 2               # wrong hunt objects allowed before the Rabbit stops listening
REACT_LOCK_HOURS = 24              # ...for this long (test mode: TEST_DELAY_SECONDS)
POOL_MIN, POOL_MAX = 2, 5
ICON_MAX_BYTES = 256 * 1024
HALL_BUDGET = 3800                 # characters of solver names per Hall of Secrets post (embed limit is 4096)

TIER_ORDER = ("easy", "medium", "hard", "expert")
# Easy deliberately has no role or badge: solvers are only listed in the Hall of Secrets.
TIER_ROLE_NAMES = {"medium": "Secret Finder", "hard": "Timekeeper", "expert": "Eye of Wonder"}

DEFAULT_GUILD = {
    "enabled": False,
    "salt": "",              # secret mixed into the per-member variant hash; created on first use
    "pool": [],              # channel ids; variant i hides its object in pool[i]
    "published": [],         # puzzle ids whose trigger word is live (old ones never expire)
    "active": "",            # newest published puzzle (shown in status)
    "objects": {},           # puzzle id -> {channel id (str): pinned object message id}
    "finishers": {},         # puzzle id -> [user ids in finishing order]
    "roles": {}, "puzzle_roles": {},             # tier -> role id (Easy never has one)
    "announce_channel_id": None,  # where "X solved <puzzle>" is announced (e.g. the general chat)
    "hall_channel_id": None, # the Hall of Secrets: one post per puzzle listing its solvers
    "hall_messages": {}, "poems": {}, "hall_intro": 0,     # puzzle id -> that puzzle's Hall post
    "test_mode": False,
}

DEFAULT_MEMBER = {
    "puzzles": {},           # puzzle id -> state dict (see engine.new_state)
}
