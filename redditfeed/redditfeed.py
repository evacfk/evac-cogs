"""RedditFeed: config-driven subreddit -> channel image feed.

Replaces the old Reddit RSS/API-based posting (both discontinued by Reddit).
Shared poll loop across all mapped subreddits, image-only posts (galleries
batched, video/RedGIFs posted as links), dedup by (channel, post id) with a
rolling TTL, per-mapping keyword filter + pause toggle, no backfill.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from . import constants, discovery, discovery_ui, embeds, engine, queue_ui, redgifs
from .arctic_shift import ArcticShiftSource, RedditSource, RedditSourceError
from .dashboard_integration import DashboardIntegration, dashboard_page
from .dashboard_view import PAGE_TEMPLATE
from .models import QueueEntry, SubredditMapping

log = logging.getLogger("red.redditfeed")


class RedditFeed(DashboardIntegration, commands.Cog):
    """Subreddit -> Discord channel image feed, Arctic Shift-backed."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA12DD17F, force_registration=True)
        self.config.register_global(
            mappings={},
            poll_interval_seconds=constants.DEFAULT_POLL_INTERVAL_SECONDS,
            stagger_seconds=constants.DEFAULT_STAGGER_SECONDS,
            fetch_limit=constants.DEFAULT_FETCH_LIMIT,
            dedup_store={},
            dedup_ttl_days=constants.DEFAULT_DEDUP_TTL_DAYS,
            denied_subreddits=[],   # rejected in `discover`; never suggested again
            queue={},                # queue message id -> QueueEntry.to_dict()
            queue_channel_id=None,   # mod-only channel where manual-mode posts wait
            queue_min_score=constants.DEFAULT_QUEUE_MIN_SCORE,
            queue_min_age_minutes=constants.DEFAULT_QUEUE_MIN_AGE_MINUTES,
            queue_max_pending=constants.DEFAULT_QUEUE_MAX_PENDING,
            x_button=True,           # mod-only X under every feed post
            log_channel_id=constants.DEFAULT_LOG_CHANNEL_ID,
            posted_map={},           # feed message id -> {sub, pid, ch, ts}, for the X log line
            learned_topics={},       # topic word -> channel id, learned from approvals in `discover`
            new_channel_category_id=None,   # where `discover` may create channels (unset = never create)
            new_channel_prefix="",   # e.g. "🔞・"
            redgifs_mode=constants.REDGIFS_UPLOAD,   # upload the clip so it plays inline, or post the bare link
        )
        self.source: RedditSource = ArcticShiftSource()
        self.redgifs = redgifs.RedgifsResolver()
        self._redgifs_sem = asyncio.Semaphore(2)   # at most two clips downloading at once
        self._poll_task: Optional[asyncio.Task] = None
        self._cycle_lock = asyncio.Lock()
        self._discover_lock = asyncio.Lock()   # one discovery run at a time (politeness to Arctic Shift)
        self._queue_locks: dict = {}
        self._posted_buf: dict = {}
        self._last_stats: dict = {}
        self._channel_lock = asyncio.Lock()    # serialises "create the channel if it's missing" in discovery

    async def cog_load(self) -> None:
        # Fixed custom_ids: these keep working on old messages after a restart.
        self.bot.add_view(queue_ui.QueueView(self))
        self.bot.add_view(queue_ui.FeedPostView(self))
        self._poll_task = self.bot.loop.create_task(self._poll_loop())

    def cog_unload(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()

    # -- Poll loop -------------------------------------------------------------

    async def _poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                async with self._cycle_lock:
                    await self._run_poll_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- never let the loop die silently
                log.exception("redditfeed poll cycle crashed; will retry next interval")
            interval = await self.config.poll_interval_seconds()
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _run_poll_cycle(self) -> None:
        mappings_raw = await self.config.mappings()
        stagger = await self.config.stagger_seconds()
        fetch_limit = await self.config.fetch_limit()
        dedup_store = await self.config.dedup_store()
        ttl_days = await self.config.dedup_ttl_days()
        now = time.time()

        dedup_store = engine.prune_dedup_store(dedup_store, now, ttl_days)
        qcfg = await self._queue_settings()
        self._last_stats = {}

        first = True
        for key, raw in list(mappings_raw.items()):
            mapping = SubredditMapping.from_dict(raw)
            if mapping.paused or not mapping.channel_ids:
                continue

            if not first:
                await asyncio.sleep(stagger)
            first = False

            poll_started = time.time()
            advance_cursor = True
            mapping.last_error = None
            try:
                if mapping.approval == constants.APPROVAL_AUTO:
                    dedup_store = await self._poll_one_subreddit(mapping, fetch_limit, dedup_store, now)
                else:
                    dedup_store = await self._queue_one_subreddit(mapping, dedup_store, now, qcfg)
            except RedditSourceError as exc:
                mapping.last_error = str(exc)
                # Fetch failed, nothing was processed: keep the cursor where it was so
                # the posts from this window are picked up on the next successful poll
                # (bounded by MAX_LOOKBACK_SECONDS; dedup prevents any double-posting).
                advance_cursor = False
                log.warning("redditfeed: fetch failed for r/%s: %s", mapping.subreddit, exc)
            except Exception as exc:  # noqa: BLE001 -- one bad subreddit must not stop the rest
                mapping.last_error = f"unexpected error: {exc}"
                log.exception("redditfeed: unexpected error polling r/%s", mapping.subreddit)

            if advance_cursor:
                # Start-of-fetch time, not end: posts created while the fetch/post loop
                # ran are re-fetched next cycle rather than missed (dedup absorbs repeats).
                mapping.last_poll_ts = poll_started
            mappings_raw[key] = mapping.to_dict()

        # Merge instead of overwriting: a mod may have changed mappings (mode, pause,
        # add/remove) while this cycle was awaiting the network.
        async with self.config.mappings() as live:
            for key, raw in mappings_raw.items():
                if key in live:
                    live[key] = self._merge_poll_state(live[key], raw)
        await self.config.dedup_store.set(dedup_store)
        await self._flush_posted()
        try:
            await self._expire_queue(time.time())
        except Exception:  # noqa: BLE001
            log.exception("redditfeed: queue expiry failed")

    @staticmethod
    def _merge_poll_state(live: dict, polled: dict) -> dict:
        """Only the poll-owned fields come from the cycle; everything a mod may
        have edited meanwhile (channels, keywords, paused, approval) stays as live."""
        merged = dict(live)
        for field_name in ("last_poll_ts", "last_post_found_ts", "last_post_id", "last_error"):
            merged[field_name] = polled.get(field_name)
        return merged

    async def _poll_one_subreddit(
        self, mapping: SubredditMapping, fetch_limit: int, dedup_store: dict, now: float
    ) -> dict:
        after_ts = engine.compute_after_ts(mapping.last_poll_ts, time.time())
        posts = await self.source.fetch_new_posts(mapping.subreddit, after_ts, fetch_limit)
        posts = engine.sort_posts_oldest_first(posts)

        newest_post = None
        for post in posts:
            if not engine.passes_keyword_filter(post, mapping.require_keywords, mapping.block_keywords):
                continue

            media_items = engine.extract_media_items(post)
            if not media_items:
                continue  # text-only / unsupported post type -- nothing to post

            for channel_id in mapping.channel_ids:
                dedup_key = engine.build_dedup_key(channel_id, post["id"])
                if dedup_key in dedup_store:
                    continue
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    continue
                await self._post_media_items(channel, post, media_items, mapping.subreddit)
                dedup_store[dedup_key] = now

            newest_post = post

        if newest_post is not None:
            mapping.last_post_found_ts = newest_post.get("created_utc")
            mapping.last_post_id = newest_post.get("id")

        return dedup_store

    async def _post_media_items(self, channel, post: dict, media_items: list, subreddit: str = "") -> None:
        """Image-only, no text, no embed title/author/footer/link anywhere.

        A single direct image posts as a bare URL (Discord unfurls it inline).
        Multiple images from one post (a gallery) are batched into ONE message
        as multiple image-only embeds sharing a grouping url -- Discord tiles
        those into a single gallery message instead of N separate ones.
        Video/RedGIFs links can't be embedded as an image and always post
        individually as a bare URL. Each message carries the mod-only X button
        (if enabled) and is remembered so a removal can be logged with its source.
        """
        track = (str(post.get("id", "")), subreddit)
        view = await self._x_view()
        image_items, link_items = engine.partition_media_items(media_items)

        if len(image_items) == 1:
            await self._send_text(channel, embeds.build_link_message(post, image_items[0]), track, view)
        elif len(image_items) > 1:
            for batch in engine.chunk_items(image_items, constants.MAX_EMBEDS_PER_MESSAGE):
                if not await self._send_embeds(channel, embeds.build_gallery_embeds(post, batch), track, view):
                    return  # no permission -- don't bother with the rest of this post
                await asyncio.sleep(0.5)

        for item in link_items:
            if item.kind == constants.MEDIA_KIND_REDGIFS_LINK and await self.config.redgifs_mode() == constants.REDGIFS_UPLOAD:
                ok = await self._send_redgifs(channel, post, item, track, view)
            else:
                ok = await self._send_text(channel, embeds.build_link_message(post, item), track, view)
            if not ok:
                return
            await asyncio.sleep(0.5)

    async def _send_redgifs(self, channel, post: dict, item, track, view) -> bool:
        """RedGifs pages don't unfurl in Discord, so upload the clip itself. Any
        problem (RedGifs refusing, clip too big for this server, upload rejected)
        falls back to the plain link, so a post is never lost over this."""
        limit = redgifs.upload_limit(getattr(getattr(channel, "guild", None), "filesize_limit", None))
        try:
            async with self._redgifs_sem:
                filename, data = await self.redgifs.fetch(item.url, limit)
        except redgifs.RedgifsError as exc:
            log.info("redditfeed: RedGifs upload skipped for %s: %s", item.url, exc)
            self._bump("redgifs_link_fallback")
            return await self._send_text(channel, embeds.build_link_message(post, item), track, view)
        kwargs = {"file": discord.File(io.BytesIO(data), filename=filename)}
        if view is not None:
            kwargs["view"] = view
        try:
            message = await channel.send(**kwargs)
            self._note_posted(message, channel, track)
        except discord.Forbidden:
            log.warning("redditfeed: missing permission to post in channel %s", channel.id)
            return False
        except discord.HTTPException as exc:
            log.warning("redditfeed: RedGifs upload rejected in channel %s: %s", channel.id, exc)
            self._bump("redgifs_link_fallback")
            return await self._send_text(channel, embeds.build_link_message(post, item), track, view)
        self._bump("redgifs_uploaded")
        return True

    async def _x_view(self):
        if not await self.config.x_button():
            return None
        return queue_ui.FeedPostView(self)

    def _note_posted(self, message, channel, track) -> None:
        if message is None or not track or getattr(message, "id", None) is None:
            return
        self._posted_buf[str(message.id)] = {
            "sub": track[1], "pid": track[0], "ch": getattr(channel, "id", None), "ts": time.time(),
        }

    async def _flush_posted(self) -> None:
        if not self._posted_buf:
            return
        pending, self._posted_buf = self._posted_buf, {}
        async with self.config.posted_map() as posted:
            posted.update(pending)
            kept = engine.prune_posted_map(posted, time.time())
            posted.clear()
            posted.update(kept)

    async def _send_text(self, channel, content: str, track=None, view=None) -> bool:
        """Returns False on a permission failure (caller should stop posting
        further items to this channel for this post); True otherwise.
        """
        try:
            message = await (channel.send(content, view=view) if view is not None else channel.send(content))
            self._note_posted(message, channel, track)
        except discord.Forbidden:
            log.warning("redditfeed: missing permission to post in channel %s", channel.id)
            return False
        except discord.HTTPException as exc:
            log.warning("redditfeed: failed to post to channel %s: %s", channel.id, exc)
        return True

    async def _send_embeds(self, channel, embed_list: list, track=None, view=None) -> bool:
        try:
            message = await (channel.send(embeds=embed_list, view=view) if view is not None
                             else channel.send(embeds=embed_list))
            self._note_posted(message, channel, track)
        except discord.Forbidden:
            log.warning("redditfeed: missing permission to post in channel %s", channel.id)
            return False
        except discord.HTTPException as exc:
            log.warning("redditfeed: failed to post gallery to channel %s: %s", channel.id, exc)
        return True

    # -- Approval queue -------------------------------------------------------------

    async def _queue_settings(self) -> dict:
        queue = await self.config.queue()
        return {
            "channel_id": await self.config.queue_channel_id(),
            "min_score": await self.config.queue_min_score(),
            "min_age": await self.config.queue_min_age_minutes() * 60,
            "max_pending": await self.config.queue_max_pending(),
            "pending": len(engine.pending_ids(queue)),
        }

    def _bump(self, key: str, n: int = 1) -> None:
        self._last_stats[key] = self._last_stats.get(key, 0) + n

    async def _queue_one_subreddit(self, mapping: SubredditMapping, dedup_store: dict, now: float, qcfg: dict) -> dict:
        """Manual mode: find posts that have settled (old enough, enough score),
        and put them in the mod queue instead of the public channel. Posts that
        aren't ready yet are simply looked at again next cycle until they age
        out of the lookback window."""
        queue_channel = self.bot.get_channel(qcfg["channel_id"]) if qcfg["channel_id"] else None
        if queue_channel is None:
            mapping.last_error = "manual approval is on but no queue channel is set (.redditfeed queue channel #mod-queue)"
            self._bump("no_queue_channel")
            return dedup_store

        after = engine.queue_fetch_after(time.time(), mapping.added_ts)
        posts = await self.source.fetch_new_posts(mapping.subreddit, after, constants.QUEUE_FETCH_LIMIT)
        posts = engine.sort_posts_oldest_first(posts)

        queued = 0
        newest_post = None
        for post in posts:
            if not engine.passes_keyword_filter(post, mapping.require_keywords, mapping.block_keywords):
                self._bump("keyword_filtered")
                continue
            media_items = engine.extract_media_items(post)
            if not media_items:
                continue
            fresh = [c for c in mapping.channel_ids if engine.build_dedup_key(c, post["id"]) not in dedup_store]
            if not fresh:
                continue
            reason = engine.queue_skip_reason(post, time.time(), mapping.added_ts, qcfg["min_age"], qcfg["min_score"])
            if reason is not None:
                self._bump(reason)
                continue
            if qcfg["pending"] >= qcfg["max_pending"]:
                self._bump("queue_full")
                break
            if queued >= constants.QUEUE_MAX_PER_SUB_PER_CYCLE:
                self._bump("per_sub_cap")
                break

            entry = QueueEntry(
                subreddit=mapping.subreddit,
                post=engine.trim_post(post, mapping.subreddit),
                media=[m.to_dict() for m in media_items],
                channel_ids=fresh,
                created_ts=time.time(),
                queue_channel_id=queue_channel.id,
            )
            content, card_embeds = queue_ui.build_queue_message(entry)
            try:
                message = await queue_channel.send(content, embeds=card_embeds, view=queue_ui.QueueView(self))
            except discord.Forbidden:
                mapping.last_error = "I can't post in the queue channel (need Send Messages, Embed Links)"
                return dedup_store
            except discord.HTTPException as exc:
                log.warning("redditfeed: could not queue a post from r/%s: %s", mapping.subreddit, exc)
                continue
            async with self.config.queue() as stored:
                stored[str(message.id)] = entry.to_dict()
            for c in fresh:
                dedup_store[engine.build_dedup_key(c, post["id"])] = now
            qcfg["pending"] += 1
            queued += 1
            self._bump("queued")
            newest_post = post
            await asyncio.sleep(constants.QUEUE_POST_DELAY_SECONDS)

        if newest_post is not None:
            mapping.last_post_found_ts = newest_post.get("created_utc")
            mapping.last_post_id = newest_post.get("id")
        return dedup_store

    async def _is_moderator(self, user) -> bool:
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and perms.manage_guild:
            return True
        try:
            return bool(await self.bot.is_mod(user))
        except Exception:  # noqa: BLE001 -- unknown means not allowed
            return False

    async def _log(self, text: str) -> None:
        channel_id = await self.config.log_channel_id()
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            log.warning("redditfeed: could not write to the log channel")

    async def _deliver(self, entry: QueueEntry) -> int:
        """Post an approved item to its destination channel(s). Returns how many
        channels were reachable."""
        reached = 0
        items = entry.media_items()
        for channel_id in entry.channel_ids:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            await self._post_media_items(channel, {"id": entry.post.get("id", "")}, items, entry.subreddit)
            reached += 1
        return reached

    async def handle_queue_action(self, interaction, action: str) -> None:
        """Approve / Reject / Pause on a queue card. Safe against double clicks
        and works on cards from before a restart (state is in Config)."""
        if not await self._is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can use these buttons.", ephemeral=True)
            return
        message_id = str(interaction.message.id)
        lock = self._queue_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            raw = (await self.config.queue()).get(message_id)
            if raw is None:
                await interaction.response.send_message("That item isn't in the queue any more.", ephemeral=True)
                return
            entry = QueueEntry.from_dict(raw)
            if entry.status != constants.QUEUE_PENDING:
                await interaction.response.send_message(f"Someone already decided this one ({entry.status}).", ephemeral=True)
                return
            await interaction.response.defer()

            who = interaction.user.mention
            if action == "approve":
                try:
                    reached = await self._deliver(entry)
                except Exception:  # noqa: BLE001 -- leave it pending so a mod can retry
                    log.exception("redditfeed: delivering a queued post failed")
                    await interaction.followup.send("Something went wrong posting that. It's still in the queue.", ephemeral=True)
                    return
                if reached == 0:
                    await interaction.followup.send("I couldn't reach the destination channel(s). It's still in the queue.", ephemeral=True)
                    return
                entry.status = constants.QUEUE_APPROVED
                result = f"\u2705 Approved by {who}: posted to {' '.join(f'<#{c}>' for c in entry.channel_ids)}"
            else:
                entry.status = constants.QUEUE_REJECTED
                result = f"\u274c Rejected by {who}."
                if action == "pause":
                    await self._apply_paused(entry.subreddit, True)
                    result = f"\u23f8\ufe0f Rejected by {who}, and r/{entry.subreddit} is paused (`.redditfeed resume {entry.subreddit}` to undo)."
                    await self._log(f"\u23f8\ufe0f {interaction.user.display_name} paused r/{entry.subreddit} from the approval queue.")
            entry.resolved_ts = time.time()
            entry.resolved_by = interaction.user.id
            async with self.config.queue() as stored:
                stored[message_id] = entry.to_dict()
            await self._flush_posted()

            try:
                await interaction.message.edit(
                    content=None, embeds=queue_ui.resolved_embeds(list(interaction.message.embeds), result), view=None
                )
            except discord.HTTPException as exc:
                log.warning("redditfeed: could not update a queue card: %s", exc)
        self._queue_locks.pop(message_id, None)

    async def handle_feed_x(self, interaction) -> None:
        """Mod-only delete under a feed post; logs who removed what to the log channel."""
        if not await self._is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can remove feed posts.", ephemeral=True)
            return
        info = (await self.config.posted_map()).get(str(interaction.message.id)) or {}
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(f"I couldn't delete it: {exc}", ephemeral=True)
            return
        await interaction.response.send_message("Removed.", ephemeral=True)
        source = f"r/{info['sub']}, https://redd.it/{info['pid']}" if info.get("sub") and info.get("pid") else "source unknown"
        where = f"<#{info['ch']}>" if info.get("ch") else "a feed channel"
        await self._log(f"\u2716\ufe0f {interaction.user.display_name} removed a feed post in {where} ({source}).")

    async def _expire_queue(self, now: float) -> None:
        """Discard undecided items after the TTL (their cards are marked, buttons
        removed) and prune old decided records."""
        expired: list = []
        async with self.config.queue() as stored:
            for message_id in engine.expired_pending_ids(stored, now):
                entry = QueueEntry.from_dict(stored[message_id])
                entry.status = constants.QUEUE_EXPIRED
                entry.resolved_ts = now
                stored[message_id] = entry.to_dict()
                expired.append((message_id, entry.queue_channel_id))
            kept = engine.prune_queue(stored, now)
            for key in [k for k in stored if k not in kept]:
                del stored[key]
        for message_id, channel_id in expired:
            channel = self.bot.get_channel(channel_id) if channel_id else None
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(
                    content=None,
                    embeds=queue_ui.resolved_embeds(list(message.embeds), "\u23f3 Expired: nobody decided in 24 hours, discarded."),
                    view=None,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    # -- Command tree -------------------------------------------------------------

    @commands.group(name="redditfeed", aliases=["rf"], invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    async def redditfeed(self, ctx: commands.Context) -> None:
        """Manage the subreddit -> channel image feed."""
        await ctx.send_help(ctx.command)

    @redditfeed.command(name="version")
    async def redditfeed_version(self, ctx: commands.Context) -> None:
        """Version-probe command -- confirms a deploy actually took."""
        await ctx.send("redditfeed build: redgifs-v1 (RedGifs clips upload as video, mod queue, X button)")

    async def _map_subreddit(self, name: str, channel_id: int) -> bool:
        """The one place a subreddit gets mapped to a channel -- `add` and the
        discovery Approve button both go through here. True if this created a
        brand-new mapping, False if it only added a channel to an existing one.
        """
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            mapping = SubredditMapping.from_dict(raw) if raw else SubredditMapping(subreddit=name)
            if channel_id not in mapping.channel_ids:
                mapping.channel_ids.append(channel_id)
            if raw is None:
                mapping.last_poll_ts = time.time()  # no backfill: starts polling from now
                mapping.added_ts = mapping.last_poll_ts
            mappings[name] = mapping.to_dict()
        return raw is None

    @redditfeed.command(name="add")
    async def redditfeed_add(
        self, ctx: commands.Context, subreddit: str, channel: discord.TextChannel
    ) -> None:
        """Map a subreddit to a channel. Safe to call again to add another channel
        to a subreddit already mapped, or to map another subreddit to the same channel.
        """
        name = engine.normalize_subreddit(subreddit)
        await self._map_subreddit(name, channel.id)
        await ctx.send(f"r/{name} -> {channel.mention} ({await self._mode_of(name)} approval)")

    async def _mode_of(self, name: str) -> str:
        raw = (await self.config.mappings()).get(name) or {}
        return SubredditMapping.from_dict(raw).approval if raw else constants.DEFAULT_APPROVAL

    @redditfeed.command(name="addmany")
    async def redditfeed_addmany(self, ctx: commands.Context, channel: discord.TextChannel, *subreddits: str) -> None:
        """Map several subreddits to one channel at once.
        `.redditfeed addmany #feet feet footfetish soles`"""
        names = []
        for raw in subreddits:
            name = engine.normalize_subreddit(raw)
            if name and name not in names:
                names.append(name)
        if not names:
            await ctx.send("Give me at least one subreddit after the channel.")
            return
        if not channel.is_nsfw():
            await ctx.send(f"{channel.mention} isn't age-restricted. Fix that first; these feeds are NSFW.")
            return
        for name in names:
            await self._map_subreddit(name, channel.id)
        await ctx.send(f"Mapped {len(names)} subreddit(s) -> {channel.mention}: " + ", ".join(f"r/{n}" for n in names)
                       + f"\nNew mappings use **{constants.DEFAULT_APPROVAL}** approval (posts wait in the queue).")

    @redditfeed.command(name="resetall")
    async def redditfeed_resetall(self, ctx: commands.Context, confirm: str = "") -> None:
        """Remove EVERY subreddit mapping and forget what was already posted.
        Settings, the denied list and learned topics are kept. `.redditfeed resetall yes`"""
        if confirm.lower() != "yes":
            count = len(await self.config.mappings())
            await ctx.send(f"This removes all {count} mapping(s). Run `.redditfeed resetall yes` to confirm.")
            return
        async with self.config.mappings() as mappings:
            removed = len(mappings)
            mappings.clear()
        await self.config.dedup_store.set({})
        async with self.config.queue() as stored:
            now = time.time()
            for message_id in engine.pending_ids(stored):
                stored[message_id]["status"] = constants.QUEUE_EXPIRED
                stored[message_id]["resolved_ts"] = now
        await ctx.send(f"Removed {removed} mapping(s) and cleared the dedup memory. Pending queue items were discarded.")

    @redditfeed.command(name="mode")
    async def redditfeed_mode(self, ctx: commands.Context, subreddit: str, mode: str) -> None:
        """`manual` = posts wait in the mod queue; `auto` = posts go straight to the channel."""
        parsed = engine.parse_approval(mode)
        name = engine.normalize_subreddit(subreddit)
        if parsed is None:
            await ctx.send("Mode must be `manual` or `auto`.")
            return
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            mapping = SubredditMapping.from_dict(raw)
            mapping.approval = parsed
            mappings[name] = mapping.to_dict()
        await ctx.send(f"r/{name} is now **{parsed}**.")

    @redditfeed.command(name="modeall")
    async def redditfeed_modeall(self, ctx: commands.Context, mode: str) -> None:
        """Set every mapped subreddit to `manual` or `auto` at once."""
        parsed = engine.parse_approval(mode)
        if parsed is None:
            await ctx.send("Mode must be `manual` or `auto`.")
            return
        async with self.config.mappings() as mappings:
            for name, raw in list(mappings.items()):
                mapping = SubredditMapping.from_dict(raw)
                mapping.approval = parsed
                mappings[name] = mapping.to_dict()
            count = len(mappings)
        await ctx.send(f"{count} subreddit(s) are now **{parsed}**.")

    @redditfeed.command(name="remove")
    async def redditfeed_remove(self, ctx: commands.Context, subreddit: str) -> None:
        """Remove a subreddit mapping entirely (all its channels)."""
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            if name not in mappings:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            del mappings[name]
        await ctx.send(f"Removed r/{name}.")

    @redditfeed.command(name="removechannel")
    async def redditfeed_removechannel(
        self, ctx: commands.Context, subreddit: str, channel: discord.TextChannel
    ) -> None:
        """Remove one channel from a subreddit's mapping, keeping the rest."""
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            mapping = SubredditMapping.from_dict(raw)
            if channel.id in mapping.channel_ids:
                mapping.channel_ids.remove(channel.id)
            mappings[name] = mapping.to_dict()
        await ctx.send(f"Removed {channel.mention} from r/{name}.")

    @redditfeed.command(name="pause")
    async def redditfeed_pause(self, ctx: commands.Context, subreddit: str) -> None:
        """Pause polling for a subreddit without deleting its mapping."""
        await self._set_paused(ctx, subreddit, True)

    @redditfeed.command(name="resume")
    async def redditfeed_resume(self, ctx: commands.Context, subreddit: str) -> None:
        """Resume polling for a previously paused subreddit."""
        await self._set_paused(ctx, subreddit, False)

    async def _apply_paused(self, name: str, paused: bool) -> bool:
        """The one place a mapping's paused flag is written -- the commands and
        the dashboard page both go through here. False if `name` isn't mapped.
        """
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                return False
            mapping = SubredditMapping.from_dict(raw)
            mapping.paused = paused
            mappings[name] = mapping.to_dict()
        return True

    async def _set_paused(self, ctx: commands.Context, subreddit: str, paused: bool) -> None:
        name = engine.normalize_subreddit(subreddit)
        if not await self._apply_paused(name, paused):
            await ctx.send(f"r/{name} isn't mapped.")
            return
        await ctx.send(f"r/{name} is now {'paused' if paused else 'active'}.")

    @redditfeed.command(name="interval")
    async def redditfeed_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set the shared poll interval, in seconds (floor: 30s)."""
        clamped = engine.clamp_poll_interval(seconds)
        await self.config.poll_interval_seconds.set(clamped)
        note = f" (raised from {seconds} to the {clamped}s floor)" if clamped != seconds else ""
        await ctx.send(f"Poll interval set to {clamped}s{note}.")

    @redditfeed.command(name="stagger")
    async def redditfeed_stagger(self, ctx: commands.Context, seconds: float) -> None:
        """Set the delay between individual subreddit requests within a poll cycle."""
        clamped = engine.clamp_stagger(seconds)
        await self.config.stagger_seconds.set(clamped)
        await ctx.send(f"Stagger set to {clamped}s.")

    @redditfeed.command(name="list")
    async def redditfeed_list(self, ctx: commands.Context) -> None:
        """List all mapped subreddits."""
        mappings_raw = await self.config.mappings()
        if not mappings_raw:
            await ctx.send("No subreddits mapped yet. `.redditfeed add <subreddit> #channel`")
            return
        lines = [f"r/{name}" for name in sorted(mappings_raw)]
        await ctx.send("\n".join(lines))

    @redditfeed.command(name="status")
    async def redditfeed_status(self, ctx: commands.Context) -> None:
        """Per-subreddit status: channel(s), last poll, last post found, active/paused."""
        mappings_raw = await self.config.mappings()
        if not mappings_raw:
            await ctx.send("No subreddits mapped yet. `.redditfeed add <subreddit> #channel`")
            return
        mappings = [SubredditMapping.from_dict(raw) for raw in mappings_raw.values()]
        lines = embeds.build_status_lines(mappings)
        text = "\n\n".join(lines)
        for chunk_start in range(0, len(text), 1900):
            await ctx.send(text[chunk_start : chunk_start + 1900])

    # -- Queue + feed settings ---------------------------------------------------------

    @redditfeed.group(name="queue", invoke_without_command=True)
    async def redditfeed_queue(self, ctx: commands.Context) -> None:
        """Show the approval queue: where it is, what's waiting, and what the last cycle did."""
        stored = await self.config.queue()
        pending = engine.pending_ids(stored)
        channel_id = await self.config.queue_channel_id()
        mappings = [SubredditMapping.from_dict(r) for r in (await self.config.mappings()).values()]
        manual = sum(1 for m in mappings if m.approval == constants.APPROVAL_MANUAL)
        stats = self._last_stats or {}
        lines = [
            f"Queue channel: {f'<#{channel_id}>' if channel_id else '**not set** (`.redditfeed queue channel #mod-queue`)'}",
            f"Waiting for a decision: **{len(pending)}** (cap {await self.config.queue_max_pending()})",
            f"Queued only when a post is at least **{await self.config.queue_min_age_minutes()} min** old "
            f"and has **{await self.config.queue_min_score()}+** score",
            f"Subreddits: {manual} manual, {len(mappings) - manual} auto. X button: "
            f"{'on' if await self.config.x_button() else 'off'}. Log: <#{await self.config.log_channel_id()}>",
        ]
        if stats:
            lines.append("Last cycle: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(stats.items())))
        await ctx.send("\n".join(lines))

    @redditfeed_queue.command(name="channel")
    async def redditfeed_queue_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the mod-only channel where manual-approval posts wait."""
        if not channel.is_nsfw():
            await ctx.send(f"{channel.mention} must be age-restricted (the previews are explicit).")
            return
        if channel.permissions_for(channel.guild.default_role).view_channel:
            await ctx.send(f"{channel.mention} is visible to @everyone. Make it mods-only first.")
            return
        await self.config.queue_channel_id.set(channel.id)
        await ctx.send(f"Queue channel set to {channel.mention}. Manual-approval posts will appear there.")

    @redditfeed_queue.command(name="minscore")
    async def redditfeed_queue_minscore(self, ctx: commands.Context, score: int) -> None:
        """Only queue posts with at least this score (0 = no minimum)."""
        await self.config.queue_min_score.set(max(0, score))
        await ctx.send(f"Minimum score: {max(0, score)}.")

    @redditfeed_queue.command(name="minage")
    async def redditfeed_queue_minage(self, ctx: commands.Context, minutes: int) -> None:
        """Only queue posts at least this many minutes old (lets scores settle)."""
        await self.config.queue_min_age_minutes.set(max(0, minutes))
        await ctx.send(f"Minimum age: {max(0, minutes)} minutes.")

    @redditfeed_queue.command(name="max")
    async def redditfeed_queue_max(self, ctx: commands.Context, count: int) -> None:
        """Stop adding to the queue once this many items are waiting."""
        await self.config.queue_max_pending.set(max(1, count))
        await ctx.send(f"Queue cap: {max(1, count)} waiting items.")

    @redditfeed.command(name="xbutton")
    async def redditfeed_xbutton(self, ctx: commands.Context, state: str) -> None:
        """`on`/`off`: the mod-only X under new feed posts (existing posts keep theirs)."""
        value = state.strip().lower()
        if value not in ("on", "off"):
            await ctx.send("Use `on` or `off`.")
            return
        await self.config.x_button.set(value == "on")
        await ctx.send(f"X button is now {value} for new posts.")

    @redditfeed.command(name="redgifs")
    async def redditfeed_redgifs(self, ctx: commands.Context, mode: str = "") -> None:
        """`upload` (default): post RedGifs clips as a playable video file. `link`: post the bare link (Discord won't embed it)."""
        value = mode.strip().lower()
        if not value:
            await ctx.send(f"RedGifs mode: **{await self.config.redgifs_mode()}**. Use `upload` or `link`.")
            return
        if value not in (constants.REDGIFS_UPLOAD, constants.REDGIFS_LINK):
            await ctx.send("Use `upload` or `link`.")
            return
        await self.config.redgifs_mode.set(value)
        await ctx.send(f"RedGifs posts will now use **{value}**.")

    @redditfeed.command(name="logchannel")
    async def redditfeed_logchannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Where X removals and queue pauses are logged (default: #mod-commands)."""
        await self.config.log_channel_id.set(channel.id)
        await ctx.send(f"Logging to {channel.mention}.")

    @redditfeed.command(name="category")
    async def redditfeed_category(self, ctx: commands.Context, category: discord.CategoryChannel) -> None:
        """Category where `discover` may create new channels. New channels inherit its
        permissions and are age-restricted. Until this is set, discover never creates one."""
        await self.config.new_channel_category_id.set(category.id)
        await ctx.send(f"`discover` may create new channels in **{category.name}**.")

    @redditfeed.command(name="prefix")
    async def redditfeed_prefix(self, ctx: commands.Context, *, prefix: str) -> None:
        """Text put in front of new channel names, e.g. `.redditfeed prefix 🔞・`. `off` clears it."""
        value = "" if prefix.strip().lower() == "off" else prefix.strip()
        await self.config.new_channel_prefix.set(value)
        await ctx.send(f"New channel names will start with `{value}`." if value else "New channel names get no prefix.")

    # -- Discovery: suggest subreddits, mods approve/deny with a preview ---------------

    async def _approve_suggestion(self, name: str, channel_id=None, new_name=None, guild=None, display_name: str = ""):
        """Map an approved subreddit. Creates the destination channel first when the
        proposal was a new one. Returns (created_new_mapping, channel_id)."""
        if channel_id is None:
            channel_id = await self._ensure_channel(guild, new_name)
        created = await self._map_subreddit(name, channel_id)
        async with self.config.denied_subreddits() as denied:
            if name in denied:
                denied.remove(name)
        topic = discovery.topic_of(display_name or name)
        if topic:
            async with self.config.learned_topics() as learned:
                learned[topic] = channel_id     # next time this topic is proposed straight into the same channel
        return created, channel_id

    async def _ensure_channel(self, guild, new_name):
        """Reuse a channel with this name if one exists (an earlier approval in the
        same run may have just made it), else create it in the configured category."""
        if guild is None or not new_name:
            raise discovery.DestinationError("No destination channel to create.")
        async with self._channel_lock:
            wanted = new_name.lower()
            for channel in guild.text_channels:
                if channel.name.lower() == wanted:
                    if not channel.is_nsfw():
                        raise discovery.DestinationError(f"{channel.mention} exists but isn't age-restricted. Fix that, then approve again.")
                    return channel.id
            category_id = await self.config.new_channel_category_id()
            category = guild.get_channel(category_id) if category_id else None
            if category is None:
                raise discovery.DestinationError(
                    "I'm not allowed to create channels yet. Set a category with `.redditfeed category <category>`, "
                    "or pick an existing channel from the dropdown."
                )
            try:
                channel = await guild.create_text_channel(
                    new_name, category=category, nsfw=True, reason="redditfeed discover: approved subreddit"
                )
            except discord.Forbidden:
                raise discovery.DestinationError("I need Manage Channels to create that channel.")
            except discord.HTTPException as exc:
                raise discovery.DestinationError(f"Discord refused to create the channel: {exc}")
            return channel.id

    async def _deny_suggestion(self, name: str) -> None:
        async with self.config.denied_subreddits() as denied:
            if name not in denied:
                denied.append(name)

    @redditfeed.command(name="discover")
    async def redditfeed_discover(
        self,
        ctx: commands.Context,
        prefixes: str,
        channel: Optional[discord.TextChannel] = None,
        min_subscribers: int = constants.DISCOVER_DEFAULT_MIN_SUBSCRIBERS,
    ) -> None:
        """Suggest NSFW subreddits whose NAME STARTS WITH a prefix, each with a
        link, a preview of recent top posts, a PROPOSED destination channel and
        Approve/Deny buttons.

        Several prefixes: `feet,foot,sole` (max 5). Leave the channel out and each
        card proposes one (an existing channel that matches, or a new one if a
        category is set with `.redditfeed category`); a dropdown on the card changes
        it. Give a channel to force every card to it. Run it in an age-restricted
        channel (previews are explicit). Examples:
        `.redditfeed discover feet,foot`   `.redditfeed discover feet #feet 5000`
        """
        if not ctx.channel.is_nsfw() or (channel is not None and not channel.is_nsfw()):
            await ctx.send(
                "Both this channel and the target channel must be age-restricted. "
                "Previews are explicit, and approved feeds post there."
            )
            return
        prefix_list = discovery.parse_prefixes(prefixes)
        if not prefix_list:
            await ctx.send("Give me one or more name prefixes (letters, numbers, underscores), e.g. `feet,foot`.")
            return
        if self._discover_lock.locked():
            await ctx.send("A discovery run is already in progress. Give it a minute.")
            return

        async with self._discover_lock:
            try:
                await self._run_discovery(ctx, prefix_list, channel, max(0, min_subscribers))
            except Exception:  # noqa: BLE001 -- surface it instead of dying silently
                log.exception("redditfeed: discovery run crashed")
                await ctx.send("Discovery hit an unexpected error. Check the bot logs.")

    async def _destination_inputs(self, ctx: commands.Context) -> dict:
        """What the proposal needs from the server: age-restricted channels to match
        against, topics learned from earlier approvals, and whether creating is allowed."""
        guild = getattr(ctx, "guild", None)
        skip = {ctx.channel.id, await self.config.queue_channel_id()}
        channels = [
            (c.id, c.name) for c in (guild.text_channels if guild is not None else [])
            if c.is_nsfw() and c.id not in skip
        ]
        category_id = await self.config.new_channel_category_id()
        return {
            "channels": channels,
            "learned": await self.config.learned_topics(),
            "can_create": bool(guild is not None and category_id and guild.get_channel(category_id) is not None),
            "prefix": await self.config.new_channel_prefix(),
        }

    async def _run_discovery(
        self, ctx: commands.Context, prefixes: list[str], channel, min_subscribers: int
    ) -> None:
        dest_inputs = await self._destination_inputs(ctx) if channel is None else None
        raw_results: list[dict] = []
        failures: list[str] = []
        async with ctx.typing():
            for i, prefix in enumerate(prefixes):
                if i:
                    await asyncio.sleep(constants.DISCOVER_STAGGER_SECONDS)
                try:
                    raw_results.extend(
                        await self.source.search_subreddits(prefix, min_subscribers, constants.DISCOVER_SEARCH_LIMIT)
                    )
                except RedditSourceError as exc:
                    failures.append(f"`{prefix}`: {exc}")

            mapped = await self.config.mappings()
            denied = await self.config.denied_subreddits()
            suggestions, stats = discovery.filter_candidates(raw_results, mapped.keys(), denied)
            await ctx.send(discovery.format_summary(stats, len(suggestions), prefixes, min_subscribers, failures))

            for i, candidate in enumerate(suggestions):
                if i:
                    await asyncio.sleep(constants.DISCOVER_STAGGER_SECONDS)
                preview_failed = False
                try:
                    posts = await self.source.fetch_new_posts(
                        candidate.name,
                        time.time() - constants.PREVIEW_WINDOW_SECONDS,
                        constants.PREVIEW_FETCH_LIMIT,
                    )
                except RedditSourceError:
                    posts, preview_failed = [], True
                preview = discovery.pick_preview(posts)
                if channel is not None:
                    dest = discovery.Destination(channel_id=channel.id, reason="the channel you chose")
                else:
                    dest = discovery.propose_destination(candidate.display_name or candidate.name, **dest_inputs)
                suggestion_embeds = discovery_ui.build_suggestion_embeds(
                    candidate, preview, dest.channel_id or 0, preview_failed=preview_failed, destination=dest
                )
                view = discovery_ui.SuggestionView(
                    self, candidate.name, dest.channel_id, suggestion_embeds, ctx.author.id,
                    new_name=dest.new_name, guild=getattr(ctx, "guild", None),
                    display_name=candidate.display_name or candidate.name,
                )
                try:
                    view.message = await ctx.channel.send(embeds=suggestion_embeds, view=view)
                except discord.Forbidden:
                    await ctx.send("I can't post embeds here. I need Send Messages and Embed Links in this channel.")
                    return
                except discord.HTTPException as exc:
                    log.warning("redditfeed: could not post suggestion for r/%s: %s", candidate.name, exc)

    @redditfeed.command(name="denied")
    async def redditfeed_denied(self, ctx: commands.Context) -> None:
        """List subreddits you denied in `discover` (they're never suggested again)."""
        denied = await self.config.denied_subreddits()
        if not denied:
            await ctx.send("No denied subreddits.")
            return
        await ctx.send("Denied: " + ", ".join(f"r/{name}" for name in sorted(denied)) + "\nUndo with `.redditfeed undeny <subreddit>`.")

    @redditfeed.command(name="undeny")
    async def redditfeed_undeny(self, ctx: commands.Context, subreddit: str) -> None:
        """Let a previously denied subreddit be suggested again."""
        name = engine.normalize_subreddit(subreddit)
        async with self.config.denied_subreddits() as denied:
            if name not in denied:
                await ctx.send(f"r/{name} isn't on the denied list.")
                return
            denied.remove(name)
        await ctx.send(f"r/{name} can be suggested again.")

    @redditfeed.group(name="keyword", invoke_without_command=True)
    async def redditfeed_keyword(self, ctx: commands.Context) -> None:
        """Manage per-subreddit keyword filters."""
        await ctx.send_help(ctx.command)

    @redditfeed_keyword.command(name="show")
    async def redditfeed_keyword_show(self, ctx: commands.Context, subreddit: str) -> None:
        name = engine.normalize_subreddit(subreddit)
        mappings_raw = await self.config.mappings()
        raw = mappings_raw.get(name)
        if not raw:
            await ctx.send(f"r/{name} isn't mapped.")
            return
        mapping = SubredditMapping.from_dict(raw)
        require = ", ".join(mapping.require_keywords) or "(none)"
        block = ", ".join(mapping.block_keywords) or "(none)"
        await ctx.send(f"r/{name}\nrequire: {require}\nblock: {block}")

    @redditfeed_keyword.command(name="require")
    async def redditfeed_keyword_require(
        self, ctx: commands.Context, action: str, subreddit: str, *, word: str
    ) -> None:
        """`.redditfeed keyword require add|remove <subreddit> <word>`"""
        await self._edit_keyword_list(ctx, action, subreddit, word, constants.KEYWORD_MODE_REQUIRE)

    @redditfeed_keyword.command(name="block")
    async def redditfeed_keyword_block(
        self, ctx: commands.Context, action: str, subreddit: str, *, word: str
    ) -> None:
        """`.redditfeed keyword block add|remove <subreddit> <word>`"""
        await self._edit_keyword_list(ctx, action, subreddit, word, constants.KEYWORD_MODE_BLOCK)

    async def _edit_keyword_list(
        self, ctx: commands.Context, action: str, subreddit: str, word: str, mode: str
    ) -> None:
        action = action.lower()
        if action not in ("add", "remove"):
            await ctx.send("Action must be `add` or `remove`.")
            return
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            mapping = SubredditMapping.from_dict(raw)
            target = mapping.require_keywords if mode == constants.KEYWORD_MODE_REQUIRE else mapping.block_keywords
            word_lower = word.lower()
            if action == "add" and word_lower not in target:
                target.append(word_lower)
            elif action == "remove" and word_lower in target:
                target.remove(word_lower)
            mappings[name] = mapping.to_dict()
        await ctx.send(f"r/{name} {mode} keywords: {', '.join(target) or '(none)'}")

    # -- Dashboard page ------------------------------------------------------------
    # Registration is handled by DashboardIntegration (dashboard_integration.py).
    # Everything the page can do is also a `.redditfeed` command; the page is a
    # second front end over the same config, and writes go through _apply_paused.

    async def _on_bot_loop(self, coro):
        """Run `coro` on the bot's own event loop and await its result.

        The dashboard calls page handlers from its web thread's event loop, not
        Red's. Anything that writes Red Config (asyncio locks, the JSON driver)
        or talks to Discord must run on the bot's loop; across loops it can raise
        "bound to a different event loop" or race the poll loop. If we are
        already on the bot's loop this just awaits inline.
        """
        loop = getattr(self.bot, "loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or loop is running:
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def _dashboard_can_edit(self, user, guild: discord.Guild) -> bool:
        """Same bar as the command group: bot owner, Manage Server, or mod role."""
        if await self.bot.is_owner(user):
            return True
        member = guild.get_member(user.id)
        if member is None:
            return False
        if member.guild_permissions.manage_guild:
            return True
        return await self.bot.is_mod(member)

    async def _dashboard_apply_pause(
        self, user, guild: discord.Guild, subreddit: str, action: str
    ) -> tuple[str, str]:
        """Permission-checked pause/resume for the dashboard. Returns
        (notification category, message). Checks happen here, in the write path
        itself, not only in whether the form was shown.
        """
        paused = engine.parse_pause_action(action)
        if paused is None:
            return "error", "Unknown action."
        if not await self._dashboard_can_edit(user, guild):
            return "error", "You need the mod role or Manage Server to do that."
        name = engine.normalize_subreddit(subreddit)
        visible = engine.mappings_for_channels(
            await self.config.mappings(), {c.id for c in guild.text_channels}
        )
        if name not in {m.subreddit for m in visible}:
            return "error", f"r/{name} isn't mapped to a channel in this server."
        if not await self._on_bot_loop(self._apply_paused(name, paused)):
            return "error", f"r/{name} isn't mapped."
        return "success", f"r/{name} is now {'paused' if paused else 'active'}."

    @dashboard_page(
        name="feeds",
        description="View RedditFeed subreddit feeds and pause or resume them.",
        methods=("GET", "POST"),
    )
    async def dashboard_redditfeed(self, user: discord.User, guild: discord.Guild, **kwargs):
        can_edit = await self._dashboard_can_edit(user, guild)
        guild_channel_ids = {c.id for c in guild.text_channels}
        notifications = []
        form = None

        Form = kwargs.get("Form")
        if Form is not None and can_edit:
            import wtforms  # shipped with the dashboard; imported lazily so the cog loads without it

            class PauseForm(Form):
                def __init__(self):
                    super().__init__(prefix="redditfeed_pause_form_")

                subreddit: wtforms.SelectField = wtforms.SelectField(
                    "Subreddit", validators=[wtforms.validators.InputRequired()]
                )
                action: wtforms.SelectField = wtforms.SelectField(
                    "Action",
                    choices=[
                        (constants.DASHBOARD_ACTION_PAUSE, "Pause"),
                        (constants.DASHBOARD_ACTION_RESUME, "Resume"),
                    ],
                )
                submit: wtforms.SubmitField = wtforms.SubmitField("Apply")

            form = PauseForm()
            visible = engine.mappings_for_channels(await self.config.mappings(), guild_channel_ids)
            form.subreddit.choices = [(m.subreddit, f"r/{m.subreddit}") for m in visible]
            if form.validate_on_submit() and await form.validate_dpy_converters():
                category, message = await self._dashboard_apply_pause(
                    user, guild, form.subreddit.data, form.action.data
                )
                notifications.append({"message": message, "category": category})

        # Read after any write so the table shows the new state.
        visible = engine.mappings_for_channels(await self.config.mappings(), guild_channel_ids)
        channel_names = {c.id: f"#{c.name}" for c in guild.text_channels}
        rows = engine.build_dashboard_rows(visible, channel_names, time.time())

        result = {
            "status": 0,
            "web_content": {
                "source": PAGE_TEMPLATE,
                "rows": rows,
                "form": form,
                "can_edit": can_edit,
                "guild_name": guild.name,
            },
        }
        if notifications:
            result["notifications"] = notifications
        return result
