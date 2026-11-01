"""Pure game logic for cardcollect: rarity rolls, tier bucketing, drop-chance
rolls, drop building, the pick-your-card draw (owner priority and pity), series
sets, and claim outcomes. No discord/redbot
imports -- fully unit-testable with plain pytest, same split as
blackjacktable's engine.py.

Every function takes an optional `rng` (a random.Random instance) so tests
can inject a seeded one instead of depending on the global random module.
"""

import random
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .constants import FINISHES, SCORE_POINTS, PITY_CAP, PITY_STEP, SET_BONUS_BASE, SET_BONUS_PER_CARD, SET_MIN_CARDS, TIERS
from .models import ActiveDrop, Card, SellToken


def _rng(rng: Optional[random.Random]) -> random.Random:
    return rng if rng is not None else random._inst  # module-level default instance


def bucket_tier(favourites: int, cutoffs: dict) -> str:
    """Pick the rarest tier whose cutoff `favourites` meets or exceeds,
    checked rarest-first so a character sitting right at a boundary lands in
    the rarer tier, not the more common one."""
    for tier in ("legendary", "epic", "rare", "common"):
        if favourites >= cutoffs.get(tier, 0):
            return tier
    return "common"


def roll_tier(weights: dict, rng: Optional[random.Random] = None) -> str:
    """Weighted-random pick of a rarity tier. `weights` need not sum to 100 --
    any positive numbers work, they're normalized internally."""
    rng = _rng(rng)
    tiers = [t for t in TIERS if weights.get(t, 0) > 0]
    if not tiers:
        raise ValueError("No tier has a positive weight")
    total = sum(weights[t] for t in tiers)
    roll = rng.uniform(0, total)
    upto = 0.0
    for tier in tiers:
        upto += weights[tier]
        if roll <= upto:
            return tier
    return tiers[-1]  # float rounding fallback


def pick_card_for_tier(
    pool: Sequence[Card], tier: str, rng: Optional[random.Random] = None, boosts: Optional[Dict[int, float]] = None
) -> Optional[Card]:
    """Pick among pool cards in the given tier. Returns None if the pool has no
    cards in that tier (caller should re-roll or fall back). Every card is
    equally likely unless `boosts` gives it a weight (card_id -> multiplier,
    e.g. 3.0 for this week's featured series)."""
    rng = _rng(rng)
    candidates = [c for c in pool if c.rarity == tier]
    if not candidates:
        return None
    if not boosts:
        return rng.choice(candidates)
    weights = [max(0.0, float(boosts.get(c.card_id, 1.0))) for c in candidates]
    if sum(weights) <= 0:
        return rng.choice(candidates)
    return rng.choices(candidates, weights=weights, k=1)[0]


def should_drop(drop_chance: float, rng: Optional[random.Random] = None) -> bool:
    """One roll of the chance-per-message drop trigger."""
    rng = _rng(rng)
    if drop_chance <= 0:
        return False
    if drop_chance >= 1:
        return True
    return rng.random() < drop_chance


def build_drop(
    pool: Sequence[Card],
    weights: dict,
    drop_size: int,
    guild_id: int,
    channel_id: int,
    rng: Optional[random.Random] = None,
    is_test: bool = False,
    boosts: Optional[Dict[int, float]] = None,
) -> Optional[ActiveDrop]:
    """Roll a full drop: up to `drop_size` different cards, each an independent
    weighted-tier roll (a card is never offered twice in one drop). Returns
    None if the pool can't fill even one card (caller should just skip this tick
    rather than post a broken drop). A pool smaller than `drop_size` gives a
    smaller drop."""
    rng = _rng(rng)
    chosen: List[Card] = []
    taken = set()
    for _ in range(min(drop_size, len(pool))):
        card = None
        for _attempt in range(20):  # a tier with few cards may keep repeating one we already have
            tier = roll_tier(weights, rng)
            candidate = pick_card_for_tier([c for c in pool if c.card_id not in taken], tier, rng, boosts)
            if candidate is not None:
                card = candidate
                break
        if card is None:
            # empty tier (or unlucky repeats) -- fall back to any card not yet offered rather
            # than fail the whole drop over one unlucky roll into a tier you haven't imported yet
            remaining = [c for c in pool if c.card_id not in taken]
            if not remaining:
                break
            card = rng.choice(remaining)
        taken.add(card.card_id)
        chosen.append(card)
    if not chosen:
        return None
    return ActiveDrop(
        message_id=None,
        guild_id=guild_id,
        channel_id=channel_id,
        cards=[{"card_id": card.card_id, "position": i} for i, card in enumerate(chosen)],
        is_test=is_test,
    )


