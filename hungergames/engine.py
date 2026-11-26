"""Pure game logic for hungergames: schedule, draft, scoring, puzzles, the arena simulation, the shop
and the prize. No discord/redbot imports, so all of it is testable on its own.

An edition is one plain dict (stored in Config as-is). Tribute ids are strings (JSON keys).
"""
from __future__ import annotations

import math
import random
import re
from datetime import date, datetime, timedelta

from .constants import (
    BETRAYAL_ALIVE, BETRAYAL_CHANCE, BLOODBATH_SHARE, BREAD_BONUS, BROADCASTS, CHALLENGES, CHARISMA_GIFT_MAX,
    DAY_MULTIPLIER, FINALE_TARGET, FORAGE_FIND_CHANCE, GEAR_COMBAT, GEAR_HAZARD, GIFT_ITEMS,
    HAZARD_BASE, HAZARD_SAVE, HAZARD_STAT_WEIGHT, HIDE_BONUS, HOME_TZ, HUNT_BONUS, ITEMS, MAX_ALLIANCE,
    MED_KIT_SAVE, PARACHUTES_PER_DAY, POWER_RANDOM, SCHEDULE, SOFT_CAP_CEILING, SOFT_CAP_FLOOR, SPEED_MAX,
    STEW_BONUS, TEST_SCHEDULE, TICKET_CAP, UNUSED_ITEM_FINALE_BONUS, ACCURACY_MAX, AGILITY_MISS_COST,
    STRENGTH_ROUNDS, ENDURANCE_MAX, INTELLECT_PUZZLES,
)

ARENA_KINDS = ("fire", "mutt", "flood", "night", "poison", "rule_change")


# =====================================================================================================
# time and schedule
# =====================================================================================================

def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, HOME_TZ)


def local_date(ts: float) -> date:
    return local_dt(ts).date()


def day_key(ts: float) -> str:
    return local_date(ts).isoformat()


def sunday_of(ts: float) -> date:
    """The Sunday that starts the (Pacific) week containing ts."""
    d = local_date(ts)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def week_schedule(sunday: date) -> list:
    """[[step, ts], ...] for an edition whose Reaping is on `sunday`. DST-safe: built from local wall times."""
    out = []
    for step, offset, hour, minute in SCHEDULE:
        d = sunday + timedelta(days=offset)
        out.append([step, datetime(d.year, d.month, d.day, hour, minute, tzinfo=HOME_TZ).timestamp()])
    return out


def test_schedule(start_ts: float) -> list:
    return [[step, start_ts + secs] for step, secs in TEST_SCHEDULE]


def next_reaping(now: float) -> tuple:
    """(sunday, schedule) of the next edition whose Reaping can still be joined.

    This week's edition counts until its Second Reaping; after that the next one is a week out, so an
    outage never starts an edition with no time left to volunteer.
    """
    sunday = sunday_of(now)
    sched = week_schedule(sunday)
    if now >= step_ts(sched, "second_reaping"):
        sunday = sunday + timedelta(days=7)
        sched = week_schedule(sunday)
    return sunday, sched


def step_ts(sched: list, name: str, nth: int = 1) -> float:
    seen = 0
    for step, ts in sched:
        if step == name:
            seen += 1
            if seen == nth:
                return ts
    raise KeyError(name)


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def edition_title(ed: dict) -> str:
    if ed.get("test"):
        return "The Hunger Games (test run)"
    return f"The {ordinal(ed['edition'])} Hunger Games"


def current_phase(ed: dict | None) -> str:
    if not ed:
        return "idle"
    if ed.get("victor"):
        return "complete"
    step = ed["step"]
    if step == 0:
        return "idle"
    return ed["schedule"][step - 1][0]


# =====================================================================================================
# edition
# =====================================================================================================

def new_edition(number: int, schedule: list, *, test: bool, channel_id: int, rng: random.Random) -> dict:
    feast = rng.choice((2, 3))
    arena = rng.choice([i for i in (3, 4) if i != feast] or [4])
    return {
        "edition": number, "test": test, "schedule": schedule, "step": 0, "channel_id": channel_id,
        "roster": {}, "swap_used": [], "swap_requests": {}, "withdrawn": [],
        "threads": {}, "interview_msgs": {}, "cheers": {}, "puzzles_used": [],
        "alliances": [], "ally_requests": {},
        "ledger": [], "used": [], "broadcast": None, "broadcasts_done": 0,
        "feast_broadcast": feast, "arena_broadcast": arena, "arena_kind": rng.choice(ARENA_KINDS),
        "messages": {}, "victor": None, "prize": 0, "minted": 0, "pressers": [],
        "counts": {"volunteers": 0, "swap_requests": 0, "swaps": 0, "withdrawals": 0, "cheers": 0},
    }


def new_tribute(name: str, origin: str, *, fake: bool = False) -> dict:
    return {
        "name": name, "origin": origin, "tag": origin == "drafted", "fake": fake, "alive": True,
        "kills": 0, "death": None, "challenges": {}, "interview": None, "stats": {}, "score": 0, "total": 0,
        "strategy": "hide", "inventory": {"consumables": [], "gear": []}, "finale_bonus": 0, "parachutes": 0,
    }


def alive_ids(ed: dict) -> list:
    return [uid for uid, t in ed["roster"].items() if t["alive"]]


def due_step(ed: dict, now: float):
    """The next step if it is due, else None."""
    if ed["step"] >= len(ed["schedule"]):
        return None
    name, ts = ed["schedule"][ed["step"]]
    return name if now >= ts else None


