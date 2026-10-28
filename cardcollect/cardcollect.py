"""Main cog: message listener (chance-per-message drop trigger), the claim
flow, and all `.card` commands.

Game logic lives in engine.py / captcha.py (pure, unit-tested standalone);
this file is the Discord-facing wiring around it -- Config I/O, the message
listener, image compositing calls into imagegen.py, and command handlers. The
button/pop-up UI itself lives in views.py.

Claiming: every card in a drop has its own short CAPTCHA code burned into the
art, and the drop message carries a Claim button. Pressing it opens a pop-up
where the member types a code; the first correct submission claims that card
-- see submit_code. Nothing is typed into chat, so it never mixes with the
channel's conversation. (This replaced an emoji-reaction mechanic whose
reactions sometimes never registered and occasionally got rate-limited.)
"""

import asyncio
import functools
import io
import json
import logging
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

from . import captcha, embeds, engine, imagegen, import_characters, storage, views
from . import panel as panel_specs
from .settings_panel import PanelError, open_panel
from .constants import (
    ACTIVITY_TIMEZONE,
    COG_VERSION,
    DEFAULT_CLAIM_COOLDOWN_SECONDS,
    DAILY_STREAK_BONUS_EVERY,
    DEFAULT_CLAIM_QUOTA,
    DEFAULT_DAILY_WEIGHTS,
    DEFAULT_DROP_CHANCE,
    DEFAULT_DROP_COOLDOWN_SECONDS,
    DEFAULT_DROP_EXPIRY_SECONDS,
    DEFAULT_DROP_SIZE,
    DEFAULT_DROP_WEIGHTS,
    DEFAULT_MAX_WRONG_GUESSES,
    DEFAULT_SELL_PRICES,
    DEFAULT_TIER_CUTOFFS,
    DEFAULT_WRONG_GUESS_PENALTY_SECONDS,
    GALLERY_PAGE_SIZE,
    IMPORT_GENDERS,
    MAX_COPIES_KEPT,
    MAX_SHOWCASE_SLOTS,
    SERIES_IMPORT_DEFAULT,
    SERIES_IMPORT_MAX,
    SHEET_COLUMNS,
    TIERS,
    ART_MAX_UPLOAD_BYTES,
)
from .models import ActiveDrop, Card, MemberState, SeriesPreview

log = logging.getLogger("red.evac-cogs.cardcollect")

MSG_OVER = "This drop is over — it was fully claimed or has expired."
MSG_ALREADY = "You've already claimed a card from this drop — one per person."
MSG_LOCKED = "You're locked out of this drop (too many wrong codes)."
MSG_TAKEN = "Too slow — that card has already been claimed. Try another card's code."
MSG_MALFORMED = (
    "That doesn't look like a code — they're 5 characters: letters, numbers and one symbol "
    "(like `K3+9T`). Check the card and try again; this didn't cost you a guess."
)
MSG_ERROR = "Something went wrong saving that claim — the card is still open, so try again."


def _secs(seconds: float) -> int:
    return max(1, int(seconds + 0.999))


def _cooldown_text(left: float) -> str:
    return f"⏳ You're on claim cooldown for another {_secs(left)}s."


def _penalty_text(left: float) -> str:
    return f"\U0001f6ab You're locked out from guessing for another {_secs(left)}s."