def owner_tier(collection: Sequence[int], card_id: int, max_copies: int) -> int:
    """How a member ranks for a card in a draw (lower wins): 0 = doesn't own it,
    1 = owns it but has room for a spare, 2 = at the copy limit (a win would
    only become a sell token). Everyone in the best tier present is drawn from;
    lower tiers only win when nobody better picked the card."""
    count = list(collection).count(card_id)
    if count == 0:
        return 0
    return 1 if count < max_copies else 2


def pity_multiplier(pity: int) -> float:
    """Draw weight for a member who has lost `pity` equal draws in a row:
    1 + PITY_STEP per loss, never more than PITY_CAP."""
    return min(PITY_CAP, 1.0 + PITY_STEP * max(0, pity))


def resolve_drop(
    cards: Sequence[dict],
    picks: Dict[int, int],
    collections: Dict[int, Sequence[int]],
    pities: Dict[int, int],
    max_copies: int,
    rng: Optional[random.Random] = None,
) -> Tuple[List[dict], Dict[int, int]]:
    """Run the draw for a drop. `picks` maps member id -> the position they
    picked; `collections` and `pities` hold each picker's current state.

    For each card: the pickers are ranked by owner_tier, and the winner is drawn
    at random (weighted by pity_multiplier) from the best tier present -- so a
    member who already owns the card only wins it if nobody who doesn't (or
    has room for a spare) picked it too. A card nobody picked is wasted.

    Returns (results, pity_after). results has one dict per card, in order:
    position, card_id, pickers (how many picked it), winner (member id or None),
    outcome ("new" | "duplicate" | "sell_token" | None), contested (True if the
    winner beat at least one equal rival) and boosted (True if the winner's
    pity bonus was active). pity_after has a new pity for every picker: 0 for
    the winner, +1 for each rival they beat in the same tier (a lost lottery),
    unchanged for anyone who lost on owner priority."""
    rng = _rng(rng)
    results: List[dict] = []
    pity_after: Dict[int, int] = {uid: int(pities.get(uid, 0)) for uid in picks}
    for entry in cards:
        position, card_id = entry["position"], entry["card_id"]
        pickers = sorted(uid for uid, pos in picks.items() if pos == position)
        if not pickers:
            results.append(
                {"position": position, "card_id": card_id, "pickers": 0, "winner": None, "outcome": None,
                 "contested": False, "boosted": False}
            )
            continue
        tiers = {uid: owner_tier(collections.get(uid, ()), card_id, max_copies) for uid in pickers}
        best = min(tiers.values())
        contenders = [uid for uid in pickers if tiers[uid] == best]
        weights = [pity_multiplier(pity_after.get(uid, 0)) for uid in contenders]
        roll = rng.uniform(0, sum(weights))
        upto = 0.0
        winner = contenders[-1]  # float-rounding fallback
        for uid, weight in zip(contenders, weights):
            upto += weight
            if roll <= upto:
                winner = uid
                break
        boosted = pity_multiplier(pity_after.get(winner, 0)) > 1.0
        for uid in contenders:
            pity_after[uid] = 0 if uid == winner else pity_after.get(uid, 0) + 1
        results.append(
            {
                "position": position,
                "card_id": card_id,
                "pickers": len(pickers),
                "winner": winner,
                "outcome": claim_outcome(collections.get(winner, ()), card_id, max_copies),
                "contested": len(contenders) > 1,
                "boosted": boosted,
            }
        )
    return results, pity_after


def build_sets(cards: Sequence[Card]) -> Dict[str, List[Card]]:
    """Series sets: every active (not retired) card grouped by its exact series
    text, keeping only series with at least SET_MIN_CARDS cards."""
    grouped: Dict[str, List[Card]] = {}
    for card in cards:
        if card.retired or not card.series:
            continue
        grouped.setdefault(card.series, []).append(card)
    return {series: members for series, members in grouped.items() if len(members) >= SET_MIN_CARDS}


