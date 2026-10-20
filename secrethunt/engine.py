"""Pure logic for secrethunt: no discord or redbot imports, so all of it is testable standalone.

A member's progress through one puzzle is a plain dict (`new_state`), stored under
`member.puzzles[puzzle_id]`. Stages: RIDDLE (sent the first clue) -> FOUND (reacted to their own
object; waiting for the morning) -> FINAL (the jumbled word is out) -> SOLVED.
"""
from __future__ import annotations

import hashlib
import random
import re
from datetime import datetime, time as dtime, timedelta

from .constants import (
    DM_RETRY_SECONDS, HALL_BUDGET, HOME_TZ, MAX_DM_FAILS, MAX_WRONG_PER_DAY, MAX_WRONG_REACTS, MIN_DELAY_HOURS, REACT_LOCK_HOURS,
    TEST_DELAY_SECONDS, UNLOCK_HOUR,
)

STAGE_RIDDLE, STAGE_FOUND, STAGE_FINAL, STAGE_SOLVED = 1, 2, 3, 4
FAR_FUTURE = 1e15  # "never" that survives a JSON round trip (inf does not)


def normalize(text: str) -> str:
    """Lowercase letters and digits only, so 'Hat-ter!' == 'hatter'."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def normalize_emoji(text: str) -> str:
    return (text or "").replace("️", "")


def variant_for(salt: str, puzzle_id: str, user_id: int, n: int) -> int:
    """Stable per-member variant in [0, n). The salt is private, so nobody can predict or compute it."""
    digest = hashlib.sha256(f"{salt}:{puzzle_id}:{user_id}".encode()).hexdigest()
    return int(digest, 16) % n


def scramble(word: str, seed: str) -> str:
    """Deterministic anagram of `word` that is never the word itself (when that is possible)."""
    letters = list(word)
    if len(set(letters)) < 2:
        return word
    rng = random.Random(seed)
    for _ in range(100):
        rng.shuffle(letters)
        if "".join(letters) != word:
            break
    return "".join(letters)


def title_hint(name: str, seed: str):
    """A channel or forum-post title with each word's letters shaken loose, as (hint, shape), or None when the
    title has too few letters to make a puzzle. Words under 3 letters stay as they are; `shape` is the letter
    count of every word, e.g. "6" or "4 + 2 + 5", so the hint is solvable."""
    words = re.findall(r"[^\W\d_]+", (name or "").lower())
    if sum(len(w) for w in words) < 3:
        return None
    parts = [scramble(w, f"{seed}:{i}") if len(w) >= 3 else w for i, w in enumerate(words)]
    return " ".join(parts), " + ".join(str(len(w)) for w in words)


def letter_clue(name: str):
    """A room pointer that needs no jumble: how many letters its name has and which letter it ends on, e.g.
    "the room whose name is 6 letters long and ends in **G**". None when the title has too few letters."""
    words = re.findall(r"[^\W\d_]+", (name or "").lower())
    if sum(len(w) for w in words) < 3:
        return None
    shape = " + ".join(str(len(w)) for w in words)
    return f"the room whose name has {shape} letters and ends in **{words[-1][-1].upper()}**"


def pointer_for(puzzle: dict, name: str, variant: int):
    """How a puzzle that points at rooms by description (instead of a jumbled title) names this member's room.
    A variant listed in `letter_variants` always gets the letter clue, so the riddles are not all one trick; every other
    room gets its written description when `pointers` has a keyword that its title contains, else the letter clue.
    Returns None when neither works (the caller falls back to the plain channel mention)."""
    low = (name or "").lower()
    if variant not in puzzle.get("letter_variants", ()):
        for keyword, text in puzzle.get("pointers", {}).items():
            if keyword in low:
                return text
    return letter_clue(name)


def object_hint(puzzle: dict, variant: int) -> str:
    """The line that tells this variant which button on the object is the right one."""
    buttons = puzzle["buttons"]
    return puzzle["button_hints"][buttons[variant % len(buttons)]]


def escape_markdown(text: str) -> str:
    """Backslash-escape Discord markdown so a display name cannot restyle or break the Hall post."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", text or "")


def hall_text(entries, budget: int = HALL_BUDGET):
    """`(rank, name)` or `(rank, name, note)` entries in finishing order as a numbered list, cut off at `budget`
    characters. A note (for example "2 hints") follows the name. Returns (text, hidden_count). Ranks are the original
    finishing positions, so someone leaving the server leaves a gap instead of renumbering everybody."""
    lines, used = [], 0
    for entry in entries:
        rank, name = entry[0], entry[1]
        line = f"{rank}. {name}" + (f" \N{MIDDLE DOT} {entry[2]}" if len(entry) > 2 and entry[2] else "")
        if used + len(line) + 1 > budget:
            return "\n".join(lines), len(entries) - len(lines)
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines), 0


