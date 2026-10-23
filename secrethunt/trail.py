"""Pure logic for The Treacle Well (kind "trail"): no discord or redbot imports, so all of it is testable standalone.

A trail puzzle is a chain of stages, each a different kind of task. Everything personal is regenerated from a private
seed (salt + puzzle + member), so nothing needs storing but a few counters. Stages built so far:

  1  Wake the Dormouse   react to the pinned poem with three emoji in order (state does not exist yet: this starts it)
  2  The Three Sisters   find three rooms one after another; each sister's line holds the riddle for the next
  3  The Muchness        the Dormouse draws only some words (a hidden rule per member); probe it by DM, then pass a test
  4  The Long Pour       press Pour exactly N times in one room (N = the sisters' numbers added up), then Toast in another

The draw order in `plan` is part of the contract: later stages only APPEND draws, so a member's earlier choices (their
three rooms, their numbers) never change when a new stage ships.
"""
from __future__ import annotations

import random
import re
from collections import deque

from . import engine
from .constants import (
    TEST_DELAY_SECONDS, TRAIL_MUCHNESS_REST_MINUTES, TRAIL_PROBES_PER_DAY, TRAIL_POUR_CAP, TRAIL_POUR_REST_MINUTES,
    TRAIL_POUR_WRONGS, TRAIL_REACT_WINDOW, TRAIL_REACTS_PER_HOUR, TRAIL_REST_MINUTES, TRAIL_WRONG_LOOKS,
)

SISTERS = ("Elsie", "Lacie", "Tillie")
NUMBER_RANGE = (2, 13)          # each sister's number; stage 4's count is their sum, so 6 to 39

# Stage 1. Each slot accepts a few look-alike emoji so a fair reading of the poem is never refused. The poem names
# the three images by what they are (never as emoji), in this order.
EMOJI_SLOTS = (
    ("\N{HOT BEVERAGE}", "\N{TEACUP WITHOUT HANDLE}", "\N{TEAPOT}"),
    ("\N{HONEY POT}",),
    ("\N{SLEEPING FACE}", "\N{SLEEPING SYMBOL}", "\N{BED}"),
)

# Words a sister keeps. Stage 6 takes the first letters of six of them (three from the sisters, three from later stages).
TOKEN_WORDS = (
    "toffee", "ladle", "teaspoon", "candle", "saucer", "lantern", "marmalade", "clover", "pocket", "kettle",
    "sugar", "spoon", "biscuit", "cushion", "thimble", "violet", "bobbin", "crumb", "feather", "ribbon",
)


def seed(salt: str, pid: str, user_id: int) -> str:
    """Private and stable: nobody can compute another member's rooms, numbers or words."""
    return f"{salt}:{pid}:{user_id}:trail"


# ---------------------------------------------------------------- stage 1: the poem reactions

def _slot_of(emoji: str):
    emoji = engine.normalize_emoji(emoji)
    return next((i for i, group in enumerate(EMOJI_SLOTS) if emoji in {engine.normalize_emoji(e) for e in group}), None)


def record_reaction(history: deque, emoji: str, now: float) -> bool:
    """Remember one reaction a member put on the poem (in memory only). False when they are past the hourly cap, in
    which case the reaction is ignored entirely."""
    while history and history[0][0] < now - 3600:
        history.popleft()
    if len(history) >= TRAIL_REACTS_PER_HOUR:
        return False
    history.append((now, engine.normalize_emoji(emoji)))
    return True


def poem_solved(history) -> bool:
    """True when the member's last three reactions, within the window, are the three images in order."""
    last = list(history)[-3:]
    if len(last) < 3 or last[-1][0] - last[0][0] > TRAIL_REACT_WINDOW:
        return False
    return [_slot_of(e) for _t, e in last] == [0, 1, 2]


# ---------------------------------------------------------------- the personal plan