def broadcast_number(ed: dict) -> int:
    """1-based number of the broadcast step about to run (or running)."""
    return ed["broadcasts_done"] + 1


# =====================================================================================================
# the draft
# =====================================================================================================

def tickets(messages: int) -> float:
    return math.sqrt(max(0, min(messages, TICKET_CAP)))


def weighted_draw(candidates: dict, k: int, rng: random.Random) -> list:
    """Draw up to k ids without replacement, weighted by tickets(messages)."""
    pool = {uid: tickets(n) for uid, n in candidates.items() if tickets(n) > 0}
    out = []
    while pool and len(out) < k:
        total = sum(pool.values())
        pick = rng.random() * total
        acc = 0.0
        chosen = None
        for uid, w in sorted(pool.items()):
            acc += w
            if pick <= acc:
                chosen = uid
                break
        chosen = chosen or sorted(pool)[-1]
        out.append(chosen)
        del pool[chosen]
    return out


def draft_pool(people: list, roster: dict, *, min_messages: int, last_tributes: list, withdrawn: list) -> dict:
    """people: [{uid, messages, bot, lurker, opt_out}] -> {uid: messages} of who may be drafted."""
    out = {}
    blocked = set(roster) | set(map(str, last_tributes)) | set(map(str, withdrawn))
    for p in people:
        uid = str(p["uid"])
        if p.get("bot") or p.get("lurker") or p.get("opt_out") or uid in blocked:
            continue
        if p["messages"] < min_messages:
            continue
        out[uid] = p["messages"]
    return out


def seat_for_withdrawal(ed: dict, target: str):
    """Who takes a withdrawn tribute's seat: the first pending swap requester, else None."""
    req = ed["swap_requests"].get(target)
    if req:
        for uid in req["volunteers"]:
            if uid not in ed["roster"]:
                return uid
    return None


# =====================================================================================================
# training scores
# =====================================================================================================

def agility_accuracy(misses: int) -> float:
    return max(0, ACCURACY_MAX - AGILITY_MISS_COST * misses)


def strength_accuracy(correct: int) -> float:
    return ACCURACY_MAX * correct / STRENGTH_ROUNDS


def endurance_accuracy(longest_cleared: int) -> float:
    return ACCURACY_MAX * max(0, longest_cleared - 2) / (ENDURANCE_MAX - 2)


def intellect_accuracy(first_try: int, second_try: int) -> float:
    return ACCURACY_MAX * (first_try + 0.5 * second_try) / INTELLECT_PUZZLES


def speed_scores(times: dict) -> dict:
    """times: {uid: seconds or None (did not finish)} -> {uid: 0-30}. Lower time is better."""
    finished = {u: t for u, t in times.items() if t is not None and t > 0}
    if not finished:
        return {u: 0.0 for u in times}
    fastest = min(finished.values())
    return {u: (SPEED_MAX * fastest / finished[u] if u in finished else 0.0) for u in times}


def score_challenges(roster: dict) -> None:
    """Fill each tribute's stats (0-100 per challenge), total and Training Score (1-12) from their results.

    A challenge result is {"acc": 0-70, "time": seconds, "finished": bool}; a missing one scores 0.
    """
    for kind in CHALLENGES:
        times = {}
        for uid, t in roster.items():
            r = t["challenges"].get(kind)
            times[uid] = r["time"] if r and r.get("finished") and "acc" in r else None
        speeds = speed_scores(times)
        for uid, t in roster.items():
            r = t["challenges"].get(kind)
            # a challenge that was started but never recorded (a restart mid-run) scores 0
            t["stats"][kind] = round(r["acc"] + speeds[uid]) if r and "acc" in r else 0
    for t in roster.values():
        t["total"] = sum(t["stats"].get(k, 0) for k in CHALLENGES)
        t["score"] = training_score(t["total"])


def training_score(total: float) -> int:
    return 1 + int(math.floor(max(0, min(400, total)) / 400 * 11))


def charisma_scores(cheers: dict, uids) -> dict:
    counts = {u: len(set(cheers.get(u, []))) for u in uids}
    top = max(counts.values(), default=0)
    if top == 0:
        return {u: 50 for u in uids}
    return {u: round(100 * c / top) for u, c in counts.items()}


def ranking(roster: dict, messages: dict | None = None) -> list:
    """Tribute ids best first: score, then raw total, then 7-day messages."""
    messages = messages or {}
    return sorted(roster, key=lambda u: (-roster[u]["score"], -roster[u]["total"], -messages.get(u, 0), u))


# =====================================================================================================
# Capitol Broadcast puzzles
# =====================================================================================================

def normalize_answer(text: str) -> str:
    text = re.sub(r"[^A-Z0-9 ]", "", (text or "").upper()).strip()
    text = re.sub(r"^(A|AN|THE) ", "", text)
    return text.replace(" ", "")


def scramble(word: str, rng: random.Random) -> str:
    for _ in range(20):
        parts = []
        for w in word.split(" "):
            letters = list(w)
            rng.shuffle(letters)
            parts.append("".join(letters))
        out = " ".join(parts)
        if out != word:
            return out
    return word[::-1]


def caesar(word: str, shift: int) -> str:
    return "".join(chr((ord(c) - 65 + shift) % 26 + 65) if c.isalpha() else c for c in word.upper())


