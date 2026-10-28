"""Main cog: message listener (chance-per-message drop trigger), the pick-your-card
draw, and all `.card` commands.

Game logic lives in engine.py (pure, unit-tested standalone); this file is the
Discord-facing wiring around it -- Config I/O, the message listener, image
compositing calls into imagegen.py, and command handlers. The button/pop-up UI
itself lives in views.py.

The draw: a drop is one image of numbered cards with one button per card. Anyone
who presses a card's button before the draw closes (90s by default) is entered
for that card; they may change their pick until then, and may be in for only one
card per drop. When the window closes the bot draws a random winner per card --
members who don't own the card win before members who do, and a member who keeps
losing equal draws gets a small pity boost (engine.resolve_drop) -- then edits the
drop message in place with the results. Nothing is typed into chat. (This replaced
a CAPTCHA claim, which replaced an emoji-reaction mechanic.)
"""

import asyncio
import functools
import io
import json
import logging
import random
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import aiohttp
import discord
from redbot.core import Config, bank, commands
from redbot.core.data_manager import cog_data_path

from . import embeds, engine, imagegen, import_characters, sources, storage, views
from . import panel as panel_specs
from .settings_panel import PanelError, open_panel
from .constants import (
    ACTIVITY_TIMEZONE,
    ART_MAX_UPLOAD_BYTES,
    COG_VERSION,
    DAILY_STREAK_BONUS_EVERY,
    DEFAULT_DAILY_WEIGHTS,
    DEFAULT_DRAW_WINDOW_SECONDS,
    DEFAULT_DROP_CHANCE,
    DEFAULT_DROP_COOLDOWN_SECONDS,
    DEFAULT_DROP_SIZE,
    DEFAULT_DROP_WEIGHTS,
    DEFAULT_SELL_PRICES,
    DEFAULT_TIER_CUTOFFS,
    GALLERY_PAGE_SIZE,
    IMPORT_GENDERS,
    MAX_COPIES_KEPT,
    MAX_DRAW_WINDOW_SECONDS,
    MAX_DROP_SIZE,
    MAX_SHOWCASE_SLOTS,
    MIN_DRAW_WINDOW_SECONDS,
    REQUEST_MAX_LENGTH,
    UPLOAD_CANCEL_WORDS,
    UPLOAD_CANCELLED,
    REQUEST_MAX_PENDING,
    SERIES_IMPORT_DEFAULT,
    SERIES_IMPORT_MAX,
    SHEET_COLUMNS,
    TIERS,
    WISH_PING_MAX,
    WISHLIST_MAX,
)
from .models import ActiveDrop, Card, MemberState, SeriesPreview

log = logging.getLogger("red.evac-cogs.cardcollect")

MSG_OVER = "This drop is over — its draw has already closed."
MSG_ERROR = "Something went wrong with that -- try again."


def _secs(seconds: float) -> int:
    return max(1, int(seconds + 0.999))


def _new_stats() -> dict:
    return {"since": 0, "drops": 0, "cards_offered": 0, "picks": 0, "cards_unpicked": 0, "contested": 0, "daily_pulls": {}}


DEFAULT_GUILD = {
    "channel_id": None,
    "pool": {},
    "next_id": 1,
    "drop_chance": DEFAULT_DROP_CHANCE,
    "drop_cooldown_seconds": DEFAULT_DROP_COOLDOWN_SECONDS,
    "drop_size": DEFAULT_DROP_SIZE,
    "drop_weights": dict(DEFAULT_DROP_WEIGHTS),
    "daily_weights": dict(DEFAULT_DAILY_WEIGHTS),
    "tier_cutoffs": dict(DEFAULT_TIER_CUTOFFS),
    "draw_window_seconds": DEFAULT_DRAW_WINDOW_SECONDS,
    "sell_prices": dict(DEFAULT_SELL_PRICES),
    "test_mode": False,
    "activity_tracking": {"hourly_buckets": {}, "sampling_since": 0},
    "stats": _new_stats(),
    "request_channel_id": None,
    "requests": {},  # request id (str) -> {"user_id", "text", "status", "message_id", "card_id"}
    "next_request_id": 1,
}

DEFAULT_MEMBER = {
    "collection": [],
    "showcase_card_ids": [],
    "sell_tokens": [],
    "daily_pull_date": "",
    "daily_streak": 0,
    "pity": 0,
    "wishlist": [],
    "wish_pings_off": False,
    "sets_claimed": [],
}


