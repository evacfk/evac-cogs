"""Config defaults and lookup tables for hungergames. No discord/redbot imports here."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.2.3"
CONFIG_IDENTIFIER = 0x48474D31
HOME_TZ = ZoneInfo("America/Los_Angeles")

STAFF_ROLE_IDS = (            # may open `.hg admin` (as may anyone with Manage Server, and the bot owner)
    1556159237451681803,      # Staff
    426696709780013066,       # Moderators
    723377047950590033,       # superpowers
)

TICK_SECONDS = 5              # the phase loop; also paces the live broadcast posts
POST_GAP = (180, 300)         # fallback gap for a broadcast saved before adaptive pacing (1.1.0)
BROADCAST_SECONDS = 1800       # a normal night runs about 30 minutes ...
FEAST_SECONDS = 2700           # ... the Feast night about 45 ...
FINALE_SECONDS = 1800         # ... and the Finale about 30 (a quiet one ends sooner: gaps stop at GAP_MAX)
GAP_MIN, GAP_MAX = 45, 360    # seconds between two story lines, whatever the line count
SCENE_GAP = (20, 40)          # a scene header to its first line, the last line to the cannons, the cannons to the Crowning
TEST_POST_GAP = (2, 4)        # test runs post everything fast
CANNON_BEAT = 4               # seconds between cannons when the fallen list is revealed one at a time
CANNON_BUDGET = 20            # ... never more than this long in total
BANNER_BASE = "https://raw.githubusercontent.com/evacfk/evac-cogs/main/hungergames/assets/banners"
NEXT_FLAVOR = (
    "The cameras swing toward the next tribute.",
    "The Gamemakers confer.",
    "Somewhere in the arena, a twig snaps.",
    "The Capitol holds its breath.",
    "The sponsors lean in.",
    "The arena is quiet. Too quiet.",
    "A hovercraft hums far overhead.",
    "The mockingjays have stopped singing.",
)

# --- the week (Pacific). day offsets are from the edition's Sunday ---------------------------------------
# (step, day offset, hour, minute)
SCHEDULE = (
    ("reaping", 0, 10, 0),
    ("second_reaping", 0, 21, 0),
    ("lock", 0, 23, 59),
    ("training", 1, 8, 0),
    ("training_close", 1, 23, 59),
    ("scores", 2, 12, 0),
    ("broadcast", 2, 19, 0),
    ("broadcast", 3, 19, 0),
    ("broadcast", 4, 19, 0),
    ("broadcast", 5, 19, 0),
    ("finale", 6, 19, 0),
)
# test mode: seconds after the start (the training window is long enough to actually play)
TEST_SCHEDULE = (
    ("reaping", 0), ("second_reaping", 45), ("lock", 75), ("training", 80), ("training_close", 380),
    ("scores", 390), ("broadcast", 480), ("broadcast", 600), ("broadcast", 720), ("broadcast", 840), ("finale", 960),
)
BROADCASTS = 4

PHASE_LABEL = {
    "idle": "Waiting for the next Reaping",
    "reaping": "The Reaping is open",
    "second_reaping": "Second Reaping: last call for volunteers",
    "lock": "Roster locked",
    "training": "Training day",
    "training_close": "Training closed",
    "scores": "Sponsorship is open",
    "broadcast": "The Games are on",
    "finale": "The Finale",
    "complete": "A Victor has been crowned",
}

# --- roster ---------------------------------------------------------------------------------------------
DRAW_AT_REAPING = 4
TOPUP_TARGET = 8
MAX_TRIBUTES = 12
MIN_TRIBUTES = 4
MIN_MESSAGES_7D = 20
TICKET_CAP = 200              # tickets = sqrt(min(messages, cap))

# --- training -------------------------------------------------------------------------------------------
CHALLENGES = ("agility", "strength", "endurance", "intellect")
CHALLENGE_LABEL = {
    "agility": ("Sprint to the Cornucopia", "🏃"),
    "strength": ("Combat Trainer", "🥊"),
    "endurance": ("Memory March", "🧠"),
    "intellect": ("Capitol Broadcast", "📡"),
}
ACCURACY_MAX = 70
SPEED_MAX = 30
AGILITY_STEPS = 6
AGILITY_LIMIT = 45
AGILITY_MISS_COST = 10
STRENGTH_ROUNDS = 8
STRENGTH_LIMIT = 60
ENDURANCE_START = 3
ENDURANCE_MAX = 12
ENDURANCE_SHOW_SECONDS = 3
ENDURANCE_IDLE_LIMIT = 30
INTELLECT_PUZZLES = 3
INTELLECT_LIMIT = 180
INTELLECT_ATTEMPTS = 2
INTERVIEW_MAX_CHARS = 300

# --- arena ----------------------------------------------------------------------------------------------
SOFT_CAP_FLOOR = 15
SOFT_CAP_CEILING = 90
HAZARD_BASE = 40
HAZARD_STAT_WEIGHT = 0.3
POWER_RANDOM = 60
CHARISMA_GIFT_MAX = 0.15
FORAGE_FIND_CHANCE = 0.25
BETRAYAL_CHANCE = 0.15
BETRAYAL_ALIVE = 4            # allies can turn on each other once this few remain
MAX_ALLIANCE = 3
BLOODBATH_SHARE = (0.25, 0.35)
FINALE_TARGET = 3             # aim to enter the finale with 2-3 alive
STRATEGIES = {
    "hunt": ("Hunt", "🏹", "+10 in fights, more likely to find one"),
    "hide": ("Hide", "🌿", "+10 against hazards, fewer fights (the default)"),
    "forage": ("Forage", "🍄", "25% chance to find a supply, more exposure to hazards"),
}
HUNT_BONUS = 10
HIDE_BONUS = 10
UNUSED_ITEM_FINALE_BONUS = 10

# --- shop -----------------------------------------------------------------------------------------------
# key -> name, emoji, base price, kind, effect text
ITEMS = {
    "water": ("Purified Water Flask", "💧", 1500, "consumable", "saves from one heat or dehydration hazard"),
    "bread": ("District 11 Bread", "🍞", 2500, "consumable", "+12 on the next hazard check"),
    "stew": ("Hot Lamb Stew", "🍲", 3500, "consumable", "+15 in the next fight"),
    "serum": ("Calming Sleep Serum", "💤", 6000, "consumable", "saves from one night, fog or hallucination hazard"),
    "burn_cream": ("Specialized Burn Salve", "🧴", 10000, "consumable", "saves from one fire hazard"),
    "med_kit": ("First-Aid Kit & Antivenom", "🩹", 20000, "consumable", "70% chance to survive any one death"),
    "survival_kit": ("Spiles, Flint & Rafter Kit", "🧰", 50000, "gear", "+20 against every hazard"),
    "weapon": ("Tempered Machete / Trident", "🗡️", 100000, "gear", "+30 in fights"),
    "armor": ("Thermal Gear & Combat Boots", "🛡️", 100000, "gear", "+25 in fights, +10 against hazards"),
}
CONSUMABLES = tuple(k for k, v in ITEMS.items() if v[3] == "consumable")
GEAR = tuple(k for k, v in ITEMS.items() if v[3] == "gear")
GIFT_ITEMS = ("water", "bread", "stew", "serum", "burn_cream")   # what the Capitol / foraging can hand out
# which consumable fully saves from which hazard category
HAZARD_SAVE = {"fire": "burn_cream", "heat": "water", "night": "serum"}
MED_KIT_SAVE = 0.70
GEAR_COMBAT = {"weapon": 30, "armor": 25}
GEAR_HAZARD = {"survival_kit": 20, "armor": 10}
STEW_BONUS = 15
BREAD_BONUS = 12
# local weekday (Mon=0) -> price multiplier; anything else is 1.0
DAY_MULTIPLIER = {1: 1.0, 2: 1.15, 3: 1.3, 4: 1.4, 5: 1.5}
PARACHUTES_PER_DAY = 3
PRIZE_SHARE = 0.15
PRIZE_FLOOR = 10000

# --- Config ---------------------------------------------------------------------------------------------
DEFAULT_GUILD = {
    "enabled": False,
    "channel_id": 1573436403134505040,
    "ping_role_id": 1573436099127283955,
    "victor_role_id": 1573436201820618823,
    "teaser_channel_id": None,        # #cuddle one-liners (Reaping open, Victor crowned); None = off
    "test_channel_id": None,          # owner-only channel for test mode
    "prize_share": PRIZE_SHARE,
    "prize_floor": PRIZE_FLOOR,
    "prices": {k: v[2] for k, v in ITEMS.items()},
    "signup_message_id": None,        # the pinned sign-up post (pings / never draft me)
    "edition_number": 0,              # the last real edition that ran
    "current": {},                    # the live edition, {} when none (Red can't merge a dict over a None default)
    "last_tributes": [],              # previous edition's tributes: never drafted twice in a row
    "last_puzzles": [],
    "last_crown": {},                 # the pinned Victor card of the last real edition ({channel, message}); unpinned at the next Reaping
    "last_templates": [],
    "hall": [],                       # past victors
    "reports": [],                    # usage counts per finished edition (newest last, capped)
}
DEFAULT_MEMBER = {
    "opt_out": False,
    "editions": 0,
    "wins": 0,
    "kills": 0,
    "deaths": 0,
    "best_score": 0,
    "drafted": 0,
    "volunteered": 0,
    "items_sent": 0,
    "coins_spent": 0,
}
REPORTS_KEPT = 12

# --- custom ids (fixed, so every button keeps working after a restart) ---------------------------------
BTN = {
    "volunteer": "hg:volunteer",
    "withdraw": "hg:withdraw",
    "keep": "hg:keep",
    "optout": "hg:optout",
    "pings": "hg:pings",
    "swap_accept": "hg:swap_accept",
    "swap_keep": "hg:swap_keep",
    "train": "hg:train:",          # + challenge key or "interview"
    "cheer": "hg:cheer",
    "sponsor": "hg:sponsor",
    "plan": "hg:plan",
    "ally_yes": "hg:ally_yes",
    "ally_no": "hg:ally_no",
}
