"""Config defaults and lookup tables for wonderpet. No discord/redbot imports here."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.1.0"
CONFIG_IDENTIFIER = 0x3E7E27B2
HOME_TZ = ZoneInfo("America/Los_Angeles")

STAFF_ROLE_IDS = (            # may run the staff commands
    1556159237451681803,      # Staff
    426696709780013066,       # Moderators
    723377047950590033,       # superpowers
)

TICK_SECONDS = 300
MAX_CATCHUP_HOURS = 6          # after a restart/outage the pet is only aged this much, never punished for downtime
RENDER_DELAY_SECONDS = 5       # button presses redraw the card at most this often

# --- meters (0-100), all drain slowly so a quiet weekend is survivable -------------------------
METERS = ("hunger", "happy", "clean")
DECAY_PER_HOUR = {"hunger": 2.5, "happy": 2.0, "clean": 1.5}
CARE_GAIN = 20                 # a free Feed / Play / Clean
TREAT_GAIN = 30

# --- neglect: how a pet can be lost, and how much warning there is -----------------------------
NEGLECT_AVG = 25               # average meter below this = neglected, the clock runs
RECOVER_AVG = 45               # at/above this the clock runs backwards
WORRIED_AVG = 40               # below this (or any meter under 20) the pet "needs help" (level 1)
LEVEL_HOURS = {2: 12, 3: 36, 4: 60}   # neglect hours at which warnings 2 (sick), 3 (critical), 4 (final) go out
GONE_HOURS = 72                # neglect hours at which the pet is lost

# --- growth ----------------------------------------------------------------------------------
CARE_FREE = 1                  # growth points for a free action
CARE_TREAT = 2                 # growth points for a treat
STAGES = {                     # stage -> (min hours in stage, care points needed in stage, next stage)
    "egg": (12, 6, "baby"),
    "baby": (7 * 24, 80, "teen"),     # 1.1.0: one free action a day (was three), so the point targets were cut to match
    "teen": (10 * 24, 200, "adult"),
}
ADULT_RETIRE_DAYS = 21         # an adult is sent off to the Pet Hall after this long, then a new egg arrives
NEW_EGG_DELAY_HOURS = 24

FORM_RADIANT_QUALITY = 70
FORM_HAPPY_QUALITY = 45

# --- treats (bought with wondercoins) ----------------------------------------------------------
# key -> (label, emoji, meters it fills, price multiplier). v1: treats are a coin sink with a bigger boost
# than the free actions; no other perk is tied to them yet.
TREATS = {
    "snack": ("Tasty snack", "\N{CUT OF MEAT}", ("hunger",), 1),
    "toy": ("Squeaky toy", "\N{TENNIS RACQUET AND BALL}", ("happy",), 1),
    "bubbles": ("Bubble bath", "\N{BUBBLES}", ("clean",), 1),
    "feast": ("Royal feast", "\N{GLOWING STAR}", METERS, 4),
}
DEFAULT_TREAT_PRICE = 10000
DEFAULT_TREAT_DAILY_CAP = 3

# --- looks (placeholders until the community's own art is uploaded with `.wonderpet art`) ----
STAGE_LABEL = {"egg": "Egg", "baby": "Baby", "teen": "Teen", "adult": "Adult"}
STAGE_EMOJI = {"egg": "\N{EGG}", "baby": "\N{HATCHING CHICK}", "teen": "\N{FRONT-FACING BABY CHICK}"}
FORMS = {   # key -> (label, placeholder emoji)
    "scruffy": ("Scruffy", "\N{RACCOON}"),
    "happy": ("Happy", "\N{DUCK}"),
    "radiant": ("Radiant", "\N{UNICORN FACE}"),
}
ART_SLOTS = ("egg", "baby", "teen", "scruffy", "happy", "radiant", "sick")
DEFAULT_NAMES = ("Bloop", "Mochi", "Pip", "Nugget", "Waffles", "Biscuit", "Pudding", "Sprout", "Tofu", "Gizmo")

BTN_FEED = "wonderpet:feed"
BTN_PLAY = "wonderpet:play"
BTN_CLEAN = "wonderpet:clean"
BTN_TREAT = "wonderpet:treat"

COLOR_OK = 0x2ECC71
COLOR_WARN = 0xF1C40F
COLOR_BAD = 0xE74C3C
COLOR_EGG = 0x9B59B6

DEFAULT_GUILD = {
    "channel_id": None,           # #cuddle
    "ping_role_id": None,         # pinged on the serious warnings (optional)
    "daily_hour": 9,              # Pacific hour the card is re-posted fresh
    "treat_price": DEFAULT_TREAT_PRICE,
    "treat_cap": DEFAULT_TREAT_DAILY_CAP,
    "pet": {},                    # current pet's state; {} = none (Red's Config can't merge a dict into a None default)
    "card_message_id": None,
    "daily_post_date": None,
    "next_egg_ts": None,          # when the next egg arrives after a pet is gone
    "week_carers": {},            # ISO week key -> {user id: actions}
    "history": [],                # past pets, newest last
    "next_pet_id": 1,
    "last_flavor_ts": 0.0,
}

DEFAULT_MEMBER = {
    "daily": {},                  # {"date": iso, "used": "feed"|"play"|"clean"|None, "treats": int}
}