def make_puzzle(kind: str, item, rng: random.Random) -> dict:
    if kind == "scramble":
        word = item.upper()
        return {"id": f"word:{word}", "kind": "Unscramble the arena word",
                "prompt": " ".join(scramble(word, rng).replace(" ", "/")), "answers": [word]}
    if kind == "caesar":
        word = item.upper()
        shift = rng.randint(1, 3)
        return {"id": f"word:{word}", "kind": f"Capitol code: shift every letter back by {shift}",
                "prompt": caesar(word, shift), "answers": [word]}
    return {"id": f"riddle:{item['q'][:40]}", "kind": "Riddle", "prompt": item["q"], "answers": list(item["a"])}


def pick_puzzles(bank: dict, rng: random.Random, avoid=()) -> list:
    """One scramble, one Capitol code and one riddle; avoids ids in `avoid` while it can."""
    avoid = set(avoid)
    words = list(bank["words"])
    riddles = list(bank["riddles"])
    rng.shuffle(words)
    rng.shuffle(riddles)

    def first(items, key):
        fresh = [i for i in items if key(i) not in avoid]
        return (fresh or items)[0]

    w1 = first(words, lambda w: f"word:{w.upper()}")
    avoid.add(f"word:{w1.upper()}")
    w2 = first([w for w in words if w != w1], lambda w: f"word:{w.upper()}")
    riddle = first(riddles, lambda r: f"riddle:{r['q'][:40]}")
    return [make_puzzle("scramble", w1, rng), make_puzzle("caesar", w2, rng), make_puzzle("riddle", riddle, rng)]


def check_answer(puzzle: dict, guess: str) -> bool:
    g = normalize_answer(guess)
    return bool(g) and any(g == normalize_answer(a) for a in puzzle["answers"])


# =====================================================================================================
# survival maths
# =====================================================================================================

def soft_cap(raw: float) -> float:
    """Survival chance (percent) with diminishing returns: full value to 60, half to 80, a quarter after."""
    if raw <= 60:
        v = raw
    elif raw <= 80:
        v = 60 + (raw - 60) / 2
    else:
        v = 70 + (raw - 80) / 4
    return max(SOFT_CAP_FLOOR, min(SOFT_CAP_CEILING, v))


def broadcast_budget(alive: int, broadcasts_left: int, rng: random.Random) -> int:
    """Deaths to aim for in one Tue-Fri broadcast so 2-3 reach the finale."""
    if broadcasts_left <= 0:
        return 0
    base = round(max(0, alive - FINALE_TARGET) / broadcasts_left)
    if broadcasts_left == 1:   # the last one before the finale lands the target exactly (or one under)
        return max(0, min(base + rng.randint(0, 1), alive - 2))
    jitter = rng.randint(-1, 1) if base > 0 or alive > FINALE_TARGET else 0
    return max(0, min(base + jitter, alive - 2))


def bloodbath_quota(alive: int, rng: random.Random) -> int:
    share = rng.uniform(*BLOODBATH_SHARE)
    return max(0, min(max(1, round(alive * share)), alive - 2))


# =====================================================================================================
# the arena
# =====================================================================================================

def _render(text: str, names: list) -> str:
    if len(names) == 1:
        text = text.replace("{tribute}", names[0])
    keys = ("{tribute_a}", "{tribute_b}", "{tribute_c}", "{tribute_d}")
    for key, name in zip(keys, names):
        text = text.replace(key, name)
    return text