def _lockout_text(penalty_seconds: float, is_test: bool) -> str:
    if is_test:
        return "❌ You're out of chances and locked out of this drop. (Test mode, so no real penalty this time.)"
    if penalty_seconds > 0:
        return (
            f"❌ You're out of chances and locked out from guessing for {int(penalty_seconds)}s. "
            "Now watch everyone else collect the cards in front of you, cuck! \U0001f921"
        )
    return "❌ You're out of chances and locked out of this drop."


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
    "claim_cooldown_seconds": DEFAULT_CLAIM_COOLDOWN_SECONDS,
    "max_wrong_guesses": DEFAULT_MAX_WRONG_GUESSES,
    "wrong_guess_penalty_seconds": DEFAULT_WRONG_GUESS_PENALTY_SECONDS,
    "drop_expiry_seconds": DEFAULT_DROP_EXPIRY_SECONDS,
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
    "daily_pull_date": "",
    "daily_streak": 0,
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
        self.last_drop_time: Dict[int, float] = {}  # guild_id -> monotonic time
        self.claim_cooldown_until: Dict[int, float] = {}  # user_id -> monotonic time
        # set when a member burns all their wrong guesses on a drop -- see
        # _record_wrong_guess. Excludes a user from *winning* any drop while
        # active, checked alongside claim_cooldown_until in _claim.
        self.wrong_guess_penalty_until: Dict[int, float] = {}  # user_id -> monotonic time
        self._drop_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> lock (drop cooldown)
        # One lock per guild around the whole claim transaction (eligibility
        # checks -> Config write -> marking the card claimed). Near-
        # simultaneous correct codes are common (that's the whole game), and
        # the eligibility checks and the write span real awaits, so without
        # this two submissions for the same card -- or two from the same
        # member for different cards -- could both pass their checks before
        # either was recorded. asyncio.Lock is FIFO, so the submission the
        # listener saw first is also the one that wins.
        self._claim_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> lock
        self._persistent_view: Optional[views.ClaimView] = None
        # AniList's public API rate-limits aggressively; without this, two
        # `.card importpool` runs fired close together each start their own
        # up-to-200-page loop and multiply the request rate against the same
        # limit, tripping 429s that neither run alone would have hit. This
        # lock is process-wide (not per-guild) since it's AniList's own
        # global limit being protected, not anything guild-scoped.
        self._importpool_lock = asyncio.Lock()

        self._session: Optional[aiohttp.ClientSession] = None

        # Activity counts are buffered here and merged into Config every
        # ACTIVITY_FLUSH_SECONDS. Writing per message rewrote this cog's whole
        # settings file (pool + every member) on every chat message in the
        # drop channel. A crash loses at most one interval of diagnostics counts.
        self._activity_pending: Dict[int, Dict[str, int]] = {}  # guild_id -> {hour: count}
        self._activity_since: Dict[int, float] = {}  # guild_id -> first-buffered time
        self._activity_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        # One shared, persistent Claim button handler: a click on a drop that
        # predates a restart still lands here, and submit_code/
        # claim_button_refusal answer "this drop is over" instead of Discord
        # showing "This interaction failed".
        self._persistent_view = views.ClaimView(self)
        self.bot.add_view(self._persistent_view)

        # a hung AniList request or a slow image host would otherwise leave
        # .card importpool waiting forever -- give every request on this
        # session a ceiling
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self._activity_task = asyncio.create_task(self._activity_flush_loop())

    async def cog_unload(self):
        if self._activity_task is not None:
            self._activity_task.cancel()
        try:
            await self._flush_activity()
        except Exception:
            log.exception("cardcollect: final activity flush failed")
        if self._persistent_view is not None:
            self._persistent_view.stop()
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
        """Drop a fully resolved (or expired) drop's in-memory state --
        without this, self.active_drops would grow by one entry per drop
        ever posted, forever."""
        self.active_drops.pop(message_id, None)

    def _prune_expired_drops(self):
        now = time.monotonic()
        for message_id, drop in list(self.active_drops.items()):
            if drop.is_expired(now):
                self._forget_drop(message_id)

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
            return "The card pool is empty. Add characters first with `.card addcard` or `.card importpool`."

        weights = await conf.drop_weights()
        drop_size = await conf.drop_size()
        max_wrong_guesses = await conf.max_wrong_guesses()
        wrong_guess_penalty = await conf.wrong_guess_penalty_seconds()
        expiry = await conf.drop_expiry_seconds()

        self._prune_expired_drops()

        drop = engine.build_drop(
            pool_cards,
            weights,
            drop_size,
            guild.id,
            channel.id,
            is_test=is_test,
        )
        if drop is None:
            return "Couldn't roll a drop from the current pool (no cards available in any rarity tier)."

        entries = []
        kept_cards = []
        for card_entry in drop.cards:
            card = await self._card_by_id(guild, card_entry["card_id"])
            image_bytes = self._read_card_image(guild, card_entry["card_id"])
            if card is None or image_bytes is None:
                continue
            entries.append((card, image_bytes, card_entry["code"]))
            kept_cards.append(card_entry)
        if not entries:
            return (
                "Rolled cards but couldn't find their art on disk -- the pool may be "
                "out of sync with the image files. Check `.card settings` and the data folder."
            )
        # a card whose art couldn't be rendered has no visible code, so it can
        # never be claimed -- leaving it in drop.cards would keep the drop
        # from ever counting as fully claimed
        drop.cards = kept_cards

        # Pillow work off the event loop: a 3-card composite is tens of ms of
        # CPU, which is long enough to delay gateway heartbeats if done inline
        composite = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(imagegen.render_drop, entries, is_test=is_test)
        )
        if is_test:
            content = "\U0001f9ea **Test drop** — claims here won't award anything. Press **Claim a card** and enter a code to try it."
        else:
            content = "\U0001f524 **Press Claim a card and enter the code shown on a card!** First correct code wins — one card per person."
        sent = await channel.send(
            content=content, file=discord.File(composite, filename="drop.png"), view=views.ClaimView(self)
        )

        # registered synchronously right after the send returns, before any
        # further await, so a press of the button the instant the drop appears
        # can never arrive before the drop is known
        drop.message_id = sent.id
        drop.max_wrong_guesses = max_wrong_guesses
        drop.wrong_guess_penalty_seconds = wrong_guess_penalty
        drop.expires_at = time.monotonic() + expiry if expiry > 0 else None
        self.active_drops[sent.id] = drop
        return None

    # ------------------------------------------------------------------
    # claim flow (Claim button -> code pop-up -> submit_code)
    # ------------------------------------------------------------------

    def claim_button_refusal(self, member: discord.abc.User, message_id: int) -> Optional[str]:
        """Why this member can't open the code pop-up for this drop right now
        (a private message for them), or None if they can. Purely a courtesy
        so they aren't asked to type a code that can't work -- every one of
        these is re-checked, authoritatively, inside submit_code/_claim."""
        self._prune_expired_drops()
        drop = self.active_drops.get(message_id)
        if drop is None:
            return MSG_OVER
        if member.id in drop.claimed_by:
            return MSG_ALREADY
        if drop.is_locked_out(member.id):
            return MSG_LOCKED
        if not drop.is_test:
            now = time.monotonic()
            cooldown_left = self.claim_cooldown_until.get(member.id, 0.0) - now
            if cooldown_left > 0:
                return _cooldown_text(cooldown_left)
            penalty_left = self.wrong_guess_penalty_until.get(member.id, 0.0) - now
            if penalty_left > 0:
                return _penalty_text(penalty_left)
        return None

    def _record_wrong_guess(self, member: discord.abc.User, drop: ActiveDrop) -> str:
        """Charge a wrong (but code-shaped) guess and build the private
        reply. Fully synchronous. Reaching the cap locks the member out of
        this drop and, on a real drop, from winning any drop for a while."""
        if drop.max_wrong_guesses <= 0:
            return "❌ Wrong code."
        count = drop.wrong_guesses.get(member.id, 0) + 1
        drop.wrong_guesses[member.id] = count
        left = drop.max_wrong_guesses - count
        if left > 0:
            return f"❌ Wrong code. {left} guess{'es' if left != 1 else ''} left on this drop."

        penalty = 0.0
        if not drop.is_test:
            # test drops still enforce the guess cap (so `.card testdrop`
            # previews the real interaction accurately) but never set the
            # penalty itself -- same test-mode exemption the claim cooldown
            # and daily quota get in _claim, so testing can't lock an admin
            # out of winning real drops
            penalty = drop.wrong_guess_penalty_seconds
            if penalty > 0:
                self.wrong_guess_penalty_until[member.id] = time.monotonic() + penalty
        return _lockout_text(penalty, drop.is_test)

    async def submit_code(
        self, member: discord.abc.User, channel: discord.abc.Messageable, drop_message_id: int, raw_code: str
    ) -> str:
        """Handle one code submitted from the pop-up. Returns the private
        (ephemeral) reply for the member; a successful claim also posts the
        public result embed.

        Everything before _claim is synchronous on purpose -- see
        CodeModal.handle_submit: who wins a near-tie is decided by who
        reaches the claim lock first, so nothing may await ahead of it."""
        self._prune_expired_drops()
        drop = self.active_drops.get(drop_message_id)
        if drop is None:
            return MSG_OVER
        if member.id in drop.claimed_by:
            return MSG_ALREADY
        if drop.is_locked_out(member.id):
            return MSG_LOCKED

        code = captcha.normalize(raw_code)
        entry = drop.entry_for_code(code)
        if entry is None:
            if not captcha.looks_like_code(code):
                # almost certainly a typo, not a guess -- hint, don't charge
                return MSG_MALFORMED
            return self._record_wrong_guess(member, drop)
        if entry["position"] in drop.claimed_positions:
            return MSG_TAKEN  # right code, but someone was faster -- not the member's fault
        return await self._claim(member, channel, drop, entry)

    async def _post_to_channel(self, channel: discord.abc.Messageable, **kwargs):
        """Best-effort public post: a failed announcement must never undo or
        crash a claim that has already been saved."""
        try:
            await channel.send(**kwargs)
        except discord.HTTPException:
            log.exception("cardcollect: failed to post to the drop channel")

    async def _claim(
        self, member: discord.abc.User, channel: discord.abc.Messageable, drop: ActiveDrop, entry: dict
    ) -> str:
        guild = channel.guild
        position = entry["position"]

        async with self._get_claim_lock(guild.id):
            # Re-check everything now that we hold the lock: a claim queued
            # ahead of this one may have taken the card, used up this
            # member's one card, or the drop may have expired meanwhile.
            if self.active_drops.get(drop.message_id) is not drop or drop.is_expired(time.monotonic()):
                return MSG_OVER
            if member.id in drop.claimed_by:
                return MSG_ALREADY
            if drop.is_locked_out(member.id):
                return MSG_LOCKED
            if position in drop.claimed_positions:
                return MSG_TAKEN

            now = time.monotonic()
            if not drop.is_test:
                cooldown_left = self.claim_cooldown_until.get(member.id, 0.0) - now
                if cooldown_left > 0:
                    return _cooldown_text(cooldown_left)
                penalty_left = self.wrong_guess_penalty_until.get(member.id, 0.0) - now
                if penalty_left > 0:
                    return _penalty_text(penalty_left)

            conf = self.config.guild(guild)
            today = engine.today_str(ACTIVITY_TIMEZONE)
            claim_quota = 0 if drop.is_test else await conf.claim_quota()
            state = await self._member_state(member)
            if claim_quota > 0 and not engine.has_quota_remaining(
                state.daily_claims, state.daily_claims_date, claim_quota, today
            ):
                return f"You've used all {claim_quota} of today's claims. It resets at midnight Pacific."

            card = await self._card_by_id(guild, entry["card_id"])
            if card is None:
                return MSG_ERROR

            sell_prices = await conf.sell_prices()
            # up to MAX_COPIES_KEPT copies (1 original + 1 tradeable spare)
            # stay as real collection entries; anything beyond that converts
            # straight to a sell token instead of piling up more duplicates
            outcome = engine.claim_outcome(state.collection, card.card_id, MAX_COPIES_KEPT)
            price = engine.sell_price(card.rarity, sell_prices) if outcome == "sell_token" else None

            claim_cooldown = 0
            if not drop.is_test:
                if outcome == "sell_token":
                    state.sell_tokens.append(engine.make_sell_token(card.card_id, card.rarity))
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
                claim_cooldown = await conf.claim_cooldown_seconds()
                try:
                    await self._save_member_state(member, state)
                except Exception:
                    # Nothing has been marked yet, so the card is still open
                    # for this member to retry or anyone else to take -- a
                    # failed write must never silently eat a card.
                    log.exception("cardcollect: failed to save claim for member %s", member.id)
                    return MSG_ERROR

            # committed -- mark it, in-memory, with no await since the checks
            # above other than the write itself (and the lock still held)
            drop.claimed_positions.add(position)
            drop.claimed_by.add(member.id)
            if not drop.is_test:
                self.claim_cooldown_until[member.id] = time.monotonic() + claim_cooldown
            all_claimed = len(drop.claimed_positions) >= len(drop.cards)
            if all_claimed:
                self._forget_drop(drop.message_id)

            embed = embeds.claim_result_embed(card, member, outcome, price=price, is_test=drop.is_test)
            # Started inside the lock so announcements go out in claim order,
            # but awaited outside it: posting must not hold up the next claim.
            announcement = asyncio.ensure_future(self._post_to_channel(channel, embed=embed))
            drop.announcements.append(announcement)

        await announcement
        if all_claimed:
            # Wait for every claim's embed -- not just this one's -- so this
            # can never land ahead of an earlier claim's announcement that
            # was still in flight. Exactly one claim sees all_claimed (the
            # lock serializes them), so it's posted exactly once.
            await asyncio.gather(*drop.announcements)
            await self._post_to_channel(
                channel,
                content=(
                    "\U0001f9ea Test drop finished — all of its cards were claimed."
                    if drop.is_test
                    else "\U0001f389 All cards from this drop have been claimed!"
                ),
            )

        reply = f"✅ You claimed **{card.name}**!"
        if drop.is_test:
            reply += " (Test drop — nothing was awarded.)"
        return reply

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.group(name="card", aliases=["cards"], invoke_without_command=True)
    @commands.guild_only()
    async def card(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """View your card collection (legendary first, then epic, rare, common, A-Z within each), or another member's with `.card @member`."""
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
        # rarest first, then A-Z within each rarity; sorted before paging so
        # page 1 always holds the legendaries
        entries.sort(key=lambda e: engine.gallery_sort_key(e[0]))

        pages = [entries[i : i + GALLERY_PAGE_SIZE] for i in range(0, len(entries), GALLERY_PAGE_SIZE)]

        if len(pages) == 1:
            # common case: whole collection fits on one page -- no point
            # showing paging buttons that would both be permanently disabled
            gallery = await asyncio.to_thread(
                imagegen.render_gallery,
                pages[0],
                showcase_card_ids=state.showcase_card_ids,
                quantities=quantities,
            )
            content = None if target.id == ctx.author.id else f"{target.display_name}'s collection:"
            await ctx.send(content=content, file=discord.File(gallery, filename="collection.png"))
            return

        whose = "Your" if target.id == ctx.author.id else f"{target.display_name}'s"
        view = views.GalleryView(ctx.author.id, pages, entries, state.showcase_card_ids, quantities, whose)
        gallery, content = await view.render_current_async()
        msg = await ctx.send(content=content, file=discord.File(gallery, filename="collection.png"), view=view)
        view.message = msg

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
        # admins get a "Change rarity" dropdown under the card; everyone else a plain embed
        admin_view = None
        if await self._is_pool_admin(ctx.author):
            admin_view = views.CardAdminView(
                self, ctx.author.id, card, len(owner_ids), owner_mentions, image_bytes is not None
            )
        kwargs = {"view": admin_view} if admin_view is not None else {}
        if image_bytes is not None:
            file = discord.File(io.BytesIO(image_bytes), filename="card_info.png")
            embed.set_image(url="attachment://card_info.png")
            sent = await ctx.send(embed=embed, file=file, **kwargs)
        else:
            sent = await ctx.send(embed=embed, **kwargs)
        if admin_view is not None:
            admin_view.message = sent

    async def _owned_cards(self, member: discord.Member) -> Tuple[MemberState, List[Card]]:
        """(state, one Card per unique pool card the member owns). Cards that
        have since vanished from the pool are skipped."""
        state = await self._member_state(member)
        owned_ids = set(state.collection)
        pool_cards = await self._pool_cards(member.guild)
        return state, [c for c in pool_cards if c.card_id in owned_ids]

    @staticmethod
    def _card_line(card: Card) -> str:
        series = f" ({card.series})" if card.series else ""
        text = f"{card.name}{series}".replace("`", "'")
        if len(text) > 100:  # keep a 15-line list safely under Discord's 2000-char message cap
            text = text[:99] + "…"
        return f"`{card.card_id}` {card.rarity} — {text}"

    @card.command(name="show")
    @commands.guild_only()
    async def card_show(self, ctx: commands.Context, *, card_arg: str):
        """Show one of YOUR cards at full size, by ID or by any part of the
        name or series. If several of your cards match, they're listed with
        their IDs so you can pick one. Only cards you own."""
        state, owned = await self._owned_cards(ctx.author)
        arg = card_arg.strip().lstrip("#")

        card = None
        if arg.isdigit():
            card = next((c for c in owned if c.card_id == int(arg)), None)
        if card is None:
            matches = engine.search_cards(owned, arg)
            if len(matches) == 1:
                card = matches[0]
            elif not matches:
                await ctx.send("You don't own a card matching that. (`.card` shows your collection.)")
                return
            else:
                shown = matches[:15]
                lines = "\n".join(self._card_line(c) for c in shown)
                more = f"\n…and {len(matches) - len(shown)} more." if len(matches) > len(shown) else ""
                await ctx.send(
                    f"{len(matches)} of your cards match — use the ID with `.card show <id>`, "
                    f"or `.card find {arg}` to see them all:\n{lines}{more}"
                )
                return

        image_bytes = self._read_card_image(ctx.guild, card.card_id)
        if image_bytes is None:
            await ctx.send(f"**{card.name}** is yours, but its art is missing — ask an admin to check the pool.")
            return
        quantity = state.collection.count(card.card_id)
        rendered = await asyncio.to_thread(imagegen.render_single, card, image_bytes, quantity)
        series = f" — {card.series}" if card.series else ""
        spare = f" (you hold {quantity})" if quantity > 1 else ""
        await ctx.send(
            content=f"**{card.name}**{series} · {card.rarity} · `#{card.card_id}`{spare}",
            file=discord.File(rendered, filename="card.png"),
        )

    @card.command(name="find", aliases=["search"])
    @commands.guild_only()
    async def card_find(self, ctx: commands.Context, *, query: str):
        """Search YOUR cards by character name or anime/series (partial
        matches work: `.card find lucy`, `.card find edgerunners`). Only
        cards you own."""
        state, owned = await self._owned_cards(ctx.author)
        matches = engine.search_cards(owned, query)
        if not matches:
            await ctx.send(
                f"None of your cards match **{query.strip()}**. "
                f"(`.card pool {query.strip()}` searches every card in the pool.)"
            )
            return

        shown = matches[:GALLERY_PAGE_SIZE]
        entries = []
        for card in shown:
            image_bytes = self._read_card_image(ctx.guild, card.card_id)
            if image_bytes is not None:
                entries.append((card, image_bytes))

        caption = f"Your cards matching **{query.strip()}** ({len(matches)}):\n" + "\n".join(
            self._card_line(c) for c in shown
        )
        if len(matches) > len(shown):
            caption += f"\n…and {len(matches) - len(shown)} more — narrow the search to see them."
        if not entries:
            await ctx.send(caption)
            return
        quantities = Counter(state.collection)
        gallery = await asyncio.to_thread(imagegen.render_gallery, entries, quantities=quantities)
        await ctx.send(content=caption[:1990], file=discord.File(gallery, filename="matches.png"))

    async def _is_pool_admin(self, member: discord.Member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.manage_guild or perms.administrator):
            return True
        is_admin = getattr(self.bot, "is_admin", None)
        return bool(is_admin is not None and await is_admin(member))

    @card.command(name="pool")
    @commands.guild_only()
    async def card_pool(self, ctx: commands.Context, *, query: str):
        """Search EVERY card in the pool by character name or anime/series
        (partial matches work: `.card pool lucy`, `.card pool edgerunners`).
        Shows all matches with their art, and marks the ones you own. Retired
        cards are hidden unless you own one."""
        state = await self._member_state(ctx.author)
        owned_ids = set(state.collection)
        pool_cards = await self._pool_cards(ctx.guild)
        if not await self._is_pool_admin(ctx.author):
            pool_cards = [c for c in pool_cards if not c.retired or c.card_id in owned_ids]
        matches = [
            c
            for c in engine.search_cards(pool_cards, query)
            if storage.card_image_exists(self.data_path, ctx.guild.id, c.card_id)
        ]
        shown_query = discord.utils.escape_mentions(discord.utils.escape_markdown(query.strip()))[:60]
        if not matches:
            await ctx.send(f"No card in the pool matches **{shown_query}**.")
            return

        pages = [matches[i : i + GALLERY_PAGE_SIZE] for i in range(0, len(matches), GALLERY_PAGE_SIZE)]
        owned_count = sum(1 for c in matches if c.card_id in owned_ids)
        quantities = Counter(state.collection)

        def caption_for(page_index: int, page_cards: List[Card]) -> str:
            head = f"Pool cards matching **{shown_query}**: {len(matches)} found, you own {owned_count} (✅ = owned)"
            if len(pages) > 1:
                head += f" · page {page_index + 1}/{len(pages)}"
            lines = "\n".join(
                f"{'✅' if c.card_id in owned_ids else '⬜'} {self._card_line(c)}{' [retired]' if c.retired else ''}"
                for c in page_cards
            )
            return f"{head}\n{lines}"[:1990]

        def read_image(card_id: int) -> Optional[bytes]:
            return self._read_card_image(ctx.guild, card_id)

        view = views.PoolSearchView(ctx.author.id, pages, quantities, read_image, caption_for)
        gallery, caption = await view.render_current_async()
        if len(pages) == 1:
            await ctx.send(content=caption, file=discord.File(gallery, filename="pool.png"))
            return
        msg = await ctx.send(content=caption, file=discord.File(gallery, filename="pool.png"), view=view)
        view.message = msg

    @card.command(name="poolsearch", aliases=["searchpool"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_poolsearch(self, ctx: commands.Context, *, query: str):
        """Admin: search the whole pool by character name or series before
        adding someone (`.card poolsearch lucy` lists every Lucy; add a word
        like `edgerunners` to narrow). Partial, any-order, case-insensitive;
        retired characters are included and marked."""
        pool_cards = await self._pool_cards(ctx.guild)
        matches = engine.search_cards(pool_cards, query)
        if not matches:
            await ctx.send(
                f"No character in the pool matches **{query.strip()}** "
                f"({len(pool_cards)} searched) — safe to add."
            )
            return

        def line(c: Card) -> str:
            series = f" ({c.series})" if c.series else ""
            flag = " [retired]" if c.retired else ""
            return f"{c.card_id}\t{c.rarity}\t{c.name}{series}\tfavs={c.favourites}{flag}"

        header = f"**{len(matches)}** match{'es' if len(matches) != 1 else ''} for **{query.strip()}**"
        body = "\n".join(line(c).replace("`", "'") for c in matches)
        if len(matches) <= 15 and len(header) + len(body) < 1800:
            await ctx.send(f"{header}:\n```\n{body}\n```")
            return
        buf = io.BytesIO("\n".join(line(c) for c in matches).encode("utf-8"))
        await ctx.send(content=f"{header}. Full list attached.", file=discord.File(buf, filename="pool_search.txt"))

    @card.command(name="version")
    async def card_version(self, ctx: commands.Context):
        """Show the running cardcollect version (deploy probe)."""
        await ctx.send(f"cardcollect v{COG_VERSION}")

    @card.command(name="daily", aliases=["pull"])
    @commands.guild_only()
    async def card_daily(self, ctx: commands.Context):
        """Your free card for today. Mostly commons, sometimes rares, rarely epics,
        very rarely a legendary. Every 7th day in a row is bumped up one rarity."""
        result = await self.daily_pull(ctx.guild, ctx.author)
        if isinstance(result, str):
            await ctx.send(result)
            return
        card, outcome, price, streak, bonus = result
        lines = [f"\U0001f3b4 {ctx.author.mention} pulled **{card.name}**"
                 + (f" ({card.series})" if card.series else "") + f" · **{card.rarity}** · `#{card.card_id}`"]
        if outcome == "duplicate":
            lines.append("Spare copy: you can trade it with `.card give`.")
        elif outcome == "sell_token":
            lines.append(f"Already at your copy limit, so it became a sell token (`.card sell` for {price:,}).")
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
        (card, outcome, sell_price_or_None, streak, was_bonus_day)."""
        async with self._get_claim_lock(guild.id):  # same lock as claims: one write to a member at a time
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
            outcome = engine.claim_outcome(state.collection, card.card_id, MAX_COPIES_KEPT)
            price = None
            if outcome == "sell_token":
                state.sell_tokens.append(engine.make_sell_token(card.card_id, card.rarity))
                price = engine.sell_price(card.rarity, await conf.sell_prices())
            else:
                state.collection.append(card.card_id)
            state.daily_pull_date, state.daily_streak = today, streak
            await self._save_member_state(member, state)
        return card, outcome, price, streak, bonus

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
        """Settings panel for admins; everyone else sees the current configuration."""
        perms = getattr(ctx.author, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_guild):
            await open_panel(ctx, self, panel_specs.build_specs(), title="Card Collect", intro=panel_specs.INTRO)
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
        expiry = self._whole(values, "expiry", "Drop expiry", 30, " Drops need to stay claimable for a while.")
        wrong = self._whole(values, "wrong", "Wrong guesses", 0, " Use 0 for unlimited.")
        penalty = self._whole(values, "penalty", "Lockout penalty", 0, " Use 0 for none.")
        conf = self.config.guild(guild)
        await conf.drop_chance.set(percent / 100)
        await conf.drop_cooldown_seconds.set(cooldown)
        await conf.drop_expiry_seconds.set(expiry)
        await conf.max_wrong_guesses.set(wrong)
        await conf.wrong_guess_penalty_seconds.set(penalty)
        return (f"A qualifying message drops a card {percent:g}% of the time, at most once every {cooldown}s; "
                f"drops expire after {expiry}s. Takes effect on the next drop.")

    async def _apply_claim_quota(self, guild, member, values) -> str:
        quota = self._whole(values, "quota", "Quota", 0, " Use 0 for unlimited.")
        await self.config.guild(guild).claim_quota.set(quota)
        return f"Daily claim quota: {quota if quota > 0 else 'unlimited'} (resets at midnight Pacific)."

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

    @card.command(name="setimage")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_setimage(self, ctx: commands.Context, card_id: int):
        """Swap out a pool character's art without touching anything else
        about the card (name, series, rarity, ID, and everyone's owned
        copies all stay exactly as they were). Attach the replacement image
        to this message. Use `.card viewpool` to look up a card's ID."""
        card = await self._card_by_id(ctx.guild, card_id)
        if card is None:
            await ctx.send(f"No card with ID {card_id}. Check `.card viewpool` for the right ID.")
            return
        if not ctx.message.attachments:
            await ctx.send("Attach the replacement image to this message.")
            return

        image_bytes = await ctx.message.attachments[0].read()
        storage.save_card_image(self.data_path, ctx.guild.id, card_id, image_bytes)
        await ctx.send(embed=embeds.card_image_updated_embed(card))

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
                added.append(entry)
            for link in links or []:
                data = guild_data["pool"].get(str(link["card_id"]))
                if data is not None and data.get("anilist_id") is None and link.get("anilist_id") is not None:
                    data["anilist_id"] = link["anilist_id"]
                    if not data.get("series_alt") and link.get("series_alt"):
                        data["series_alt"] = link["series_alt"]
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

    async def wait_for_upload(self, channel_id: int, user_id: int, timeout: float):
        """The next message from `user_id` in the channel, or None if they say
        nothing for `timeout` seconds."""
        def check(message):
            return message.author.id == user_id and message.channel.id == channel_id

        try:
            return await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None

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
            await ctx.send("Set a drop channel first in `.card settings`.")
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