def plan(seed_text: str, n_rooms: int) -> dict:
    """Everything personal that stages 1-2 need: the chain of three trail rooms (indexes into the configured list), the
    sisters' numbers and the words they keep. Deterministic; later stages append draws after these."""
    rng = random.Random(seed_text)
    chain = rng.sample(range(n_rooms), len(SISTERS))
    numbers = [rng.randint(*NUMBER_RANGE) for _ in SISTERS]
    tokens = rng.sample(TOKEN_WORDS, len(SISTERS))
    # ---- everything below was added by later pushes: appended draws only, never reordered (a test pins the draws above)
    rule = RULES[rng.randrange(len(RULES))]                        # stage 3: the Dormouse's hidden rule
    pour_room, toast_room = rng.sample(range(n_rooms), 2)          # stage 4: where to pour and where to toast
    extra = rng.sample([w for w in TOKEN_WORDS if w not in tokens], 3)  # the words stages 3, 4 and 5 give
    return {"chain": chain, "numbers": numbers, "tokens": tokens, "rule": rule, "pour_room": pour_room,
            "toast_room": toast_room, "extra_tokens": extra, "pour_total": sum(numbers), "seed": seed_text}


# ---------------------------------------------------------------- state

def _log(state: dict, step: int) -> dict:
    """The funnel record of one stage (created when missing: a state saved by an earlier version has no record)."""
    return state["log"].setdefault(str(step), {"opened": 0.0, "solved": 0.0})


def ensure(state: dict) -> dict:
    """Add the trail-only keys to a state created by engine.new_state."""
    state.setdefault("step", 2)           # the stage the member is on (stage 1 is the poem, so a started member is on 2)
    state.setdefault("found", 0)          # sisters found so far in stage 2
    state.setdefault("wrong_looks", 0)    # wrong tables in a row
    state.setdefault("rest_until", 0.0)   # the tea tables are quiet until then
    state.setdefault("clue", "")          # the text `recap` sends again
    state.setdefault("log", {})           # step -> {"opened": ts, "solved": ts}, for the funnel
    state.setdefault("probes", 0)         # stage 3: words asked about today...
    state.setdefault("probe_date", "")    # ...(Pacific date they were counted on)
    state.setdefault("test_words", [])    # stage 3: the eight words of the open test, empty when none is open
    state.setdefault("test_roll", 0)      # stage 3: how many tests have been failed (each gets fresh words)
    state.setdefault("test_until", 0.0)   # stage 3: no new test until then
    state.setdefault("pours", 0)          # stage 4: pours so far in the pour room
    state.setdefault("pour_wrongs", 0)    # stage 4: wrong toasts in a row
    state.setdefault("pour_rest_until", 0.0)  # stage 4: the cups are being washed until then
    return state


def start(variant: int, now: float) -> dict:
    """A member has woken the Dormouse: their trail begins on stage 2."""
    state = ensure(engine.new_state(variant, now))
    state["log"]["2"] = {"opened": now, "solved": 0.0}
    return state


def look(state, room_index: int, p: dict, now: float, test_mode: bool = False):
    """The member pressed Look on the tea table in trail room `room_index`. Returns (action, detail):

      idle        no trail yet
      rest        the tables are resting (detail = when it ends)
      rest_start  that wrong table was the third in a row: the tables rest now (detail = when it ends)
      wrong       just a tea table
      sister      the next sister, found now (detail = her index 0-2)
      replay      a sister already found, heard again (detail = her index)
    """
    if not state or state.get("stage") != engine.STAGE_RIDDLE:
        return "idle", None
    ensure(state)
    until = state["rest_until"]
    if until > now:
        return "rest", until
    if until:
        state["rest_until"], state["wrong_looks"] = 0.0, 0
    chain, found = p["chain"], state["found"]
    if room_index in chain[:found]:
        return "replay", chain.index(room_index)
    if found >= len(chain):
        return "wrong", None  # stage 2 is done: Look in any other room is just a tea table, and costs nothing
    if room_index == chain[found]:
        state["found"] = found + 1
        state["wrong_looks"] = 0
        if state["found"] == len(chain):
            state["step"] = 3
            _log(state, 2)["solved"] = now
            state["log"]["3"] = {"opened": now, "solved": 0.0}
        return "sister", found
    state["wrong_looks"] += 1
    if state["wrong_looks"] >= TRAIL_WRONG_LOOKS:
        state["rest_until"] = now + (TEST_DELAY_SECONDS if test_mode else TRAIL_REST_MINUTES * 60)
        state["wrong_looks"] = 0
        return "rest_start", state["rest_until"]
    return "wrong", None


# ---------------------------------------------------------------- what the sisters say