class Arena:
    """One broadcast's worth of simulation over an edition dict. Mutates the edition."""

    def __init__(self, ed: dict, pool: dict, rng: random.Random):
        self.ed, self.pool, self.rng = ed, pool, rng
        self.roster = ed["roster"]
        self.used = set(ed.get("used", []))
        self.lines: list = []
        self.dead_now: list = []
        self.templates = pool["templates"]
        self.text = pool["lines"]
        self.last_before_finale = False

    # ---------- helpers ----------

    def name(self, uid: str) -> str:
        return f"**{self.roster[uid]['name']}**"

    def say(self, text: str, names=(), kind: str = "calm"):
        """One arena line. `uids` is who it is about (the first is the one an avatar is shown for) and `kind`
        drives its icon: calm, death, save, boost, gift, find or victor. A later kill() upgrades the line to death."""
        self.lines.append({"t": "line", "text": _render(text, [self.name(u) for u in names]),
                           "uids": [str(u) for u in names], "kind": kind})

    def header(self, title: str, desc: str = "", scene: str = ""):
        self.lines.append({"t": "header", "title": title, "desc": desc, "scene": scene})

    def pick_line(self, key: str, sub: str | None = None) -> str:
        src = self.text[key]
        if sub is not None:
            src = src.get(sub) or src.get("other") or []
        return self.rng.choice(src) if src else ""

    def alive(self) -> list:
        return alive_ids(self.ed)

    def allies_of(self, uid: str) -> set:
        for group in self.ed["alliances"]:
            if uid in group:
                return set(group) - {uid}
        return set()

    def allied(self, a: str, b: str) -> bool:
        return b in self.allies_of(a)

    def inv(self, uid: str) -> dict:
        return self.roster[uid]["inventory"]

    def has(self, uid: str, item: str) -> bool:
        inv = self.inv(uid)
        return item in inv["gear"] or item in inv["consumables"]

    def consume(self, uid: str, item: str) -> bool:
        cons = self.inv(uid)["consumables"]
        if item in cons:
            cons.remove(item)
            return True
        return False

    def stat(self, uid: str, key: str) -> float:
        return float(self.roster[uid]["stats"].get(key, 0) or 0)

    def kill(self, victim: str, killers=(), phase: str = ""):
        t = self.roster[victim]
        t["alive"] = False
        t["death"] = {"broadcast": self.ed["broadcasts_done"] + 1, "by": list(killers), "phase": phase}
        for k in killers:
            if k in self.roster:
                self.roster[k]["kills"] += 1
        self.dead_now.append(victim)
        if self.lines and self.lines[-1]["t"] == "line":   # the line just said is this death (a fight can kill two)
            last = self.lines[-1]
            if last["kind"] != "death":
                last["uids"] = [str(k) for k in killers] + [u for u in last["uids"] if u not in map(str, killers)]
                last["kind"] = "death"
            if victim not in last["uids"]:
                last["uids"].append(victim)
        for group in self.ed["alliances"]:
            if victim in group:
                group.remove(victim)
        self.ed["alliances"] = [g for g in self.ed["alliances"] if len(g) >= 2]

    def choose_template(self, *, phase: str, fatal: bool | None = None, size: int | None = None, max_size: int = 4,
                        kind: str | None = None, hazard: str | None = None, tag: str | None = None,
                        max_victims: int = 99, extra=None):
        def ok(t):
            if phase != "any" and phase not in t["phases"] and "any" not in t["phases"]:
                return False
            if fatal is not None and bool(t["victims"]) != fatal:
                return False
            if size is not None and t["size"] != size:
                return False
            if t["size"] > max_size or len(t["victims"]) > max_victims:
                return False
            if kind is not None and t["type"] != kind:
                return False
            if hazard is not None and t.get("hazard") != hazard:
                return False
            if tag is not None and tag not in t.get("tags", []):
                return False
            return extra(t) if extra else True

        cands = [t for t in self.templates if ok(t)]
        fresh = [t for t in cands if t["id"] not in self.used]
        pick = self.rng.choice(fresh or cands) if (fresh or cands) else None
        if pick:
            self.used.add(pick["id"])
        return pick

    # ---------- strength of a tribute in a fight / against a hazard ----------

    def power(self, uid: str, stat: str) -> float:
        """Fight strength, counting a held stew and any finale bonus (spent later by spend_boosts)."""
        t = self.roster[uid]
        p = self.stat(uid, stat or "strength")
        p += sum(v for g, v in GEAR_COMBAT.items() if g in t["inventory"]["gear"])
        if t["strategy"] == "hunt":
            p += HUNT_BONUS
        if "stew" in t["inventory"]["consumables"]:
            p += STEW_BONUS
        p += t.get("finale_bonus") or 0
        return p + self.rng.uniform(0, POWER_RANDOM)

    def spend_boosts(self, uids):
        for uid in uids:
            if self.consume(uid, "stew"):
                self.say(self.pick_line("boost", "stew"), [uid], "boost")
            self.roster[uid]["finale_bonus"] = 0

    def survival_chance(self, uid: str, stat: str) -> float:
        t = self.roster[uid]
        raw = HAZARD_BASE + self.stat(uid, stat or "endurance") * HAZARD_STAT_WEIGHT
        raw += sum(v for g, v in GEAR_HAZARD.items() if g in t["inventory"]["gear"])
        if t["strategy"] == "hide":
            raw += HIDE_BONUS
        if self.consume(uid, "bread"):
            raw += BREAD_BONUS
            self.say(self.pick_line("boost", "bread"), [uid], "boost")
        raw += t.get("finale_bonus") or 0
        t["finale_bonus"] = 0
        return soft_cap(raw)

    def try_save(self, uid: str, hazard: str | None, *, narrate: bool = True) -> bool:
        """A matching consumable or a Med Kit saves a tribute who would die. True if saved.

        narrate=False is for fights: the caller posts one "silver parachute" line that names the attacker,
        so the item's own line would tell the same rescue twice (and could be about a hazard, not a fight)."""
        specific = HAZARD_SAVE.get(hazard or "")
        if specific and self.consume(uid, specific):
            if narrate:
                self.say(self.pick_line("save", specific), [uid], "save")
            return True
        if self.consume(uid, "med_kit"):
            if self.rng.random() < MED_KIT_SAVE:
                if narrate:
                    self.say(self.pick_line("save", "med_kit"), [uid], "save")
                return True
            self.say(self.pick_line("medkit_fail"), [uid])
        return False

    # ---------- picking who takes part ----------

    def weighted_take(self, remaining: list, weights: dict, k: int, exclude=()) -> list:
        pool = [u for u in remaining if u not in exclude]
        out = []
        while pool and len(out) < k:
            ws = [weights.get(self.roster[u]["strategy"], 1.0) for u in pool]
            pick = self.rng.choices(pool, weights=ws, k=1)[0]
            out.append(pick)
            pool.remove(pick)
        return out

    # ---------- events ----------

    def combat(self, remaining: list, phase: str, deaths_left: int, final: bool = False) -> int:
        """A fight that kills someone (unless a Med Kit intervenes). Returns deaths caused."""
        tmpl = self.choose_template(phase=phase, fatal=True, kind="combat", max_size=len(remaining),
                                    max_victims=deaths_left, size=2 if final else None)
        if tmpl is None:
            return 0
        group = None
        no_ally_kills = len(self.alive()) > BETRAYAL_ALIVE
        for _ in range(6):
            cand = self.weighted_take(remaining, {"hunt": 2.0, "forage": 1.0, "hide": 0.5}, tmpl["size"])
            if len(cand) < tmpl["size"]:
                break
            if no_ally_kills and any(self.allied(a, b) for a in cand for b in cand if a != b):
                continue
            group = cand
            break
        if group is None:
            self.used.discard(tmpl["id"])
            return 0
        stat = tmpl.get("stat") or "strength"
        powers = {u: self.power(u, stat) for u in group}
        order = sorted(group, key=lambda u: -powers[u])
        n_k, n_v = len(tmpl["killers"]), len(tmpl["victims"])
        killers, victims = order[:n_k], order[len(order) - n_v:]
        middle = [u for u in order if u not in killers and u not in victims]
        # a multi-victim line can't show one victim being saved, and an armed killer gets a weapon line:
        # both become a one-on-one fight between the strongest and the weakest
        armed = self.has(killers[0], "weapon") and self.rng.random() < 0.6
        medkit = n_v > 1 and any("med_kit" in self.inv(v)["consumables"] for v in victims)
        if armed or medkit:
            alt = None
            if armed:
                alt = self.choose_template(phase="any", fatal=True, size=2, tag="weapon")
            if alt is None and (medkit or tmpl["size"] > 2):
                alt = self.choose_template(phase=phase, fatal=True, size=2, kind="combat",
                                           extra=lambda t: "weapon" not in t.get("tags", []))
            if alt is not None:
                self.used.discard(tmpl["id"])
                tmpl = alt
                killers, victims, middle = killers[:1], victims[-1:], []
                group = killers + victims
                n_k, n_v = 1, 1
        roles = [None] * tmpl["size"]
        for idx, u in zip(tmpl["killers"], killers):
            roles[idx] = u
        for idx, u in zip(tmpl["victims"], victims):
            roles[idx] = u
        rest = iter(middle)
        roles = [r if r is not None else next(rest) for r in roles]
        for u in group:
            remaining.remove(u)
        self.spend_boosts(group)
        saved = [v for v in victims if self.try_save(v, "combat", narrate=False)]
        if saved:
            self.say(self.pick_line("combat_saved"), [killers[0], saved[0]], "save")
            return 0
        self.say(tmpl["text"], roles)
        for v in victims:
            self.kill(v, killers, phase)
        return len(victims)

    def hazard(self, remaining: list, phase: str, hazard_kind: str | None = None) -> int:
        """A deadly hazard: a survival check, then a matching item, then death."""
        tmpl = self.choose_template(phase=phase if not hazard_kind else "any", fatal=True, size=1,
                                    kind="hazard", hazard=hazard_kind)
        if tmpl is None and hazard_kind:
            tmpl = self.choose_template(phase=phase, fatal=True, size=1, kind="hazard")
        if tmpl is None:
            return 0
        uid = self.weighted_take(remaining, {"forage": 1.5, "hunt": 1.0, "hide": 0.7}, 1)[0]
        remaining.remove(uid)
        hz = tmpl.get("hazard") or "other"
        if self.rng.random() * 100 < self.survival_chance(uid, tmpl.get("stat") or "endurance"):
            esc = self.choose_template(phase="any", fatal=False, size=1, kind="hazard", hazard=hz)
            self.say(esc["text"] if esc else self.pick_line("escape", hz), [uid])
            return 0
        if self.try_save(uid, hz):
            return 0
        self.say(tmpl["text"], [uid])
        self.kill(uid, (), phase)
        return 1

    def betrayal(self, remaining: list, phase: str, deaths_left: int) -> int:
        groups = [[u for u in g if u in remaining] for g in self.ed["alliances"]]
        groups = [g for g in groups if len(g) >= 2]
        if not groups or self.rng.random() >= BETRAYAL_CHANCE:
            return 0
        a, b = self.rng.sample(self.rng.choice(groups), 2)
        if deaths_left > 0:
            tmpl = self.choose_template(phase="any", fatal=True, size=2, tag="betrayal")
            if tmpl is None:
                return 0
            pa, pb = self.power(a, "strength"), self.power(b, "strength")
            killer, victim = (a, b) if pa >= pb else (b, a)
            remaining.remove(a)
            remaining.remove(b)
            self.spend_boosts([a, b])
            if self.try_save(victim, "combat", narrate=False):
                self.say(self.pick_line("combat_saved"), [killer, victim], "save")
                self.split(a, b)
                return 0
            self.say(tmpl["text"], [killer, victim])
            self.kill(victim, [killer], phase)
            return 1
        tmpl = self.choose_template(phase="any", fatal=False, size=2, tag="break")
        if tmpl is None:
            return 0
        remaining.remove(a)
        remaining.remove(b)
        self.say(tmpl["text"], [a, b])
        self.split(a, b)
        return 0

    def split(self, a: str, b: str):
        for group in self.ed["alliances"]:
            if a in group and b in group:
                group.remove(b)
        self.ed["alliances"] = [g for g in self.ed["alliances"] if len(g) >= 2]

    def ally(self, members: list):
        """Join members into one alliance if it stays within the size limit."""
        merged = set(members)
        others = []
        for g in self.ed["alliances"]:
            if merged & set(g):
                merged |= set(g)
            else:
                others.append(g)
        if len(merged) > MAX_ALLIANCE:
            return False
        others.append(sorted(merged))
        self.ed["alliances"] = others
        return True

    def calm(self, remaining: list, phase: str):
        """A non-fatal event for the next tribute (or a few)."""
        first = remaining[0]
        strategy = self.roster[first]["strategy"]
        mates = [u for u in remaining[1:] if self.allied(first, u)]
        # allies tend to spend their time together
        if mates and self.rng.random() < 0.5:
            tmpl = self.choose_template(phase=phase, fatal=False, size=2, kind="social",
                                        extra=lambda t: not t.get("alliance"))
            if tmpl:
                pair = [first, mates[0]]
                for u in pair:
                    remaining.remove(u)
                self.say(tmpl["text"], pair)
                return
        # gear flavor now and then
        gear = self.inv(first)["gear"]
        if gear and self.rng.random() < 0.15:
            g = self.rng.choice(gear)
            line = self.pick_line("gear", g)
            if line:
                remaining.remove(first)
                self.say(line, [first])
                return
        tag = {"hunt": "hunt", "hide": "hide", "forage": "forage"}.get(strategy)
        tmpl = None
        if tag and self.rng.random() < 0.6:
            tmpl = self.choose_template(phase=phase, fatal=False, tag=tag, max_size=len(remaining),
                                        extra=lambda t: not t.get("alliance") and t["type"] != "item")
        if tmpl is None:
            tmpl = self.choose_template(phase=phase, fatal=False, max_size=len(remaining),
                                        extra=lambda t: t["type"] not in ("item",) and (
                                            not t.get("alliance") or t["alliance"] == "form"))
        if tmpl is None:
            remaining.remove(first)
            self.say(self.pick_line("filler"), [first])
            return
        group = [first] + self.rng.sample(remaining[1:], tmpl["size"] - 1)
        for u in group:
            remaining.remove(u)
        if tmpl.get("alliance") == "form" and not self.ally(group):
            self.used.discard(tmpl["id"])
            fallback = self.choose_template(phase=phase, fatal=False, size=tmpl["size"], kind="social",
                                            extra=lambda t: not t.get("alliance")) if tmpl["size"] <= 2 else None
            if fallback is None:
                for u in group:
                    self.say(self.pick_line("filler"), [u])
                return
            tmpl = fallback
        self.say(tmpl["text"], group)
        if "forage" in tmpl.get("tags", []) and self.rng.random() < FORAGE_FIND_CHANCE:
            item = self.rng.choice(GIFT_ITEMS)
            self.inv(first)["consumables"].append(item)
            self.say(self.pick_line("forage_find").replace("{item}", ITEMS[item][0]), [first], "find")
        if phase == "feast" and "feast" in tmpl.get("tags", []):
            item = self.most_needed(first)
            self.inv(first)["consumables"].append(item)
            self.say(self.pick_line("feast_find").replace("{item}", ITEMS[item][0]), [first], "find")

    def most_needed(self, uid: str) -> str:
        have = set(self.inv(uid)["consumables"])
        for item in ("med_kit", "burn_cream", "serum", "water", "bread"):
            if item not in have:
                return item
        return "stew"

    # ---------- phases ----------

    def phase(self, phase: str, quota: int, *, hazard_kind: str | None = None) -> int:
        remaining = self.alive()
        self.rng.shuffle(remaining)
        deaths = 0
        floor = 2
        if len(self.alive()) <= BETRAYAL_ALIVE and phase != "bloodbath":
            deaths += self.betrayal(remaining, phase, min(quota, len(self.alive()) - floor))
        while remaining:
            before = len(remaining)
            left = min(quota - deaths, len(self.alive()) - floor)
            want = left > 0 and self.rng.random() < min(1.0, 2.0 * left / len(remaining))
            if want:
                if hazard_kind and self.rng.random() < 0.8:
                    deaths += self.hazard(remaining, phase, hazard_kind)
                elif len(remaining) >= 2 and self.rng.random() < 0.75:
                    deaths += self.combat(remaining, phase, left)
                else:
                    deaths += self.hazard(remaining, phase)
            elif hazard_kind and self.rng.random() < 0.5:
                esc = self.choose_template(phase="any", fatal=False, size=1, kind="hazard", hazard=hazard_kind)
                if esc:
                    self.say(esc["text"], [remaining.pop(0)])
            if remaining and len(remaining) == before:
                self.calm(remaining, phase)
        if phase == "night" and self.last_before_finale:
            short = min(quota - deaths, len(self.alive()) - floor)
            pool = self.alive()
            self.rng.shuffle(pool)
            while short > 0 and len(pool) >= 2:
                got = self.combat(pool, "night", short)
                if not got and pool:
                    pool.pop(0)
                deaths += got
                short -= got
        return deaths

    def gifts(self):
        for uid in self.alive():
            chance = self.roster[uid]["stats"].get("charisma", 50) / 100 * CHARISMA_GIFT_MAX
            if self.rng.random() < chance:
                item = self.rng.choice(GIFT_ITEMS)
                self.inv(uid)["consumables"].append(item)
                self.say(self.pick_line("gift").replace("{item}", f"{ITEMS[item][1]} {ITEMS[item][0]}"), [uid], "gift")

    def broadcast(self, index: int) -> list:
        """Tue-Fri broadcast `index` (1-4): returns the queue of lines to post."""
        ed = self.ed
        alive = len(self.alive())
        n = ed["broadcasts_done"] + 1
        if index == 1:
            self.header("🔔 The Bloodbath", self.pick_line("bloodbath_intro"), "bloodbath")
            plan = [("bloodbath", bloodbath_quota(alive, self.rng))]
            budget = broadcast_budget(alive, BROADCASTS - index + 1, self.rng)
            plan.append(("night", max(0, min(budget - plan[0][1], 1))))
        else:
            budget = broadcast_budget(alive, BROADCASTS - index + 1, self.rng)
            phases = ["day"]
            if index == ed["feast_broadcast"]:
                phases.append("feast")
            if index == ed["arena_broadcast"]:
                phases.append("arena")
            phases.append("night")
            plan = []
            left = budget
            for i, p in enumerate(phases):
                share = math.ceil(left / (len(phases) - i)) if left > 0 else 0
                plan.append((p, share))
                left -= share
        self.last_before_finale = index == BROADCASTS
        gifted = False
        carry = 0
        for p, quota in plan:
            if p == "day":
                self.header(f"☀️ Day {n}", self.pick_line("day_intro"), "day")
            elif p == "night":
                self.header(f"🌙 Night {n}", self.pick_line("night_intro"), "night")
            elif p == "feast":
                self.header("🍖 The Feast", self.pick_line("feast_intro"), "feast")
            elif p == "arena":
                kind = ed["arena_kind"]
                self.header("⚠️ The Gamemakers intervene", self.pick_line("arena_intro", kind), "arena")
                if kind == "rule_change":
                    p = "day"
            if not gifted:   # the Capitol's gifts open the first scene, under its header (not loose above it)
                gifted = True
                self.gifts()
            want = quota + carry
            got = self.phase(p, want, hazard_kind=ed["arena_kind"] if p == "arena" else None)
            carry = max(0, want - got)
        self.lines.append({"t": "fallen", "uids": list(self.dead_now), "night": n})
        ed["used"] = sorted(self.used)
        return self.lines

    def finale(self) -> list:
        ed = self.ed
        self.header("🏟️ The Finale", self.pick_line("finale_intro"), "finale")
        for uid in self.alive():
            cons = self.inv(uid)["consumables"]
            if cons:
                bonus = UNUSED_ITEM_FINALE_BONUS * len(cons)
                self.roster[uid]["finale_bonus"] = bonus
                names = ", ".join(ITEMS[c][0] for c in cons)
                self.say(self.pick_line("unused_bonus").replace("{items}", names).replace("{bonus}", str(bonus)), [uid])
                cons.clear()
        guard = 0
        while len(self.alive()) > 1:
            guard += 1
            remaining = self.alive()
            self.rng.shuffle(remaining)
            if guard > 40:   # never loops forever: a plain fight with no saves
                a, b = remaining[:2]
                pa, pb = self.power(a, "strength"), self.power(b, "strength")
                killer, victim = (a, b) if pa >= pb else (b, a)
                self.spend_boosts([a, b])
                tmpl = self.choose_template(phase="finale", fatal=True, size=2, kind="combat")
                self.say(tmpl["text"] if tmpl else "{tribute_a} outlasts {tribute_b}. 💥", [killer, victim])
                self.kill(victim, [killer], "finale")
                continue
            if len(remaining) == 2 or self.rng.random() < 0.7:
                self.combat(remaining, "finale", 1, final=len(remaining) == 2)
            else:
                self.hazard(remaining, "finale", self.rng.choice(("fire", "flood", "mutt", None)))
        victor = self.alive()[0]
        ed["victor"] = victor
        self.say(self.pick_line("victor").replace("{edition}", edition_title(ed)), [victor], "victor")
        self.lines.append({"t": "fallen", "uids": list(self.dead_now), "night": None})
        self.lines.append({"t": "victor", "uid": victor})
        ed["used"] = sorted(self.used)
        return self.lines

    def disqualify(self, uids: list) -> list:
        for uid in uids:
            if uid in self.roster and self.roster[uid]["alive"]:
                self.say(self.pick_line("dq"), [uid])
                self.kill(uid, (), "dq")
        return self.lines


