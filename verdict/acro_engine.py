"""Pure logic for Acrophobia (the second Verdict round type): no discord/redbot imports."""
from __future__ import annotations

import random
from datetime import date, datetime, time as dtime
from typing import Iterable, Optional, Sequence

from .constants import (
    ACRO_LETTER_COUNTS, ACRO_MAX_ENTRY_LEN, ACRO_THEME_CHANCE, HOME_TZ,
)

# Rough share of English words that start with each letter. Q, X and Z are left out on purpose:
# nobody can write a good acronym from them.
LETTER_WEIGHTS = {
    "T": 16, "A": 11, "O": 8, "I": 7, "S": 7, "W": 6, "C": 5, "B": 5, "P": 4, "H": 4, "F": 4, "M": 3,
    "D": 3, "R": 3, "L": 2, "E": 2, "N": 2, "G": 2, "Y": 1, "U": 1, "V": 1, "J": 1, "K": 1,
}
LEADING_PUNCT = "\"'“”‘’([{¿¡-*_~`"
BANNED_FRAGMENTS = ("<", ">", "@", "://", "www.", "discord.gg", "discord.com/invite")
_MD_CHARS = "\\*_~`|>[]"


def pick_letters(rng: Optional[random.Random] = None, count: Optional[int] = None) -> str:
    """3-5 different letters, weighted toward common word starters, never Q, X or Z."""
    rng = rng or random
    count = count or rng.choice(ACRO_LETTER_COUNTS)
    pool = dict(LETTER_WEIGHTS)
    out = []
    for _ in range(count):
        letters, weights = zip(*pool.items())
        pick = rng.choices(letters, weights=weights, k=1)[0]
        out.append(pick)
        pool.pop(pick)
    return "".join(out)


def pick_theme(themes: Sequence[str], rng: Optional[random.Random] = None) -> Optional[str]:
    """Most rounds get a theme from the mods' list; some get none."""
    rng = rng or random
    themes = [t for t in (t.strip() for t in themes) if t]
    if not themes or rng.random() >= ACRO_THEME_CHANCE:
        return None
    return rng.choice(themes)