def room_pointer(puzzle: dict, name: str, step: int):
    """How the next room is named in a sister's line (a written description when the room's title has a keyword, the
    letter clue otherwise; `letter_variants` forces the letter clue for some steps so it is not one trick)."""
    return engine.pointer_for(puzzle, name, step)


def sister_line(puzzle: dict, k: int, p: dict, pointer) -> str:
    """Sister `k`'s line. `pointer` names the next room (None for the last sister, or when it cannot be named)."""
    template = puzzle["sisters"][k]
    return template.format(number=p["numbers"][k], token=p["tokens"][k], pointer=pointer or "a room I cannot name")


def first_clue(puzzle: dict, pointer) -> str:
    return puzzle["clue1"].format(pointer=pointer or "a room I cannot name")


# ---------------------------------------------------------------- stage 3: the Muchness (a hidden rule about words)

VOWELS = "aeiou"


def _consonant(ch: str) -> bool:
    return ch.isalpha() and ch not in VOWELS


# A rule is a tuple; `holds` says whether the Dormouse can draw a word. RULES is FIXED once shipped: a member's rule is
# picked by position (rng.randrange(len(RULES))), so adding, removing or reordering entries changes everyone's rule. Append
# only, and only before anyone has reached stage 3. A test proves every rule has enough yes and no words in WORDS.
RULES = (
    *[("has", c) for c in "eaotrsnld"],
    *[("starts", c) for c in "stcmbp"],
    *[("ends", c) for c in "etdyn"],
    *[("lacks", c) for c in "eastr"],
    ("len_even",), ("len_odd",), ("len_ge", 6), ("len_le", 4),
    ("double",), ("vowels_ge", 3), ("vowels_le", 1), ("pair_vowels",),
    ("starts_vowel",), ("ends_vowel",), ("starts_consonant_ends_vowel",), ("first_half",), ("last_after_first",),
    ("both", "a", "e"), ("both", "o", "r"), ("both", "t", "n"), ("both", "s", "e"),
)


def holds(rule: tuple, word: str) -> bool:
    kind, args = rule[0], rule[1:]
    w = word.lower()
    if kind == "has":
        return args[0] in w
    if kind == "lacks":
        return args[0] not in w
    if kind == "starts":
        return w[0] == args[0]
    if kind == "ends":
        return w[-1] == args[0]
    if kind == "len_even":
        return len(w) % 2 == 0
    if kind == "len_odd":
        return len(w) % 2 == 1
    if kind == "len_ge":
        return len(w) >= args[0]
    if kind == "len_le":
        return len(w) <= args[0]
    if kind == "double":
        return any(a == b for a, b in zip(w, w[1:]))
    if kind == "vowels_ge":
        return sum(c in VOWELS for c in w) >= args[0]
    if kind == "vowels_le":
        return sum(c in VOWELS for c in w) <= args[0]
    if kind == "pair_vowels":
        return any(a in VOWELS and b in VOWELS for a, b in zip(w, w[1:]))
    if kind == "starts_vowel":
        return w[0] in VOWELS
    if kind == "ends_vowel":
        return w[-1] in VOWELS
    if kind == "starts_consonant_ends_vowel":
        return _consonant(w[0]) and w[-1] in VOWELS
    if kind == "first_half":
        return w[0] <= "m"
    if kind == "last_after_first":
        return w[-1] > w[0]
    if kind == "both":
        return args[0] in w and args[1] in w
    raise ValueError(f"unknown rule {rule!r}")


def rule_text(rule: tuple) -> str:
    """The rule in words, for the admin's preview only: a member never sees it."""
    kind, args = rule[0], rule[1:]
    return {
        "has": lambda: f"contains the letter {args[0].upper()}", "lacks": lambda: f"has no {args[0].upper()}",
        "starts": lambda: f"starts with {args[0].upper()}", "ends": lambda: f"ends with {args[0].upper()}",
        "len_even": lambda: "has an even number of letters", "len_odd": lambda: "has an odd number of letters",
        "len_ge": lambda: f"has at least {args[0]} letters", "len_le": lambda: f"has at most {args[0]} letters",
        "double": lambda: "has a double letter", "vowels_ge": lambda: f"has at least {args[0]} vowels",
        "vowels_le": lambda: f"has at most {args[0]} vowel", "pair_vowels": lambda: "has two vowels side by side",
        "starts_vowel": lambda: "starts with a vowel", "ends_vowel": lambda: "ends with a vowel",
        "starts_consonant_ends_vowel": lambda: "starts with a consonant and ends with a vowel",
        "first_half": lambda: "starts with a letter from A to M", "last_after_first": lambda: "ends later in the alphabet than it starts",
        "both": lambda: f"contains both {args[0].upper()} and {args[1].upper()}",
    }[kind]()


