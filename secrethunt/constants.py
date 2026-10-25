"""Config defaults and fixed values for secrethunt (Secrets of Wonderland)."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.14.1"
CONFIG_IDENTIFIER = 0x5EC8E7A1
HOME_TZ = ZoneInfo("America/Los_Angeles")

LAUNCH_WARN_SECONDS = 3600      # a scheduled launch that is not ready gets one warning DM this long before it is due
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
TRIAL_LOCK_R1_HOURS = 4            # The Knave's Trial: a wrong round 1 answer locks the member out this long, then re-rolls
TRIAL_LOCK_R2_HOURS = 4            # ...and a wrong round 2 answer cools down for the same time before its testimony re-rolls
HINT_FIRST_HOURS = 48              # a trial hint can be asked for this long after a round opens...
HINT_GAP_HOURS = 24                # ...and each further one this long after the previous
HINT_MAX = 3                       # per round
POOL_MIN, POOL_MAX = 2, 5
TRAIL_MIN_ROOMS, TRAIL_MAX_ROOMS = 6, 16   # The Treacle Well hides its tea tables in this many public rooms (12 planned)
TRAIL_REACT_WINDOW = 600           # Treacle Well stage 1: the three reactions must come within this many seconds
TRAIL_REACTS_PER_HOUR = 30         # ...and a member's reactions on the poem beyond this many per hour are ignored
TRAIL_WRONG_LOOKS = 3              # stage 2: this many wrong tables in a row and the tea tables rest...
TRAIL_REST_MINUTES = 30            # ...for this long (test mode: TEST_DELAY_SECONDS)
TRAIL_PROBES_PER_DAY = 30          # stage 3: words a member can ask the Dormouse about per Pacific day
TRAIL_PROBE_BURST = 10             # stage 3: after this many words in a row (per day) the Dormouse rests...
TRAIL_PROBE_PAUSE_MINUTES = 10     # ...for this long (test mode: TEST_DELAY_SECONDS)
TRAIL_MUCHNESS_REST_MINUTES = 15   # stage 3: a wrong test is followed by this long without a new one (fresh words after)
TRAIL_POUR_CAP = 99                # stage 4: the cup holds no more than this many pours
TRAIL_POUR_WRONGS = 3              # stage 4: this many wrong toasts in a row and the cups are washed...
TRAIL_POUR_REST_MINUTES = 60       # ...for this long (test mode: TEST_DELAY_SECONDS)
TRAIL_COURTESY_SLIPS = 5           # stage 5: this many wrong presses in a day and the tables close...
TRAIL_COURTESY_REST_MINUTES = 120  # ...for this long (test mode: TEST_DELAY_SECONDS)
TRAIL_ANSWER_REST_MINUTES = 60     # stage 6: a wrong answer costs this long (test mode: TEST_DELAY_SECONDS)
TRAIL_HINT_FIRST_HOURS = 24        # a stage's first hint comes this long after the stage opened...
TRAIL_HINT_GAP_HOURS = 12          # ...and the second this long after the first (test mode: TEST_DELAY_SECONDS)
TRAIL_HINTS_PER_STAGE = 2
ICON_MAX_BYTES = 256 * 1024
HALL_BUDGET = 3800                 # characters of solver names per Hall of Secrets post (embed limit is 4096)

TIER_ORDER = ("easy", "medium", "hard", "expert")
# Easy and Medium deliberately have no role or badge: their solvers are only listed in the Hall of Secrets.
NO_ROLE_TIERS = ("easy", "medium")
TIER_ROLE_NAMES = {"hard": "Timekeeper", "expert": "Eye of Wonder"}

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
    "poem_ids": {},          # poem message id (str) -> puzzle id, so a reaction on a poem can be told which puzzle it starts
    "trail": [],             # channel/forum-post ids that hold the Treacle Well's tea tables
    "test_channel_id": None, # while Test mode is on, every puzzle's poem and objects are posted here instead (a private channel)
    "test_objects": {},      # puzzle id -> {room id (str): message id} of the rehearsal copies of the objects (kept apart from `objects`)
    "test_poems": {},        # puzzle id -> message id of its rehearsal poem (kept out of `poems`: that feeds a public post)
    "poem_targets": {},      # puzzle id -> the channel its real poem goes to (remembered even when a rehearsal took the post)
    "blocked_role_id": None, # members with this role (Lurker) are ignored by every puzzle
    "test_mode": False,
    "click_log": [],         # the last few button clicks (tea table, hunt object, witness) that got no answer: [time, why, user, channel]
    "press_log": [],         # TEMPORARY diagnostics: the last dozen button presses that reached the bot: [time, what, user, channel, message, custom id]
    "launches": {},          # puzzle id -> {at, channel, by, state: pending|running|done|failed, note, warned}: a scheduled go-live + poem
}

DEFAULT_MEMBER = {
    "puzzles": {},           # puzzle id -> state dict (see engine.new_state)
    "presses": [],           # the last few tea table presses: [time, room id, button, what happened] (shown by `who`)
}
