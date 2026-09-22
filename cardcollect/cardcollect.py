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
    DEFAULT_CLAIM_WINDOW_SECONDS,
    DEFAULT_DECOY_COUNT,
    DEFAULT_DECOYS_ENABLED,
    DEFAULT_DROP_CHANCE,
    DEFAULT_DROP_COOLDOWN_SECONDS,
    DEFAULT_DROP_SIZE,
    DEFAULT_DROP_WEIGHTS,
    DEFAULT_SELL_PRICES,
    DEFAULT_TIER_CUTOFFS,
    EMOJI_POOL,
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
    "sell_prices": dict(DEFAULT_SELL_PRICES),
    "test_mode": False,
    "activity_tracking": {"hourly_buckets": {}, "sampling_since": 0},
}

DEFAULT_MEMBER = {
    "collection": [],
    "favorite_card_id": None,
    "sell_tokens": [],
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

        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

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

        await self._track_activity(guild)

        now = time.monotonic()
        cooldown = await conf.drop_cooldown_seconds()
        last = self.last_drop_time.get(guild.id, 0.0)
        if now - last < cooldown:
            return

        drop_chance = await conf.drop_chance()
        if not engine.should_drop(drop_chance):
            return

        self.last_drop_time[guild.id] = now
        test_mode = await conf.test_mode()
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

    async def _post_drop(self, channel: discord.abc.Messageable, guild: discord.Guild, is_test: bool):
        conf = self.config.guild(guild)
        pool_cards = await self._rollable_pool_cards(guild)
        if not pool_cards:
            return

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
            return

        entries = []
        for card_entry in drop.cards:
            card = await self._card_by_id(guild, card_entry["card_id"])
            image_bytes = self._read_card_image(guild, card_entry["card_id"])
            if card is None or image_bytes is None:
                continue
            entries.append((card, image_bytes, card_entry["emoji"]))
        if not entries:
            return

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

    # ------------------------------------------------------------------
    # claim resolution
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return

        drop = self.active_drops.get(payload.message_id)
        if drop is None:
            return

        emoji = str(payload.emoji)
        if not drop.is_real_emoji(emoji):
            return

        card_entry = drop.emoji_for(emoji)
        if card_entry["position"] in drop.claimed_positions:
            return

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
                    self.active_drops.pop(message_id, None)
                return

        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            if len(drop.claimed_positions) >= len(drop.cards):
                self.active_drops.pop(message_id, None)
            return

        reaction = discord.utils.find(lambda r: str(r.emoji) == emoji, message.reactions)
        if reaction is None:
            if len(drop.claimed_positions) >= len(drop.cards):
                self.active_drops.pop(message_id, None)
            return

        now = time.monotonic()
        reactor_ids = []
        async for user in reaction.users():
            if user.bot:
                continue
            if self.claim_cooldown_until.get(user.id, 0.0) > now:
                continue
            reactor_ids.append(user.id)

        if not reactor_ids:
            if len(drop.claimed_positions) >= len(drop.cards):
                self.active_drops.pop(message_id, None)
            return

        winner_id = engine.resolve_claim(reactor_ids)
        guild = channel.guild
        member = guild.get_member(winner_id)
        if member is None:
            try:
                member = await guild.fetch_member(winner_id)
            except discord.HTTPException:
                if len(drop.claimed_positions) >= len(drop.cards):
                    self.active_drops.pop(message_id, None)
                return

        card = await self._card_by_id(guild, card_entry["card_id"])
        if card is None:
            if len(drop.claimed_positions) >= len(drop.cards):
                self.active_drops.pop(message_id, None)
            return

        conf = self.config.guild(guild)
        sell_prices = await conf.sell_prices()

        if drop.is_test:
            state = await self._member_state(member)
            dupe = engine.would_be_dupe(state.collection, card.card_id)
            price = engine.sell_price(card.rarity, sell_prices) if dupe else None
            embed = embeds.claim_result_embed(card, member, dupe, price=price, is_test=True)
        else:
            state = await self._member_state(member)
            dupe = state.owns(card.card_id)
            price = None
            if dupe:
                token = engine.make_sell_token(card.card_id, card.rarity)
                state.sell_tokens.append(token)
                price = engine.sell_price(card.rarity, sell_prices)
            else:
                state.collection.append(card.card_id)
            await self._save_member_state(member, state)

            claim_cooldown = await conf.claim_cooldown_seconds()
            self.claim_cooldown_until[member.id] = time.monotonic() + claim_cooldown

            embed = embeds.claim_result_embed(card, member, dupe, price=price, is_test=False)

        await channel.send(embed=embed)

        if len(drop.claimed_positions) >= len(drop.cards):
            self.active_drops.pop(message_id, None)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.group(name="card", aliases=["cards"], invoke_without_command=True)
    @commands.guild_only()
    async def card(self, ctx: commands.Context):
        """View your card collection."""
        state = await self._member_state(ctx.author)
        if not state.collection:
            await ctx.send("You haven't claimed any cards yet.")
            return

        entries = []
        for card_id in state.collection:
            card = await self._card_by_id(ctx.guild, card_id)
            image_bytes = self._read_card_image(ctx.guild, card_id)
            if card is None or image_bytes is None:
                continue
            entries.append((card, image_bytes))
        if not entries:
            await ctx.send("Your cards exist but their art is missing -- ask an admin to check the pool.")
            return

        gallery = imagegen.render_gallery(entries, favorite_card_id=state.favorite_card_id)
        await ctx.send(file=discord.File(gallery, filename="collection.png"))

    @card.command(name="favorite")
    @commands.guild_only()
    async def card_favorite(self, ctx: commands.Context, *, card_arg: str):
        """Set your favorite card -- shown first and larger in your gallery."""
        card = await self._resolve_card_arg(ctx.guild, card_arg)
        if card is None:
            await ctx.send("No card matches that.")
            return
        state = await self._member_state(ctx.author)
        if not state.owns(card.card_id):
            await ctx.send("You don't own that card.")
            return
        state.favorite_card_id = card.card_id
        await self._save_member_state(ctx.author, state)
        await ctx.send(f"**{card.name}** is now your favorite.")

    @card.command(name="give")
    @commands.guild_only()
    async def card_give(self, ctx: commands.Context, member: discord.Member, *, card_arg: str):
        """Give one of your cards to another member. One-way, no confirmation."""
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

        giver_state.collection.remove(card.card_id)
        if giver_state.favorite_card_id == card.card_id:
            giver_state.favorite_card_id = None
        await self._save_member_state(ctx.author, giver_state)

        if receiver_state.owns(card.card_id):
            # receiver already has one -- consistent with the dupe rule
            # elsewhere, a second copy becomes a sell token, not a second
            # gallery tile (price is looked up later, at .card sell time)
            token = engine.make_sell_token(card.card_id, card.rarity)
            receiver_state.sell_tokens.append(token)
            await self._save_member_state(member, receiver_state)
            await ctx.send(
                f"{member.mention} already owned **{card.name}** — it was converted to a sell token for them instead."
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
    async def card_removecard(self, ctx: commands.Context, card_id: int):
        """Remove a character from future drops. Members keep copies they already have.

        This retires the card rather than deleting it outright: the pool
        entry and its art stay on disk so anyone who already owns a copy
        still gets a working gallery tile. Only future drop rolls skip it."""
        card = await self._card_by_id(ctx.guild, card_id)
        if card is None:
            await ctx.send("No card with that ID.")
            return
        async with self.config.guild(ctx.guild).pool() as pool:
            entry = pool.get(str(card_id))
            if entry is not None:
                entry["retired"] = True
        await ctx.send(embed=embeds.card_removed_embed(card_id, card.name))

    @card.command(name="importpool")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def card_importpool(self, ctx: commands.Context, count: int = 30):
        """Bulk-import the top `count` female characters by AniList favourites."""
        if count < 1 or count > 200:
            await ctx.send("Pick a count between 1 and 200.")
            return

        await ctx.send(f"Fetching the top {count} female characters from AniList…")
        tier_cutoffs = await self.config.guild(ctx.guild).tier_cutoffs()

        try:
            raw_entries = await import_characters.fetch_top_female_characters(
                self._session, tier_cutoffs, engine.bucket_tier, count
            )
        except aiohttp.ClientError as e:
            await ctx.send(f"AniList request failed: {e}")
            return

        if not raw_entries:
            await ctx.send("No characters came back -- AniList may be unreachable, or the filter matched nothing.")
            return

        added = 0
        async with self.config.guild(ctx.guild).all() as guild_data:
            for entry in raw_entries:
                try:
                    async with self._session.get(entry["image_url"]) as resp:
                        resp.raise_for_status()
                        image_bytes = await resp.read()
                except aiohttp.ClientError:
                    continue

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
                ).to_dict()
                added += 1

        await ctx.send(f"Imported {added} characters. Prune unwanted ones with `.card removecard <id>`.")

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
        await self._post_drop(channel, ctx.guild, is_test=True)
        if channel.id != ctx.channel.id:
            await ctx.send(f"Test drop posted in {channel.mention}.")
