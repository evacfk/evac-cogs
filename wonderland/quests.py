"""Daily quests: the pure rules. No discord/redbot imports, so all of it is unit-tested.

Each non-lurker member gets up to three quests per Pacific day, dealt the first time they open the
panel. Only quests they can actually do today are dealt (the cog checks channel permissions, each
cog's own gates and time windows), quests they already did today are skipped, and features they
haven't touched in a week are weighted up. Each quest pays a third of that member's payday times
the guild multiplier; a full set is one payday. No bonus. Streaks (consecutive full days) are shown,
never paid.

Every quest key has exactly one source (a cog hook, a command listener or the chat listener). An
event from any other source is ignored, which is what guarantees one action counts once even if
several listeners see it.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")   # the same midnight-Pacific reset as the rest of the server
QUESTS_PER_DAY = 3
FRESH_DAYS = 7          # a quest the member hasn't done in this many days is "fresh"...
FRESH_WEIGHT = 3        # ...and three times as likely to be dealt
SET_SHARE = 3           # each quest pays 1/3 of the member's payday; a full set of 3 pays one payday
DEFAULT_MULTIPLIER = 1.0
MAX_MULTIPLIER = 3.0
CHAT_GAP_SECONDS = 15   # chat quest: a message within 15s of the last counted one doesn't count
STATS_KEEP_DAYS = 60
REVIEW_DAYS = 21
LOW_COMPLETION = 0.20   # under this after the review: propose removing the quest
BAR_WIDTH = 10

HOOK, COMMAND, CHAT = "hook", "command", "chat"

# Never offered, never matched: checked by a test against every quest's key and text.
FORBIDDEN_WORDS = ("secret", "afterdark", "after dark", "rabbit", "pillow")


@dataclass(frozen=True)
class Quest:
    key: str
    title: str        # "{n}" becomes the target
    how: str          # one line: where and how. "{#slug}" becomes a channel link when it resolves
    source: str       # HOOK | COMMAND | CHAT
    target: int = 1
    command: str = ""  # COMMAND quests: the qualified command name that is listened for


QUESTS = (
    Quest("verdict", "Answer today's Verdict", "Tap your answer, then your guess, on today's Verdict question.", HOOK),
    Quest("pet", "Look after the pet", "Feed, Play or Clean on the pet's card. It's free once a day.", HOOK),
    Quest("card", "Pull your daily card", "Tap **Daily Cards** in {#wondercasino}, or type `.card daily`.", HOOK),
    Quest("puzzle", "Claim a puzzle piece", "React 🧩 on a new piece when one drops.", HOOK),
    Quest("minigame", "Win a minigame in {#cuddle}", "Be first to a drop, a math question or a hunt when one pops up.",
          HOOK),
    Quest("chat", "Send {n} messages in {#cuddle}", "Say hi, reply to someone, join in.", CHAT, target=10),
    Quest("fish", "Catch {n} fish", "Type `.fish` in {#bot-commands}.", COMMAND, target=3, command="fish"),
    Quest("star", "Give someone a star", "Thank a good noodle with `.stars @them` in {#bot-commands}.", COMMAND,
          command="stars"),
    Quest("blackjack", "Play a hand of Wonderjack", "Pick **Wonderjack** in the More… menu in {#wondercasino} and bet.",
          HOOK),
    Quest("rsvp", "RSVP to an event", "Tap **Going** or **Maybe** on an upcoming event.", HOOK),
    Quest("sponsor", "Sponsor a tribute", "Send a parachute from the Hunger Games shop (`.hg`).", HOOK),
    Quest("canvas", "Paint a canvas pixel", "Pick **Pixel Canvas** in the More… menu in {#wondercasino}.", HOOK),
    Quest("plant", "Drop coins in {#cuddle}", "Pick **Drop coins in #cuddle** in the More… menu in {#wondercasino}. 10% is burned.",
          HOOK),
)
BY_KEY = {q.key: q for q in QUESTS}


# --------------------------------------------------------------------------- time

def now_local(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(TZ)).astimezone(TZ)


def today(now: Optional[datetime] = None) -> str:
    return now_local(now).date().isoformat()


def yesterday_of(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def seconds_to_reset(now: Optional[datetime] = None) -> int:
    local = now_local(now)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), TZ)
    return max(0, int((midnight - local).total_seconds()))


def fmt_duration(seconds: int) -> str:
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


# --------------------------------------------------------------------------- payday

def payday_amount(base: int, role_credits: Iterable[int], role_ids: Iterable[int], *, stack: bool = False,
                  role_bonuses: Optional[dict] = None, static_bonuses: Optional[dict] = None,
                  global_bank: bool = False) -> int:
    """What this member's payday pays, mirroring vrt-cogs ExtendedEconomy overrides/payday.py.

    base: Economy's PAYDAY_CREDITS (guild, or global for a global bank). role_credits: Economy's role
    PAYDAY_CREDITS for every role the member holds (@everyone included). role_ids: the member's role IDs.
    A global bank uses the base only, exactly like the override.
    """
    credit = int(base)
    if global_bank:
        return credit
    for credits in role_credits:
        if stack:
            credit += int(credits)
        elif credits > credit:
            credit = int(credits)
    ids = list(role_ids)
    bonuses = role_bonuses or {}
    if bonuses and any(rid in bonuses for rid in ids):
        highest = max(bonuses.get(rid, 0) for rid in ids)
        credit += round(credit * highest)
    statics = static_bonuses or {}
    if statics and any(rid in statics for rid in ids):
        credit += max(statics.get(rid, 0) for rid in ids)
    return credit


def quest_reward(payday: int, multiplier: float, index: int, share: int = SET_SHARE) -> int:
    """Coins for the index-th quest finished today (0, 1, 2). The three always sum to exactly one
    payday x multiplier: the last third takes the rounding remainder."""
    total = max(0, round(payday * multiplier))
    base = total // share
    return total - base * (share - 1) if index == share - 1 else base


# --------------------------------------------------------------------------- member state

def fresh_state() -> dict:
    return {"day": "", "assigned": [], "progress": {}, "paid": [], "earned": 0, "opened": False,
            "payday": False, "streak": 0, "best_streak": 0, "last_full": "", "sets": 0, "last_done": {}}


def roll_day(state: Optional[dict], day: str) -> dict:
    """The member's state for `day`: yesterday's quests and progress cleared, history kept."""
    out = fresh_state()
    out.update(dict(state or {}))
    if out["day"] != day:
        out.update(day=day, assigned=[], progress={}, paid=[], earned=0, opened=False, payday=False)
    out["progress"] = dict(out["progress"])
    out["last_done"] = dict(out["last_done"])
    out["assigned"], out["paid"] = list(out["assigned"]), list(out["paid"])
    return out


def target_of(key: str, targets: Optional[dict] = None) -> int:
    custom = (targets or {}).get(key)
    try:
        value = int(custom) if custom is not None else BY_KEY[key].target
    except (TypeError, ValueError):
        value = BY_KEY[key].target
    return max(1, value)


def is_done(state: dict, key: str, targets: Optional[dict] = None) -> bool:
    return int(state["progress"].get(key, 0)) >= target_of(key, targets)


def add_progress(state: dict, key: str, amount: int, targets: Optional[dict] = None) -> bool:
    """Count `amount` toward `key` (capped at the target). Returns True if this finished it."""
    target = target_of(key, targets)
    before = int(state["progress"].get(key, 0))
    if before >= target or amount <= 0:
        return False
    after = min(target, before + int(amount))
    state["progress"][key] = after
    if after >= target:
        state["last_done"][key] = state["day"]
        return True
    return False


def settle(state: dict, targets: Optional[dict] = None) -> tuple[list[str], bool]:
    """Mark newly finished dealt quests as paid. Returns (newly paid keys, whether the set just completed).
    Idempotent: a quest is paid once and a set completes once per day."""
    new = [k for k in state["assigned"] if k not in state["paid"] and is_done(state, k, targets)]
    was_full = bool(state["assigned"]) and len(state["paid"]) == len(state["assigned"])
    state["paid"].extend(new)
    full = bool(state["assigned"]) and len(state["paid"]) == len(state["assigned"])
    set_done = full and not was_full and bool(new)
    if set_done:
        day = state["day"]
        state["streak"] = state["streak"] + 1 if state.get("last_full") == yesterday_of(day) else 1
        state["best_streak"] = max(state.get("best_streak", 0), state["streak"])
        state["last_full"] = day
        state["sets"] = state.get("sets", 0) + 1
    return new, set_done


def current_streak(state: Optional[dict], day: str) -> int:
    """The streak as it stands today: it survives until a whole day is missed."""
    if not state or not state.get("last_full"):
        return 0
    return int(state.get("streak", 0)) if state["last_full"] in (day, yesterday_of(day)) else 0


def deal(candidates: Iterable[str], last_done: dict, day: str, rng: random.Random, n: int = QUESTS_PER_DAY) -> list:
    """Pick up to n quests, weighting ones the member hasn't done in FRESH_DAYS up by FRESH_WEIGHT."""
    pool = sorted(set(candidates))
    cutoff = (date.fromisoformat(day) - timedelta(days=FRESH_DAYS)).isoformat()
    picked = []
    while pool and len(picked) < n:
        weights = [1 if (last_done.get(k) or "") > cutoff else FRESH_WEIGHT for k in pool]
        choice = rng.choices(pool, weights=weights, k=1)[0]
        picked.append(choice)
        pool.remove(choice)
    return picked


