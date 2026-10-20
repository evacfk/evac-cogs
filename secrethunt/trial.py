"""Pure logic for The Knave's Trial (a Medium puzzle): no discord or redbot imports, so all of it is testable standalone.

The idea: five witnesses (the five hiding places) each give a private statement, different for every member. Some of
the witnesses lie. The member works out who stole the tarts (round 1: the thief; round 2: the thief and the hour).

Everything a member sees is *generated* from a private seed, never picked from a fixed list, and every generated case is
checked by a brute-force solver to have exactly one valid answer. A wrong answer re-rolls that round's testimony after a
lockout (round 1) or cooldown (round 2).

A member's state is the same dict engine.new_state builds, plus the extra keys `ensure` adds. Stages reuse engine's:
RIDDLE = round 1 is open, FOUND = round 1 solved and the court is adjourned, FINAL = round 2 is open, SOLVED.
"""
from __future__ import annotations

import itertools
import random
import re
from functools import lru_cache

from . import engine
from .constants import (
    HINT_FIRST_HOURS, HINT_GAP_HOURS, HINT_MAX, TEST_DELAY_SECONDS, TRIAL_LOCK_R1_HOURS, TRIAL_LOCK_R2_HOURS,
)

SUSPECTS = ("Hatter", "Hare", "Dormouse", "Duchess", "Cook")
WITNESSES = 5                       # one per hiding place
LIARS = {1: 2, 2: 3}                # round -> exactly how many of the five witnesses lie (the court says so out loud)
HOURS = range(1, 13)