def set_progress(sets: Dict[str, List[Card]], owned_ids) -> List[Tuple[str, int, int]]:
    """(series, owned, total) for every set the member has at least one card of,
    most complete first, then biggest, then A-Z."""
    owned = set(owned_ids)
    rows = []
    for series, members in sets.items():
        have = sum(1 for c in members if c.card_id in owned)
        if have:
            rows.append((series, have, len(members)))
    rows.sort(key=lambda r: (-(r[1] / r[2]), -r[2], _fold(r[0])))
    return rows


def set_bonus(size: int) -> int:
    """Wondercoin paid the first time a member completes a set of `size` cards."""
    return SET_BONUS_BASE + SET_BONUS_PER_CARD * size


def make_sell_token(card_id: int, rarity: str) -> SellToken:
    return SellToken(token_id=uuid.uuid4().hex[:12], card_id=card_id, rarity=rarity, claimed_at=time.time())


def sell_price(rarity: str, sell_prices: dict) -> int:
    return int(sell_prices.get(rarity, 0))


def claim_outcome(member_collection: Sequence[int], card_id: int, max_copies: int) -> str:
    """What claiming `card_id` would do to a member currently holding
    `member_collection` (a list of owned card_ids -- duplicates of the same
    id are allowed up to `max_copies`, see constants.MAX_COPIES_KEPT):

    - "new": the member owns none yet -- becomes their first copy.
    - "duplicate": the member owns at least one but fewer than `max_copies`
      -- becomes an extra, still-tradeable copy (a real gallery/collection
      entry, giftable with `.card give`), not converted to currency.
    - "sell_token": the member is already at `max_copies` -- this claim
      converts straight to a sell token instead of piling up a 3rd+ copy,
      so duplicates stay tradeable without being hoardable indefinitely.

    Read-only: takes the collection as given and returns a verdict, doesn't
    mutate anything. Used both for a real claim (whose result the caller
    then applies) and for a test-mode claim preview (whose result the
    caller only displays)."""
    count = list(member_collection).count(card_id)
    if count >= max_copies:
        return "sell_token"
    if count == 0:
        return "new"
    return "duplicate"


def today_str(tz: str, now: Optional[datetime] = None) -> str:
    """Today's date (ISO, e.g. '2026-09-22') in `tz`. `now` is injectable so
    quota logic is testable without depending on the real wall clock."""
    moment = now if now is not None else datetime.now(ZoneInfo(tz))
    return moment.date().isoformat()