def rng_for(guild_id: int, member_id: int, day: str) -> random.Random:
    return random.Random(f"{guild_id}:{member_id}:{day}")


def bar(progress: int, target: int, width: int = BAR_WIDTH) -> str:
    filled = 0 if target <= 0 else min(width, round(width * progress / target))
    return "▰" * filled + "▱" * (width - filled)


def title_of(key: str, targets: Optional[dict] = None) -> str:
    return BY_KEY[key].title.replace("{n}", str(target_of(key, targets)))


# --------------------------------------------------------------------------- guild usage counts

def stats_day(stats: dict, day: str) -> dict:
    entry = stats.setdefault(day, {})
    for name in ("opens", "clicks", "payday", "both", "sets", "paid"):
        entry.setdefault(name, 0)
    entry.setdefault("offered", {})
    entry.setdefault("done", {})
    return entry


def prune(stats: dict, day: str, keep: int = STATS_KEEP_DAYS) -> None:
    cutoff = (date.fromisoformat(day) - timedelta(days=keep)).isoformat()
    for old in [d for d in stats if d < cutoff]:
        stats.pop(old, None)


def window(stats: dict, day: str, days: int) -> list[str]:
    start = (date.fromisoformat(day) - timedelta(days=days - 1)).isoformat()
    return sorted(d for d in stats if start <= d <= day)