def pending_deaths(ed: dict) -> set:
    """Tributes who die in the broadcast that is still being posted. The whole night is simulated up front,
    so anything that shows who is alive (status, plans) must treat these as still standing until the
    broadcast has finished posting, or a slow broadcast spoils its own deaths."""
    b = ed.get("broadcast")
    if not b or b["pos"] >= len(b["queue"]):
        return set()
    n = ed["broadcasts_done"]
    return {u for u, t in ed["roster"].items() if not t["alive"] and (t.get("death") or {}).get("broadcast") == n}


def shown_kills(ed: dict, uid: str, hidden: set) -> int:
    """Kills as the audience has seen them: not counting the pending deaths."""
    return ed["roster"][uid]["kills"] - sum(
        1 for v in hidden if uid in (ed["roster"][v].get("death") or {}).get("by", ()))


def run_broadcast(ed: dict, pool: dict, rng: random.Random, *, gone=()) -> list:
    """Generate the next broadcast (or the finale after 4) and return its queue. Mutates `ed`."""
    arena = Arena(ed, pool, rng)
    if gone:
        arena.disqualify(list(gone))
    index = ed["broadcasts_done"] + 1
    if index > BROADCASTS:
        lines = arena.finale()
    else:
        lines = arena.broadcast(index)
    ed["broadcasts_done"] = index
    return lines


