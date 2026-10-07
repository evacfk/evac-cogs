"""Pure logic for wonderpet: meters, growth, neglect warnings, outcomes, weekly carers.

No discord.py / redbot imports, so it is fully unit-testable. All times are unix seconds.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from .constants import (
    ADULT_RETIRE_DAYS, CARE_FREE, CARE_GAIN, CARE_TREAT, DECAY_PER_HOUR, DEFAULT_NAMES, FORM_HAPPY_QUALITY,
    EMPTY_BELOW, FORM_RADIANT_QUALITY, GONE_HOURS, HOME_TZ, LEVEL_HOURS, MAX_CATCHUP_HOURS, METERS, NEGLECT_AVG, RECOVER_AVG,
    STAGES, TREAT_GAIN, TREATS, WORRIED_AVG,
)

ACTIONS = ("feed", "play", "clean")
ACTION_METER = {"feed": "hunger", "play": "happy", "clean": "clean"}


def local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, HOME_TZ).date()


def local_hour(ts: float) -> int:
    return datetime.fromtimestamp(ts, HOME_TZ).hour


def week_key(ts: float) -> str:
    iso = local_date(ts).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def prev_week_key(ts: float) -> str:
    return week_key(ts - 7 * 86400)


def new_pet(now: float, pet_id: int, name: str | None = None, rng=random) -> dict:
    return {
        "id": pet_id, "name": name or rng.choice(DEFAULT_NAMES), "stage": "egg", "form": None, "alive": True,
        "born_ts": now, "stage_ts": now, "last_tick_ts": now,
        "hunger": 80.0, "happy": 80.0, "clean": 80.0,
        "care_points": 0.0, "stage_care": 0.0, "neglect_hours": 0.0, "warn_level": 0, "weak": "hunger",
        "quality_sum": 0.0, "quality_hours": 0.0, "carers": {}, "ended": None,
    }


def avg_meter(pet: dict) -> float:
    return sum(pet[m] for m in METERS) / len(METERS)


def any_empty(pet: dict) -> bool:
    return any(pet[m] < EMPTY_BELOW for m in METERS)


def lowest_meter(pet: dict) -> str:
    return min(METERS, key=lambda m: pet[m])


def level_for(pet: dict) -> int:
    """0 fine, 1 needs help, 2 sick (growth paused), 3 critical, 4 final warning."""
    if pet["stage"] == "egg" or not pet["alive"]:
        return 0
    if avg_meter(pet) >= RECOVER_AVG and not any_empty(pet):
        return 0   # looked after again: better right away. The neglect clock still burns down on its own,
                   # so a relapse picks up where it left off instead of starting from zero.
    hours = pet["neglect_hours"]
    for lvl in (4, 3, 2):
        if hours >= LEVEL_HOURS[lvl]:
            return lvl
    if avg_meter(pet) < WORRIED_AVG or min(pet[m] for m in METERS) < 20:
        return 1
    return 0


def hours_left(pet: dict) -> float:
    """Hours of neglect remaining before the pet is lost."""
    return max(0.0, GONE_HOURS - pet["neglect_hours"])


def adult_form(pet: dict) -> str:
    hours = pet["quality_hours"]
    quality = pet["quality_sum"] / hours if hours else 0.0
    if quality >= FORM_RADIANT_QUALITY:
        return "radiant"
    if quality >= FORM_HAPPY_QUALITY:
        return "happy"
    return "scruffy"


def quality(pet: dict) -> float:
    return pet["quality_sum"] / pet["quality_hours"] if pet["quality_hours"] else 0.0


def growth_progress(pet: dict, now: float) -> dict | None:
    spec = STAGES.get(pet["stage"])
    if spec is None:
        return None
    min_hours, need, nxt = spec
    return {"next": nxt, "hours": (now - pet["stage_ts"]) / 3600, "hours_needed": min_hours,
            "care": pet["stage_care"], "care_needed": need}


def _end(pet: dict, now: float, kind: str) -> None:
    pet["alive"] = False
    pet["ended"] = {"kind": kind, "ts": now}


def advance(pet: dict, now: float) -> list[str]:
    """Age the pet to `now`. Returns events: hatched, grew:teen, grew:adult, warn:N, recovered,
    retired, lost:died, lost:ran_away. A long gap (restart/outage) only ages it MAX_CATCHUP_HOURS."""
    if not pet["alive"]:
        return []
    events: list[str] = []
    dt = max(0.0, min((now - pet["last_tick_ts"]) / 3600, MAX_CATCHUP_HOURS))
    pet["last_tick_ts"] = now

    if pet["stage"] != "egg" and dt > 0:
        for m in METERS:
            pet[m] = max(0.0, pet[m] - DECAY_PER_HOUR[m] * dt)
        avg = avg_meter(pet)
        pet["quality_sum"] += avg * dt
        pet["quality_hours"] += dt
        if avg < NEGLECT_AVG or any_empty(pet):
            pet["neglect_hours"] += dt
            pet["weak"] = lowest_meter(pet)
        elif avg >= RECOVER_AVG and not any_empty(pet):
            pet["neglect_hours"] = max(0.0, pet["neglect_hours"] - 2 * dt)

    if pet["neglect_hours"] >= GONE_HOURS:
        _end(pet, now, "died" if pet["weak"] == "hunger" else "ran_away")
        return events + [f"lost:{pet['ended']['kind']}"]

    level = level_for(pet)
    if level > pet["warn_level"]:
        events.append(f"warn:{level}")
    elif level == 0 and pet["warn_level"] >= 2:
        events.append("recovered")
    pet["warn_level"] = level

    events += _grow(pet, now)
    if pet["stage"] == "adult" and now - pet["stage_ts"] >= ADULT_RETIRE_DAYS * 86400:
        _end(pet, now, "retired")
        events.append("retired")
    return events


def _grow(pet: dict, now: float) -> list[str]:
    events: list[str] = []
    while pet["alive"] and pet["stage"] in STAGES and pet["warn_level"] < 2:
        min_hours, need, nxt = STAGES[pet["stage"]]
        if now - pet["stage_ts"] < min_hours * 3600 or pet["stage_care"] < need:
            break
        pet["stage"], pet["stage_ts"], pet["stage_care"] = nxt, now, 0.0
        if nxt == "baby":
            events.append("hatched")
        else:
            events.append(f"grew:{nxt}")
        if nxt == "adult":
            pet["form"] = adult_form(pet)
    return events


# --- daily limits (per member) ----------------------------------------------------------------

def daily_for(daily: dict | None, today: str) -> dict:
    """A member's day: which single free action they used (None = still free) and how many treats."""
    if not daily or daily.get("date") != today:
        return {"date": today, "used": None, "treats": 0}
    used = daily.get("used") or next((k for k in ACTIONS if daily.get(k)), None)  # 1.0.x stored a flag per action
    return {"date": today, "used": used, "treats": int(daily.get("treats", 0))}


