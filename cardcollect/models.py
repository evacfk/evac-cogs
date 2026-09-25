"""Data shapes for cardcollect. Pure dataclasses, zero discord/redbot dependency.

Config (a redbot.core.Config guild/member store) holds plain dicts, not these
objects directly -- to_dict()/from_dict() convert at the boundary, mirroring
how blackjacktable's models.py relates to its Config schema.
"""

from dataclasses import dataclass, field
from typing import Optional

from .constants import TIERS


@dataclass
class Card:
    """One entry in a guild's card pool (not a specific owned copy)."""

    card_id: int
    name: str
    series: str
    rarity: str
    image_path: str
    favourites: int = 0
    added_by: Optional[int] = None  # None for AniList-imported cards, else the admin's id
    retired: bool = False  # removed from future rolls, but existing owners keep it
    anilist_id: Optional[int] = None  # None for hand-added cards; used to dedupe re-imports

    def __post_init__(self):
        if self.rarity not in TIERS:
            raise ValueError(f"Unknown rarity {self.rarity!r}, must be one of {TIERS}")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "series": self.series,
            "rarity": self.rarity,
            "image_path": self.image_path,
            "favourites": self.favourites,
            "added_by": self.added_by,
            "retired": self.retired,
            "anilist_id": self.anilist_id,
        }

    @staticmethod
    def from_dict(card_id: int, data: dict) -> "Card":
        return Card(
            card_id=card_id,
            name=data["name"],
            series=data.get("series", ""),
            rarity=data["rarity"],
            image_path=data["image_path"],
            favourites=data.get("favourites", 0),
            added_by=data.get("added_by"),
            retired=data.get("retired", False),
            anilist_id=data.get("anilist_id"),
        )


@dataclass
class SellToken:
    """A pending duplicate, awaiting `.card sell`. Rarity is captured at claim
    time so later re-tuning of tier cutoffs never retroactively changes a
    token a member is already holding."""

    token_id: str
    card_id: int
    rarity: str
    claimed_at: float

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "card_id": self.card_id,
            "rarity": self.rarity,
            "claimed_at": self.claimed_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "SellToken":
        return SellToken(
            token_id=data["token_id"],
            card_id=data["card_id"],
            rarity=data["rarity"],
            claimed_at=data["claimed_at"],
        )


@dataclass
class MemberState:
    """One member's cardcollect state within a guild."""

    collection: list = field(default_factory=list)  # list[int] of owned card_ids, no duplicates
    showcase_card_ids: list = field(default_factory=list)  # list[int], up to MAX_SHOWCASE_SLOTS, ordered
    sell_tokens: list = field(default_factory=list)  # list[SellToken]
    daily_claims: int = 0  # real claims made on daily_claims_date; see engine.has_quota_remaining
    daily_claims_date: str = ""  # ISO date (ACTIVITY_TIMEZONE) daily_claims was last reset for

    def owns(self, card_id: int) -> bool:
        return card_id in self.collection

    def to_dict(self) -> dict:
        return {
            "collection": list(self.collection),
            "showcase_card_ids": list(self.showcase_card_ids),
            "sell_tokens": [t.to_dict() for t in self.sell_tokens],
            "daily_claims": self.daily_claims,
            "daily_claims_date": self.daily_claims_date,
        }

    @staticmethod
    def from_dict(data: dict) -> "MemberState":
        showcase = data.get("showcase_card_ids")
        if showcase is None:
            # pre-showcase data only ever had a single favorite_card_id --
            # carry it over as a one-card showcase instead of losing it
            legacy_favorite = data.get("favorite_card_id")
            showcase = [legacy_favorite] if legacy_favorite is not None else []
        return MemberState(
            collection=list(data.get("collection", [])),
            showcase_card_ids=list(showcase),
            sell_tokens=[SellToken.from_dict(t) for t in data.get("sell_tokens", [])],
            daily_claims=data.get("daily_claims", 0),
            daily_claims_date=data.get("daily_claims_date", ""),
        )


@dataclass
class ActiveDrop:
    """An in-flight, unresolved drop. Lives in memory only -- losing this on
    a restart just means one drop silently expires, not a real loss."""

    message_id: Optional[int]  # set once the drop message is actually sent
    guild_id: int
    channel_id: int
    cards: list  # list[dict]: {"card_id": int, "code": str, "position": int}
    claimed_positions: set = field(default_factory=set)  # positions already resolved
    claimed_by: set = field(default_factory=set)  # user ids who already won a card from this drop
    is_test: bool = False  # test-mode drop: claims resolve fully but nothing is awarded
    # user id -> number of wrong (but code-shaped) guesses made on this drop
    wrong_guesses: dict = field(default_factory=dict)
    # Snapshotted from guild config when the drop is posted, so the claim
    # hot path (on_message) needs no Config awaits before it reaches the
    # claim lock -- an await there would let near-simultaneous submissions
    # reorder themselves before the lock could serialize them fairly.
    max_wrong_guesses: int = 0  # 0 = unlimited
    wrong_guess_penalty_seconds: float = 0.0
    expires_at: Optional[float] = None  # time.monotonic() deadline; None = never

    def entry_for_code(self, code: str) -> Optional[dict]:
        """The card entry whose code is `code` (already normalized), or None."""
        for c in self.cards:
            if c["code"] == code:
                return c
        return None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def is_locked_out(self, user_id: int) -> bool:
        """True once `user_id` has used up their wrong guesses on this drop."""
        return self.max_wrong_guesses > 0 and self.wrong_guesses.get(user_id, 0) >= self.max_wrong_guesses
