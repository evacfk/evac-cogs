"""Pure logic for The Treacle Well (kind "trail"): no discord or redbot imports, so all of it is testable standalone.

A trail puzzle is a chain of stages, each a different kind of task. Everything personal is regenerated from a private
seed (salt + puzzle + member), so nothing needs storing but a few counters. Stages built so far:

  1  Wake the Dormouse   react to the pinned poem with three emoji in order (state does not exist yet: this starts it)
  2  The Three Sisters   find three rooms one after another; each sister's line holds the riddle for the next

The draw order in `plan` is part of the contract: later stages only APPEND draws, so a member's earlier choices (their
three rooms, their numbers) never change when a new stage ships.
"""
from __future__ import annotations

import random
from collections import deque

from . import engine
from .constants import (
    TEST_DELAY_SECONDS, TRAIL_REACT_WINDOW, TRAIL_REACTS_PER_HOUR, TRAIL_REST_MINUTES, TRAIL_WRONG_LOOKS,
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
    return {"chain": chain, "numbers": numbers, "tokens": tokens}


# ---------------------------------------------------------------- state

def ensure(state: dict) -> dict:
    """Add the trail-only keys to a state created by engine.new_state."""
    state.setdefault("step", 2)           # the stage the member is on (stage 1 is the poem, so a started member is on 2)
    state.setdefault("found", 0)          # sisters found so far in stage 2
    state.setdefault("wrong_looks", 0)    # wrong tables in a row
    state.setdefault("rest_until", 0.0)   # the tea tables are quiet until then
    state.setdefault("clue", "")          # the text `recap` sends again
    state.setdefault("log", {})           # step -> {"opened": ts, "solved": ts}, for the funnel
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
    if found < len(chain) and room_index == chain[found]:
        state["found"] = found + 1
        state["wrong_looks"] = 0
        if state["found"] == len(chain):
            state["step"] = 3
            state["log"]["2"]["solved"] = now
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
