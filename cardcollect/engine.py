"""Pure game logic for cardcollect: rarity rolls, tier bucketing, drop-chance
rolls, drop building (with per-card CAPTCHA codes), and claim outcomes. No discord/redbot
imports -- fully unit-testable with plain pytest, same split as
blackjacktable's engine.py.

Every function takes an optional `rng` (a random.Random instance) so tests
can inject a seeded one instead of depending on the global random module.
"""

import random
import time
import uuid
from datetime import datetime
from typing import List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from . import captcha
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


def build_drop(
    pool: Sequence[Card],
    weights: dict,
    drop_size: int,
    guild_id: int,
    channel_id: int,
    rng: Optional[random.Random] = None,
    is_test: bool = False,
) -> Optional[ActiveDrop]:
    """Roll a full drop: `drop_size` cards, each an independent weighted-tier
    roll, each assigned its own CAPTCHA claim code (see captcha.py). Returns None if the pool
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

    codes = captcha.generate_codes(len(cards), rng)

    drop_cards = [
        {"card_id": card.card_id, "code": code, "position": i}
        for i, (card, code) in enumerate(zip(cards, codes))
    ]

    return ActiveDrop(
        message_id=None,
        guild_id=guild_id,
        channel_id=channel_id,
        cards=drop_cards,
        is_test=is_test,
    )


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


def has_quota_remaining(daily_claims: int, daily_claims_date: str, quota: int, today: str) -> bool:
    """Daily claim quota check (locked decision: resets at local midnight,
    not a rolling window). `quota <= 0` disables the check entirely (an
    admin-configured "unlimited" sentinel). The reset itself is lazy: a
    `daily_claims_date` that isn't today just means the stored count is
    stale and the member has their full quota again -- nothing has to run
    at midnight to make that true."""
    if quota <= 0:
        return True
    if daily_claims_date != today:
        return True
    return daily_claims < quota


def record_claim(daily_claims: int, daily_claims_date: str, today: str) -> Tuple[int, str]:
    """Returns the (daily_claims, daily_claims_date) pair to persist after a
    real claim. Same lazy-reset logic as has_quota_remaining: a stale date
    starts the count over at 1 instead of incrementing a stale number."""
    if daily_claims_date != today:
        return 1, today
    return daily_claims + 1, today