# --- care --------------------------------------------------------------------------------------

def give_care(pet: dict, kind: str, uid: int) -> None:
    """A free Feed / Play / Clean."""
    meter = ACTION_METER[kind]
    pet[meter] = min(100.0, pet[meter] + CARE_GAIN)
    _credit(pet, uid, CARE_FREE)


def give_treat(pet: dict, key: str, uid: int) -> None:
    _label, _emoji, meters, _mult = TREATS[key]
    for m in meters:
        pet[m] = min(100.0, pet[m] + TREAT_GAIN)
    _credit(pet, uid, CARE_TREAT)


def _credit(pet: dict, uid: int, points: float) -> None:
    pet["care_points"] += points
    pet["stage_care"] += points
    pet["carers"][str(uid)] = pet["carers"].get(str(uid), 0) + 1


def bump_streak(state: dict, today: str) -> dict:
    """Record that a member cared today. state: {streak, best_streak, last_care}."""
    last = state.get("last_care") or ""
    if last == today:
        return {"streak": state.get("streak", 0), "best_streak": state.get("best_streak", 0), "last_care": today}
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    streak = state.get("streak", 0) + 1 if last == yesterday else 1
    return {"streak": streak, "best_streak": max(state.get("best_streak", 0), streak), "last_care": today}


def live_streak(state: dict, today: str) -> int:
    """The streak to show: still alive if they cared today or yesterday, otherwise 0."""
    last = state.get("last_care") or ""
    if not last:
        return 0
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    return state.get("streak", 0) if last in (today, yesterday) else 0


def treat_price(key: str, base: int) -> int:
    return int(base) * TREATS[key][3]


def record_week(week_carers: dict, now: float, uid: int, keep: int = 4) -> dict:
    wk = week_key(now)
    bucket = week_carers.setdefault(wk, {})
    bucket[str(uid)] = bucket.get(str(uid), 0) + 1
    for old in sorted(week_carers)[:-keep]:
        week_carers.pop(old, None)
    return week_carers


def top_carers(counts: dict | None, n: int = 5) -> list[tuple[int, int]]:
    ranked = sorted(((int(u), c) for u, c in (counts or {}).items()), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:n]


def forget_user(pet: dict | None, week_carers: dict, history: list, uid: int) -> None:
    key = str(uid)
    if pet:
        pet["carers"].pop(key, None)
    for bucket in week_carers.values():
        bucket.pop(key, None)
    for past in history:
        past.get("carers", {}).pop(key, None)


def history_entry(pet: dict, now: float) -> dict:
    top = top_carers(pet["carers"], 3)
    return {
        "id": pet["id"], "name": pet["name"], "stage": pet["stage"], "form": pet["form"],
        "born_ts": pet["born_ts"], "end_ts": (pet.get("ended") or {}).get("ts", now),
        "end": (pet.get("ended") or {}).get("kind"), "carers": dict(pet["carers"]),
        "top": [{"user": u, "n": c} for u, c in top], "quality": round(quality(pet), 1),
    }


# --- display helpers ---------------------------------------------------------------------------

def bar(value: float, width: int = 10) -> str:
    filled = max(0, min(width, round(value / 100 * width)))
    return "\N{LARGE GREEN SQUARE}" * filled + "\N{BLACK LARGE SQUARE}" * (width - filled) if value >= 40 else \
        "\N{LARGE RED SQUARE}" * filled + "\N{BLACK LARGE SQUARE}" * (width - filled)


def age_text(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int(seconds % 86400 // 3600)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h"