WORDS = tuple(sorted(set("""
apple arrow autumn basket beach bell berry bird blanket bottle branch bread bridge brook brush butter button cabin cake
candle canyon carpet castle cat chair cheese cherry chimney circle clock cloud clover coat cookie copper cotton crab crown
crumb cup curtain daisy dance door dragon dream drum duck eagle earth egg engine fabric feather fence field finger fire
fish flame floor flower fog forest fork fox frog garden gate ghost giant glass glove goat gold grape grass hammer hand
harbor hat hedge hill honey horse house ice island ivy jacket jam jelly jewel jungle kettle key king kitchen kite knife
ladder lake lamp lantern leaf lemon letter lion lock marble meadow melon mirror moon moss mountain mouse mushroom nail
needle nest night noodle ocean olive onion orange otter owl paint palace paper parrot peach pearl pebble pencil pepper
piano pillow pine plate pocket pond pony potato puddle pumpkin queen quilt rabbit rain raven ribbon river road robin rocket
rose saddle sail salad salt sand saucer scarf shadow sheep shell shoe silver singer sky snail snow soap spider spoon
spring squirrel stable star stone storm straw street sugar summer sun swan sword table teapot thimble thistle thunder tiger
toast tomato tower train tree trumpet tulip tunnel turtle umbrella valley velvet violet wagon walnut water wheel whistle
willow window winter wolf wool yarn zebra
arch bark bean bee boat bone book bowl box brick cave clay coal cork corn dawn deer dew dust fern flag foam gift glow
hay hoop hive inn iron jar jug lace lawn lily log map mint mist mud nut oak oar oven pail path pie plum pot rope ruby
rug sack seed silk slate soup stew tart tent tide toy twig vase vine wax web wing wire yew
""".split())))


def viable(rule: tuple, minimum: int = 8) -> bool:
    """A rule is playable when the word bank has enough words it draws and enough it does not."""
    yes = sum(holds(rule, w) for w in WORDS)
    return yes >= minimum and len(WORDS) - yes >= minimum


def make_test(seed_text: str, roll: int, rule: tuple):
    """The eight words of a test and which of them the Dormouse can draw: 3 to 5 yes, the rest no, in a shuffled order.
    Every roll (a failed test) gives fresh words."""
    rng = random.Random(f"{seed_text}:muchness:{roll}")
    yes_pool = [w for w in WORDS if holds(rule, w)]
    no_pool = [w for w in WORDS if not holds(rule, w)]
    n_yes = rng.randint(3, 5)
    words = rng.sample(yes_pool, n_yes) + rng.sample(no_pool, 8 - n_yes)
    rng.shuffle(words)
    return words, sorted(w for w in words if holds(rule, w))


def words_in(text: str) -> list:
    """The lowercase letter-words of a message ("Tea, moon and fig!" -> tea, moon, and, fig)."""
    return re.findall(r"[a-z]+", (text or "").lower())


def probe_ok(word: str) -> bool:
    return 2 <= len(word) <= 20