def _fold(text: str) -> str:
    """Lowercase and strip accents so 'Rebecca' / 'rebecca' / 'Rébecca' all
    compare equal in a search."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


fold_text = _fold  # public name for other modules (import_characters)


def search_cards(cards: Sequence[Card], query: str) -> List[Card]:
    """Cards whose name or series contains every word of `query`, in any
    order and anywhere in the text ('lucy' finds 'Lucy Kushinada' and
    'Lucy' alike; 'lucy edgerunners' narrows to that series). Case- and
    accent-insensitive. Matches on the character's name rank ahead of
    series-only matches; ties sort by name, then id. An empty query
    matches nothing."""
    tokens = _fold(query).split()
    if not tokens:
        return []
    ranked = []
    for card in cards:
        name = _fold(card.name)
        series = _fold(card.series)
        series_alt = _fold(getattr(card, "series_alt", ""))
        haystack = f"{name} {series} {series_alt}"
        if all(t in haystack for t in tokens):
            in_name = all(t in name for t in tokens)
            ranked.append((0 if in_name else 1, name, card.card_id, card))
    ranked.sort(key=lambda r: r[:3])
    return [r[3] for r in ranked]


def daily_streak(last_date: str, streak: int, today: str) -> Tuple[bool, int]:
    """`.card daily` bookkeeping. Returns (can_pull, streak_after_this_pull).
    Already pulled today -> (False, streak). Pulled yesterday -> streak + 1.
    Anything older (or never) -> streak restarts at 1."""
    if last_date == today:
        return False, streak
    try:
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    except ValueError:
        return True, 1
    return True, (streak + 1 if last_date == yesterday else 1)


def upgrade_tier(tier: str) -> str:
    """One tier rarer; legendary stays legendary."""
    i = TIERS.index(tier)
    return TIERS[min(i + 1, len(TIERS) - 1)]


def is_bonus_day(streak: int, every: int) -> bool:
    return every > 0 and streak > 0 and streak % every == 0


def roll_daily(
    pool: Sequence[Card], weights: dict, bonus: bool, rng: Optional[random.Random] = None,
    boosts: Optional[Dict[int, float]] = None,
) -> Optional[Card]:
    """Roll a tier with `weights` (bumped one tier on a bonus day) and pick a
    card in it. If the pool has nothing in that tier, fall back to the nearest
    tier that has cards, preferring rarer first so a member is never shortchanged."""
    rng = _rng(rng)
    if not pool:
        return None
    tier = roll_tier(weights, rng)
    if bonus:
        tier = upgrade_tier(tier)
    i = TIERS.index(tier)
    order = [tier] + [TIERS[j] for j in range(i + 1, len(TIERS))] + [TIERS[j] for j in range(i - 1, -1, -1)]
    for t in order:
        card = pick_card_for_tier(pool, t, rng, boosts)
        if card is not None:
            return card
    return None


def _tier_fallback_order(tier: str) -> List[str]:
    """`tier`, then rarer tiers, then commoner ones: where to look when a tier
    has no cards left, so a member is never shortchanged."""
    i = TIERS.index(tier)
    return [tier] + TIERS[i + 1:] + TIERS[:i][::-1]


def pack_floor(pity: int, pity_after: int) -> str:
    """The guaranteed slot's lowest tier: rare, or epic once a member has opened
    `pity_after` packs in a row without an epic or better."""
    return "epic" if pity_after > 0 and pity >= pity_after else "rare"


def roll_pack(
    pool: Sequence[Card],
    weights: dict,
    size: int,
    floor: str = "rare",
    rng: Optional[random.Random] = None,
    boosts: Optional[Dict[int, float]] = None,
) -> List[Card]:
    """A daily pack: `size` different cards. All but the last roll with
    `weights` (any rarity); the last is guaranteed `floor` or better, rolled
    with the same weights restricted to those tiers. A tier with no cards left
    falls back to the nearest tier that has some, rarer first. A pool smaller
    than `size` gives a smaller pack."""
    rng = _rng(rng)
    remaining = list(pool)
    chosen: List[Card] = []
    floor_index = TIERS.index(floor)
    for slot in range(min(size, len(pool))):
        guaranteed = slot == size - 1
        slot_weights = dict(weights)
        if guaranteed:
            slot_weights = {t: w for t, w in weights.items() if t in TIERS and TIERS.index(t) >= floor_index}
            if not any(w > 0 for w in slot_weights.values()):
                slot_weights = {floor: 1}
        tier = roll_tier(slot_weights, rng)
        order = _tier_fallback_order(tier)
        if guaranteed:  # never fall back below the guarantee unless nothing at or above it is left
            order = [t for t in order if TIERS.index(t) >= floor_index] + [t for t in order if TIERS.index(t) < floor_index]
        card = None
        for t in order:
            card = pick_card_for_tier(remaining, t, rng, boosts)
            if card is not None:
                break
        if card is None:
            break
        remaining = [c for c in remaining if c.card_id != card.card_id]
        chosen.append(card)
    return chosen


def is_epic_or_better(rarity: str) -> bool:
    return rarity in TIERS and TIERS.index(rarity) >= TIERS.index("epic")


def upgrade_plan(
    collection: Sequence[int], sell_tokens: Sequence[SellToken], card_id: int, level: int, costs: dict
) -> dict:
    """What upgrading a member's copy of `card_id` from finish `level` to the
    next one takes, and whether they have it. Duplicates are that card's sell
    tokens first, then the tradeable spare (the member always keeps one copy).

    Returns {"ok", "reason", "level" (the new level), "finish" (its name),
    "dupes", "coins", "token_ids" (tokens to spend), "spares" (copies to spend),
    "have" (duplicates on hand)}. Coins aren't checked here -- the bank is."""
    count = list(collection).count(card_id)
    plan = {"ok": False, "reason": "", "level": level + 1, "finish": "", "dupes": 0, "coins": 0,
            "token_ids": [], "spares": 0, "have": 0}
    if count == 0:
        plan["reason"] = "You don't own that card."
        return plan
    if level + 1 >= len(FINISHES):
        plan["reason"] = f"It's already {FINISHES[-1].title()}, the top finish."
        return plan
    finish = FINISHES[level + 1]
    cost = costs.get(finish) or {}
    dupes, coins = max(0, int(cost.get("dupes", 0))), max(0, int(cost.get("coins", 0)))
    tokens = [t.token_id for t in sell_tokens if t.card_id == card_id]
    spares = count - 1
    plan.update(finish=finish, dupes=dupes, coins=coins, have=len(tokens) + spares)
    if len(tokens) + spares < dupes:
        plan["reason"] = (f"{finish.title()} needs {dupes} duplicate{'s' if dupes != 1 else ''} of this card "
                          f"(a sell token or your spare copy); you have {len(tokens) + spares}.")
        return plan
    plan["token_ids"] = tokens[:dupes]
    plan["spares"] = dupes - len(plan["token_ids"])
    plan["ok"] = True
    return plan