def completion(stats: dict, day: str, days: int = REVIEW_DAYS) -> dict:
    """key -> (offered, done) over the last `days` days."""
    out = {q.key: [0, 0] for q in QUESTS}
    for d in window(stats, day, days):
        entry = stats[d]
        for key, n in (entry.get("offered") or {}).items():
            out.setdefault(key, [0, 0])[0] += int(n)
        for key, n in (entry.get("done") or {}).items():
            out.setdefault(key, [0, 0])[1] += int(n)
    return {k: (v[0], v[1]) for k, v in out.items()}


def rate_text(offered: int, done: int) -> str:
    if not offered:
        return "not dealt yet"
    pct = done * 100 // offered
    flag = " ⚠ under 20%" if done < offered * LOW_COMPLETION else ""
    return f"dealt {offered:,} · done {done:,} ({pct}%){flag}"


def day_line(entry: dict) -> str:
    """One admin row for a day: openers against payday claimers, full sets and coins paid."""
    payday, both = int(entry.get("payday", 0)), int(entry.get("both", 0))
    share = f" ({both * 100 // payday}% of payday)" if payday else ""
    return (f"{int(entry.get('opens', 0)):,} opened · {both:,}/{payday:,} payday users{share} · "
            f"{int(entry.get('sets', 0)):,} full sets · {int(entry.get('paid', 0)):,} paid")


def faucet(completers: int, average_payday: int, multiplier: float) -> int:
    """Daily coins created if `completers` members finish a full set."""
    return max(0, round(completers * average_payday * multiplier))
