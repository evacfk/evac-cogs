"""Hunt "offering" animals -- safe animals you give something to instead of
saluting (a crow wants a shiny coin, a mouse wants cheese).

An offering animal is an ordinary `safe_animals` entry (safe: True, with its
own `safe_word` and penalty/reward) that also carries a `success_text`
template. When that's set, hunt posts the template on success instead of the
generic "saluted the <animal>" line, and drops the salute reaction for that
animal, since you can't salute a crow into handing over coins.

Kept free of discord/redbot imports so it's unit-testable on its own.
"""
import copy

# Placeholders a success_text template may use. Replaced with plain
# str.replace rather than str.format so a stray "{" or "}" in an admin-typed
# template can't raise.
PLACEHOLDERS = ("{user}", "{animal}", "{amount}", "{currency}")

MAX_TEMPLATE_LEN = 300

# Used when an offering animal's reward rolls to 0 (daily payout cap reached,
# or a 0 reward range) -- "gives you 0 wondercoins" reads badly.
ZERO_REWARD_TEXT = "The {animal} liked {user}'s offering, but has no {currency} to spare right now!"

CROW_TEXT = (
    "\U0001FA99 The crow snatches the shiny coin from {user}, loves the shiny, "
    "and gives them {amount} {currency} in return!"
)
MOUSE_TEXT = (
    "\U0001F9C0 The mouse nibbles the cheese {user} offered, squeaks happily, "
    "and gives them {amount} {currency}!"
)

# One-time seed (see seed_hunt_offerings). `animal` entries are only added if
# the guild doesn't already have that key; `text` entries are only added to an
# existing safe animal that has no success_text yet.
MOUSE_ANIMAL = {
    "emoji": "\U0001F401",
    "text": "**_Squeak!_** *sniffs the air hopefully... it sure would love some \U0001F9C0*",
}
# The mouse is fed by reacting with the cheese emoji, not by typing a word.
MOUSE_SAFE_ENTRY = {
    "safe": True,
    "penalty_pct": 5.0,
    "reward_range": [50, 200],
    "safe_reaction": "\U0001F9C0",
    "success_text": MOUSE_TEXT,
}
# First build seeded the mouse as a typed "cheese" word; fix_mouse_reaction
# converts exactly that shape.
_OLD_MOUSE_TEXT = "**_Squeak!_** *sniffs the air hopefully... it sure would love some cheese.*"


def render(template: str, *, user: str, animal: str, amount: int, currency: str) -> str:
    """Fill a success_text template. Unknown {placeholders} are left as-is."""
    return (
        template.replace("{user}", user)
        .replace("{animal}", animal)
        .replace("{amount}", f"{amount:,}")
        .replace("{currency}", currency)
    )


def offering_text(animal_conf) -> str:
    """The animal's success_text if it's a safe offering animal, else ''."""
    if not animal_conf or not animal_conf.get("safe", True):
        return ""
    return (animal_conf.get("success_text") or "").strip()


def fix_mouse_reaction(hunt_conf: dict) -> bool:
    """Convert a mouse seeded by the first build (safe word "cheese") to the
    cheese-emoji reaction. Only touches that exact shape, so an admin's own
    mouse settings are left alone. Returns True if anything changed."""
    entry = (hunt_conf.get("safe_animals") or {}).get("mouse")
    if not entry or entry.get("safe_word") != "cheese" or entry.get("safe_reaction"):
        return False
    entry.pop("safe_word", None)
    entry["safe_reaction"] = MOUSE_SAFE_ENTRY["safe_reaction"]
    mouse = (hunt_conf.get("animals") or {}).get("mouse")
    if mouse and mouse.get("text") == _OLD_MOUSE_TEXT:
        mouse["text"] = MOUSE_ANIMAL["text"]
    return True


def seed_hunt_offerings(hunt_conf: dict) -> bool:
    """Add the mouse and give the crow its shiny-coin message, once per guild.

    Guarded by hunt_conf["offerings_seeded"] so an admin who later removes the
    mouse or edits the crow's text never has it put back. Mutates hunt_conf in
    place; returns True if anything changed (including setting the flag).
    """
    if hunt_conf.get("offerings_seeded"):
        return False
    animals = hunt_conf.setdefault("animals", {})
    safe = hunt_conf.setdefault("safe_animals", {})

    if "mouse" not in animals:
        animals["mouse"] = copy.deepcopy(MOUSE_ANIMAL)
        safe.setdefault("mouse", copy.deepcopy(MOUSE_SAFE_ENTRY))

    crow = safe.get("crow")
    if crow and crow.get("safe", True) and not crow.get("success_text"):
        crow["success_text"] = CROW_TEXT

    hunt_conf["offerings_seeded"] = True
    return True