# =====================================================================================================
# alliances (from the tributes' own plans)
# =====================================================================================================

def form_alliance(ed: dict, a: str, b: str) -> str | None:
    """Ally a and b. Returns an error, or None on success."""
    roster = ed["roster"]
    if a not in roster or b not in roster or not roster[a]["alive"] or not roster[b]["alive"]:
        return "That tribute isn't in the arena any more."
    arena = Arena(ed, {"templates": [], "lines": {}}, random.Random(0))
    if arena.allied(a, b):
        return "You're already allies."
    if not arena.ally([a, b]):
        return f"Alliances can have at most {MAX_ALLIANCE} tributes."
    return None


# =====================================================================================================
# the shop
# =====================================================================================================

def unit_price(base: int, ts: float, *, test: bool = False) -> int:
    mult = 1.0 if test else DAY_MULTIPLIER.get(local_dt(ts).weekday(), 1.0)
    return int(math.floor(base * mult / 100.0 + 0.5)) * 100   # half up, not banker's rounding


def shop_open(ed: dict | None) -> bool:
    if not ed or ed.get("victor"):
        return False
    done = [s for s, _ in ed["schedule"][: ed["step"]]]
    if "scores" not in done or "finale" in done:
        return False
    if ed.get("broadcast") and ed["broadcast"].get("queue"):
        return False
    return bool(alive_ids(ed))


