"""Pure logic for contest: schedule and phases, vote rules, placements, prize tiers. No discord or redbot imports."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from .constants import HOME_TZ, PLACES, TIER_GOLD, TIER_PARTICIPANT, TIER_SECOND, TIER_THIRD

SCHEDULE_KEYS = ("open_ts", "entries_close_ts", "voting_close_ts", "reveal_ts", "purge_ts")
SCHEDULE_LABELS = {
    "open_ts": "Channel opens, voting starts",
    "entries_close_ts": "Entries close",
    "voting_close_ts": "Voting closes",
    "reveal_ts": "Results revealed",
    "purge_ts": "Channel and photos deleted",
}

# ----------------------------------------------------------------------------- time


def pt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, HOME_TZ).strftime("%Y-%m-%d")


def fmt_pt(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, HOME_TZ)
    hour = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%a %b')} {dt.day}, {hour} PT"


def parse_pt(text: str) -> float:
    """'2026-10-31 23:00' (Pacific time) -> timestamp. Raises ValueError with a readable message."""
    try:
        dt = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Use the form YYYY-MM-DD HH:MM in Pacific time, e.g. 2026-10-31 23:00.")
    return dt.replace(tzinfo=HOME_TZ).timestamp()


def validate_schedule(s: dict) -> str:
    """'' when the schedule is coherent, else the problem."""
    if not s["open_ts"] < s["voting_close_ts"]:
        return "Voting must close after it opens."
    if not s["open_ts"] < s["entries_close_ts"]:
        return "Entries must close after the channel opens."
    if s["entries_close_ts"] > s["voting_close_ts"]:
        return "Entries cannot close after voting closes."
    if not s["voting_close_ts"] <= s["reveal_ts"]:
        return "Results cannot be revealed before voting closes."
    if not s["reveal_ts"] < s["purge_ts"]:
        return "The purge must come after the reveal."
    return ""


def voting_days(s: dict) -> list[str]:
    """Pacific dates on which a vote can be cast, oldest first."""
    first = datetime.fromtimestamp(s["open_ts"], HOME_TZ).date()
    last = datetime.fromtimestamp(s["voting_close_ts"] - 1, HOME_TZ).date()
    out, day = [], first
    while day <= last:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def phase(now: float, s: dict, *, launched: bool, revealed: bool, purged: bool) -> str:
    if not launched:
        return "setup"
    if purged:
        return "purged"
    if revealed:
        return "revealed"
    if now >= s["voting_close_ts"]:
        return "picks"
    if now >= s["open_ts"]:
        return "voting"
    return "entries"


def entries_open(now: float, s: dict, *, launched: bool, purged: bool = False) -> bool:
    return launched and not purged and now < s["entries_close_ts"]


def voting_open(now: float, s: dict, *, launched: bool, purged: bool = False) -> bool:
    return launched and not purged and s["open_ts"] <= now < s["voting_close_ts"]


# ----------------------------------------------------------------------------- people


def has_any(role_ids: Iterable[int], wanted: Iterable[int]) -> bool:
    wanted = set(wanted)
    return any(r in wanted for r in role_ids)


def is_excluded(role_ids: Iterable[int], excluded: Iterable[int]) -> bool:
    return has_any(role_ids, excluded)


# ----------------------------------------------------------------------------- votes
# votes: {"voter_id": {"YYYY-MM-DD": costume_number}}


def vote_problem(votes: dict, voter: int, number: int, today: str, owners: dict[int, int]) -> str:
    """'' if `voter` may vote for costume `number` today, else the reason. `owners` maps number -> entrant user id."""
    if number not in owners:
        return "That costume is not in the contest."
    if owners[number] == voter:
        return "You cannot vote for your own costume."
    for day, n in (votes.get(str(voter)) or {}).items():
        if n == number and day != today:
            return f"You already voted for Costume #{number} on another day. Pick a different costume today."
    return ""


def cast_vote(votes: dict, voter: int, number: int, today: str) -> dict:
    """Today's vote becomes `number` (replacing today's earlier pick). Returns a new dict."""
    new = {k: dict(v) for k, v in votes.items()}
    new.setdefault(str(voter), {})[today] = number
    return new


def drop_entry_votes(votes: dict, number: int) -> dict:
    new = {}
    for voter, days in votes.items():
        kept = {d: n for d, n in days.items() if n != number}
        if kept:
            new[voter] = kept
    return new


def tally(votes: dict, numbers: Iterable[int]) -> dict[int, int]:
    counts = {n: 0 for n in numbers}
    for days in votes.values():
        for n in days.values():
            if n in counts:
                counts[n] += 1
    return counts


def voters_for(votes: dict, number: int) -> int:
    return sum(1 for days in votes.values() if number in days.values())


def places_from_counts(counts: dict[int, int], places: int = PLACES) -> dict[int, int]:
    """Standard competition ranking: ties share a place and the next place is skipped (1, 1, 3).
    Entries with no votes never place. Returns {costume number: place} for places 1..`places`."""
    out = {}
    for n, c in counts.items():
        if c <= 0:
            continue
        place = 1 + sum(1 for other in counts.values() if other > c)
        if place <= places:
            out[n] = place
    return out


def days_voted(votes: dict, voter: int) -> int:
    return len(votes.get(str(voter)) or {})


# ----------------------------------------------------------------------------- mods' choice


def eligible_for_picks(approved: dict[int, int], mod_numbers: Iterable[int], pc_places: dict[int, int]) -> list[int]:
    """Costume numbers a mod may pick for Mods' Choice: approved, not entered by a mod, not already on the People's Choice podium."""
    mods = set(mod_numbers)
    return sorted(n for n in approved if n not in mods and n not in pc_places)


def picks_problem(picks: list, eligible: Iterable[int]) -> str:
    chosen = [p for p in picks if p is not None]
    if len(chosen) != len(set(chosen)):
        return "The same costume cannot be picked twice."
    ok = set(eligible)
    for p in chosen:
        if p not in ok:
            return (f"Costume #{p} cannot be picked: it is not in the contest, was entered by a mod, "
                    "or is already on the People's Choice podium.")
    return ""


def picks_complete(picks: list, eligible: Iterable[int]) -> bool:
    """True once every podium slot that can be filled is filled (fewer candidates than slots is fine)."""
    need = min(PLACES, len(list(eligible)))
    return len([p for p in picks if p is not None]) >= need


def mc_places(picks: list) -> dict[int, int]:
    return {p: i + 1 for i, p in enumerate(picks[:PLACES]) if p is not None}


# ----------------------------------------------------------------------------- prizes


def assign_tiers(approved: dict[int, int], pc_places: dict[int, int], mc: dict[int, int]) -> dict[int, dict]:
    """One tier per person: their best placement across both awards, else participant.
    `approved` maps costume number -> user id. Returns {user_id: {"tier": int, "number": int, "why": [...]}}."""
    out: dict[int, dict] = {}
    for number, uid in approved.items():
        options = []
        if number in pc_places:
            options.append((pc_places[number], f"People's Choice {ordinal(pc_places[number])}"))
        if number in mc:
            options.append((mc[number], f"Mods' Choice {ordinal(mc[number])}"))
        if options:
            options.sort()
            tier = _tier_for_place(options[0][0])
            why = [label for _, label in options]
        else:
            tier, why = TIER_PARTICIPANT, ["Participant"]
        best = out.get(uid)
        if best is None or tier < best["tier"]:
            out[uid] = {"tier": tier, "number": number, "why": why}
    return out


def _tier_for_place(place: int) -> int:
    return {1: TIER_GOLD, 2: TIER_SECOND, 3: TIER_THIRD}.get(place, TIER_PARTICIPANT)


def coins_for(tier: int, coins: dict) -> int:
    key = {TIER_GOLD: "gold", TIER_SECOND: "second", TIER_THIRD: "third"}.get(tier, "participant")
    return int(coins[key])


def role_kind(tier: int) -> str:
    return "gold" if tier == TIER_GOLD else "bronze"


def ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def next_number(approved_numbers: Iterable[int]) -> int:
    nums = list(approved_numbers)
    return (max(nums) + 1) if nums else 1


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def parse_pick(text: str) -> Optional[int]:
    """'#7', '7' or '' -> 7 / None. Raises ValueError for anything else."""
    t = text.strip().lstrip("#").strip()
    if not t:
        return None
    if not t.isdigit():
        raise ValueError(f"'{text.strip()}' is not a costume number.")
    return int(t)
