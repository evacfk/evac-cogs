"""Main cog: message listener (chance-per-message drop trigger), reaction-based
claim resolution, and all `.card` commands.

Game logic lives in engine.py (pure, unit-tested standalone); this file is
the Discord-facing wiring around it -- Config I/O, the message/reaction
listeners, image compositing calls into imagegen.py, and command handlers.
"""

import asyncio
import io
import json
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

from . import embeds, engine, imagegen, import_characters, storage
from .constants import (
    ACTIVITY_TIMEZONE,
    DEFAULT_CLAIM_COOLDOWN_SECONDS,
    DEFAULT_CLAIM_QUOTA,
    DEFAULT_CLAIM_WINDOW_SECONDS,
    DEFAULT_DECOY_COUNT,
    DEFAULT_DECOYS_ENABLED,
    DEFAULT_DROP_CHANCE,
    DEFAULT_DROP_COOLDOWN_SECONDS,
    DEFAULT_DROP_SIZE,
    DEFAULT_DROP_WEIGHTS,
    DEFAULT_SELL_PRICES,
    DEFAULT_TIER_CUTOFFS,
    DEFAULT_WRONG_GUESS_PENALTY_SECONDS,
    EMOJI_POOL,
    MAX_COPIES_KEPT,
    MAX_SHOWCASE_SLOTS,
    TIERS,
)
from .models import ActiveDrop, Card, MemberState

DEFAULT_GUILD = {
    "channel_id": None,
    "pool": {},
    "next_id": 1,
    "drop_chance": DEFAULT_DROP_CHANCE,
    "drop_cooldown_seconds": DEFAULT_DROP_COOLDOWN_SECONDS,
    "drop_size": DEFAULT_DROP_SIZE,
    "drop_weights": dict(DEFAULT_DROP_WEIGHTS),
    "tier_cutoffs": dict(DEFAULT_TIER_CUTOFFS),
    "decoys_enabled": DEFAULT_DECOYS_ENABLED,
    "decoy_count": DEFAULT_DECOY_COUNT,
    "claim_window_seconds": DEFAULT_CLAIM_WINDOW_SECONDS,
    "claim_cooldown_seconds": DEFAULT_CLAIM_COOLDOWN_SECONDS,
    "wrong_guess_penalty_seconds": DEFAULT_WRONG_GUESS_PENALTY_SECONDS,
    "claim_quota": DEFAULT_CLAIM_QUOTA,
    "sell_prices": dict(DEFAULT_SELL_PRICES),
    "test_mode": False,
    "activity_tracking": {"hourly_buckets": {}, "sampling_since": 0},
}

DEFAULT_MEMBER = {
    "collection": [],
    "showcase_card_ids": [],
    "sell_tokens": [],
    "daily_claims": 0,
    "daily_claims_date": "",
}


