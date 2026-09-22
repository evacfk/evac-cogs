"""Pure game logic for cardcollect: rarity rolls, tier bucketing, drop-chance
rolls, emoji picking, and claim-fairness resolution. No discord/redbot
imports -- fully unit-testable with plain pytest, same split as
blackjacktable's engine.py.

Every function takes an optional `rng` (a random.Random instance) so tests
can inject a seeded one instead of depending on the global random module.
"""

import random
import time
import uuid
from typing import Iterable, List, Optional, Sequence

from .constants import TIERS
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


def pick_card_for_tier(pool: Sequence[Card], tier: str, rng: Optional[random.Random] = None) -> Optional[Card]:
    """Pick uniformly among pool cards in the given tier. Returns None if the
    pool has no cards in that tier (caller should re-roll or fall back)."""
    rng = _rng(rng)
    candidates = [c for c in pool if c.rarity == tier]
    if not candidates:
        return None
    return rng.choice(candidates)


def should_drop(drop_chance: float, rng: Optional[random.Random] = None) -> bool:
    """One roll of the chance-per-message drop trigger."""
    rng = _rng(rng)
    if drop_chance <= 0:
        return False
    if drop_chance >= 1:
        return True
    return rng.random() < drop_chance


def pick_drop_emojis(emoji_pool: Sequence[str], count: int, rng: Optional[random.Random] = None) -> List[str]:
    """Pick `count` distinct emoji for the real cards in one drop."""
    rng = _rng(rng)
    if count > len(emoji_pool):
        raise ValueError("count exceeds the size of the emoji pool")
    return rng.sample(list(emoji_pool), count)


def pick_decoy_emojis(
    emoji_pool: Sequence[str], exclude: Iterable[str], count: int, rng: Optional[random.Random] = None
) -> List[str]:
    """Pick `count` decoy emoji, guaranteed distinct from `exclude` (the real
    ones already chosen for this drop) and from each other."""
    rng = _rng(rng)
    exclude_set = set(exclude)
    available = [e for e in emoji_pool if e not in exclude_set]
    count = min(count, len(available))
    return rng.sample(available, count)


def build_drop(
    pool: Sequence[Card],
    weights: dict,
    drop_size: int,
    emoji_pool: Sequence[str],
    decoys_enabled: bool,
    decoy_count: int,
    guild_id: int,
    channel_id: int,
    rng: Optional[random.Random] = None,
    is_test: bool = False,
) -> Optional[ActiveDrop]:
    """Roll a full drop: `drop_size` cards, each an independent weighted-tier
    roll, each assigned a distinct claim emoji. Returns None if the pool
    can't fill a full drop (e.g. a tier the rolls landed on has zero cards
    and no fallback tier has cards either -- caller should just skip this
    tick rather than post a broken drop)."""
    rng = _rng(rng)
    cards: List[Card] = []
    for _ in range(drop_size):
        tier = roll_tier(weights, rng)
        card = pick_card_for_tier(pool, tier, rng)
        if card is None:
            # empty tier -- fall back to any non-empty tier rather than fail
            # the whole drop over one unlucky roll into a tier you haven't
            # imported yet
            non_empty_tiers = {c.rarity for c in pool}
            fallback_tiers = [t for t in TIERS if t in non_empty_tiers]
            if not fallback_tiers:
                return None
            card = pick_card_for_tier(pool, rng.choice(fallback_tiers), rng)
            if card is None:
                return None
        cards.append(card)

    emojis = pick_drop_emojis(emoji_pool, len(cards), rng)
    decoys = pick_decoy_emojis(emoji_pool, emojis, decoy_count, rng) if decoys_enabled else []

    drop_cards = [
        {"card_id": card.card_id, "emoji": emoji, "position": i}
        for i, (card, emoji) in enumerate(zip(cards, emojis))
    ]

    return ActiveDrop(
        message_id=None,
        guild_id=guild_id,
        channel_id=channel_id,
        cards=drop_cards,
        decoy_emojis=decoys,
        is_test=is_test,
    )


def reaction_add_order(drop: ActiveDrop, rng: Optional[random.Random] = None) -> List[str]:
    """The order in which the bot should add reactions to the drop message --
    real emoji and decoys shuffled together so position in the reaction bar
    never leaks which ones are real."""
    rng = _rng(rng)
    all_emoji = [c["emoji"] for c in drop.cards] + list(drop.decoy_emojis)
    rng.shuffle(all_emoji)
    return all_emoji


def resolve_claim(reactor_ids: Sequence[int], rng: Optional[random.Random] = None) -> Optional[int]:
    """Given every user id that reacted with a given real emoji within the
    collection window, pick the winner. Random among the collected reactors,
    not "whichever event arrived first" -- see design doc's claim-fairness
    decision. Returns None if nobody reacted (shouldn't normally be called
    in that case, but safe either way)."""
    rng = _rng(rng)
    if not reactor_ids:
        return None
    return rng.choice(list(reactor_ids))


def make_sell_token(card_id: int, rarity: str) -> SellToken:
    return SellToken(token_id=uuid.uuid4().hex[:12], card_id=card_id, rarity=rarity, claimed_at=time.time())


def sell_price(rarity: str, sell_prices: dict) -> int:
    return int(sell_prices.get(rarity, 0))


def would_be_dupe(member_collection: Sequence[int], card_id: int) -> bool:
    """Read-only check used by test-mode claims: would this card have been a
    new pickup or a duplicate, against the member's *real* collection --
    without writing anything. Lets a test claim show a realistic outcome."""
    return card_id in member_collection