def parse_themes(text: str, limit: int = 60, max_len: int = 80) -> list[str]:
    """One theme per line; blanks and duplicates dropped."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        t = " ".join(line.split())
        if not t:
            continue
        if len(t) > max_len:
            raise ValueError(f"A theme is too long (max {max_len} characters): {t[:30]}…")
        if t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    if len(out) > limit:
        raise ValueError(f"At most {limit} themes.")
    return out


def validate_entry(text: str, letters: str) -> tuple[Optional[str], Optional[str]]:
    """(clean_text, None) for a valid acronym, or (None, reason) written for the member."""
    clean = " ".join((text or "").split())
    spaced = " · ".join(letters)
    if not clean:
        return None, f"Type {len(letters)} words, one for each letter: {spaced}."
    low = clean.casefold()
    if any(frag in low for frag in BANNED_FRAGMENTS):
        return None, "No mentions, links or invites in an entry, please."
    if len(clean) > ACRO_MAX_ENTRY_LEN:
        return None, f"That's {len(clean)} characters. Keep it to {ACRO_MAX_ENTRY_LEN} or fewer."
    words = clean.split(" ")
    if len(words) != len(letters):
        return None, (f"You need exactly {len(letters)} words, one for each letter ({spaced}). "
                      f"You wrote {len(words)}.")
    for i, (word, letter) in enumerate(zip(words, letters), 1):
        core = word.lstrip(LEADING_PUNCT)
        if not core or core[0].upper() != letter:
            return None, f"Word {i} (\"{word}\") has to start with **{letter}**."
    return clean, None


def escape(text: str) -> str:
    """Show an entry literally: no bold, links or spoilers sneaking in."""
    return "".join("\\" + c if c in _MD_CHARS else c for c in text)


def pick_hours(avg_by_hour: dict, count: int, earliest: int, latest: int, min_gap: int,
               taken: Sequence[int] = ()) -> list[int]:
    """The `count` busiest hours inside [earliest, latest], at least `min_gap` hours apart (and apart from the
    `taken` hours, which already had a round), in clock order. Ties go to the earlier hour. Fewer hours come
    back when the gap rule leaves no room."""
    ranked = sorted((h for h in range(earliest, latest + 1)), key=lambda h: (-float(avg_by_hour.get(h, 0.0)), h))
    chosen: list[int] = []
    for h in ranked:
        if len(chosen) >= count:
            break
        if all(abs(h - c) >= min_gap for c in [*chosen, *taken]):
            chosen.append(h)
    return sorted(chosen)


def plan_slots(day: date, hours: Iterable[int], rng: Optional[random.Random] = None) -> list[float]:
    """Timestamps for today's rounds: somewhere in the first 40 minutes of each chosen hour (not on :00 sharp,
    so it doesn't land on the same minute as other bots' posts)."""
    rng = rng or random
    out = []
    for h in hours:
        minute = rng.randint(5, 40)
        out.append(datetime.combine(day, dtime(h, minute), tzinfo=HOME_TZ).timestamp())
    return sorted(out)


def due_slot(plan: dict, now_ts: float, grace: float) -> tuple[Optional[float], list[float]]:
    """(slot to run now or None, slots that are too late to run and should be written off)."""
    done = set(plan.get("done") or [])
    todo = [s for s in plan.get("slots") or [] if s not in done]
    late = [s for s in todo if now_ts - s > grace]
    ready = [s for s in todo if 0 <= now_ts - s <= grace]
    return (ready[0] if ready else None), late


def tally(votes: dict, entries: dict, removed: Iterable) -> dict:
    """{entry uid: vote count} for every live entry. A vote for a removed or unknown entry counts for nothing."""
    gone = set(removed)
    counts = {uid: 0 for uid in entries if uid not in gone}
    for target in votes.values():
        if target in counts:
            counts[target] += 1
    return counts


def winners(counts: dict) -> list[str]:
    """Entries with the top vote count (all of them on a tie); nobody when no votes were cast."""
    top = max(counts.values(), default=0)
    return sorted((u for u, c in counts.items() if c == top), key=int) if top > 0 else []


def payout(prize: int, paid_today: int, daily_cap: int, n_winners: int) -> tuple[int, int]:
    """(each winner's share, total paid). The prize is split on a tie and cut to what's left of the daily cap."""
    if n_winners <= 0 or prize <= 0:
        return 0, 0
    left = max(0, daily_cap - paid_today)
    pool = min(prize, left)
    share = pool // n_winners
    return share, share * n_winners


def ranked_entries(counts: dict, order: Sequence[str]) -> list[str]:
    """Live entries, most votes first; equal votes keep their ballot order."""
    pos = {u: i for i, u in enumerate(order)}
    return sorted(counts, key=lambda u: (-counts[u], pos.get(u, 999)))


def history_entry(cur: dict, *, cancelled: bool, entries: int, votes: int, win: Sequence[str], paid: int, ts: float,
                  aborted: bool = False) -> dict:
    """Counts only: no entry texts, no authors. `aborted`: a mod stopped it, so it says nothing about turnout."""
    return {"seq": cur["seq"], "ts": ts, "letters": cur["letters"], "entries": entries, "votes": votes,
            "cancelled": cancelled, "winners": len(win), "paid": paid, "manual": bool(cur.get("manual")),
            "aborted": aborted}


def averages(history: Sequence[dict], last: int = 20) -> dict:
    """Rounds run, and average entries/votes per round over the latest `last` rounds (cancelled ones count:
    they are exactly the rounds that had too few entries)."""
    played = [r for r in history if not r.get("aborted")]
    rows = played[-last:]
    n = len(rows)
    return {"rounds": len(played), "window": n, "cancelled": sum(1 for r in rows if r["cancelled"]),
            "avg_entries": (sum(r["entries"] for r in rows) / n) if n else 0.0,
            "avg_votes": (sum(r["votes"] for r in rows if not r["cancelled"]) /
                          max(1, sum(1 for r in rows if not r["cancelled"]))) if n else 0.0}


def review_state(launch_ts: float, now_ts: float, history: Sequence[dict], review_days: int, min_avg: float) -> dict:
    """Is the 3-week review due, and does the feature pass it?"""
    if not launch_ts:
        return {"due": False, "days_left": None, "retire": False, "avg": 0.0}
    since = [r for r in history if r["ts"] >= launch_ts and not r.get("aborted")]
    avg = (sum(r["entries"] for r in since) / len(since)) if since else 0.0
    days_left = review_days - int((now_ts - launch_ts) // 86400)
    due = days_left <= 0
    return {"due": due, "days_left": max(0, days_left), "retire": due and avg < min_avg, "avg": avg}


def month_wins(months: dict, key: str) -> dict:
    return {uid: n for uid, n in (months.get(key) or {}).items() if n > 0}


def merge_top(rows: Sequence, wins: dict, limit: int = 15) -> list:
    """(uid, correct, played, acro wins) rows: the Mind Reader ranking first, then anyone who only has Acrophobia wins."""
    out = [(uid, c, p, wins.get(uid, 0)) for uid, c, p in rows]
    seen = {r[0] for r in out}
    extra = sorted(((u, n) for u, n in wins.items() if u not in seen), key=lambda kv: (-kv[1], int(kv[0])))
    out += [(u, None, None, n) for u, n in extra]
    return out[:limit]