def missing_for_offer(collection: Sequence[int], card_ids: Sequence[int]) -> List[int]:
    """Card ids in an offer the member doesn't hold enough copies of (offering
    the same card twice needs two copies). Empty means they can give it all."""
    have = {}
    for cid in collection:
        have[cid] = have.get(cid, 0) + 1
    missing = []
    for cid in card_ids:
        if have.get(cid, 0) > 0:
            have[cid] -= 1
        else:
            missing.append(cid)
    return missing


def week_start(today: str) -> str:
    """The Monday (ISO date) of the week `today` falls in."""
    day = date.fromisoformat(today)
    return (day - timedelta(days=day.weekday())).isoformat()


def pick_featured(sets: Dict[str, List[Card]], history: Sequence[str], rng: Optional[random.Random] = None) -> Optional[str]:
    """A random set's series for the week, avoiding recent picks when anything
    else is available. None if there are no sets at all."""
    rng = _rng(rng)
    if not sets:
        return None
    fresh = [series for series in sorted(sets) if series not in set(history)]
    return rng.choice(fresh or sorted(sets))


def event_key(name: str) -> str:
    return _fold(name)


def event_status(event: Optional[dict], today: str) -> str:
    """"live", "upcoming" or "ended" for an event {"start", "end"} (ISO
    dates, inclusive); "ended" also covers an event that no longer exists."""
    if not event:
        return "ended"
    if today < event.get("start", ""):
        return "upcoming"
    if today > event.get("end", ""):
        return "ended"
    return "live"


def can_roll(card: Card, events: dict, today: str) -> bool:
    """Whether a card can come out of a drop, daily pull or pack right now:
    not retired, and if it's an event card, its event is live."""
    if card.retired:
        return False
    if not card.event:
        return True
    return event_status(events.get(event_key(card.event)), today) == "live"


def roll_boosts(
    pool: Sequence[Card], featured: Optional[str], featured_boost: float, event_boost: float = 1.0
) -> Dict[int, float]:
    """Per-card weights for a roll: this week's featured series, and (for packs)
    event cards. A card that is both gets both."""
    boosts: Dict[int, float] = {}
    for card in pool:
        weight = 1.0
        if featured and card.series == featured:
            weight *= featured_boost
        if card.event:
            weight *= event_boost
        if weight != 1.0:
            boosts[card.card_id] = weight
    return boosts


def collection_score(collection: Sequence[int], finishes: Dict[int, int], rarity_of: Dict[int, str]) -> int:
    """Rarity points for every different card owned, times (1 + finish level)."""
    total = 0
    for cid in set(collection):
        rarity = rarity_of.get(cid)
        if rarity is None:
            continue
        total += SCORE_POINTS.get(rarity, 0) * (1 + max(0, int(finishes.get(cid, 0))))
    return total


def pick_trade_up(
    pool: Sequence[Card], tier: str, collection: Sequence[int], max_copies: int, rng: Optional[random.Random] = None
) -> Optional[Card]:
    """A random card of `tier` for a trade-up, preferring ones the member
    doesn't own, then ones they have room for, so the reward is rarely just a
    sell token. None if the pool has nothing of that tier."""
    rng = _rng(rng)
    candidates = [c for c in pool if c.rarity == tier]
    if not candidates:
        return None
    for rank in (0, 1, 2):
        group = [c for c in candidates if owner_tier(collection, c.card_id, max_copies) == rank]
        if group:
            return rng.choice(group)
    return None


def gallery_sort_key(card: Card) -> Tuple[int, str, int]:
    """Collection order: rarest tier first (legendary, epic, rare, common),
    then alphabetical by name (case- and accent-insensitive), then card id so
    two same-named cards never swap places between views."""
    tier_rank = TIERS.index(card.rarity) if card.rarity in TIERS else -1
    return (-tier_rank, _fold(card.name), card.card_id)