def purchase_error(ed: dict, sponsor: str, tribute: str, item: str, today: str) -> str | None:
    roster = ed["roster"]
    if item not in ITEMS:
        return "That item doesn't exist."
    if tribute not in roster or not roster[tribute]["alive"]:
        return "That tribute is no longer in the arena."
    if sponsor == tribute:
        return "You can't sponsor yourself."
    for row in ed["ledger"]:
        if row["sponsor"] == sponsor and row["tribute"] == tribute:
            return "You've already sent this tribute a parachute this Games. Back someone else!"
    today_count = sum(1 for row in ed["ledger"] if row["tribute"] == tribute and row["day"] == today)
    if today_count >= PARACHUTES_PER_DAY:
        return f"{roster[tribute]['name']} already got {PARACHUTES_PER_DAY} parachutes today. Try tomorrow."
    if ITEMS[item][3] == "gear" and item in roster[tribute]["inventory"]["gear"]:
        return f"{roster[tribute]['name']} already has that."
    return None


def apply_purchase(ed: dict, sponsor: str, tribute: str, item: str, price: int, today: str, ts: float) -> None:
    inv = ed["roster"][tribute]["inventory"]
    (inv["gear"] if ITEMS[item][3] == "gear" else inv["consumables"]).append(item)
    ed["roster"][tribute]["parachutes"] += 1
    ed["ledger"].append({"sponsor": sponsor, "tribute": tribute, "item": item, "price": price, "day": today, "ts": ts})


