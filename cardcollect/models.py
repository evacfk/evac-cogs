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
    series_alt: str = ""  # the series' English title (series holds AniList's romaji one), so search finds either

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
            "series_alt": self.series_alt,
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
            series_alt=data.get("series_alt", "") or "",
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

    collection: list = field(default_factory=list)  # list[int] of owned card_ids; a tradeable spare is a second entry
    showcase_card_ids: list = field(default_factory=list)  # list[int], up to MAX_SHOWCASE_SLOTS, ordered
    sell_tokens: list = field(default_factory=list)  # list[SellToken]
    daily_pull_date: str = ""  # ISO date of the last `.card daily`
    daily_streak: int = 0  # consecutive days of `.card daily`
    pity: int = 0  # consecutive lost draws against an equal rival; see engine.pity_multiplier
    wishlist: list = field(default_factory=list)  # list[int] of wished-for card_ids
    wish_pings_off: bool = False  # True = never ping this member about wishlist cards
    sets_claimed: list = field(default_factory=list)  # list[str] of series whose completion bonus was paid

    def owns(self, card_id: int) -> bool:
        return card_id in self.collection

    def to_dict(self) -> dict:
        return {
            "collection": list(self.collection),
            "showcase_card_ids": list(self.showcase_card_ids),
            "sell_tokens": [t.to_dict() for t in self.sell_tokens],
            "daily_pull_date": self.daily_pull_date,
            "daily_streak": self.daily_streak,
            "pity": self.pity,
            "wishlist": list(self.wishlist),
            "wish_pings_off": self.wish_pings_off,
            "sets_claimed": list(self.sets_claimed),
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
            daily_pull_date=data.get("daily_pull_date", ""),
            daily_streak=int(data.get("daily_streak", 0)),
            pity=int(data.get("pity", 0)),
            wishlist=list(data.get("wishlist", [])),
            wish_pings_off=bool(data.get("wish_pings_off", False)),
            sets_claimed=list(data.get("sets_claimed", [])),
        )


@dataclass
class ActiveDrop:
    """An in-flight drop: cards on offer and who has picked what. Lives in
    memory only -- losing it on a restart just means one drop is never
    resolved (its buttons answer "this drop is over"), not a real loss."""

    message_id: Optional[int]  # set once the drop message is actually sent
    guild_id: int
    channel_id: int
    cards: list  # list[dict]: {"card_id": int, "position": int}
    is_test: bool = False  # test drop: the draw runs fully but nothing is awarded
    picks: dict = field(default_factory=dict)  # user id -> position of the card they picked
    closes_at: Optional[float] = None  # time.monotonic() deadline for picks
    closes_epoch: int = 0  # the same moment as a unix time, for Discord's <t:...:R>
    resolved: bool = False  # set (synchronously) the moment the draw starts; no pick counts after it
    message: object = None  # the drop's Message, edited in place with the results
    guild: object = None  # the discord.Guild it was posted in
    view: object = None  # the per-drop button view (stopped once the draw is resolved)
    task: object = None  # the asyncio task that closes the draw

    def card_at(self, position: int) -> Optional[dict]:
        for c in self.cards:
            if c["position"] == position:
                return c
        return None

    def is_closed(self, now: float) -> bool:
        return self.resolved or (self.closes_at is not None and now >= self.closes_at)


@dataclass
class SeriesPreview:
    """State of one in-progress `.card importseries`: what AniList returned
    and how each character would be handled. Lives in memory only, for as long
    as the admin's preview message is open -- nothing is written to the pool
    until they press an Import button."""

    media: dict  # the picked search result (id, title, title_english, format, year)
    info: dict  # fetch_series_characters() result: title, title_english, total, characters
    built: dict  # build_series_entries() result: entries / links / skipped, each with a `rank`
    pool_tiers: dict  # active (non-retired) pool cards per rarity right now
    overrides: dict = field(default_factory=dict)  # rank -> rarity chosen by the admin
    skips: set = field(default_factory=set)  # ranks the admin left out of the import
    images: dict = field(default_factory=dict)  # rank -> AniList art already downloaded for the contact sheet
    art: dict = field(default_factory=dict)  # rank -> replacement art the admin uploaded (wins over AniList's)
    flags: dict = field(default_factory=dict)  # rank -> why the AniList art looks poor (placeholder, small, wide...)