class CardCollect(commands.Cog):
    """Ambient anime-character card collecting, driven by chat activity."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xCA5DC011EC7, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)

        self.data_path: Path = cog_data_path(self)

        # in-memory only -- losing these on restart just means a drop that was
        # mid-draw never resolves (its buttons then answer "this drop is over"),
        # never touches persisted collections/tokens
        self.active_drops: Dict[int, ActiveDrop] = {}  # message_id -> ActiveDrop
        self.last_drop_time: Dict[int, float] = {}  # guild_id -> monotonic time
        self._drop_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> lock (drop cooldown)
        # One lock per guild around every write that gives or takes cards
        # (resolving a draw, a daily pull, a gift, a sale), so two of them can
        # never read the same member state and overwrite each other.
        self._claim_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> lock
        self._persistent_views: List[discord.ui.View] = []
        # AniList's public API rate-limits aggressively; without this, two
        # `.card importpool` runs fired close together each start their own
        # up-to-200-page loop and multiply the request rate against the same
        # limit, tripping 429s that neither run alone would have hit. This
        # lock is process-wide (not per-guild) since it's AniList's own
        # global limit being protected, not anything guild-scoped.
        self._importpool_lock = asyncio.Lock()
        self._rng: Optional[random.Random] = None  # tests inject a seeded one for the draw

        self._session: Optional[aiohttp.ClientSession] = None
        self._igdb = sources.IGDBClient()

        # Activity counts are buffered here and merged into Config every
        # ACTIVITY_FLUSH_SECONDS. Writing per message rewrote this cog's whole
        # settings file (pool + every member) on every chat message in the
        # drop channel. A crash loses at most one interval of diagnostics counts.
        self._activity_pending: Dict[int, Dict[str, int]] = {}  # guild_id -> {hour: count}
        self._activity_since: Dict[int, float] = {}  # guild_id -> first-buffered time
        self._activity_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        # Shared, persistent views: a press of a button on a drop (or a request)
        # that predates a restart still lands here, and is answered ("this drop
        # is over") instead of Discord showing "This interaction failed".
        self._persistent_views = [views.DrawView(self, None), views.RequestReviewView(self), views.RequestFollowView(self)]
        for view in self._persistent_views:
            self.bot.add_view(view)

        # a hung AniList request or a slow image host would otherwise leave
        # .card importpool waiting forever -- give every request on this
        # session a ceiling
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self._activity_task = asyncio.create_task(self._activity_flush_loop())

    async def cog_unload(self):
        if self._activity_task is not None:
            self._activity_task.cancel()
        for drop in list(self.active_drops.values()):
            if drop.task is not None:
                drop.task.cancel()
        try:
            await self._flush_activity()
        except Exception:
            log.exception("cardcollect: final activity flush failed")
        for view in self._persistent_views:
            view.stop()
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _pool_cards(self, guild: discord.Guild) -> List[Card]:
        pool = await self.config.guild(guild).pool()
        return [Card.from_dict(int(cid), data) for cid, data in pool.items()]

    async def _rollable_pool_cards(self, guild: discord.Guild) -> List[Card]:
        """Cards eligible to actually drop -- excludes retired characters.
        Retired cards stay in the pool dict (and their art stays on disk) so
        members who already own one still get a working gallery entry; see
        card_removecard."""
        return [c for c in await self._pool_cards(guild) if not c.retired]

    async def _card_by_id(self, guild: discord.Guild, card_id: int) -> Optional[Card]:
        pool = await self.config.guild(guild).pool()
        data = pool.get(str(card_id))
        if data is None:
            return None
        return Card.from_dict(card_id, data)

    async def _member_state(self, member: discord.Member) -> MemberState:
        data = await self.config.member(member).all()
        return MemberState.from_dict(data)

    async def _save_member_state(self, member: discord.Member, state: MemberState):
        await self.config.member(member).set(state.to_dict())

    async def _tier_breakdown_line(self, guild: discord.Guild, collection: List[int]) -> Optional[str]:
        """'Collection: N common, N rare, N epic, N legendary (N total)' for
        a member's owned card_ids -- counts every copy held (a tradeable
        spare included), same as the leaderboard's total-cards tally.
        Returns None for an empty collection rather than an all-zero line."""
        if not collection:
            return None
        by_id = {c.card_id: c for c in await self._pool_cards(guild)}
        counts: Dict[str, int] = {t: 0 for t in TIERS}
        for card_id in collection:
            card = by_id.get(card_id)
            if card is not None:
                counts[card.rarity] = counts.get(card.rarity, 0) + 1
        parts = ", ".join(f"{counts.get(t, 0)} {t}" for t in TIERS)
        return f"Collection: {parts} ({sum(counts.values())} total)"

    def _read_card_image(self, guild: discord.Guild, card_id: int) -> Optional[bytes]:
        return storage.read_card_image(self.data_path, guild.id, card_id)

    def _find_card_by_name(self, pool_cards: List[Card], name: str) -> Optional[Card]:
        name = name.strip().lower()
        for c in pool_cards:
            if c.name.strip().lower() == name:
                return c
        return None

    async def _resolve_card_arg(self, guild: discord.Guild, arg: str) -> Optional[Card]:
        if arg.isdigit():
            card = await self._card_by_id(guild, int(arg))
            if card is not None:
                return card
        pool_cards = await self._pool_cards(guild)
        return self._find_card_by_name(pool_cards, arg)

    def _get_drop_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._drop_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._drop_locks[guild_id] = lock
        return lock

    def _get_claim_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._claim_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._claim_locks[guild_id] = lock
        return lock

    def _forget_drop(self, message_id: int):
        """Drop a resolved drop's in-memory state -- without this,
        self.active_drops would grow by one entry per drop ever posted, forever."""
        drop = self.active_drops.pop(message_id, None)
        if drop is not None and drop.view is not None:
            drop.view.stop()

    # ------------------------------------------------------------------
    # drop trigger
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        guild = message.guild
        conf = self.config.guild(guild)
        channel_id = await conf.channel_id()
        if channel_id is None or message.channel.id != channel_id:
            return

        # The cooldown check-then-set below spans real awaits (Config reads
        # hit an actual backend in a live bot), so two messages arriving
        # close together could otherwise both pass the cooldown check
        # before either updates last_drop_time, chaining drops despite the
        # cooldown -- exactly what it's there to prevent. A per-guild lock
        # makes the whole decision atomic. _post_drop itself runs outside
        # the lock so rendering/sending one drop never blocks the next
        # message's eligibility check.
        test_mode = False
        should_post = False
        async with self._get_drop_lock(guild.id):
            await self._track_activity(guild)

            now = time.monotonic()
            cooldown = await conf.drop_cooldown_seconds()
            # None, not 0.0, is the "never dropped yet" sentinel --
            # time.monotonic()'s reference point is undefined (it isn't
            # process-start or epoch), so `now` can legitimately be smaller
            # than `cooldown` on a guild's very first eligible message,
            # which would wrongly block that first drop if 0.0 were used.
            last = self.last_drop_time.get(guild.id)
            if last is not None and now - last < cooldown:
                return

            drop_chance = await conf.drop_chance()
            if not engine.should_drop(drop_chance):
                return

            self.last_drop_time[guild.id] = now
            test_mode = await conf.test_mode()
            should_post = True

        if should_post:
            await self._post_drop(message.channel, guild, is_test=test_mode)

    ACTIVITY_FLUSH_SECONDS = 600

    async def _track_activity(self, guild: discord.Guild):
        """Count one message in memory; _flush_activity persists it."""
        hour = str(datetime.now(ZoneInfo(ACTIVITY_TIMEZONE)).hour)
        buckets = self._activity_pending.setdefault(guild.id, {})
        buckets[hour] = buckets.get(hour, 0) + 1
        self._activity_since.setdefault(guild.id, time.time())

    async def _flush_activity(self):
        """Merge buffered counts into Config -- one write per guild."""
        pending, since = self._activity_pending, self._activity_since
        self._activity_pending, self._activity_since = {}, {}
        for guild_id, counts in pending.items():
            try:
                async with self.config.guild_from_id(guild_id).activity_tracking() as tracking:
                    if not tracking.get("sampling_since"):
                        tracking["sampling_since"] = since.get(guild_id, time.time())
                    buckets = tracking.setdefault("hourly_buckets", {})
                    for hour, n in counts.items():
                        buckets[hour] = buckets.get(hour, 0) + n
            except Exception:
                # put the counts back so the next flush retries them
                log.exception("cardcollect: activity flush failed for guild %s", guild_id)
                back = self._activity_pending.setdefault(guild_id, {})
                for hour, n in counts.items():
                    back[hour] = back.get(hour, 0) + n
                self._activity_since.setdefault(guild_id, since.get(guild_id, time.time()))

    async def _activity_flush_loop(self):
        while True:
            await asyncio.sleep(self.ACTIVITY_FLUSH_SECONDS)
            try:
                await self._flush_activity()
            except Exception:
                log.exception("cardcollect: activity flush loop error")

    async def _post_drop(
        self, channel: discord.abc.Messageable, guild: discord.Guild, is_test: bool
    ) -> Optional[str]:
        """Attempts to post one drop. Returns None on success, or a short
        human-readable reason nothing was posted -- every silent-failure
        path used to just `return`, which made `.card testdrop` (and a
        real ambient drop) fail completely silently with zero feedback
        whenever the pool was empty or a card's art was missing. The
        ambient message-listener path ignores this return value (skipping
        a tick silently is correct there), but command call sites should
        always surface it to the admin who asked for a drop."""
        conf = self.config.guild(guild)
        pool_cards = await self._rollable_pool_cards(guild)
        if not pool_cards:
            return "The card pool is empty. Add characters first with `.card addcard` or `.card importseries`."

        weights = await conf.drop_weights()
        drop_size = max(1, min(MAX_DROP_SIZE, await conf.drop_size()))
        window = await conf.draw_window_seconds()

        drop = engine.build_drop(pool_cards, weights, drop_size, guild.id, channel.id, is_test=is_test)
        if drop is None:
            return "Couldn't roll a drop from the current pool (no cards available in any rarity tier)."

        entries = []  # (Card, art bytes), in button order
        for card_entry in drop.cards:
            card = await self._card_by_id(guild, card_entry["card_id"])
            image_bytes = self._read_card_image(guild, card_entry["card_id"])
            if card is None or image_bytes is None:
                continue
            entries.append((card, image_bytes))
        if not entries:
            return (
                "Rolled cards but couldn't find their art on disk -- the pool may be "
                "out of sync with the image files. Check `.card settings` and the data folder."
            )
        # a card whose art is missing can't be shown, so it isn't offered; the
        # remaining cards are renumbered 1..n to match the image and the buttons
        drop.cards = [{"card_id": card.card_id, "position": i} for i, (card, _) in enumerate(entries)]

        # Pillow work off the event loop: a 3-card composite is tens of ms of
        # CPU, which is long enough to delay gateway heartbeats if done inline
        composite = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(imagegen.render_drop, entries, is_test=is_test)
        )
        closes_epoch = int(time.time()) + window
        if is_test:
            lead = "\U0001f9ea **Test drop** — picks here won't award anything."
        else:
            lead = "\U0001f3b4 **A drop!**"
        content = (
            f"{lead} Press the card you want. The draw closes <t:{closes_epoch}:R> — "
            "one card per person, and you can change your pick until then."
        )
        view = views.DrawView(self, [card for card, _ in entries], window)
        sent = await channel.send(content=content, file=discord.File(composite, filename="drop.png"), view=view)

        # registered synchronously right after the send returns, before any
        # further await, so a press of a button the instant the drop appears
        # can never arrive before the drop is known
        drop.message_id = sent.id
        drop.message = sent
        drop.guild = guild
        drop.view = view
        drop.closes_epoch = closes_epoch
        drop.closes_at = time.monotonic() + window
        self.active_drops[sent.id] = drop
        drop.task = asyncio.ensure_future(self._close_drop_later(drop, window))

        if not is_test:
            await self._bump_stats(guild, drops=1, cards_offered=len(entries))
            asyncio.ensure_future(self._post_wish_pings(drop, [card for card, _ in entries]))
        return None

    # ------------------------------------------------------------------
    # the draw: pick -> close -> resolve
    # ------------------------------------------------------------------

    def register_pick(self, member: discord.abc.User, message_id: int, position: int):
        """Record (or change) a member's pick. Synchronous on purpose: it runs
        before the button press is even acknowledged, so a press made before the
        draw closes always counts, however slow the acknowledgement is.
        Returns (drop, previous_position); drop is None if the draw is over."""
        drop = self.active_drops.get(message_id)
        if drop is None or drop.is_closed(time.monotonic()) or drop.card_at(position) is None:
            return None, None
        previous = drop.picks.get(member.id)
        drop.picks[member.id] = position
        return drop, previous

    async def pick_reply(self, member: discord.abc.User, drop: ActiveDrop, position: int, previous: Optional[int]) -> str:
        """The private reply to a pick: what they're in for, and any caveat."""
        card = await self._card_by_id(drop.guild, drop.card_at(position)["card_id"])
        name = f"**{card.name}**" if card is not None else "that card"
        if previous == position:
            head = f"You're already in for {name}."
        elif previous is None:
            head = f"✅ You're in the draw for {name}."
        else:
            old = await self._card_by_id(drop.guild, drop.card_at(previous)["card_id"]) if drop.card_at(previous) else None
            head = f"✅ Switched your pick{f' from **{old.name}**' if old is not None else ''} to {name}."
        lines = [head]
        state = await self._member_state_by_id(drop.guild, member.id)
        tier = engine.owner_tier(state.collection, drop.card_at(position)["card_id"], MAX_COPIES_KEPT)
        if tier == 1:
            lines.append("You already own it, so you only win a spare if nobody who doesn't own it picks it too.")
        elif tier == 2:
            lines.append(
                "You're already at your copy limit for it, so you only win it (as a sell token) if nobody else wants it."
            )
        boost = engine.pity_multiplier(state.pity)
        if tier == 0 and boost > 1.0:
            lines.append(f"\U0001f340 Pity bonus: your entry counts x{boost:g} after your recent losses.")
        lines.append(f"The draw closes <t:{drop.closes_epoch}:R>. You can change your pick until then.")
        if drop.is_test:
            lines.append("(Test drop — nothing is awarded.)")
        return "\n".join(lines)

    async def handle_pick(self, member: discord.abc.User, message_id: int, position: int) -> str:
        """register_pick + pick_reply in one call (what a button press does,
        minus the acknowledgement)."""
        drop, previous = self.register_pick(member, message_id, position)
        if drop is None:
            return MSG_OVER
        return await self.pick_reply(member, drop, position, previous)

    async def _close_drop_later(self, drop: ActiveDrop, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._resolve_drop(drop)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cardcollect: resolving a draw failed")
            self._forget_drop(drop.message_id)

    async def _member_state_by_id(self, guild: discord.Guild, user_id: int) -> MemberState:
        return MemberState.from_dict(await self.config.member_from_ids(guild.id, user_id).all())

    async def _save_member_state_by_id(self, guild: discord.Guild, user_id: int, state: MemberState):
        await self.config.member_from_ids(guild.id, user_id).set(state.to_dict())

    def _grant_card(self, state: MemberState, card: Card, sell_prices: dict) -> Tuple[str, Optional[int]]:
        """Give a member a card they won or pulled: a new copy, a tradeable
        spare, or (at the copy limit) a sell token. Takes it off their
        wishlist once they own it. Returns (outcome, sell price or None)."""
        outcome = engine.claim_outcome(state.collection, card.card_id, MAX_COPIES_KEPT)
        price = None
        if outcome == "sell_token":
            state.sell_tokens.append(engine.make_sell_token(card.card_id, card.rarity))
            price = engine.sell_price(card.rarity, sell_prices)
        else:
            state.collection.append(card.card_id)
            if card.card_id in state.wishlist:
                state.wishlist.remove(card.card_id)
        return outcome, price

    async def _complete_set(
        self, guild: discord.Guild, member: Optional[discord.abc.User], state: MemberState, card: Card, outcome: str
    ) -> Optional[Tuple[str, int]]:
        """If getting `card` just finished a series set for the first time, pay
        the bonus and return (series, bonus). Only a NEW card can finish a set,
        so a member who finished one before sets existed is never paid
        retroactively for a duplicate."""
        if outcome != "new" or not card.series or card.series in state.sets_claimed:
            return None
        sets = engine.build_sets(await self._pool_cards(guild))
        members = sets.get(card.series)
        if not members or not all(c.card_id in state.collection for c in members):
            return None
        bonus = engine.set_bonus(len(members))
        if member is not None:
            try:
                await bank.deposit_credits(member, bonus)
            except Exception:
                log.exception("cardcollect: couldn't pay a set bonus to %s", getattr(member, "id", "?"))
                return None
        state.sets_claimed.append(card.series)
        return card.series, bonus

    async def _bump_stats(self, guild: discord.Guild, daily_pull_date: Optional[str] = None, **counts):
        """Add to the diagnostics counters (`.card diagnostics`). Pass
        `daily_pull_date` to also count one `.card daily` pull on that day."""
        async with self.config.guild(guild).stats() as stats:
            if not stats.get("since"):
                stats["since"] = time.time()
            for key, value in counts.items():
                stats[key] = stats.get(key, 0) + value
            if daily_pull_date:
                daily = stats.setdefault("daily_pulls", {})
                daily[daily_pull_date] = daily.get(daily_pull_date, 0) + 1
                for old in sorted(daily)[:-30]:  # keep the last 30 days
                    daily.pop(old, None)

    async def _resolve_drop(self, drop: ActiveDrop):
        """Close a drop's draw: pick the winners, award the cards, and edit the
        drop message in place with the results. Safe to call twice; only the
        first call does anything."""
        if drop.resolved:
            return None
        drop.resolved = True  # synchronous: from here on no pick counts
        self._forget_drop(drop.message_id)
        guild = drop.guild
        conf = self.config.guild(guild)
        picks = {uid: pos for uid, pos in drop.picks.items() if guild.get_member(uid) is not None}

        async with self._get_claim_lock(guild.id):
            sell_prices = await conf.sell_prices()
            states = {uid: await self._member_state_by_id(guild, uid) for uid in picks}
            results, pity_after = engine.resolve_drop(
                drop.cards,
                picks,
                {uid: s.collection for uid, s in states.items()},
                {uid: s.pity for uid, s in states.items()},
                MAX_COPIES_KEPT,
                rng=self._rng,
            )
            cards = {c["card_id"]: await self._card_by_id(guild, c["card_id"]) for c in drop.cards}
            set_lines: List[str] = []
            failed = set()
            if not drop.is_test:
                for result in results:
                    winner, card = result["winner"], cards.get(result["card_id"])
                    if winner is None:
                        continue
                    if card is None:  # removed from the pool mid-draw
                        result["winner"], result["outcome"] = None, "error"
                        failed.add(winner)
                        continue
                    state = states[winner]
                    try:
                        outcome, price = self._grant_card(state, card, sell_prices)
                        state.pity = pity_after[winner]
                        done = await self._complete_set(guild, guild.get_member(winner), state, card, outcome)
                        await self._save_member_state_by_id(guild, winner, state)
                        result["outcome"], result["price"] = outcome, price
                        if done is not None:
                            set_lines.append(
                                f"\U0001f3c6 <@{winner}> completed the **{done[0]}** set: +{done[1]:,} wondercoin!"
                            )
                    except Exception:
                        log.exception("cardcollect: couldn't save a draw win for %s", winner)
                        result["winner"], result["outcome"] = None, "error"
                        failed.add(winner)
                for uid, new_pity in pity_after.items():
                    state = states[uid]
                    if uid not in failed and state.pity != new_pity and not any(r["winner"] == uid for r in results):
                        state.pity = new_pity
                        try:
                            await self._save_member_state_by_id(guild, uid, state)
                        except Exception:
                            log.exception("cardcollect: couldn't save pity for %s", uid)
                unpicked = sum(1 for r in results if r["pickers"] == 0)
                await self._bump_stats(
                    guild, picks=len(picks), cards_unpicked=unpicked, contested=sum(1 for r in results if r["contested"])
                )

        embed = embeds.draw_results_embed(results, cards, len(picks), set_lines, drop.is_test)
        await self._edit_drop_message(drop, embed)
        return results

    async def _edit_drop_message(self, drop: ActiveDrop, embed: discord.Embed):
        """Edit the drop message in place into its results; if it was deleted,
        post the results as a new message instead."""
        content = "\U0001f9ea **Test draw closed**" if drop.is_test else "\U0001f3b4 **Draw closed**"
        try:
            await drop.message.edit(
                content=content, embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.warning("cardcollect: couldn't edit the drop message; posting the results instead", exc_info=True)
            channel = drop.message.channel
            await self._post_to_channel(
                channel, content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _post_to_channel(self, channel: discord.abc.Messageable, **kwargs):
        """Best-effort public post: a failed announcement must never undo or
        crash something that has already been saved."""
        try:
            await channel.send(**kwargs)
        except discord.HTTPException:
            log.exception("cardcollect: failed to post to the drop channel")

    async def _post_wish_pings(self, drop: ActiveDrop, cards: List[Card]):
        """Reply under a drop pinging the members who wished for one of its
        cards: never members who already own it or have pings off, at most
        WISH_PING_MAX named per card (the rest as "+N more")."""
        try:
            guild = drop.guild
            everyone = await self.config.all_members(guild)
            lines = []
            for card in cards:
                wishers = sorted(
                    uid
                    for uid, data in everyone.items()
                    if card.card_id in data.get("wishlist", [])
                    and card.card_id not in data.get("collection", [])
                    and not data.get("wish_pings_off")
                    and guild.get_member(uid) is not None
                )
                if not wishers:
                    continue
                shown = " ".join(f"<@{uid}>" for uid in wishers[:WISH_PING_MAX])
                more = f" +{len(wishers) - WISH_PING_MAX} more" if len(wishers) > WISH_PING_MAX else ""
                name = discord.utils.escape_markdown(card.name)[:80]
                lines.append(f"**{name}** is in this drop: {shown}{more}, it's on your wishlist!")
            if not lines:
                return
            text = "\n".join(lines) + f"\nThe draw closes <t:{drop.closes_epoch}:R>."
            await drop.message.reply(
                text[:2000], mention_author=False, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
            )
        except Exception:
            log.exception("cardcollect: wishlist pings failed")

    # ------------------------------------------------------------------
    # card requests: members ask for a character that isn't in the pool
    # ------------------------------------------------------------------

    async def submit_request(self, guild: discord.Guild, member: discord.Member, text: str) -> str:
        """Post a member's request for a character to the private review channel.
        Returns the reply for the member."""
        text = " ".join(str(text).split())[:REQUEST_MAX_LENGTH]
        if len(text) < 2:
            return "Tell me which character (and series) you'd like."
        conf = self.config.guild(guild)
        channel_id = await conf.request_channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return "Card requests aren't open on this server yet."
        folded = engine.fold_text(text)
        async with conf.all() as data:
            pending = [r for r in data["requests"].values() if r["status"] == "pending"]
            if sum(1 for r in pending if r["user_id"] == member.id) >= REQUEST_MAX_PENDING:
                return f"You already have {REQUEST_MAX_PENDING} requests waiting. Let an admin get to those first."
            if any(engine.fold_text(r["text"]) == folded for r in pending):
                return "That one is already waiting for review. It will be looked at."
            request_id = data["next_request_id"]
            data["next_request_id"] += 1
            data["requests"][str(request_id)] = {
                "user_id": member.id, "text": text, "status": "pending", "message_id": None, "card_id": None,
            }
        embed = embeds.request_embed(request_id, member, text)
        try:
            sent = await channel.send(embed=embed, view=views.RequestReviewView(self), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("cardcollect: couldn't post a card request")
            async with conf.requests() as requests:
                requests.pop(str(request_id), None)
            return "I couldn't post that request. Ask an admin to check the request channel."
        async with conf.requests() as requests:
            if str(request_id) in requests:
                requests[str(request_id)]["message_id"] = sent.id
        return "Request sent! If an admin approves it, the card goes on your wishlist once it's added."

    async def review_request(self, interaction: discord.Interaction, approve: bool):
        """An admin pressed Approve or Decline on a request message."""
        guild = interaction.guild
        if not await self._is_pool_admin(interaction.user):
            await interaction.response.send_message("Only admins can review requests.", ephemeral=True)
            return
        conf = self.config.guild(guild)
        found = None
        async with conf.requests() as requests:
            for request_id, request in requests.items():
                if request.get("message_id") == interaction.message.id:
                    if request["status"] != "pending":
                        found = (request_id, dict(request), False)
                    else:
                        request["status"] = "approved" if approve else "declined"
                        found = (request_id, dict(request), True)
                    break
        if found is None:
            await interaction.response.send_message("I can't find that request any more.", ephemeral=True)
            return
        request_id, request, changed = found
        if not changed:
            await interaction.response.send_message(f"That request was already {request['status']}.", ephemeral=True)
            return
        requester = guild.get_member(request["user_id"])
        embed = embeds.request_embed(int(request_id), requester or request["user_id"], request["text"], status=request["status"], by=interaction.user)
        await interaction.response.edit_message(embed=embed, view=views.RequestFollowView(self) if approve else None)
        if requester is not None:
            note = (
                f"Your card request **{request['text']}** was approved! When it's added to the pool it goes on your wishlist."
                if approve else f"Your card request **{request['text']}** wasn't approved this time."
            )
            try:
                await requester.send(note)
            except discord.HTTPException:
                pass  # DMs closed: nothing else to do
        if approve:
            await self.start_request_add(interaction, request_id)

    # -- adding the requested character: search, pick, add -------------------

    async def request_id_for_message(self, guild: discord.Guild, message_id: int) -> Optional[str]:
        requests = await self.config.guild(guild).requests()
        return next((rid for rid, r in requests.items() if r.get("message_id") == message_id), None)

    async def igdb_keys(self) -> Optional[Tuple[str, str]]:
        """The Twitch app keys for IGDB from Red's shared API tokens, or None."""
        getter = getattr(self.bot, "get_shared_api_tokens", None)
        if getter is None:
            return None
        tokens = await getter("igdb")
        client_id, secret = (tokens or {}).get("client_id"), (tokens or {}).get("client_secret")
        return (client_id, secret) if client_id and secret else None

    async def search_candidates(self, guild: discord.Guild, text: str):
        """Closest AniList and IGDB matches for a request's text, plus notes
        about anything that couldn't be searched."""
        cutoffs = await self.config.guild(guild).tier_cutoffs()
        return await sources.find_candidates(self._session, text, cutoffs, self._igdb, await self.igdb_keys())

    async def start_request_add(self, interaction: discord.Interaction, request_id):
        """Search for an approved request's character and show the admin the
        closest matches to choose from (privately). Used right after Approve and
        by the "Find & add card" button left on the approved request."""
        guild = interaction.guild
        requests = await self.config.guild(guild).requests()
        request = requests.get(str(request_id))
        if request is None or request["status"] not in ("approved",):
            await interaction.followup.send("That request isn't waiting to be added.", ephemeral=True)
            return
        candidates, notes = await self.search_candidates(guild, request["text"])
        view = views.CandidateView(self, interaction.user.id, str(request_id), request["text"], candidates, notes)
        view.message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

    async def add_requested_card(
        self, guild: discord.Guild, request_id: str, candidate: dict, rarity: str, art: Optional[bytes] = None
    ) -> Tuple[Optional[Card], str]:
        """Add the chosen candidate to the pool with the chosen rarity (and the
        admin's own art if given, else the source's picture), then wishlist it
        for the requester. Returns (card or None, what to tell the admin)."""
        if rarity not in TIERS:
            return None, "Pick a rarity first."
        pool = await self._pool_cards(guild)
        key = (engine.fold_text(candidate["name"]), engine.fold_text(candidate.get("series", "")))
        for card in pool:
            if (engine.fold_text(card.name), engine.fold_text(card.series)) == key or (
                candidate.get("anilist_id") is not None and card.anilist_id == candidate["anilist_id"]
            ):
                return None, f"**{card.name}** is already in the pool as `#{card.card_id}`."
        if art is None:
            if not candidate.get("image_url"):
                return None, "That match has no picture. Press **Use my own art** and upload one."
            downloaded, failed = await self._download_images([candidate])
            if failed:
                return None, "I couldn't download that picture. Try **Use my own art**."
            art = downloaded[0][1]
        if not imagegen.valid_art(art):
            return None, "That picture isn't an image I can read. Try **Use my own art**."
        entry = {
            "anilist_id": candidate.get("anilist_id"),
            "name": candidate["name"],
            "series": candidate.get("series", ""),
            "series_alt": candidate.get("series_alt", ""),
            "favourites": candidate.get("favourites", 0),
            "rarity": rarity,
        }
        added = await self._write_pool_entries(guild, [(entry, art)])
        if not added:
            return None, "That character is already in the pool."
        card = await self._card_by_id(guild, added[0]["card_id"])
        await self._finish_request(guild, request_id, card)
        return card, f"Added **{card.name}** ({card.series or 'no series'}) as **{rarity}**, `#{card.card_id}`."

    async def _finish_request(self, guild: discord.Guild, request_id: str, card: Card):
        """Mark a request fulfilled by `card`, wishlist it for the requester and
        update the review-channel message."""
        conf = self.config.guild(guild)
        info, already = None, False
        async with conf.requests() as requests:
            request = requests.get(str(request_id))
            if request is not None:
                already = request["status"] == "fulfilled"  # the pool write may have matched it by name already
                request["status"], request["card_id"] = "fulfilled", card.card_id
                info = dict(request)
        if info is None:
            return
        if not already:
            await self._wishlist_for(guild, info["user_id"], card.card_id, card.name)
        channel_id = await conf.request_channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None and info.get("message_id"):
            try:
                message = await channel.fetch_message(info["message_id"])
                requester = guild.get_member(info["user_id"]) or info["user_id"]
                await message.edit(embed=embeds.request_embed(int(request_id), requester, info["text"], status="fulfilled"), view=None)
            except (discord.HTTPException, AttributeError):
                pass  # the message is gone or unreachable: the request is still recorded as fulfilled

    async def _wishlist_for(self, guild: discord.Guild, user_id: int, card_id: int, name: str):
        """Put a card on a member's wishlist (if there's room and they don't own it) and DM them."""
        member = guild.get_member(user_id)
        if member is None:
            return
        async with self._get_claim_lock(guild.id):
            state = await self._member_state(member)
            if not state.owns(card_id) and card_id not in state.wishlist and len(state.wishlist) < WISHLIST_MAX:
                state.wishlist.append(card_id)
                await self._save_member_state(member, state)
        try:
            await member.send(f"**{name}** is now in the card pool, and it's on your wishlist!")
        except discord.HTTPException:
            pass

    async def _fulfil_requests(self, guild: discord.Guild, added: List[Tuple[int, str]]):
        """New characters just entered the pool: any approved request whose text
        names one of them puts it on the requester's wishlist."""
        if not added:
            return
        conf = self.config.guild(guild)
        matched: List[Tuple[int, int, str, str]] = []  # (user_id, card_id, card name, request id)
        async with conf.requests() as requests:
            for request_id, request in requests.items():
                if request["status"] != "approved":
                    continue
                folded = engine.fold_text(request["text"])
                for card_id, name in added:
                    name_folded = engine.fold_text(name)
                    if len(name_folded) >= 3 and name_folded in folded:
                        request["status"], request["card_id"] = "fulfilled", card_id
                        matched.append((request["user_id"], card_id, name, request_id))
                        break
        for user_id, card_id, name, _request_id in matched:
            await self._wishlist_for(guild, user_id, card_id, name)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.group(name="card", aliases=["cards"], invoke_without_command=True)
    @commands.guild_only()
    async def card(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Your card collection, or another member's with `.card @member`. Tabs: Collection, Sets and (yours only) Wishlist."""
        target = member or ctx.author
        view = views.CollectionView(self, ctx.author.id, target, own=target.id == ctx.author.id)
        payload = await view.render("collection")
        view.message = await ctx.send(view=view, **view.for_send(payload))

    # -- what the collection tabs show ----------------------------------

    async def collection_entries(self, guild: discord.Guild, state: MemberState) -> List[Tuple[Card, bytes]]:
        """One (Card, art) per unique card a member owns, rarest first then A-Z.
        Cards whose art has gone missing are skipped."""
        entries = []
        for card_id in dict.fromkeys(state.collection):
            card = await self._card_by_id(guild, card_id)
            image_bytes = self._read_card_image(guild, card_id)
            if card is not None and image_bytes is not None:
                entries.append((card, image_bytes))
        entries.sort(key=lambda e: engine.gallery_sort_key(e[0]))
        return entries

    async def sets_embed(self, guild: discord.Guild, state: MemberState, whose: str) -> discord.Embed:
        sets = engine.build_sets(await self._pool_cards(guild))
        return embeds.sets_embed(engine.set_progress(sets, state.collection), set(state.sets_claimed), whose, len(sets))

    async def wishlist_cards(self, guild: discord.Guild, state: MemberState) -> List[Card]:
        cards = [await self._card_by_id(guild, cid) for cid in state.wishlist]
        return [c for c in cards if c is not None]

    async def wishlist_embed(self, guild: discord.Guild, state: MemberState) -> discord.Embed:
        return embeds.wishlist_embed(await self.wishlist_cards(guild, state), state.wish_pings_off)

    async def toggle_wish(self, guild: discord.Guild, member: discord.Member, card_id: int) -> str:
        """Add a card to (or take it off) a member's wishlist. Returns the reply."""
        card = await self._card_by_id(guild, card_id)
        if card is None:
            return "That card is no longer in the pool."
        async with self._get_claim_lock(guild.id):
            state = await self._member_state(member)
            if card_id in state.wishlist:
                state.wishlist.remove(card_id)
                message = f"Took **{card.name}** off your wishlist."
            elif state.owns(card_id):
                return f"You already own **{card.name}**."
            elif len(state.wishlist) >= WISHLIST_MAX:
                return f"Your wishlist is full ({WISHLIST_MAX}). Remove one in the Wishlist tab of `.card` first."
            else:
                state.wishlist.append(card_id)
                message = f"Added **{card.name}** to your wishlist. You'll be pinged when it drops."
            await self._save_member_state(member, state)
        return message

    async def set_wish_pings(self, member: discord.Member, off: bool) -> None:
        async with self._get_claim_lock(member.guild.id):
            state = await self._member_state(member)
            state.wish_pings_off = off
            await self._save_member_state(member, state)

    async def remove_wishes(self, member: discord.Member, card_ids) -> None:
        async with self._get_claim_lock(member.guild.id):
            state = await self._member_state(member)
            state.wishlist = [c for c in state.wishlist if c not in set(card_ids)]
            await self._save_member_state(member, state)

    async def toggle_showcase(self, guild: discord.Guild, member: discord.Member, card_id: int) -> str:
        card = await self._card_by_id(guild, card_id)
        if card is None:
            return "That card is no longer in the pool."
        async with self._get_claim_lock(guild.id):
            state = await self._member_state(member)
            if card_id in state.showcase_card_ids:
                state.showcase_card_ids.remove(card_id)
                message = f"Removed **{card.name}** from your showcase."
            elif not state.owns(card_id):
                return "You don't own that card."
            elif len(state.showcase_card_ids) >= MAX_SHOWCASE_SLOTS:
                return f"Your showcase is full ({MAX_SHOWCASE_SLOTS} slots). Remove one first."
            else:
                state.showcase_card_ids.append(card_id)
                message = f"Added **{card.name}** to your showcase ({len(state.showcase_card_ids)}/{MAX_SHOWCASE_SLOTS})."
            await self._save_member_state(member, state)
        return message

    @card.group(name="showcase", invoke_without_command=True)
    @commands.guild_only()
    async def card_showcase(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """View your showcase slots (shown larger, up top, in your gallery),
        or another member's with `.card showcase @member`.
        Use `.card showcase add/remove <card>` to manage your own."""
        target = member or ctx.author
        state = await self._member_state(target)
        whose = "Your" if target.id == ctx.author.id else f"{target.display_name}'s"
        tier_line = await self._tier_breakdown_line(ctx.guild, state.collection)

        if not state.showcase_card_ids:
            if target.id == ctx.author.id:
                msg = f"No showcase cards set yet. Add up to {MAX_SHOWCASE_SLOTS} with `.card showcase add <card>`."
            else:
                msg = f"{whose} showcase is empty."
            if tier_line:
                msg += f"\n{tier_line}"
            await ctx.send(msg)
            return

        quantities = Counter(state.collection)
        entries = []
        for card_id in state.showcase_card_ids:
            card = await self._card_by_id(ctx.guild, card_id)
            image_bytes = self._read_card_image(ctx.guild, card_id)
            if card is None or image_bytes is None:
                continue
            entries.append((card, image_bytes))

        # list IDs/names in the caption too, not just the "#<id>" badge
        # baked into each tile -- a copy-pasteable line beats squinting at
        # the picture when all you want is the number for `.card showcase
        # add/remove` or `.card give`
        id_list = ", ".join(f"`{c.card_id}` {c.name}" for c, _ in entries)
        caption = f"{whose} showcase ({len(entries)}/{MAX_SHOWCASE_SLOTS}): {id_list}"
        if tier_line:
            caption += f"\n{tier_line}"

        if not entries:
            # every showcased card's art is missing -- fall back to the old
            # plain-text listing rather than sending nothing at all
            lines = [f"`{cid}` — (unknown)" for cid in state.showcase_card_ids]
            await ctx.send(caption + "\n" + "\n".join(lines))
            return

        showcase_img = await asyncio.to_thread(
            imagegen.render_showcase, entries, quantities=quantities
        )
        await ctx.send(content=caption, file=discord.File(showcase_img, filename="showcase.png"))

    @card_showcase.command(name="add")
    @commands.guild_only()
    async def card_showcase_add(self, ctx: commands.Context, *, card_arg: str):
        """Add a card you own to your showcase (shown larger, up top, in your gallery)."""
        card = await self._resolve_card_arg(ctx.guild, card_arg)
        if card is None:
            await ctx.send("No card matches that.")
            return
        state = await self._member_state(ctx.author)
        if card.card_id in state.showcase_card_ids:
            await ctx.send(f"**{card.name}** is already in your showcase.")
            return
        await ctx.send(await self.toggle_showcase(ctx.guild, ctx.author, card.card_id))

    @card_showcase.command(name="remove")
    @commands.guild_only()
    async def card_showcase_remove(self, ctx: commands.Context, *, card_arg: str):
        """Remove a card from your showcase."""
        card = await self._resolve_card_arg(ctx.guild, card_arg)
        if card is None:
            await ctx.send("No card matches that.")
            return
        state = await self._member_state(ctx.author)
        if card.card_id not in state.showcase_card_ids:
            await ctx.send(f"**{card.name}** isn't in your showcase.")
            return
        await ctx.send(await self.toggle_showcase(ctx.guild, ctx.author, card.card_id))

    # -- find: search the pool, open a card -------------------------------

    @staticmethod
    def _card_line(card: Card) -> str:
        series = f" ({card.series})" if card.series else ""
        text = f"{card.name}{series}".replace("`", "'")
        if len(text) > 100:  # keep a 15-line list safely under Discord's 2000-char message cap
            text = text[:99] + "…"
        return f"`{card.card_id}` {card.rarity} — {text}"

    async def _is_pool_admin(self, member: discord.Member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.manage_guild or perms.administrator):
            return True
        is_admin = getattr(self.bot, "is_admin", None)
        return bool(is_admin is not None and await is_admin(member))

    async def search_pool(self, guild: discord.Guild, member: discord.Member, query: str, scope: str) -> List[Card]:
        """Cards a member can see matching `query`: the whole pool (retired
        cards only if they own one, or they're an admin) or, with scope "mine",
        just their own. A number is a card ID; an exact name wins over longer
        names that merely contain it."""
        state = await self._member_state(member)
        owned = set(state.collection)
        pool = await self._pool_cards(guild)
        if scope == "mine":
            pool = [c for c in pool if c.card_id in owned]
        elif not await self._is_pool_admin(member):
            pool = [c for c in pool if not c.retired or c.card_id in owned]
        pool = [c for c in pool if storage.card_image_exists(self.data_path, guild.id, c.card_id)]
        text = query.strip().lstrip("#").strip()
        if text.isdigit():
            by_id = [c for c in pool if c.card_id == int(text)]
            if by_id:
                return by_id
        matches = engine.search_cards(pool, text)
        exact = [c for c in matches if engine.fold_text(c.name) == engine.fold_text(text)]
        return exact if len(exact) == 1 else matches

    async def card_detail(self, guild: discord.Guild, member: discord.Member, card: Card):
        """Everything `.card find` shows for one card: (embed, art file, quantity
        the member owns, owner count, owner mentions)."""
        everyone = await self.config.all_members(guild)
        owner_ids = [mid for mid, data in everyone.items() if card.card_id in data.get("collection", [])]
        mentions = []
        for member_id in owner_ids[:10]:
            owner = guild.get_member(member_id)
            mentions.append(owner.mention if owner is not None else f"<@{member_id}>")
        state = await self._member_state(member)
        quantity = state.collection.count(card.card_id)
        embed = embeds.card_info_embed(card, len(owner_ids), mentions)
        image_bytes = self._read_card_image(guild, card.card_id)
        file = None
        if image_bytes is not None:
            rendered = await asyncio.to_thread(imagegen.render_single, card, image_bytes, max(1, quantity))
            file = discord.File(rendered, filename="card_info.png")
            embed.set_image(url="attachment://card_info.png")
        if quantity:
            embed.add_field(name="You", value=f"You own **{quantity}**" + (" (a tradeable spare)" if quantity > 1 else ""), inline=True)
        elif card.card_id in state.wishlist:
            embed.add_field(name="You", value="★ On your wishlist", inline=True)
        return embed, file, quantity, len(owner_ids), state

    @card.command(name="find", aliases=["search", "info", "show", "pool", "poolsearch"])
    @commands.guild_only()
    async def card_find(self, ctx: commands.Context, *, query: str):
        """Search the card pool by character name, series or ID. Open any result to see its art, rarity and owners, wishlist it, showcase it or (not in the pool?) request it. Switch between the whole pool and just your cards with the button."""
        view = views.FindView(self, ctx.author, query.strip())
        await view.start(ctx)

    # -- daily ------------------------------------------------------------

    @card.command(name="daily", aliases=["pull"])
    @commands.guild_only()
    async def card_daily(self, ctx: commands.Context):
        """Your free card for today. Mostly commons, sometimes rares, rarely epics,
        very rarely a legendary. Every 7th day in a row is bumped up one rarity."""
        result = await self.daily_pull(ctx.guild, ctx.author)
        if isinstance(result, str):
            await ctx.send(result)
            return
        card, outcome, price, streak, bonus, set_done = result
        lines = [f"\U0001f3b4 {ctx.author.mention} pulled **{card.name}**"
                 + (f" ({card.series})" if card.series else "") + f" · **{card.rarity}** · `#{card.card_id}`"]
        if outcome == "duplicate":
            lines.append("Spare copy: you can trade it with `.card give`.")
        elif outcome == "sell_token":
            lines.append(f"Already at your copy limit, so it became a sell token (`.card sell` for {price:,}).")
        if set_done is not None:
            lines.append(f"\U0001f3c6 Set complete: **{set_done[0]}**! +{set_done[1]:,} wondercoin.")
        streak_line = f"\U0001f525 Streak: **{streak}** day{'s' if streak != 1 else ''}"
        if bonus:
            streak_line += " — bonus day, rarity bumped up!"
        else:
            left = DAILY_STREAK_BONUS_EVERY - (streak % DAILY_STREAK_BONUS_EVERY)
            streak_line += f" · bonus pull in {left} day{'s' if left != 1 else ''}"
        lines.append(streak_line)
        image_bytes = self._read_card_image(ctx.guild, card.card_id)
        if image_bytes is None:
            await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
            return
        rendered = await asyncio.to_thread(imagegen.render_single, card, image_bytes,
                                           (await self._member_state(ctx.author)).collection.count(card.card_id))
        await ctx.send("\n".join(lines), file=discord.File(rendered, filename="daily.png"),
                       allowed_mentions=discord.AllowedMentions.none())

    async def daily_pull(self, guild: discord.Guild, member: discord.Member):
        """Do one `.card daily` pull. Returns an error string, or
        (card, outcome, sell_price_or_None, streak, was_bonus_day, set_done_or_None)."""
        async with self._get_claim_lock(guild.id):  # same lock as the draw: one write to a member at a time
            today = engine.today_str(ACTIVITY_TIMEZONE)
            state = await self._member_state(member)
            can_pull, streak = engine.daily_streak(state.daily_pull_date, state.daily_streak, today)
            if not can_pull:
                return ("You've already pulled today. Next pull at midnight Pacific "
                        f"(streak: {state.daily_streak} day{'s' if state.daily_streak != 1 else ''}).")
            conf = self.config.guild(guild)
            bonus = engine.is_bonus_day(streak, DAILY_STREAK_BONUS_EVERY)
            card = engine.roll_daily(await self._rollable_pool_cards(guild), await conf.daily_weights(), bonus)
            if card is None:
                return "The card pool is empty right now, so there's nothing to pull."
            outcome, price = self._grant_card(state, card, await conf.sell_prices())
            set_done = await self._complete_set(guild, member, state, card, outcome)
            state.daily_pull_date, state.daily_streak = today, streak
            await self._save_member_state(member, state)
        await self._bump_stats(guild, daily_pull_date=today)
        return card, outcome, price, streak, bonus, set_done

    # -- give / sell ------------------------------------------------------

    @card.command(name="give")
    @commands.guild_only()
    async def card_give(self, ctx: commands.Context, member: discord.Member, *, card_arg: str):
        """Give one of your cards to another member. One-way, no confirmation.
        If you hold a tradeable spare (see MAX_COPIES_KEPT), giving it away
        doesn't touch your showcase as long as you still keep one copy."""
        if member.id == ctx.author.id:
            await ctx.send("You already own that one.")
            return
        if member.bot:
            await ctx.send("Bots don't collect cards.")
            return
        card = await self._resolve_card_arg(ctx.guild, card_arg)
        if card is None:
            await ctx.send("No card matches that.")
            return

        async with self._get_claim_lock(ctx.guild.id):
            giver_state = await self._member_state(ctx.author)
            if not giver_state.owns(card.card_id):
                await ctx.send("You don't own that card.")
                return
            receiver_state = await self._member_state(member)

            giver_state.collection.remove(card.card_id)  # removes exactly one copy
            # only drop the showcase entry if that was the giver's last copy --
            # giving away a tradeable spare shouldn't un-showcase the one they kept
            if not giver_state.owns(card.card_id) and card.card_id in giver_state.showcase_card_ids:
                giver_state.showcase_card_ids.remove(card.card_id)
            await self._save_member_state(ctx.author, giver_state)

            # same MAX_COPIES_KEPT cap a draw win respects: a gift that would push
            # the receiver past it converts straight to a sell token instead of
            # silently discarding it or piling up a 3rd+ copy
            outcome, _price = self._grant_card(receiver_state, card, await self.config.guild(ctx.guild).sell_prices())
            set_done = await self._complete_set(ctx.guild, member, receiver_state, card, outcome)
            await self._save_member_state(member, receiver_state)

        if outcome == "sell_token":
            await ctx.send(
                f"{member.mention} is already at their duplicate limit for **{card.name}** — "
                f"it was converted to a sell token for them instead."
            )
        else:
            await ctx.send(embed=embeds.give_result_embed(card, ctx.author, member))
        if set_done is not None:
            await ctx.send(f"\U0001f3c6 {member.mention} completed the **{set_done[0]}** set: +{set_done[1]:,} wondercoin!")

    @card.command(name="sell", aliases=["tokens"])
    @commands.guild_only()
    async def card_sell(self, ctx: commands.Context):
        """Your sell tokens (duplicates past the copy limit): sell them all at once, or one at a time."""
        view = views.SellView(self, ctx.author)
        await view.start(ctx)

    async def sell_tokens(self, guild: discord.Guild, member: discord.Member, token_ids=None) -> Tuple[int, int]:
        """Sell the given sell tokens (all of them if `token_ids` is None) for
        wondercoin. Returns (how many sold, total payout)."""
        async with self._get_claim_lock(guild.id):
            state = await self._member_state(member)
            chosen = [t for t in state.sell_tokens if token_ids is None or t.token_id in set(token_ids)]
            if not chosen:
                return 0, 0
            prices = await self.config.guild(guild).sell_prices()
            total = sum(engine.sell_price(t.rarity, prices) for t in chosen)
            state.sell_tokens = [t for t in state.sell_tokens if t not in chosen]
            await self._save_member_state(member, state)
        await bank.deposit_credits(member, total)
        return len(chosen), total

    @card.command(name="leaderboard")
    @commands.guild_only()
    async def card_leaderboard(self, ctx: commands.Context):
        """Top collectors by number of cards owned."""
        all_members = await self.config.all_members(ctx.guild)
        ranked = sorted(all_members.items(), key=lambda kv: len(kv[1].get("collection", [])), reverse=True)
        entries = []
        for member_id, data in ranked[:10]:
            if not data.get("collection"):
                continue
            member = ctx.guild.get_member(member_id)
            if member is None:
                continue
            entries.append((member, len(data["collection"])))
        await ctx.send(embed=embeds.leaderboard_embed(entries, ctx.guild.name))

    @card.command(name="settings")
    @commands.guild_only()
    async def card_settings(self, ctx: commands.Context):
        """Settings panel for admins; everyone else sees the current configuration."""
        perms = getattr(ctx.author, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_guild):
            await open_panel(ctx, self, panel_specs.build_specs(), title=f"Card Collect v{COG_VERSION}", intro=panel_specs.INTRO)
            return
        guild_config = await self.config.guild(ctx.guild).all()
        await ctx.send(embed=embeds.settings_embed(guild_config, ctx.guild.name))

    # -- admin commands --------------------------------------------------

    # -- settings (the panel calls these; each returns the message to show or raises PanelError) --

    @staticmethod
    def _whole(values, key, label, minimum, hint=""):
        try:
            number = int(str((values or {}).get(key, "")).strip())
        except ValueError:
            raise PanelError(f"{label} must be a whole number.")
        if number < minimum:
            raise PanelError(f"{label} must be at least {minimum}.{hint}")
        return number

    async def _apply_channel(self, guild, member, channel) -> str:
        if channel is None:
            raise PanelError("Pick a channel for the drops.")
        await self.config.guild(guild).channel_id.set(channel.id)
        return f"Drops will post in {channel.mention}."

    async def _apply_drop_rules(self, guild, member, values) -> str:
        try:
            percent = float(str((values or {}).get("chance", "")).strip())
        except ValueError:
            raise PanelError("Drop chance must be a number, like 1.5.")
        if not 0 < percent <= 100:
            raise PanelError("Drop chance must be greater than 0 and at most 100 (percent).")
        cooldown = self._whole(values, "cooldown", "Cooldown", 0, " Use 0 for none.")
        window = self._whole(values, "window", "Draw window", MIN_DRAW_WINDOW_SECONDS, " People need time to pick.")
        if window > MAX_DRAW_WINDOW_SECONDS:
            raise PanelError(f"Draw window can be at most {MAX_DRAW_WINDOW_SECONDS} seconds.")
        conf = self.config.guild(guild)
        await conf.drop_chance.set(percent / 100)
        await conf.drop_cooldown_seconds.set(cooldown)
        await conf.draw_window_seconds.set(window)
        return (f"A qualifying message drops a card {percent:g}% of the time, at most once every {cooldown}s; "
                f"each drop's draw stays open {window}s. Takes effect on the next drop.")

    async def _apply_request_channel(self, guild, member, channel) -> str:
        conf = self.config.guild(guild)
        if channel is None:
            await conf.request_channel_id.set(None)
            return "Card requests are off."
        await conf.request_channel_id.set(channel.id)
        return f"Card requests will arrive in {channel.mention}. Keep it private: only admins should see it."

    async def _apply_test_mode(self, guild, member, value) -> str:
        await self.config.guild(guild).test_mode.set(bool(value))
        return "Test mode ON: ambient drops run but award nothing." if value else "Test mode off: drops are live again."

    async def _apply_test_drop(self, guild, member, value) -> str:
        """Panel action: post one test drop in the drop channel right now."""
        channel_id = await self.config.guild(guild).channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            raise PanelError("Pick a drop channel first.")
        reason = await self._post_drop(channel, guild, is_test=True)
        if reason is not None:
            raise PanelError(reason)
        return f"Test drop posted in {channel.mention}. It awards nothing."

    async def _apply_daily_odds(self, guild, member, values) -> str:
        weights = {}
        for tier in ("common", "rare", "epic", "legendary"):
            try:
                weights[tier] = float(str((values or {}).get(tier, "")).strip())
            except ValueError:
                raise PanelError(f"{tier.title()} must be a number.")
        if any(v < 0 for v in weights.values()) or sum(weights.values()) <= 0:
            raise PanelError("Odds must be zero or more, and not all zero.")
        await self.config.guild(guild).daily_weights.set(weights)
        total = sum(weights.values())
        return "Daily pull odds: " + ", ".join(f"{t} {v / total * 100:g}%" for t, v in weights.items())

    @card.command(name="addcard")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_addcard(self, ctx: commands.Context, rarity: str, *, name_and_series: str):
        """Hand-add a character. Attach the card art to this message.
        Usage: .card addcard <rarity> <name> | <series>"""
        rarity = rarity.strip().lower()
        if rarity not in DEFAULT_TIER_CUTOFFS:
            await ctx.send(f"Rarity must be one of: {', '.join(DEFAULT_TIER_CUTOFFS)}.")
            return
        if not ctx.message.attachments:
            await ctx.send("Attach the card image to this message.")
            return

        if "|" in name_and_series:
            name, series = (part.strip() for part in name_and_series.split("|", 1))
        else:
            name, series = name_and_series.strip(), ""

        image_bytes = await ctx.message.attachments[0].read()

        async with self.config.guild(ctx.guild).all() as guild_data:
            card_id = guild_data["next_id"]
            guild_data["next_id"] += 1
            storage.save_card_image(self.data_path, ctx.guild.id, card_id, image_bytes)
            guild_data["pool"][str(card_id)] = Card(
                card_id=card_id,
                name=name,
                series=series,
                rarity=rarity,
                image_path=str(storage.card_image_path(self.data_path, ctx.guild.id, card_id)),
                favourites=0,
                added_by=ctx.author.id,
            ).to_dict()

        card = await self._card_by_id(ctx.guild, card_id)
        await ctx.send(embed=embeds.card_added_embed(card))
        await self._fulfil_requests(ctx.guild, [(card.card_id, card.name)])

    @card.command(name="removecard")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_removecard(self, ctx: commands.Context, *, card_ids: str):
        """Remove one or more characters from future drops. Members keep
        copies they already have. Accepts multiple space-separated IDs, e.g.
        `.card removecard 4 17 32` -- pair with `.card viewpool` to look up
        which IDs to remove.

        This retires each card rather than deleting it outright: the pool
        entry and its art stay on disk so anyone who already owns a copy
        still gets a working gallery tile. Only future drop rolls skip it."""
        ids: List[int] = []
        invalid: List[str] = []
        for token in card_ids.split():
            if token.lstrip("-").isdigit():
                ids.append(int(token))
            else:
                invalid.append(token)
        if invalid:
            await ctx.send(f"Not valid card IDs, ignored: {', '.join(invalid)}")
        if not ids:
            await ctx.send("No valid card IDs given.")
            return

        retired: List[Tuple[int, str]] = []
        not_found: List[int] = []
        async with self.config.guild(ctx.guild).pool() as pool:
            for card_id in ids:
                entry = pool.get(str(card_id))
                if entry is None:
                    not_found.append(card_id)
                    continue
                entry["retired"] = True
                retired.append((card_id, entry["name"]))

        await ctx.send(embed=embeds.cards_removed_embed(retired, not_found))

    @card.command(name="resetpool")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_resetpool(self, ctx: commands.Context, confirm: Optional[str] = None):
        """Retire every active character in the pool at once (admin). Same
        soft-delete `.card removecard` uses -- members who already own a
        card keep it, but nothing in the pool will drop again until you
        re-import or re-add characters. Destructive-feeling enough that it
        requires literally typing `.card resetpool confirm`."""
        rollable = await self._rollable_pool_cards(ctx.guild)
        if not rollable:
            await ctx.send("The pool has no active (non-retired) cards to reset.")
            return
        if confirm != "confirm":
            await ctx.send(
                f"This retires all **{len(rollable)}** active characters in the pool -- "
                f"no more drops until you re-import (`.card importpool`) or re-add "
                f"(`.card addcard`). Members who already own a card keep it either way. "
                f"Run `.card resetpool confirm` to actually do this."
            )
            return

        count = 0
        async with self.config.guild(ctx.guild).pool() as pool:
            for entry in pool.values():
                if not entry.get("retired", False):
                    entry["retired"] = True
                    count += 1

        await ctx.send(embed=embeds.pool_reset_embed(count))

    @card.command(name="viewpool")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_viewpool(self, ctx: commands.Context):
        """List every character in the pool -- ID, rarity, name, series,
        favourites, and retired status -- as a text file, sorted by ID.
        Meant to be paired with `.card removecard <id> <id> ...`: skim the
        file, then remove whatever you don't want in one command. Retired
        characters are included (marked) since their IDs are still useful
        for reference even though they no longer drop."""
        pool_cards = await self._pool_cards(ctx.guild)
        if not pool_cards:
            await ctx.send("The pool is empty.")
            return

        pool_cards.sort(key=lambda c: c.card_id)
        by_tier: Dict[str, int] = {}
        retired_count = 0
        lines = []
        for card in pool_cards:
            by_tier[card.rarity] = by_tier.get(card.rarity, 0) + 1
            if card.retired:
                retired_count += 1
            flag = " [retired]" if card.retired else ""
            series = f" ({card.series})" if card.series else ""
            lines.append(
                f"{card.card_id}\t{card.rarity}\t{card.name}{series}\tfavs={card.favourites}{flag}"
            )

        tier_summary = ", ".join(f"{by_tier.get(t, 0)} {t}" for t in ("common", "rare", "epic", "legendary"))
        summary = (
            f"**{len(pool_cards)}** characters in the pool ({tier_summary}; "
            f"{retired_count} retired). Full list attached."
        )
        text = "\n".join(lines)
        buf = io.BytesIO(text.encode("utf-8"))
        await ctx.send(content=summary, file=discord.File(buf, filename="cardcollect_pool.txt"))

    # -- pool import ---------------------------------------------------------

    async def _pool_card_dicts(self, guild: discord.Guild) -> List[dict]:
        """Every pool card INCLUDING retired ones, as plain dicts for the import
        dedupe. Retired cards must count: a character you pruned must not come
        back on the next import."""
        return [
            {
                "card_id": c.card_id,
                "anilist_id": c.anilist_id,
                "name": c.name,
                "series": c.series,
                "series_alt": c.series_alt,
            }
            for c in await self._pool_cards(guild)
        ]

    @staticmethod
    def _anilist_error_text(error: Exception) -> str:
        if isinstance(error, aiohttp.ClientResponseError) and error.status == 429:
            return (
                "AniList kept rate-limiting this even after retrying with backoff -- "
                "try again in a few minutes, and avoid running imports at the same time."
            )
        return f"AniList request failed: {error}"

    async def _download_images(self, entries: List[dict]) -> Tuple[List[Tuple[dict, bytes]], List[str]]:
        """Download each entry's art. Returns (downloaded, names_that_failed).
        Done before any Config write, never inside one -- see card_importpool."""
        downloaded: List[Tuple[dict, bytes]] = []
        failed: List[str] = []
        for entry in entries:
            try:
                async with self._session.get(entry["image_url"]) as resp:
                    resp.raise_for_status()
                    image_bytes = await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                failed.append(entry["name"])
                continue
            downloaded.append((entry, image_bytes))
        return downloaded, failed

    async def _write_pool_entries(
        self, guild: discord.Guild, downloaded: List[Tuple[dict, bytes]], links: Optional[List[dict]] = None
    ) -> List[dict]:
        """Add the downloaded characters to the pool in one quick Config write,
        and give hand-added cards that matched an AniList character their
        AniList id. Re-checks AniList ids against the pool at write time, so
        two imports (or a stale preview) can never add the same character twice.
        Returns the entries actually added."""
        added: List[dict] = []
        async with self.config.guild(guild).all() as guild_data:
            present = {d.get("anilist_id") for d in guild_data["pool"].values() if d.get("anilist_id") is not None}
            for entry, image_bytes in downloaded:
                anilist_id = entry.get("anilist_id")
                if anilist_id is not None and anilist_id in present:
                    continue
                card_id = guild_data["next_id"]
                guild_data["next_id"] += 1
                storage.save_card_image(self.data_path, guild.id, card_id, image_bytes)
                guild_data["pool"][str(card_id)] = Card(
                    card_id=card_id,
                    name=entry["name"],
                    series=entry["series"],
                    rarity=entry["rarity"],
                    image_path=str(storage.card_image_path(self.data_path, guild.id, card_id)),
                    favourites=entry["favourites"],
                    added_by=None,
                    anilist_id=anilist_id,
                    series_alt=entry.get("series_alt", ""),
                ).to_dict()
                if anilist_id is not None:
                    present.add(anilist_id)
                entry["card_id"] = card_id
                added.append(entry)
            for link in links or []:
                data = guild_data["pool"].get(str(link["card_id"]))
                if data is not None and data.get("anilist_id") is None and link.get("anilist_id") is not None:
                    data["anilist_id"] = link["anilist_id"]
                    if not data.get("series_alt") and link.get("series_alt"):
                        data["series_alt"] = link["series_alt"]
        await self._fulfil_requests(guild, [(e["card_id"], e["name"]) for e in added])
        return added

    async def apply_card_rarity(self, guild: discord.Guild, card_id: int, tier: str) -> Card:
        """Change one pool card's rarity (the dropdown on `.card info`). Raises
        KeyError if the card isn't in the pool and ValueError for a bad tier."""
        tier = tier.strip().lower()
        if tier not in TIERS:
            raise ValueError(f"Rarity must be one of: {', '.join(TIERS)}.")
        async with self.config.guild(guild).pool() as pool:
            entry = pool.get(str(card_id))
            if entry is None:
                raise KeyError(card_id)
            entry["rarity"] = tier
        return await self._card_by_id(guild, card_id)

    async def set_card_retired(self, guild: discord.Guild, card_id: int, retired: bool) -> Card:
        """Retire a card from future drops, or put it back (the button on a
        card's `.card find` page). Owners keep their copies either way. Raises
        KeyError if the card isn't in the pool."""
        async with self.config.guild(guild).pool() as pool:
            entry = pool.get(str(card_id))
            if entry is None:
                raise KeyError(card_id)
            if retired:
                entry["retired"] = True
            else:
                entry.pop("retired", None)
        return await self._card_by_id(guild, card_id)

    @card.command(name="importpool")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_importpool(self, ctx: commands.Context, count: int = 30, gender: str = "any"):
        """Bulk-import `count` AniList characters, spread across rarity tiers to
        match drop_weights (not just the top `count` by favourites -- see
        import_characters.tier_quotas for why that matters). `gender` is any
        (the default), female or male. To add one whole series instead, use
        `.card importseries <name>`."""
        gender = gender.strip().lower()
        if gender not in IMPORT_GENDERS:
            await ctx.send(f"Gender must be one of: {', '.join(IMPORT_GENDERS)}.")
            return
        if count < 1 or count > 200:
            await ctx.send("Pick a count between 1 and 200.")
            return

        # AniList rate-limits aggressively -- a second import started while one
        # is already running would multiply the request rate against the same
        # limit and trip 429s that neither run alone would hit (see the comment
        # on self._importpool_lock). Queue rather than run concurrently, and
        # say so, so a second run doesn't just look like it's doing nothing.
        if self._importpool_lock.locked():
            await ctx.send("Another AniList import is already running -- this one will start once it finishes.")

        async with self._importpool_lock:
            label = "" if gender == "any" else f"{gender} "
            await ctx.send(f"Fetching {count} {label}characters from AniList, spread across rarity tiers…")
            tier_cutoffs = await self.config.guild(ctx.guild).tier_cutoffs()
            drop_weights = await self.config.guild(ctx.guild).drop_weights()
            existing_cards = await self._pool_card_dicts(ctx.guild)
            # skip characters already in the pool (by AniList id, retired ones
            # included) so re-running the import doesn't add the same character
            # twice under a new local card_id
            existing_anilist_ids = {c["anilist_id"] for c in existing_cards if c["anilist_id"] is not None}

            try:
                raw_entries = await import_characters.fetch_top_characters(
                    self._session,
                    tier_cutoffs,
                    engine.bucket_tier,
                    count,
                    weights=drop_weights,
                    exclude_ids=existing_anilist_ids,
                    gender=gender,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                await ctx.send(self._anilist_error_text(e))
                return

            if not raw_entries:
                await ctx.send("No characters came back -- AniList may be unreachable, or the filter matched nothing.")
                return

            # a fetched character that is really a hand-added card already in the
            # pool (same name and series) isn't imported again; that card just
            # learns its AniList id so the next import recognises it
            raw_entries, links = import_characters.split_hand_added(raw_entries, existing_cards)

            # Download every image *before* touching Config, not inside the
            # "async with ... .all()" write: that block used to wrap the whole
            # network loop (up to 200 downloads), holding a mutable handle on
            # the guild's entire config open the whole time -- any other command
            # writing the same guild's config in that window (e.g. a concurrent
            # .card addcard) would have its change silently overwritten.
            downloaded, failed = await self._download_images(raw_entries)
            added = await self._write_pool_entries(ctx.guild, downloaded, links)

            message = (
                f"Imported {len(added)} characters (characters already in the pool were skipped automatically). "
                f"Prune unwanted ones with `.card removecard <id>`."
            )
            if links:
                message += f"\n{len(links)} hand-added card{'s' if len(links) != 1 else ''} matched and were linked instead of duplicated."
            if failed:
                shown = ", ".join(failed[:5]) + (f", +{len(failed) - 5} more" if len(failed) > 5 else "")
                message += f"\n{len(failed)} image download(s) failed and were not added ({shown}); run the import again to retry them."
            await ctx.send(message)

    async def build_series_preview(self, guild: discord.Guild, media: dict):
        """Fetch one AniList series' characters and work out what importing
        them would do. Returns a SeriesPreview, or a message string if it can't."""
        try:
            async with self._importpool_lock:
                info = await import_characters.fetch_series_characters(self._session, media["id"], SERIES_IMPORT_MAX)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return self._anilist_error_text(e)
        except (KeyError, TypeError):
            return "AniList's answer wasn't in the shape I expected, so nothing was imported."
        if not info["characters"]:
            return f"AniList has no characters listed for **{media['title']}**."
        tier_cutoffs = await self.config.guild(guild).tier_cutoffs()
        built = import_characters.build_series_entries(
            info["characters"],
            info["title"] or media["title"],
            info["title_english"],
            tier_cutoffs,
            engine.bucket_tier,
            await self._pool_card_dicts(guild),
        )
        active = Counter(c.rarity for c in await self._rollable_pool_cards(guild))
        preview = SeriesPreview(
            media=media, info=info, built=built, pool_tiers={t: active.get(t, 0) for t in TIERS}
        )
        await self._prefetch_preview_art(preview, SERIES_IMPORT_DEFAULT)
        return preview

    async def _prefetch_preview_art(self, preview: SeriesPreview, limit: int) -> None:
        """Download the art of the characters the preview shows (the top
        `limit` new ones) so the contact sheet can show it, and flag the images
        that look poor. The same bytes are reused at import time."""
        shown = [e for e in preview.built["entries"] if e["rank"] < limit]
        downloaded, _failed = await self._download_images(shown)
        preview.images = {entry["rank"]: image_bytes for entry, image_bytes in downloaded}
        for entry in shown:
            reason = import_characters.art_flags(entry, preview.images.get(entry["rank"]))
            if reason:
                preview.flags[entry["rank"]] = reason

    @staticmethod
    def contact_sheet_file(preview: SeriesPreview, limit: int) -> discord.File:
        """The numbered art grid shown under the series preview."""
        numbers = import_characters.entry_numbers(preview.built)
        tiles = []
        for entry in preview.built["entries"]:
            rank = entry["rank"]
            if rank >= limit:
                continue
            tiles.append(
                {
                    "number": numbers[rank],
                    "name": entry["name"],
                    "image": preview.art.get(rank) or preview.images.get(rank),
                    "flag": None if rank in preview.art else preview.flags.get(rank),
                    "skipped": rank in preview.skips,
                    "replaced": rank in preview.art,
                }
            )
        return discord.File(imagegen.render_contact_sheet(tiles, SHEET_COLUMNS), filename="contact_sheet.png")

    async def wait_for_upload(self, channel_id: int, user_id: int, timeout: float, images_only: bool = False, cancel=None):
        """The next message from `user_id` in the channel, None if they say
        nothing for `timeout` seconds, or UPLOAD_CANCELLED.

        With `images_only`, chat that carries no image is ignored (the wait goes
        on) so ordinary conversation never ends it; typing "cancel" (or pressing
        the button that sets the `cancel` event) is the way out."""
        def check(message):
            return message.author.id == user_id and message.channel.id == channel_id

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            waiter = asyncio.ensure_future(self.bot.wait_for("message", check=check, timeout=remaining))
            watchers = [waiter]
            cancel_task = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
            if cancel_task is not None:
                watchers.append(cancel_task)
            try:
                await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if cancel_task is not None and not cancel_task.done():
                    cancel_task.cancel()
            if cancel_task is not None and cancel_task.done() and not cancel_task.cancelled():
                waiter.cancel()
                return UPLOAD_CANCELLED
            try:
                message = waiter.result()
            except asyncio.TimeoutError:
                return None
            if not images_only or message.attachments:
                return message
            if (message.content or "").strip().lower() in UPLOAD_CANCEL_WORDS:
                return UPLOAD_CANCELLED

    @staticmethod
    async def read_uploaded_art(message) -> Tuple[Optional[bytes], Optional[str]]:
        """The first image attached to an admin's message as (bytes, None), or
        (None, what's wrong with it). Deletes the message afterwards if it can,
        so uploads don't clutter the channel."""
        attachment = message.attachments[0] if message.attachments else None
        if attachment is None:
            return None, "That message has no image attached."
        if getattr(attachment, "size", 0) and attachment.size > ART_MAX_UPLOAD_BYTES:
            return None, f"That image is over {ART_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        data = await attachment.read()
        if not imagegen.valid_art(data):
            return None, "That file isn't an image I can read (use PNG, JPG, WebP or GIF)."
        try:
            await message.delete()
        except (discord.HTTPException, AttributeError):
            pass  # no permission to delete is fine
        return data, None

    async def replace_card_art(self, guild: discord.Guild, card_id: int, image_bytes: bytes) -> Card:
        """Swap one pool card's art (the Replace art button on `.card info`).
        Same effect as `.card setimage`. KeyError if the card is gone,
        ValueError if the bytes aren't an image."""
        card = await self._card_by_id(guild, card_id)
        if card is None:
            raise KeyError(card_id)
        if not imagegen.valid_art(image_bytes):
            raise ValueError("not an image")
        storage.save_card_image(self.data_path, guild.id, card_id, image_bytes)
        return card

    async def commit_series(self, guild: discord.Guild, preview: SeriesPreview, limit: int) -> discord.Embed:
        """Do the import the admin confirmed: the top `limit` characters, with
        their rarity edits. Returns the result embed."""
        selected = import_characters.select_top(preview.built, limit, preview.overrides, preview.skips)
        # art already on hand (downloaded for the contact sheet, or uploaded by
        # the admin) is reused; only the rest is downloaded now
        need = [e for e in selected["entries"] if e["rank"] not in preview.art and e["rank"] not in preview.images]
        async with self._importpool_lock:
            fetched, failed = await self._download_images(need)
            by_rank = {entry["rank"]: image_bytes for entry, image_bytes in fetched}
            downloaded = []
            for entry in selected["entries"]:
                image_bytes = preview.art.get(entry["rank"]) or preview.images.get(entry["rank"]) or by_rank.get(entry["rank"])
                if image_bytes is not None:
                    downloaded.append((entry, image_bytes))
            added = await self._write_pool_entries(guild, downloaded, selected["links"])
        already = sum(1 for x in selected["skipped"] if x["reason"] == "already in the pool")
        return embeds.series_result_embed(preview.info["title"], added, failed, len(selected["links"]), already)

    @card.command(name="importseries")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_importseries(self, ctx: commands.Context, *, series: str):
        """Import characters of one series from AniList (all genders). Search,
        pick the right match, review a preview, then confirm -- nothing is
        added until you press an Import button. Example: `.card importseries Frieren`"""
        query = series.strip()
        if not query:
            await ctx.send("Give a series name, like `.card importseries Frieren`.")
            return
        try:
            async with self._importpool_lock:
                results = await import_characters.search_media(self._session, query)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await ctx.send(self._anilist_error_text(e))
            return
        except (KeyError, TypeError):
            await ctx.send("AniList's answer wasn't in the shape I expected, so nothing was imported.")
            return
        if not results:
            await ctx.send(f"AniList has no series matching **{discord.utils.escape_markdown(query)[:60]}**.")
            return

        if len(results) == 1:  # nothing to choose between -- straight to the preview
            preview = await self.build_series_preview(ctx.guild, results[0])
            if isinstance(preview, str):
                await ctx.send(preview)
                return
            view = views.SeriesPreviewView(self, ctx.author.id, preview)
            view.message = await ctx.send(view=view, **view.payload(send=True))
            return

        view = views.SeriesPickView(self, ctx.author.id, results)
        view.message = await ctx.send(embed=embeds.series_search_embed(query, results), view=view)

    @card.command(name="diagnostics")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def card_diagnostics(self, ctx: commands.Context):
        """Activity-based sizing report for drop_chance/drop_cooldown_seconds."""
        await self._flush_activity()  # include counts still buffered in memory
        tracking = await self.config.guild(ctx.guild).activity_tracking()
        buckets = tracking.get("hourly_buckets", {})
        sampling_since = tracking.get("sampling_since") or time.time()

        total = sum(buckets.values())
        if total == 0:
            suggested_chance = DEFAULT_DROP_CHANCE
            suggested_cooldown = DEFAULT_DROP_COOLDOWN_SECONDS
        else:
            peak_hour_count = max(buckets.values())
            drop_size = await self.config.guild(ctx.guild).drop_size()
            # aim for roughly one drop every ~40 qualifying messages during
            # the busiest hour, floor/ceiling'd to sane bounds
            target_drops_per_peak_hour = max(1, peak_hour_count // 40)
            suggested_chance = round(min(0.15, max(0.005, target_drops_per_peak_hour / max(peak_hour_count, 1))), 4)
            suggested_cooldown = max(180, int(3600 / max(target_drops_per_peak_hour, 1) / 2))

        stats = await self.config.guild(ctx.guild).stats()
        embed = embeds.diagnostics_embed(
            buckets, suggested_chance, suggested_cooldown, sampling_since, ctx.guild.name, stats=stats
        )

        payload = {
            "draw_stats": stats,
            "hourly_buckets": buckets,
            "sampling_since": sampling_since,
            "suggested_drop_chance": suggested_chance,
            "suggested_drop_cooldown_seconds": suggested_cooldown,
            "timezone": ACTIVITY_TIMEZONE,
        }
        file_obj = io.BytesIO(json.dumps(payload, indent=2).encode("utf-8"))
        await ctx.send(embed=embed, file=discord.File(file_obj, filename="cardcollect_diagnostics.json"))