def total_spend(ed: dict) -> int:
    return sum(row["price"] for row in ed["ledger"])


def prize(spend: int, share: float, floor: int) -> tuple:
    """(payout, newly minted). Only the part of the floor that the share doesn't cover is new money."""
    cut = int(math.floor(spend * share + 0.5))
    payout = max(cut, floor)
    return payout, max(0, payout - cut)


# =====================================================================================================
# the crowning
# =====================================================================================================

def awards(ed: dict) -> dict:
    roster = ed["roster"]
    out = {}
    if roster:
        k = max(roster, key=lambda u: (roster[u]["kills"], roster[u]["alive"]))
        if roster[k]["kills"]:
            out["most_kills"] = (k, roster[k]["kills"])
        d = max(roster, key=lambda u: roster[u]["parachutes"])
        if roster[d]["parachutes"]:
            out["darling"] = (d, roster[d]["parachutes"])
        cheers = {u: len(set(ed["cheers"].get(u, []))) for u in roster}
        f = max(cheers, key=lambda u: cheers[u])
        if cheers[f]:
            out["fan_favorite"] = (f, cheers[f])
    spent: dict = {}
    for row in ed["ledger"]:
        spent[row["sponsor"]] = spent.get(row["sponsor"], 0) + row["price"]
    if spent:
        top = max(spent, key=lambda u: spent[u])
        out["top_patron"] = (top, spent[top])
    return out


def sponsors_of(ed: dict, tribute: str) -> list:
    seen = []
    for row in ed["ledger"]:
        if row["tribute"] == tribute and row["sponsor"] not in seen:
            seen.append(row["sponsor"])
    return seen


def edition_report(ed: dict) -> dict:
    roster = ed["roster"]
    spend = total_spend(ed)
    done = {k: sum(1 for t in roster.values() if k in t["challenges"]) for k in CHALLENGES}
    done["interview"] = sum(1 for t in roster.values() if t.get("interview"))
    return {
        "edition": ed["edition"], "test": ed.get("test", False), "tributes": len(roster),
        "volunteers": ed["counts"].get("volunteers", 0), "swap_requests": ed["counts"].get("swap_requests", 0),
        "swaps": ed["counts"].get("swaps", 0), "withdrawals": ed["counts"].get("withdrawals", 0),
        "challenges_done": done, "cheer_voters": len({v for vs in ed["cheers"].values() for v in vs}),
        "sponsors": len({row["sponsor"] for row in ed["ledger"]}), "items": len(ed["ledger"]),
        "spent": spend, "prize": ed.get("prize", 0), "minted": ed.get("minted", 0),
        "burned": max(0, spend - (ed.get("prize", 0) - ed.get("minted", 0))),
        "unique_members": len(set(ed.get("pressers", []))),
    }
