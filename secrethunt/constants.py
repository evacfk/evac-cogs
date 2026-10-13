"""Config defaults and fixed values for secrethunt (Secrets of Wonderland)."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.0.0"
CONFIG_IDENTIFIER = 0x5EC8E7A1
HOME_TZ = ZoneInfo("America/Los_Angeles")

TICK_SECONDS = 60                  # how often the delivery loop looks for clues that have come due
WATCH_EMOJI = "\N{MANTELPIECE CLOCK}"   # the reaction that "clicks" a hunt object (compared without the VS16)

MIN_DELAY_HOURS = 12               # a clue never arrives sooner than this after the previous step...
UNLOCK_HOUR = 9                    # ...and always lands on the next 9:00 Pacific after that
TEST_DELAY_SECONDS = 30            # test mode: skip the wait
DM_RETRY_SECONDS = 3600            # a closed-DM clue is retried hourly...
MAX_DM_FAILS = 72                  # ...for about three days, then waits for the member to retrigger
MAX_WRONG_PER_DAY = 10
MAX_DECOY_REPLIES = 3
POOL_MIN, POOL_MAX = 2, 5
DEFAULT_ROLE_NAME = "Secret Finder"
DEFAULT_ROLE_COLOR = 0xF1C40F
ICON_MAX_BYTES = 256 * 1024

DEFAULT_GUILD = {
    "enabled": False,
    "salt": "",              # secret mixed into the per-member variant hash; created on first use
    "pool": [],              # channel ids; variant i hides its object in pool[i]
    "published": [],         # puzzle ids whose trigger word is live (old ones never expire)
    "active": "",            # newest published puzzle (shown in status)
    "objects": {},           # puzzle id -> {channel id (str): pinned object message id}
    "finishers": {},         # puzzle id -> [user ids in finishing order]
    "role_id": None,
    "hall_channel_id": None,
    "test_mode": False,
}

DEFAULT_MEMBER = {
    "puzzles": {},           # puzzle id -> state dict (see engine.new_state)
}