def acrostic(lines) -> str:
    return "".join(next((c for c in line if c.isalnum()), "") for line in lines).lower()


def match_trigger(puzzles: dict, published, word: str):
    """The id of the published puzzle whose trigger word this is, or None."""
    word = normalize(word)
    if not word:
        return None
    for pid in published:
        puzzle = puzzles.get(pid)
        if puzzle and normalize(puzzle["trigger"]) == word:
            return pid
    return None


def find_object(objects: dict, message_id: int):
    """(puzzle id, channel id) of the hunt object with this message id, or None."""
    for pid, by_channel in objects.items():
        for channel_id, mid in by_channel.items():
            if mid == message_id:
                return pid, int(channel_id)
    return None


def next_unlock(found_ts: float, test_mode: bool = False) -> float:
    """When the next clue is due: at least MIN_DELAY_HOURS later, on the next 9:00 Pacific."""
    if test_mode:
        return found_ts + TEST_DELAY_SECONDS
    earliest = datetime.fromtimestamp(found_ts + MIN_DELAY_HOURS * 3600, HOME_TZ)
    day = earliest.date()
    due = datetime.combine(day, dtime(UNLOCK_HOUR), tzinfo=HOME_TZ)
    if due < earliest:
        due = datetime.combine(day + timedelta(days=1), dtime(UNLOCK_HOUR), tzinfo=HOME_TZ)
    return due.timestamp()


def pacific_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, HOME_TZ).strftime("%Y-%m-%d")


def new_state(variant: int, now: float) -> dict:
    return {
        "variant": variant, "stage": STAGE_RIDDLE, "started_ts": now, "found_ts": 0.0, "due_ts": 0.0,
        "final_ts": 0.0, "solved_ts": 0.0, "rank": 0, "decoys": 0, "dm_fails": 0,
        "wrong_date": "", "wrong_count": 0, "locked_until": 0.0,
    }


def trigger(state, variant: int, now: float):
    """The member said the trigger word. Returns (state, action); action is one of
    start / riddle_again / waiting / final_again / done."""
    if not state:
        return new_state(variant, now), "start"
    if state["stage"] == STAGE_FOUND and state["dm_fails"] >= MAX_DM_FAILS:
        state["dm_fails"], state["due_ts"] = 0, now  # gave up on a closed DM; they are back, so try again
    return state, {
        STAGE_RIDDLE: "riddle_again", STAGE_FOUND: "waiting", STAGE_FINAL: "final_again", STAGE_SOLVED: "done",
    }[state["stage"]]


def react(state, own_object: bool, now: float, test_mode: bool = False) -> str:
    """The member reacted to a hunt object. Returns found / decoy / lockout / locked / ignore.

    Two wrong objects lock the member out of the riddle for a day (`lockout` on the second, `locked` for any reaction
    while it lasts). When the lock ends they get their two chances again, on the same riddle."""
    if not state or state["stage"] != STAGE_RIDDLE:
        return "ignore"
    until = state.get("locked_until", 0.0)
    if until > now:
        return "locked"
    if until:
        state["locked_until"], state["decoys"] = 0.0, 0
    if own_object:
        state["stage"] = STAGE_FOUND
        state["found_ts"] = now
        state["due_ts"] = next_unlock(now, test_mode)
        return "found"
    state["decoys"] += 1
    if state["decoys"] >= MAX_WRONG_REACTS:
        state["locked_until"] = now + (TEST_DELAY_SECONDS if test_mode else REACT_LOCK_HOURS * 3600)
        return "lockout"
    return "decoy"


def is_due(state, now: float) -> bool:
    return bool(state) and state["stage"] == STAGE_FOUND and state["due_ts"] <= now


def mark_final_sent(state, now: float) -> None:
    state["stage"], state["final_ts"] = STAGE_FINAL, now


def dm_failed(state, now: float) -> None:
    """The final clue could not be delivered (closed DMs): put it back and retry later, up to a cap."""
    state["stage"] = STAGE_FOUND
    state["dm_fails"] += 1
    state["due_ts"] = now + DM_RETRY_SECONDS if state["dm_fails"] < MAX_DM_FAILS else FAR_FUTURE


def guess(state, answer: str, text: str, now: float) -> str:
    """Check a DM'd answer. Returns correct / wrong / locked / ignore (not at the guessing stage)."""
    if not state or state["stage"] != STAGE_FINAL:
        return "ignore"
    today = pacific_date(now)
    if state["wrong_date"] != today:
        state["wrong_date"], state["wrong_count"] = today, 0
    if state["wrong_count"] >= MAX_WRONG_PER_DAY:
        return "locked"
    if normalize(text) == normalize(answer):
        state["stage"], state["solved_ts"] = STAGE_SOLVED, now
        return "correct"
    state["wrong_count"] += 1
    return "wrong"
