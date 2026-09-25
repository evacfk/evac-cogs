"""Pure CAPTCHA-code logic for cardcollect's claim mechanic. No discord/redbot
imports -- fully unit-testable with plain pytest, same split as engine.py.

Every card in a drop gets its own short code, rendered onto the card art
(see imagegen.render_card). A member presses the drop's Claim button and types
a code into the pop-up; the first correct submission claims that card.

Design constraints, and why:

* Uppercase-only alphabet, matched case-insensitively -- a phone keyboard
  auto-capitalizing (or not) must never cause a mismatch.
* Visually confusable glyphs are excluded outright (0/O/Q/D, 1/I/L, 5/S,
  2/Z, 8/B, U/V, G/6), so a member never has to guess "is that an O or a 0".
* Exactly one symbol per code, chosen from characters that live on the first
  symbols page of a phone keyboard.
* Every code has at least one letter and one digit, so a code has a
  distinctive *shape* (see looks_like_code). That's what lets the claim flow
  tell "this isn't even a code, check your typing" (free, with a hint) apart
  from "this is a code, and it's the wrong one" (costs a guess).
* Codes within one drop are at least MIN_CODE_DISTANCE edits apart, so a
  single typo can never accidentally match a *different* card's code.
"""

import random
from typing import List, Optional

from .constants import CODE_DIGITS, CODE_LENGTH, CODE_LETTERS, CODE_SYMBOLS, MIN_CODE_DISTANCE

ALPHABET = frozenset(CODE_LETTERS + CODE_DIGITS + CODE_SYMBOLS)
_BODY_CHARS = CODE_LETTERS + CODE_DIGITS

_MAX_GENERATE_ATTEMPTS = 1000


def _rng(rng: Optional[random.Random]) -> random.Random:
    return rng if rng is not None else random._inst


def normalize(text: str) -> str:
    """Canonical form of a member's submission for matching: surrounding
    whitespace dropped, uppercased. Anything with internal whitespace is left
    alone (and so never looks like a code)."""
    return (text or "").strip().upper()


def looks_like_code(text: str) -> bool:
    """True if `text` (already normalized) has the exact *shape* of a code --
    right length, only alphabet characters, exactly one symbol, at least one
    digit and one letter. A submission that isn't code-shaped can't be a
    correct guess, and is almost certainly a typo, so the claim flow answers
    it with a hint instead of charging a wrong guess."""
    if len(text) != CODE_LENGTH:
        return False
    if any(ch not in ALPHABET for ch in text):
        return False
    if sum(ch in CODE_SYMBOLS for ch in text) != 1:
        return False
    return any(ch in CODE_DIGITS for ch in text) and any(ch in CODE_LETTERS for ch in text)


def edit_distance(a: str, b: str) -> int:
    """Plain Levenshtein distance (insert/delete/substitute)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def generate_code(rng: Optional[random.Random] = None) -> str:
    """One random code. Not collision-checked -- see generate_codes."""
    rng = _rng(rng)
    while True:
        chars = [rng.choice(_BODY_CHARS) for _ in range(CODE_LENGTH)]
        chars[rng.randrange(CODE_LENGTH)] = rng.choice(CODE_SYMBOLS)
        code = "".join(chars)
        if looks_like_code(code):  # rejects only the rare all-letters / all-digits body
            return code


def generate_codes(
    count: int,
    rng: Optional[random.Random] = None,
    min_distance: int = MIN_CODE_DISTANCE,
) -> List[str]:
    """`count` codes, each at least `min_distance` edits from every other."""
    rng = _rng(rng)
    result: List[str] = []
    attempts = 0
    while len(result) < count:
        attempts += 1
        if attempts > _MAX_GENERATE_ATTEMPTS:
            raise ValueError("Couldn't generate enough distinct captcha codes")
        code = generate_code(rng)
        if all(edit_distance(code, other) >= min_distance for other in result):
            result.append(code)
    return result