class CardCollect(commands.Cog):
    """Ambient anime-character card collecting, driven by chat activity."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xCA5DC011EC7, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)

        self.data_path: Path = cog_data_path(self)

        # in-memory only -- losing these on restart just expires whatever
        # was mid-flight, never touches persisted collections/tokens
        self.active_drops: Dict[int, ActiveDrop] = {}  # message_id -> ActiveDrop
        self.pending_windows: Dict[Tuple[int, str], bool] = {}  # (message_id, emoji) -> scheduled
        self.last_drop_time: Dict[int, float] = {}  # guild_id -> monotonic time
        self.claim_cooldown_until: Dict[int, float] = {}  # user_id -> monotonic time
        # set by a wrong (decoy) reaction guess -- see on_raw_reaction_add.
        # Excludes a user from *winning* any drop while active, checked
        # alongside claim_cooldown_until in _resolve_claim_window.
        self.wrong_guess_penalty_until: Dict[int, float] = {}  # user_id -> monotonic time
        # A fresh multi-card drop is often claimed almost all at once -- each
        # real card triggers its own _resolve_claim_window task, and every
        # one of those used to independently call channel.fetch_message()
        # for the exact same message. That's redundant enough (2-3+ near-
        # simultaneous GETs to the same message) to occasionally trip
        # Discord's per-route rate limit; discord.py handles a 429 by
        # sleeping and retrying rather than raising, so nothing was ever
        # lost -- but a claim embed could sit unposted for several seconds
        # until its window's fetch finally went through (live bug report:
        # a 3-card drop where the third claim only appeared "if delayed").
        # See _get_drop_message: a very-short-lived cache means concurrent
        # resolutions for the same message share one fetch instead of each
        # firing their own -- the actual reactor list always comes from a
        # separate, always-fresh reaction.users() call per emoji, so this
        # never risks stale claim data, only avoids a redundant GET.
        self._message_cache: Dict[int, Tuple[discord.Message, float]] = {}  # message_id -> (message, fetched_at)
        self._message_fetch_locks: Dict[int, asyncio.Lock] = {}  # message_id -> lock
        self._drop_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> lock
        # AniList's public API rate-limits aggressively; without this, two
        # `.card importpool` runs fired close together each start their own
        # up-to-200-page loop and multiply the request rate against the same
        # limit, tripping 429s that neither run alone would have hit. This
        # lock is process-wide (not per-guild) since it's AniList's own
        # global limit being protected, not anything guild-scoped.
        self._importpool_lock = asyncio.Lock()

        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        # a hung AniList request or a slow image host would otherwise leave
        # .card importpool waiting forever -- give every request on this
        # session a ceiling
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def cog_unload(self):
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

    _MESSAGE_CACHE_TTL_SECONDS = 0.5

    async def _get_drop_message(self, channel: discord.abc.Messageable, message_id: int) -> discord.Message:
        """Fetch a drop's message, reusing a very-recent fetch instead of
        hitting the API again -- see the comment on self._message_cache in
        __init__. Concurrent callers for the same message_id coalesce
        behind a lock instead of each firing their own fetch_message()."""
        now = time.monotonic()
        cached = self._message_cache.get(message_id)
        if cached is not None and now - cached[1] < self._MESSAGE_CACHE_TTL_SECONDS:
            return cached[0]

        lock = self._message_fetch_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            # re-check after acquiring the lock -- a concurrent resolution
            # may have already fetched it while this one was waiting
            now = time.monotonic()
            cached = self._message_cache.get(message_id)
            if cached is not None and now - cached[1] < self._MESSAGE_CACHE_TTL_SECONDS:
                return cached[0]
            message = await channel.fetch_message(message_id)
            self._message_cache[message_id] = (message, time.monotonic())
            return message

    def _forget_drop(self, message_id: int):
        """Clean up every in-memory structure keyed by a drop's message_id
        once it's fully resolved -- without this, self._message_cache and
        self._message_fetch_locks would grow by one entry per drop ever
        posted, forever."""
        self.active_drops.pop(message_id, None)
        self._message_cache.pop(message_id, None)
        self._message_fetch_locks.pop(message_id, None)

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

    async def _track_activity(self, guild: discord.Guild):
        conf = self.config.guild(guild)
        tracking = await conf.activity_tracking()
        if not tracking.get("sampling_since"):
            tracking["sampling_since"] = time.time()
        hour = str(datetime.now(ZoneInfo(ACTIVITY_TIMEZONE)).hour)
        buckets = tracking.setdefault("hourly_buckets", {})
        buckets[hour] = buckets.get(hour, 0) + 1
        await conf.activity_tracking.set(tracking)

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
            return "The card pool is empty. Add characters first with `.card addcard` or `.card importpool`."

        weights = await conf.drop_weights()
        drop_size = await conf.drop_size()
        decoys_enabled = await conf.decoys_enabled()
        decoy_count = await conf.decoy_count()

        drop = engine.build_drop(
            pool_cards,
            weights,
            drop_size,
            EMOJI_POOL,
            decoys_enabled,
            decoy_count,
            guild.id,
            channel.id,
            is_test=is_test,
        )
        if drop is None:
            return "Couldn't roll a drop from the current pool (no cards available in any rarity tier)."

        entries = []
        for card_entry in drop.cards:
            card = await self._card_by_id(guild, card_entry["card_id"])
            image_bytes = self._read_card_image(guild, card_entry["card_id"])
            if card is None or image_bytes is None:
                continue
            entries.append((card, image_bytes, card_entry["emoji"]))
        if not entries:
            return (
                "Rolled cards but couldn't find their art on disk -- the pool may be "
                "out of sync with the image files. Check `.card settings` and the data folder."
            )

        composite = imagegen.render_drop(entries, is_test=is_test)
        content = "\U0001f9ea **Test drop** — claims here won't award anything." if is_test else None
        sent = await channel.send(content=content, file=discord.File(composite, filename="drop.png"))

        drop.message_id = sent.id
        self.active_drops[sent.id] = drop

        order = engine.reaction_add_order(drop)
        for emoji in order:
            try:
                await sent.add_reaction(emoji)
            except discord.HTTPException:
                continue

        return None

    # ------------------------------------------------------------------
    # claim resolution
    # ------------------------------------------------------------------

    async def _dm_decoy_result(self, payload: discord.RawReactionActionEvent, penalty_seconds: float, is_test: bool):
        """Best-effort DM to whoever just burned their shot on a decoy.
        There's no interaction token on a raw reaction event, so a true
        ephemeral reply (Discord's own "only you can see this") isn't
        possible here -- a DM is the closest thing that's actually private
        to just them. Silently does nothing if the member can't be
        resolved or has DMs closed; this is flavor text, not something
        that should ever surface an error or fall back to the channel."""
        channel = self.bot.get_channel(payload.channel_id)
        guild = getattr(channel, "guild", None)
        member = guild.get_member(payload.user_id) if guild else None
        if member is None:
            return

        if is_test:
            text = "❌ Wrong guess — that was a decoy, not a real card. (Test mode, so no real penalty this time.)"
        elif penalty_seconds > 0:
            text = (
                "❌ Wrong guess — that was a decoy, not a real card. You're locked out of "
                f"winning any drop for {int(penalty_seconds)}s. Now watch everyone else collect "
                "the cards. Cuck! \U0001f921"
            )
        else:
            text = "❌ Wrong guess — that was a decoy, not a real card."

        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return

        drop = self.active_drops.get(payload.message_id)
        if drop is None:
            return

        # One reaction is each member's "shot" at this drop -- once it's
        # used (on a decoy, or on a still-open real card), every further
        # reaction of theirs on this message is inert. This is what makes
        # "click every emoji until something hits" no longer work: you have
        # to actually look at the card art and pick once.
        if payload.user_id in drop.reacted_users:
            return

        emoji = str(payload.emoji)

        if not drop.is_real_emoji(emoji):
            # a genuine wrong guess -- spends the shot and, on a real drop,
            # costs a temporary lockout from *winning* any drop. Test drops
            # still enforce the one-shot gate (so `.card testdrop` previews
            # the real interaction accurately), but never set the penalty
            # itself -- same test-mode exemption claim_cooldown_until and
            # the daily quota already get in _resolve_claim_window, so
            # testing decoys can't lock an admin out of winning real drops.
            drop.reacted_users.add(payload.user_id)
            penalty = 0
            if not drop.is_test:
                conf = self.config.guild_from_id(payload.guild_id)
                penalty = await conf.wrong_guess_penalty_seconds()
                if penalty > 0:
                    self.wrong_guess_penalty_until[payload.user_id] = time.monotonic() + penalty
            await self._dm_decoy_result(payload, penalty, drop.is_test)
            return

        card_entry = drop.emoji_for(emoji)
        if card_entry["position"] in drop.claimed_positions:
            # the drop image gives no visual sign a card is already gone,
            # so landing on one isn't the member's fault -- don't burn
            # their shot for it, let them try again
            return

        drop.reacted_users.add(payload.user_id)

        key = (payload.message_id, emoji)
        if key in self.pending_windows:
            return
        self.pending_windows[key] = True

        conf = self.config.guild_from_id(payload.guild_id)
        window = await conf.claim_window_seconds()
        asyncio.create_task(self._resolve_claim_window(payload.channel_id, payload.message_id, emoji, window))

    async def _resolve_claim_window(self, channel_id: int, message_id: int, emoji: str, window: float):
        await asyncio.sleep(window)
        self.pending_windows.pop((message_id, emoji), None)

        drop = self.active_drops.get(message_id)
        if drop is None:
            return
        card_entry = drop.emoji_for(emoji)
        if card_entry is None or card_entry["position"] in drop.claimed_positions:
            return

        # Mark this position claimed *before* any further awaits, not after.
        # pending_windows only guards against a second window being
        # scheduled while this one is still sleeping -- it was just popped
        # above, so a reaction arriving during the fetch_message/users()
        # calls below could otherwise pass the pending_windows check again
        # and race a second _resolve_claim_window for the same card. Setting
        # claimed_positions here, synchronously, closes that window: any
        # concurrent/later call sees the position already claimed and bails
        # at the guard above instead of double-awarding the card.
        drop.claimed_positions.add(card_entry["position"])

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                if len(drop.claimed_positions) >= len(drop.cards):
                    self._forget_drop(message_id)
                return
        guild = channel.guild

        try:
            message = await self._get_drop_message(channel, message_id)
        except discord.HTTPException:
            if len(drop.claimed_positions) >= len(drop.cards):
                self._forget_drop(message_id)
            return

        reaction = discord.utils.find(lambda r: str(r.emoji) == emoji, message.reactions)
        if reaction is None:
            if len(drop.claimed_positions) >= len(drop.cards):
                self._forget_drop(message_id)
            return

        now = time.monotonic()
        today = engine.today_str(ACTIVITY_TIMEZONE)
        claim_quota = 0 if drop.is_test else await self.config.guild(guild).claim_quota()
        reactor_ids = []
        async for user in reaction.users():
            if user.bot:
                continue
            if user.id in drop.claimed_by:
                # already won a different card from this same drop -- one
                # card per person per drop, even though the other cards are
                # still independently claimable by everyone else
                continue
            if self.claim_cooldown_until.get(user.id, 0.0) > now:
                continue
            if self.wrong_guess_penalty_until.get(user.id, 0.0) > now:
                continue
            if claim_quota > 0:
                # a plain reactor.users() User, not a guild Member -- no
                # Member fetch needed just to read their quota state, so use
                # member_from_ids directly instead of resolving a full member
                member_conf = self.config.member_from_ids(guild.id, user.id)
                daily_claims = await member_conf.daily_claims()
                daily_claims_date = await member_conf.daily_claims_date()
                if not engine.has_quota_remaining(daily_claims, daily_claims_date, claim_quota, today):
                    continue
            reactor_ids.append(user.id)

        if not reactor_ids:
            if len(drop.claimed_positions) >= len(drop.cards):
                self._forget_drop(message_id)
            return

        winner_id = engine.resolve_claim(reactor_ids)
        member = guild.get_member(winner_id)
        if member is None:
            try:
                member = await guild.fetch_member(winner_id)
            except discord.HTTPException:
                if len(drop.claimed_positions) >= len(drop.cards):
                    self._forget_drop(message_id)
                return

        card = await self._card_by_id(guild, card_entry["card_id"])
        if card is None:
            if len(drop.claimed_positions) >= len(drop.cards):
                self._forget_drop(message_id)
            return

        # one card per person per drop: mark the winner before the reward
        # branches below so they can't also win any of this drop's other,
        # still-independently-claimable cards. Recorded for test drops too,
        # so a test accurately previews the real one-per-drop behavior.
        drop.claimed_by.add(member.id)

        conf = self.config.guild(guild)
        sell_prices = await conf.sell_prices()

        if drop.is_test:
            state = await self._member_state(member)
            outcome = engine.claim_outcome(state.collection, card.card_id, MAX_COPIES_KEPT)
            price = engine.sell_price(card.rarity, sell_prices) if outcome == "sell_token" else None
            embed = embeds.claim_result_embed(card, member, outcome, price=price, is_test=True)
        else:
            state = await self._member_state(member)
            # up to MAX_COPIES_KEPT copies (1 original + 1 tradeable spare)
            # stay as real collection entries; anything beyond that converts
            # straight to a sell token instead of piling up more duplicates
            outcome = engine.claim_outcome(state.collection, card.card_id, MAX_COPIES_KEPT)
            price = None
            if outcome == "sell_token":
                token = engine.make_sell_token(card.card_id, card.rarity)
                state.sell_tokens.append(token)
                price = engine.sell_price(card.rarity, sell_prices)
            else:
                state.collection.append(card.card_id)
            if claim_quota > 0:
                # every real claim counts against the quota, sell-token
                # conversions included, same as the genre convention this
                # mirrors (Mudae/Karuta) -- it's rate-limiting claim
                # *attempts* against the pool, not just new pickups
                state.daily_claims, state.daily_claims_date = engine.record_claim(
                    state.daily_claims, state.daily_claims_date, today
                )
            await self._save_member_state(member, state)

            claim_cooldown = await conf.claim_cooldown_seconds()
            self.claim_cooldown_until[member.id] = time.monotonic() + claim_cooldown

            embed = embeds.claim_result_embed(card, member, outcome, price=price, is_test=False)

        await channel.send(embed=embed)

        if len(drop.claimed_positions) >= len(drop.cards):
            self._forget_drop(message_id)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.group(name="card", aliases=["cards"], invoke_without_command=True)
    @commands.guild_only()
    async def card(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """View your card collection, or another member's with `.card @member`."""
        target = member or ctx.author
        state = await self._member_state(target)
        if not state.collection:
            if target.id == ctx.author.id:
                await ctx.send("You haven't claimed any cards yet.")
            else:
                await ctx.send(f"{target.display_name} hasn't claimed any cards yet.")
            return

        # one gallery tile per unique card_id -- a member holding a
        # tradeable spare (constants.MAX_COPIES_KEPT) gets a quantity badge
        # on that tile instead of a second, identical-looking tile
        quantities = Counter(state.collection)
        unique_ids = list(dict.fromkeys(state.collection))

        entries = []
        for card_id in unique_ids:
            card = await self._card_by_id(ctx.guild, card_id)
            image_bytes = self._read_card_image(ctx.guild, card_id)
            if card is None or image_bytes is None:
                continue
            entries.append((card, image_bytes))
        if not entries:
            await ctx.send("Those cards exist but their art is missing -- ask an admin to check the pool.")
            return

        gallery = imagegen.render_gallery(
            entries, showcase_card_ids=state.showcase_card_ids, quantities=quantities
        )
        content = None if target.id == ctx.author.id else f"{target.display_name}'s collection:"
        await ctx.send(content=content, file=discord.File(gallery, filename="collection.png"))

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
        lines = []
        for card_id in state.showcase_card_ids:
            card = await self._card_by_id(ctx.guild, card_id)
            lines.append(f"`{card_id}` — {card.name}" if card is not None else f"`{card_id}` — (unknown)")
        text = f"{whose} showcase ({len(lines)}/{MAX_SHOWCASE_SLOTS}):\n" + "\n".join(lines)
        if tier_line:
            text += f"\n\n{tier_line}"
        await ctx.send(text)

    @card_showcase.command(name="add")
    @commands.guild_only()
    async def card_showcase_add(self, ctx: commands.Context, *, card_arg: str):
        """Add a card you own to your showcase (shown larger, up top, in your gallery)."""
        card = await self._resolve_card_arg(ctx.guild, card_arg)
        if card is None:
            await ctx.send("No card matches that.")
            return
        state = await self._member_state(ctx.author)
        if not state.owns(card.card_id):
            await ctx.send("You don't own that card.")
            return
        if card.card_id in state.showcase_card_ids:
            await ctx.send(f"**{card.name}** is already in your showcase.")
            return
        if len(state.showcase_card_ids) >= MAX_SHOWCASE_SLOTS:
            await ctx.send(
                f"Your showcase is full ({MAX_SHOWCASE_SLOTS} slots) -- remove one first with "
                f"`.card showcase remove <card>`."
            )
            return
        state.showcase_card_ids.append(card.card_id)
        await self._save_member_state(ctx.author, state)
        await ctx.send(f"Added **{card.name}** to your showcase ({len(state.showcase_card_ids)}/{MAX_SHOWCASE_SLOTS}).")

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
        state.showcase_card_ids.remove(card.card_id)
        await self._save_member_state(ctx.author, state)
        await ctx.send(f"Removed **{card.name}** from your showcase.")

    @card.command(name="info")
    @commands.guild_only()
    async def card_info(self, ctx: commands.Context, *, card_arg: str):
        """Look up a character: art, rarity, favourites, and who currently owns it."""
        card = await self._resolve_card_arg(ctx.guild, card_arg)
        if card is None:
            await ctx.send("No card matches that name or ID.")
            return

        all_members = await self.config.all_members(ctx.guild)
        owner_ids = [mid for mid, data in all_members.items() if card.card_id in data.get("collection", [])]
        owner_mentions = []
        for member_id in owner_ids[:10]:
            member = ctx.guild.get_member(member_id)
            owner_mentions.append(member.mention if member is not None else f"<@{member_id}>")

        embed = embeds.card_info_embed(card, len(owner_ids), owner_mentions)
        image_bytes = self._read_card_image(ctx.guild, card.card_id)
        if image_bytes is not None:
            file = discord.File(io.BytesIO(image_bytes), filename="card_info.png")
            embed.set_image(url="attachment://card_info.png")
            await ctx.send(embed=embed, file=file)
        else:
            await ctx.send(embed=embed)

    @card.command(name="quota")
    @commands.guild_only()
    async def card_quota(self, ctx: commands.Context):
        """Check your remaining daily claim quota."""
        quota = await self.config.guild(ctx.guild).claim_quota()
        if quota <= 0:
            await ctx.send("No daily claim limit is set on this server.")
            return
        state = await self._member_state(ctx.author)
        today = engine.today_str(ACTIVITY_TIMEZONE)
        used = state.daily_claims if state.daily_claims_date == today else 0
        await ctx.send(f"You've claimed {used}/{quota} cards today. Resets at midnight Pacific.")

    @card.command(name="give")
    @commands.guild_only()
    async def card_give(self, ctx: commands.Context, member: discord.Member, *, card_arg: str):
        """Give one of your cards to another member. One-way, no confirmation.
        If you hold a tradeable spare (see MAX_COPIES_KEPT), giving it away
        doesn't touch your showcase as long as you still keep one copy."""
        if member.id == ctx.author.id:
            await ctx.send("You already own that one.")
            return
        card = await self._resolve_card_arg(ctx.guild, card_arg)
        if card is None:
            await ctx.send("No card matches that.")
            return

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

        # same MAX_COPIES_KEPT cap a claim respects: a gift that would push
        # the receiver past it converts straight to a sell token instead of
        # silently discarding it or piling up a 3rd+ copy
        outcome = engine.claim_outcome(receiver_state.collection, card.card_id, MAX_COPIES_KEPT)
        if outcome == "sell_token":
            token = engine.make_sell_token(card.card_id, card.rarity)
            receiver_state.sell_tokens.append(token)
            await self._save_member_state(member, receiver_state)
            await ctx.send(
                f"{member.mention} is already at their duplicate limit for **{card.name}** — "
                f"it was converted to a sell token for them instead."
            )
        else:
            receiver_state.collection.append(card.card_id)
            await self._save_member_state(member, receiver_state)
            await ctx.send(embed=embeds.give_result_embed(card, ctx.author, member))

    @card.command(name="sell")
    @commands.guild_only()
    async def card_sell(self, ctx: commands.Context, token_id: str):
        """Sell a duplicate token for wondercoin."""
        state = await self._member_state(ctx.author)
        token = next((t for t in state.sell_tokens if t.token_id == token_id), None)
        if token is None:
            await ctx.send("No sell token with that ID. Check `.card tokens`.")
            return
        sell_prices = await self.config.guild(ctx.guild).sell_prices()
        price = engine.sell_price(token.rarity, sell_prices)

        state.sell_tokens.remove(token)
        await self._save_member_state(ctx.author, state)
        await bank.deposit_credits(ctx.author, price)

        await ctx.send(embed=embeds.sell_result_embed(token, price, ctx.author))

    @card.command(name="tokens")
    @commands.guild_only()
    async def card_tokens(self, ctx: commands.Context):
        """List your pending sell tokens."""
        state = await self._member_state(ctx.author)
        if not state.sell_tokens:
            await ctx.send("No sell tokens on hand.")
            return
        lines = [f"`{t.token_id}` — {t.rarity} (from card #{t.card_id})" for t in state.sell_tokens]
        await ctx.send("\n".join(lines))

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
        """Show current cardcollect configuration."""
        guild_config = await self.config.guild(ctx.guild).all()
        await ctx.send(embed=embeds.settings_embed(guild_config, ctx.guild.name))

    # -- admin commands --------------------------------------------------

    @card.command(name="setchannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel drops post to."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"Drops will post in {channel.mention}.")

    @card.group(name="set", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_set(self, ctx: commands.Context):
        await ctx.send_help()

    @card_set.command(name="decoys")
    async def card_set_decoys(self, ctx: commands.Context, enabled: bool):
        await self.config.guild(ctx.guild).decoys_enabled.set(enabled)
        await ctx.send(f"Decoy reactions {'enabled' if enabled else 'disabled'}.")

    @card_set.command(name="claimquota")
    async def card_set_claimquota(self, ctx: commands.Context, quota: int):
        """Max real claims per member per day (resets at midnight Pacific). 0 = unlimited."""
        if quota < 0:
            await ctx.send("Quota can't be negative -- use 0 for unlimited.")
            return
        await self.config.guild(ctx.guild).claim_quota.set(quota)
        await ctx.send(f"Daily claim quota set to {quota if quota > 0 else 'unlimited'}.")

    @card_set.command(name="decoycount")
    async def card_set_decoycount(self, ctx: commands.Context, count: int):
        """How many decoy reactions get added alongside the real ones per drop."""
        if count < 0:
            await ctx.send("Decoy count can't be negative.")
            return
        await self.config.guild(ctx.guild).decoy_count.set(count)
        await ctx.send(f"Decoy count set to {count}.")

    @card_set.command(name="wrongguesspenalty")
    async def card_set_wrongguesspenalty(self, ctx: commands.Context, seconds: int):
        """How long a wrong (decoy) reaction guess locks a member out of *winning* any drop. 0 = no penalty."""
        if seconds < 0:
            await ctx.send("Penalty can't be negative -- use 0 to disable it.")
            return
        await self.config.guild(ctx.guild).wrong_guess_penalty_seconds.set(seconds)
        await ctx.send(f"Wrong-guess penalty set to {seconds} second(s).")

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

    @card.command(name="importpool")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_importpool(self, ctx: commands.Context, count: int = 30):
        """Bulk-import `count` female AniList characters, spread across rarity
        tiers to match drop_weights (not just the top `count` by favourites --
        see import_characters.tier_quotas for why that matters)."""
        if count < 1 or count > 200:
            await ctx.send("Pick a count between 1 and 200.")
            return

        # AniList rate-limits aggressively -- a second importpool started
        # while one is already running would multiply the request rate
        # against the same limit and trip 429s that neither run alone would
        # hit (see the comment on self._importpool_lock). Queue rather than
        # run concurrently, and say so, so a second run doesn't just look
        # like it's doing nothing.
        if self._importpool_lock.locked():
            await ctx.send("Another `.card importpool` is already running -- this one will start once it finishes.")

        async with self._importpool_lock:
            await ctx.send(f"Fetching {count} female characters from AniList, spread across rarity tiers…")
            tier_cutoffs = await self.config.guild(ctx.guild).tier_cutoffs()
            drop_weights = await self.config.guild(ctx.guild).drop_weights()
            # skip characters already in the pool (by AniList id) so
            # re-running importpool doesn't add the same character twice
            # under a new local card_id -- hand-added .card addcard entries
            # have no anilist_id and are naturally never matched here
            existing_anilist_ids = {
                c.anilist_id for c in await self._pool_cards(ctx.guild) if c.anilist_id is not None
            }

            try:
                raw_entries = await import_characters.fetch_top_female_characters(
                    self._session,
                    tier_cutoffs,
                    engine.bucket_tier,
                    count,
                    weights=drop_weights,
                    exclude_ids=existing_anilist_ids,
                )
            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    await ctx.send(
                        "AniList kept rate-limiting this import even after retrying with backoff -- "
                        "try again in a few minutes, and avoid running `.card importpool` more than "
                        "once at a time."
                    )
                else:
                    await ctx.send(f"AniList request failed: {e}")
                return
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                await ctx.send(f"AniList request failed: {e}")
                return

            if not raw_entries:
                await ctx.send("No characters came back -- AniList may be unreachable, or the filter matched nothing.")
                return

            # Download every image *before* touching Config, not inside the
            # "async with ... .all()" write below. That block used to wrap
            # the whole network loop (up to 200 image downloads, potentially
            # tens of seconds), holding a mutable handle on the guild's
            # entire config open the whole time -- any other command writing
            # the same guild's config during that window (e.g. a concurrent
            # .card addcard) would have its change silently overwritten when
            # this one finally closes and writes back. Fetching first and
            # writing once, quickly, afterward closes that window.
            downloaded = []
            for entry in raw_entries:
                try:
                    async with self._session.get(entry["image_url"]) as resp:
                        resp.raise_for_status()
                        image_bytes = await resp.read()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue
                downloaded.append((entry, image_bytes))

            added = 0
            async with self.config.guild(ctx.guild).all() as guild_data:
                for entry, image_bytes in downloaded:
                    card_id = guild_data["next_id"]
                    guild_data["next_id"] += 1
                    storage.save_card_image(self.data_path, ctx.guild.id, card_id, image_bytes)
                    guild_data["pool"][str(card_id)] = Card(
                        card_id=card_id,
                        name=entry["name"],
                        series=entry["series"],
                        rarity=entry["rarity"],
                        image_path=str(storage.card_image_path(self.data_path, ctx.guild.id, card_id)),
                        favourites=entry["favourites"],
                        added_by=None,
                        anilist_id=entry.get("anilist_id"),
                    ).to_dict()
                    added += 1

            await ctx.send(
                f"Imported {added} characters (characters already in the pool were skipped automatically). "
                f"Prune unwanted ones with `.card removecard <id>`."
            )

    @card.command(name="diagnostics")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def card_diagnostics(self, ctx: commands.Context):
        """Activity-based sizing report for drop_chance/drop_cooldown_seconds."""
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

        embed = embeds.diagnostics_embed(buckets, suggested_chance, suggested_cooldown, sampling_since, ctx.guild.name)

        payload = {
            "hourly_buckets": buckets,
            "sampling_since": sampling_since,
            "suggested_drop_chance": suggested_chance,
            "suggested_drop_cooldown_seconds": suggested_cooldown,
            "timezone": ACTIVITY_TIMEZONE,
        }
        file_obj = io.BytesIO(json.dumps(payload, indent=2).encode("utf-8"))
        await ctx.send(embed=embed, file=discord.File(file_obj, filename="cardcollect_diagnostics.json"))

    @card.command(name="testmode")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_testmode(self, ctx: commands.Context, enabled: bool):
        """Toggle test mode: all drops (ambient and forced) run the full
        claim flow but award nothing."""
        await self.config.guild(ctx.guild).test_mode.set(enabled)
        if enabled:
            await ctx.send("Test mode ON — every drop from now on is a test drop. Nothing will be awarded.")
        else:
            await ctx.send("Test mode off — drops are live again.")

    @card.command(name="testdrop")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_testdrop(self, ctx: commands.Context):
        """Force an immediate test drop, bypassing chance/cooldown. Always a
        test drop regardless of the testmode setting, so it's safe to spam
        while tuning without needing to flip testmode on first."""
        channel_id = await self.config.guild(ctx.guild).channel_id()
        if channel_id is None:
            await ctx.send("Set a drop channel first with `.card setchannel`.")
            return
        channel = ctx.guild.get_channel(channel_id)
        if channel is None:
            await ctx.send("Configured drop channel no longer exists.")
            return
        reason = await self._post_drop(channel, ctx.guild, is_test=True)
        if reason is not None:
            await ctx.send(reason)
            return
        if channel.id != ctx.channel.id:
            await ctx.send(f"Test drop posted in {channel.mention}.")
