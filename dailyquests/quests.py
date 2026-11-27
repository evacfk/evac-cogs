"""Daily quests: the pure rules. No discord/redbot imports, so all of it is unit-tested.

Each non-lurker member gets up to three quests per Pacific day, dealt the first time they open the
panel. Only quests they can actually do today are dealt (the cog checks channel permissions, each
cog's own gates and time windows), quests they already did today are skipped, and features they
haven't touched in a week are weighted up. Each quest pays a third of that member's payday times
the guild multiplier; a full set is one payday. A streak is consecutive days with at least one quest
finished; streak milestones, a weekly prize (quests on N of 7 days), bonus XP and card packs sit on top.

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

# ---- streak milestones, weekly prize, XP, packs, reroll (all editable in `.dailyquests`)
DEFAULT_XP_PER_QUEST = 50
DEFAULT_REROLL_COSTS = (1000, 5000, 10000)   # then it doubles for every further reroll in the same day
DEFAULT_SET_PACK = "half"            # what a full set earns from CardCollect: off | half (half price) | free
SET_PACK_MODES = ("off", "half", "free")
MILESTONE_DAYS = (3, 7, 14, 30, 100)
BADGES = {3: ("🔥", "Warming Up"), 7: ("🏅", "Week Warrior"), 14: ("⚡", "Fortnight Fighter"),
          30: ("👑", "Monthly Monarch"), 100: ("💎", "Centurion")}
# coins = paydays (x the multiplier), xp = plain LevelUp XP, pack = a free card pack
DEFAULT_PRIZES = {3: {"coins": 0.0, "xp": 100, "pack": False}, 7: {"coins": 1.0, "xp": 250, "pack": True},
                  14: {"coins": 0.0, "xp": 500, "pack": False}, 30: {"coins": 3.0, "xp": 1000, "pack": True},
                  100: {"coins": 5.0, "xp": 3000, "pack": True}}
DEFAULT_WEEKLY = {"days": 5, "coins": 1.0, "xp": 300, "pack": False}   # quests on 5 of 7 days
DEFAULT_XP_BOOST = {"start": "", "end": "", "multiplier": 2.0}          # a holiday XP window (Pacific dates)
MAX_XP = 100000

HOOK, COMMAND, CHAT, CALL, POST, COUNTER = "hook", "command", "chat", "call", "post", "counter"   # CALL: our own cog calls _q_record; POST: a message in a named channel

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
    channel: str = ""  # POST quests: the channel (found by name, or set in `.dailyquests`) where a message counts


QUESTS = (
    Quest("verdict", "Answer today's Verdict", "Tap your answer, then your guess, on today's Verdict question.", HOOK),
    Quest("pet", "Take care of the pet", "Tap Feed, Play or Clean on the pet's card. Any one counts, and it's free once a day.", HOOK),
    Quest("card", "Pull your daily card", "Tap **Daily Cards** in {#wondercasino}, or type `.card daily`.", HOOK),
    Quest("pick", "Pick a card from a drop", "Tap a card on a drop when one appears. Picking is free.", HOOK),
    Quest("puzzle", "Claim a puzzle piece", "React 🧩 on a new piece when one drops.", HOOK),
    Quest("minigame", "Win a minigame in {#cuddle}", "Be first to a drop, a math question or a hunt when one pops up.",
          HOOK),
    Quest("chat", "Send {n} messages in {#cuddle}", "Say hi, reply to someone, join in.", CHAT, target=10),
    Quest("fish", "Catch {n} fish", "Create a table in {#wondercasino}, then type `.fish` in your table thread.", COMMAND, target=3, command="fish"),
    Quest("star", "Give someone a star", "Thank a good noodle with `.stars @them` in {#bot-commands}.", COMMAND,
          command="stars"),
    Quest("blackjack", "Play a hand of Wonderjack", "Pick **Wonderjack** in the More… menu in {#wondercasino} and bet.",
          HOOK),
    Quest("sponsor", "Sponsor a tribute", "Send a parachute from the Hunger Games shop (`.hg`).", HOOK),
    Quest("canvas", "Paint a canvas pixel", "Pick **Pixel Canvas** in the More… menu in {#wondercasino}.", HOOK),
    Quest("selfie", "Post a selfie in {#selfie}", "Share a photo of yourself in {#selfie}.", POST, channel="selfie"),
    Quest("counting", "Count in {#counting}", "Add the next number in {#counting}.", POST, channel="counting"),
    Quest("heist", "Finish a heist", "Run one: `.heist start` in {#bot-commands}. Win or lose, it counts once it ends.", COUNTER),
    Quest("getaway", "Make a clean getaway", "Pull off a heist and don't get caught: `.heist start` in {#bot-commands}.", COUNTER),
    Quest("duel", "Play a duel", "Challenge someone with `.duel @them <wager>` and see the fight through.", HOOK),
    Quest("guide", "Look something up in the guide", "Type `.wld search card` (or any word) in {#bot-commands}.", HOOK),
    Quest("acro", "Play Acrophobia", "Enter an acronym or vote on one when a round pops up. Rounds run twice a day at busy hours.",
          HOOK),
    Quest("plant", "Drop coins in {#cuddle}", "Pick **Drop coins in #cuddle** in the More… menu in {#wondercasino}. 10% is burned. It counts once another member claims your drop (or use `.dropcoins`).",
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
            "payday": False, "streak": 0, "best_streak": 0, "last_active": "", "last_full": "", "sets": 0,
            "last_done": {}, "xp": 0, "rerolls": 0, "swapped": [], "notes": [],
            "ms_given": [], "week": {"key": "", "days": []}, "week_paid": "", "packs": 0, "set_pack_day": "",
            "base": {}}


def roll_day(state: Optional[dict], day: str) -> dict:
    """The member's state for `day`: yesterday's quests and progress cleared, history kept."""
    out = fresh_state()
    out.update(dict(state or {}))
    if out["day"] != day:
        out.update(day=day, assigned=[], progress={}, paid=[], earned=0, opened=False, payday=False,
                   xp=0, rerolls=0, swapped=[], notes=[], base={})
    out["progress"], out["base"] = dict(out["progress"]), dict(out["base"])
    out["last_done"] = dict(out["last_done"])
    out["notes"], out["ms_given"], out["swapped"] = list(out["notes"]), list(out["ms_given"]), list(out["swapped"])
    out["week"] = {"key": out["week"].get("key", ""), "days": list(out["week"].get("days", []))}
    # a quest removed from the pool after it was dealt (or paid) simply drops out of today's set
    out["assigned"] = [k for k in out["assigned"] if k in BY_KEY]
    out["paid"] = [k for k in out["paid"] if k in BY_KEY]
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
        state["last_full"] = state["day"]
        state["sets"] = state.get("sets", 0) + 1
    return new, set_done


# --------------------------------------------------------------------------- streak, milestones, weekly

def advance_streak(state: dict, day: str) -> Optional[int]:
    """The first quest finished on a day extends the streak (or restarts it after a missed day, which also
    reopens the milestone prizes). Returns the new length, or None when today was already counted."""
    last = state.get("last_active") or ""
    if last == day:
        return None
    if last == yesterday_of(day):
        streak = int(state.get("streak", 0)) + 1
    else:
        streak = 1
        state["ms_given"] = []
    state.update(streak=streak, last_active=day, best_streak=max(int(state.get("best_streak", 0)), streak))
    return streak


def current_streak(state: Optional[dict], day: str) -> int:
    """The streak as it stands today: it survives until a whole day is missed."""
    if not state or not state.get("last_active"):
        return 0
    return int(state.get("streak", 0)) if state["last_active"] in (day, yesterday_of(day)) else 0


def milestones_due(streak: int, given: Iterable[int]) -> list:
    done = set(given)
    return [d for d in MILESTONE_DAYS if d <= streak and d not in done]


def badge_for(days: int) -> Optional[tuple]:
    """(days, emoji, title) of the highest milestone reached with `days` days in a row, or None."""
    reached = [d for d in MILESTONE_DAYS if d <= days]
    return (reached[-1], *BADGES[reached[-1]]) if reached else None


def next_badge(streak: int) -> Optional[tuple]:
    ahead = [d for d in MILESTONE_DAYS if d > streak]
    return (ahead[0], *BADGES[ahead[0]]) if ahead else None


def badge_text(days: int) -> str:
    badge = badge_for(days)
    return f"{badge[1]} {badge[2]}" if badge else ""


def week_key(day: str) -> str:
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def track_week(state: dict, day: str, need: int) -> bool:
    """Record `day` as an active day of its (Monday to Sunday) week. True the first time the week reaches
    `need` days; the prize is paid once per week."""
    key = week_key(day)
    if state["week"].get("key") != key:
        state["week"] = {"key": key, "days": []}
    if day not in state["week"]["days"]:
        state["week"]["days"].append(day)
    if len(state["week"]["days"]) >= max(1, int(need)) and state.get("week_paid") != key:
        state["week_paid"] = key
        return True
    return False


def week_days(state: dict, day: str) -> int:
    week = state.get("week") or {}
    return len(week.get("days", [])) if week.get("key") == week_key(day) else 0


def prize_for(milestones: Optional[dict], days: int) -> dict:
    custom = (milestones or {}).get(str(days)) or {}
    out = dict(DEFAULT_PRIZES[days])
    out.update({k: custom[k] for k in ("coins", "xp", "pack") if k in custom})
    return out


def coins_for(payday: int, paydays: float, multiplier: float) -> int:
    """Coins for a prize worth `paydays` paydays at the guild multiplier."""
    return max(0, round(payday * float(paydays) * float(multiplier)))


# --------------------------------------------------------------------------- XP weekend, packs, reroll

def boost_multiplier(boost: Optional[dict], day: str) -> float:
    """The quest XP multiplier on `day` (Pacific date): the window's multiplier inside it, else 1."""
    boost = boost or {}
    start, end = boost.get("start") or "", boost.get("end") or ""
    try:
        value = float(boost.get("multiplier", 1) or 1)
    except (TypeError, ValueError):
        return 1.0
    return value if start and end and start <= day <= end and value > 0 else 1.0