# What a suspect may be called in an answer. Matched word by word, so "the Hare", "March Hare" and "hare" all work.
ALIASES = {
    "hatter": 0, "madhatter": 0, "hare": 1, "marchhare": 1, "dormouse": 2, "duchess": 3, "cook": 4,
}
HOUR_WORDS = {w: i for i, w in enumerate(
    ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"), 1)}


# ---------------------------------------------------------------- cases: generate and solve

# A statement is a tuple of parts; it is true when every part is. A part is a tuple:
#   ("not", s) ("is", s) ("either", s1, s2)          about the thief
#   ("before", h) ("after", h) ("at", h)             about the hour (round 2 only)
#   ("liar", w) ("honest", w)                        about another witness
Part = tuple


def part_holds(part: Part, thief: int, hour: int, truths) -> bool:
    kind = part[0]
    if kind == "not":
        return thief != part[1]
    if kind == "is":
        return thief == part[1]
    if kind == "either":
        return thief in part[1:]
    if kind == "before":
        return hour < part[1]
    if kind == "after":
        return hour > part[1]
    if kind == "at":
        return hour == part[1]
    if kind == "liar":
        return not truths[part[1]]
    if kind == "honest":
        return truths[part[1]]
    raise ValueError(kind)


def statement_holds(statement, thief: int, hour: int, truths) -> bool:
    return all(part_holds(p, thief, hour, truths) for p in statement)


def solve(statements, n_liars: int, with_hour: bool):
    """Every (thief, hour, liars) that fits all five statements, given that exactly `n_liars` witnesses lie. Round 1 has
    no hour (the hour is reported as 0). A witness is truthful exactly when their statement holds."""
    hours = HOURS if with_hour else (0,)
    # Parts about the thief or the hour do not depend on who lies, so each is settled once per (thief, hour); only the
    # parts about other witnesses are re-checked for every guess at who the honest ones are.
    split = [([p for p in st if p[0] not in ("liar", "honest")], [p for p in st if p[0] in ("liar", "honest")])
             for st in statements]
    n_honest = WITNESSES - n_liars
    found = []
    for thief in range(len(SUSPECTS)):
        for hour in hours:
            base_ok = [all(part_holds(p, thief, hour, None) for p in base) for base, _links in split]
            for honest in itertools.combinations(range(WITNESSES), n_honest):
                if not all(base_ok[i] for i in honest):  # an honest witness's statement must hold
                    continue
                truths = [i in honest for i in range(WITNESSES)]
                if all((base_ok[i] and all(part_holds(p, thief, hour, truths) for p in split[i][1])) == truths[i]
                       for i in range(WITNESSES)):
                    found.append((thief, hour, tuple(i for i in range(WITNESSES) if i not in honest)))
    return found


def _random_part(rng: random.Random, me: int, with_hour: bool) -> Part:
    kinds = ["not", "not", "either", "liar", "honest"]
    if with_hour:
        kinds = ["not", "is", "liar", "honest", "before", "after", "at"]
    kind = rng.choice(kinds)
    if kind in ("not", "is"):
        return (kind, rng.randrange(len(SUSPECTS)))
    if kind == "either":
        a, b = rng.sample(range(len(SUSPECTS)), 2)
        return ("either", a, b)
    if kind == "before":
        return ("before", rng.randint(2, 12))
    if kind == "after":
        return ("after", rng.randint(1, 11))
    if kind == "at":
        return ("at", rng.randint(1, 12))
    return (kind, rng.choice([w for w in range(WITNESSES) if w != me]))


def _links(statements) -> int:
    return sum(1 for s in statements if any(p[0] in ("liar", "honest") for p in s))


def _hour_statements(statements) -> int:
    return sum(1 for s in statements if any(p[0] in ("before", "after", "at") for p in s))


def _fits(statement) -> int:
    """How many (thief, hour) pairs the statement's own claims about the thief and the hour allow, ignoring what it says
    about other witnesses."""
    return sum(1 for t in range(len(SUSPECTS)) for h in HOURS
               if all(part_holds(p, t, h, None) for p in statement if p[0] not in ("liar", "honest")))


def _acceptable(statements, round_no: int) -> bool:
    """Difficulty gate, so a case is never a one-liner and never reads like a bug: no statement may contradict itself
    (that would expose a liar for free), none may hand over the whole answer by itself, witnesses must vouch for or
    accuse each other, and in round 2 several statements must talk about the hour."""
    if any(_fits(s) <= 1 for s in statements):
        return False
    if round_no == 1:
        return _links(statements) >= 2
    return _links(statements) >= 2 and _hour_statements(statements) >= 2


def _consistent_statement(rng, me: int, thief: int, hour: int, truths, with_hour: bool):
    """A random statement that is true under the intended answer when witness `me` is honest and false when they lie."""
    for _ in range(300):
        n_parts = rng.choice((1, 2)) if with_hour else 1
        stmt = tuple(_random_part(rng, me, with_hour) for _ in range(n_parts))
        if statement_holds(stmt, thief, hour, truths) == truths[me]:
            return stmt
    return None


@lru_cache(maxsize=4096)
def generate(seed: str, round_no: int):
    """The case for one seed and round: (statements, thief, hour, liars). Deterministic, and the answer is unique.

    Built by repair rather than luck: pick the intended answer, write five statements that fit it, then while some other
    (thief, hour, liars) also fits, rewrite one statement so that it rules that rival out and still fits the intended
    answer. A case that ends up too easy (see `_acceptable`) is thrown away and rebuilt."""
    rng = random.Random(f"{seed}:{round_no}")
    n_liars, with_hour = LIARS[round_no], round_no == 2
    while True:
        thief = rng.randrange(len(SUSPECTS))
        hour = rng.choice(list(HOURS)) if with_hour else 0
        liars = tuple(sorted(rng.sample(range(WITNESSES), n_liars)))
        truths = [i not in liars for i in range(WITNESSES)]
        statements = [_consistent_statement(rng, i, thief, hour, truths, with_hour) for i in range(WITNESSES)]
        if None in statements:
            continue
        rivals = [s for s in solve(statements, n_liars, with_hour) if s != (thief, hour, liars)]
        for _ in range(80):
            if not rivals:
                break
            r_thief, r_hour, r_liars = rng.choice(rivals)
            r_truths = [i not in r_liars for i in range(WITNESSES)]
            i = rng.randrange(WITNESSES)
            for _ in range(60):
                cand = _consistent_statement(rng, i, thief, hour, truths, with_hour)
                # it rules the rival out when the rival's witness i would have to be honest or lying the other way
                if cand is not None and statement_holds(cand, r_thief, r_hour, r_truths) != r_truths[i]:
                    statements[i] = cand
                    break
            rivals = [s for s in solve(statements, n_liars, with_hour) if s != (thief, hour, liars)]
        if not rivals and _acceptable(statements, round_no):
            return tuple(statements), thief, hour, liars


def case_seed(salt: str, pid: str, user_id: int, round_no: int, roll: int) -> str:
    """Private and stable: a member's testimony for one round and one re-roll. Nobody can compute anyone else's."""
    return f"{salt}:{pid}:{user_id}:{round_no}:{roll}"


# ---------------------------------------------------------------- what the witnesses say

def _name(s: int) -> str:
    return f"the {SUSPECTS[s]}"


def part_text(part: Part, rooms) -> str:
    """One sentence. `rooms[w]` is how witness w is named (a channel mention, so it survives renames)."""
    kind = part[0]
    if kind == "not":
        return f"It was not {_name(part[1])}."
    if kind == "is":
        return f"It was {_name(part[1])}."
    if kind == "either":
        return f"It was {_name(part[1])} or {_name(part[2])}."
    if kind == "before":
        return f"It happened before {part[1]} o'clock."
    if kind == "after":
        return f"It happened after {part[1]} o'clock."
    if kind == "at":
        return f"It happened at {part[1]} o'clock."
    if kind == "liar":
        return f"The witness in {rooms[part[1]]} is lying."
    return f"The witness in {rooms[part[1]]} is telling the truth."


def testimony(statement, rooms) -> str:
    return " ".join(part_text(p, rooms) for p in statement)


# ---------------------------------------------------------------- reading an answer

def _hours_in(text: str):
    found = {int(m) for m in re.findall(r"(?<![\d:])(\d{1,2})(?::\d{2})?(?!\d)", text)}
    found |= {HOUR_WORDS[w] for w in re.findall(r"[a-z]+", text) if w in HOUR_WORDS}
    return found


def parse_answer(text: str, round_no: int):
    """Read a DM as an answer. Returns ("ok", (thief, hour)), ("malformed", why) or ("none", None) when the text names
    no suspect at all (so it is probably not an answer, and is left to the other puzzles). Round 1 wants the thief;
    round 2 wants the thief and the hour (1-12)."""
    low = (text or "").lower()
    suspects = {ALIASES[w] for w in re.findall(r"[a-z]+", low) if w in ALIASES}
    if not suspects:
        return "none", None
    if len(suspects) > 1:
        return "malformed", "one_suspect"
    thief = next(iter(suspects))
    if round_no == 1:
        return "ok", (thief, 0)
    hours = _hours_in(low)
    if len(hours) != 1:
        return "malformed", "hour"
    hour = next(iter(hours))
    if hour not in HOURS:
        return "malformed", "hour"
    return "ok", (thief, hour)


# ---------------------------------------------------------------- one member's progress

def ensure(state: dict) -> dict:
    """Add the trial-only keys to a state created by engine.new_state."""
    state.setdefault("roll", 0)              # how many times this round has been re-rolled
    state.setdefault("hints", 0)             # hints taken in the whole trial (the Hall shows it)
    state.setdefault("round_hints", 0)       # hints taken in the current round
    state.setdefault("last_hint_ts", 0.0)
    state.setdefault("wrongs", 0)            # wrong answers in the whole trial
    return state


def round_of(state) -> int:
    """1 or 2 while a round is open, else 0."""
    stage = state.get("stage") if state else None
    return {engine.STAGE_RIDDLE: 1, engine.STAGE_FINAL: 2}.get(stage, 0)


def round_started(state) -> float:
    return state["started_ts"] if round_of(state) == 1 else state["final_ts"]


def start_round2(state: dict) -> None:
    """The court reconvenes: a fresh round, with its own hint ladder and its own first testimony."""
    ensure(state)
    state["roll"], state["round_hints"], state["last_hint_ts"], state["locked_until"] = 0, 0, 0.0, 0.0


def recess_until(state, now: float) -> float:
    """When the current lockout or cooldown ends, or 0 if the court is sitting."""
    until = state.get("locked_until", 0.0)
    return until if until > now else 0.0


def check_answer(state: dict, case, text: str, now: float, test_mode: bool = False) -> str:
    """The member DM'd an answer. Returns correct / wrong / recess / malformed_suspect / malformed_hour / ignore.

    Wrong, well-formed answers re-roll the round's testimony and start a recess: 4 hours in round 1 (a lockout),
    4 hours in round 2 (a cooldown). Malformed answers cost nothing."""
    round_no = round_of(state)
    if not round_no:
        return "ignore"
    ensure(state)
    verdict, parsed = parse_answer(text, round_no)
    if verdict == "none":
        return "ignore"
    if recess_until(state, now):
        return "recess"
    if verdict == "malformed":
        return "malformed_suspect" if parsed == "one_suspect" else "malformed_hour"
    _statements, thief, hour, _liars = case
    if parsed == (thief, hour):
        if round_no == 1:
            state["stage"] = engine.STAGE_FOUND
            state["found_ts"] = now
            state["due_ts"] = engine.next_unlock(now, test_mode)
        else:
            state["stage"], state["solved_ts"] = engine.STAGE_SOLVED, now
        return "correct"
    state["wrongs"] += 1
    state["roll"] += 1
    hours = TRIAL_LOCK_R1_HOURS if round_no == 1 else TRIAL_LOCK_R2_HOURS
    state["locked_until"] = now + (TEST_DELAY_SECONDS if test_mode else hours * 3600)
    return "wrong"


# ---------------------------------------------------------------- hints

def hint_ready_at(state, test_mode: bool = False) -> float:
    """When the next hint can be asked for: HINT_FIRST_HOURS after the round opened for the first one, then
    HINT_GAP_HOURS after the previous one."""
    first, gap = (TEST_DELAY_SECONDS, TEST_DELAY_SECONDS) if test_mode else (HINT_FIRST_HOURS * 3600, HINT_GAP_HOURS * 3600)
    if state.get("round_hints", 0) == 0:
        return round_started(state) + first
    return state["last_hint_ts"] + gap


def hint_text(case, round_no: int, number: int, rooms, seed: str) -> str:
    """Hint 1 vouches for an honest witness, hint 2 names a liar, hint 3 clears a suspect (round 1) or narrows the hour
    to a four-hour window (round 2). All of them are about the member's own current testimony and are always true."""
    _statements, thief, hour, liars = case
    rng = random.Random(f"{seed}:hint")
    honest = [w for w in range(WITNESSES) if w not in liars]
    if number == 1:
        return f"The witness in {rooms[rng.choice(honest)]} is telling the truth."
    if number == 2:
        return f"The witness in {rooms[rng.choice(list(liars))]} is lying."
    if round_no == 1:
        return f"It was not {_name(rng.choice([s for s in range(len(SUSPECTS)) if s != thief]))}."
    low = rng.randint(max(1, hour - 3), min(hour, 9))
    return f"The hour is somewhere between {low} and {low + 3} o'clock."


def request_hint(state: dict, now: float, test_mode: bool = False):
    """The member asked for a hint. Returns ("ok", number) after counting it, or ("early", ready_ts), ("max", None),
    ("recess", until), ("none", None) when no round is open."""
    round_no = round_of(state)
    if not round_no:
        return "none", None
    ensure(state)
    until = recess_until(state, now)
    if until:
        return "recess", until
    if state["round_hints"] >= HINT_MAX:
        return "max", None
    ready = hint_ready_at(state, test_mode)
    if now < ready:
        return "early", ready
    state["round_hints"] += 1
    state["hints"] += 1
    state["last_hint_ts"] = now
    return "ok", state["round_hints"]


def hint_label(count: int) -> str:
    """How the Hall shows it: 'no hints', '1 hint', '2 hints'."""
    return "no hints" if not count else f"{count} hint" + ("" if count == 1 else "s")