def muchness(state, plan_: dict, text: str, now: float, test_mode: bool = False):
    """A DM from a member on stage 3 (the Dormouse's words). Returns (action, detail):

      not_here    this member is not on stage 3
      probe       one word asked about (detail = (word, can_draw))
      saving      that word is in the open test, so no verdict (detail = word)
      tired       too many words asked about today
      ready       a test begins (detail = its eight words)
      test_open   a test is already open (detail = its words)
      resting     a failed test's rest (detail = when it ends)
      passed      the test is right: on to stage 4
      failed      the test was wrong; fresh words next time (detail = when the rest ends)
      malformed   a test answer that is not a list of its words (costs nothing)
      ignore      nothing the Dormouse would answer
    """
    if not state or state.get("stage") != engine.STAGE_RIDDLE or state.get("step") != 3:
        return "not_here", None
    ensure(state)
    rule, seed_text = tuple(plan_["rule"]), plan_["seed"]
    tokens = words_in(text)
    if not tokens:
        return "ignore", None
    test = state["test_words"]
    if engine.normalize(text) == "ready":
        if state["test_until"] > now:
            return "resting", state["test_until"]
        if test:
            return "test_open", test
        test, _yes = make_test(seed_text, state["test_roll"], rule)
        state["test_words"] = test
        return "ready", test
    if test and (engine.normalize(text) == "none" or (len(tokens) >= 2 and set(tokens) <= set(test))):
        answer = [] if engine.normalize(text) == "none" else tokens
        _words, yes = make_test(seed_text, state["test_roll"], rule)
        if sorted(set(answer)) == yes:
            state["test_words"], state["step"] = [], 4
            _log(state, 3)["solved"] = now
            state["log"]["4"] = {"opened": now, "solved": 0.0}
            return "passed", None
        state["test_words"], state["test_roll"] = [], state["test_roll"] + 1
        state["test_until"] = now + (TEST_DELAY_SECONDS if test_mode else TRAIL_MUCHNESS_REST_MINUTES * 60)
        return "failed", state["test_until"]
    if test and len(tokens) >= 2 and not set(tokens) <= set(test):
        return "malformed", None
    if len(tokens) != 1 or not probe_ok(tokens[0]):
        return "ignore", None
    word = tokens[0]
    if word in test:
        return "saving", word
    today = engine.pacific_date(now)
    if state["probe_date"] != today:
        state["probe_date"], state["probes"] = today, 0
    if state["probes"] >= TRAIL_PROBES_PER_DAY:
        return "tired", None
    state["probes"] += 1
    return "probe", (word, holds(rule, word))


# ---------------------------------------------------------------- stage 4: the Long Pour

def pour(state, room_index: int, p: dict, now: float, test_mode: bool = False):
    """Pour was pressed in trail room `room_index`. Returns (action, detail):

      idle      not on stage 4
      rest      the cups are being washed (detail = when it ends)
      wrong     not the pour room
      poured    one more pour (detail = how many so far)
      full      the cup is full and cannot take more (detail = the cap)
    """
    if not state or state.get("stage") != engine.STAGE_RIDDLE or state.get("step") != 4:
        return "idle", None
    ensure(state)
    if state["pour_rest_until"] > now:
        return "rest", state["pour_rest_until"]
    if state["pour_rest_until"]:
        state["pour_rest_until"], state["pour_wrongs"] = 0.0, 0
    if room_index != p["pour_room"]:
        return "wrong", None
    if state["pours"] >= TRAIL_POUR_CAP:
        return "full", TRAIL_POUR_CAP
    state["pours"] += 1
    return "poured", state["pours"]


def toast(state, room_index: int, p: dict, now: float, test_mode: bool = False):
    """Toast was pressed in trail room `room_index`. Returns (action, detail):

      idle        not on stage 4
      rest        the cups are being washed (detail = when it ends)
      wrong       not the toast room
      empty       nothing has been poured yet
      passed      exactly the right count: on to stage 5
      miscount    wrong count; the pouring starts again (detail = pours counted)
      miscount_rest  the third wrong toast in a row: the cups rest (detail = when it ends)
    """
    if not state or state.get("stage") != engine.STAGE_RIDDLE or state.get("step") != 4:
        return "idle", None
    ensure(state)
    if state["pour_rest_until"] > now:
        return "rest", state["pour_rest_until"]
    if state["pour_rest_until"]:
        state["pour_rest_until"], state["pour_wrongs"] = 0.0, 0
    if room_index != p["toast_room"]:
        return "wrong", None
    if state["pours"] == 0:
        return "empty", None
    if state["pours"] == p["pour_total"]:
        state["step"], state["pours"], state["pour_wrongs"] = 5, 0, 0
        _log(state, 4)["solved"] = now
        state["log"]["5"] = {"opened": now, "solved": 0.0}
        return "passed", None
    counted, state["pours"] = state["pours"], 0
    state["pour_wrongs"] += 1
    if state["pour_wrongs"] >= TRAIL_POUR_WRONGS:
        state["pour_rest_until"] = now + (TEST_DELAY_SECONDS if test_mode else TRAIL_POUR_REST_MINUTES * 60)
        state["pour_wrongs"] = 0
        return "miscount_rest", state["pour_rest_until"]
    return "miscount", counted