def quest_xp(base: int, boost: Optional[dict], day: str) -> int:
    return max(0, round(int(base) * boost_multiplier(boost, day)))


def valid_day(text: str) -> bool:
    """A real YYYY-MM-DD date."""
    try:
        return date.fromisoformat(text).isoformat() == text
    except (ValueError, TypeError):
        return False


def reroll_cost(costs: Iterable[int], count: int) -> int:
    """What the next reroll costs after `count` today: the listed prices in order, then double the last again and
    again (1k, 5k, 10k, 20k, 40k...). No daily limit; the price is the limit."""
    prices = [max(0, int(c)) for c in costs] or list(DEFAULT_REROLL_COSTS)
    count = max(0, int(count))
    return prices[count] if count < len(prices) else prices[-1] * 2 ** (count - len(prices) + 1)


def set_pack_price(mode: str, pack_price: int) -> Optional[int]:
    """What a full set's card pack costs: None = no offer, 0 = free, else half the normal price."""
    if mode == "free":
        return 0
    if mode == "half":
        return max(0, int(pack_price) // 2)
    return None


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
    extra = "".join(f" · {int(entry.get(k, 0)):,} {label}" for k, label in
                    (("rerolls", "rerolls"), ("milestones", "streak/weekly prizes"), ("packs", "packs")) if entry.get(k))
    return (f"{int(entry.get('opens', 0)):,} opened · {both:,}/{payday:,} payday users{share} · "
            f"{int(entry.get('sets', 0)):,} full sets · {int(entry.get('paid', 0)):,} paid" + extra)


def faucet(completers: int, average_payday: int, multiplier: float) -> int:
    """Daily coins created if `completers` members finish a full set."""
    return max(0, round(completers * average_payday * multiplier))


def match_channel(slug: str, names: dict) -> Optional[int]:
    """Find a channel id by name. Names carry decoration ("🧸・cuddle"), so match the tail and require a
    non-word character before it, so "shop" never matches "wondershop"."""
    target = slug.lower()
    for channel_id, name in names.items():
        lowered = name.lower().strip()
        if not lowered.endswith(target):
            continue
        head = lowered[: len(lowered) - len(target)]
        if head == "" or not (head[-1].isalnum() or head[-1] == "-"):
            return channel_id
    return None
