"""TGFeed: mirror the photos and videos of a Telegram forum group you belong to into
Discord, one channel per topic. Media only: no text, captions, usernames or file
names ever reach Discord.

The Telegram side is read-only and slow on purpose (see source.py). One poll cycle:
one history scan for the whole group, then each mapped topic's new photos/videos are
downloaded one at a time with random gaps, shrunk if over the upload limit, posted
(albums together), and the temp files deleted.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import shutil
import time
from collections import defaultdict
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from . import constants, engine, pipeline, transcode
from .models import GroupInfo, TopicMapping, TopicStats
from .source import SourceAuthError, SourceError, SourceFlood, TelethonSource
from .store import PostedStore
from .views import FeedPostView

log = logging.getLogger("red.tgfeed")


class TGFeed(commands.Cog):
    """Telegram forum group -> Discord channels, media only."""

    def __init__(self, bot: Red, source_factory=None):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x7E1E6F33, force_registration=True)
        self.config.register_global(
            group={},                       # {"id", "title", "username", "is_forum"}
            mappings={},                    # str(topic id) -> TopicMapping.to_dict()
            poll_interval_seconds=constants.DEFAULT_POLL_INTERVAL_SECONDS,
            file_gap_min=constants.DEFAULT_FILE_GAP_MIN,
            file_gap_max=constants.DEFAULT_FILE_GAP_MAX,
            max_per_hour=constants.DEFAULT_MAX_FILES_PER_HOUR,
            max_per_day=constants.DEFAULT_MAX_FILES_PER_DAY,
            paused=False,
            pause_reason=None,
            x_button=True,
            log_channel_id=constants.DEFAULT_LOG_CHANNEL_ID,
            category_id=None,               # where `mapall` creates channels
            name_prefix="",
            view_role_ids=[constants.DEFAULT_VIEW_ROLE_ID],   # roles that can see created channels
            rate_log=[],                    # download timestamps, last 24h
            flood_events=[],                # FloodWait timestamps, last 24h
            cooldown_until=0.0,
        )
        self._source_factory = source_factory
        self._source = None
        self._store: Optional[PostedStore] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._cycle_lock = asyncio.Lock()
        self._fails: dict = {}
        self._last_cycle: dict = {}
        self._last_cycle_ts = 0.0
        self._notified: set = set()
        self._data_dir = os.environ.get(constants.ENV_DATA_DIR, constants.DEFAULT_DATA_DIR)

    # -- Lifecycle -------------------------------------------------------------------

    async def cog_load(self) -> None:
        tmp = os.path.join(self._data_dir, constants.TMP_DIR_NAME)
        try:
            os.makedirs(tmp, mode=0o700, exist_ok=True)
            for name in os.listdir(tmp):      # leftovers from a crash: videos must not linger
                try:
                    os.remove(os.path.join(tmp, name))
                except OSError:
                    pass
            self._store = PostedStore(os.path.join(self._data_dir, constants.POSTED_DB_NAME))
            await asyncio.to_thread(self._store.prune, time.time() - constants.POSTED_RETENTION_DAYS * 86400)
        except OSError:
            log.exception("tgfeed: cannot use the data dir %s; the feed cannot run", self._data_dir)
        self.bot.add_view(FeedPostView(self))
        self._poll_task = self.bot.loop.create_task(self._poll_loop())

    def cog_unload(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        if self._source is not None:
            self.bot.loop.create_task(self._source.disconnect())

    @property
    def _tmpdir(self) -> str:
        return os.path.join(self._data_dir, constants.TMP_DIR_NAME)

    def _get_source(self):
        if self._source is not None:
            return self._source
        if self._source_factory is not None:
            self._source = self._source_factory()
            return self._source
        api_id = os.environ.get(constants.ENV_API_ID, "").strip()
        api_hash = os.environ.get(constants.ENV_API_HASH, "").strip()
        if not api_id.isdigit() or not api_hash:
            raise SourceAuthError(f"{constants.ENV_API_ID} / {constants.ENV_API_HASH} are not set in the red container's environment.")
        self._source = TelethonSource(int(api_id), api_hash, os.path.join(self._data_dir, constants.SESSION_BASENAME))
        return self._source

    # -- Poll loop -------------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(random.uniform(*constants.STARTUP_DELAY_RANGE))
        while True:
            try:
                async with self._cycle_lock:
                    await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- never let the loop die silently
                log.exception("tgfeed poll cycle crashed; will retry next interval")
            interval = await self.config.poll_interval_seconds()
            await asyncio.sleep(engine.jittered(max(interval, constants.MIN_POLL_INTERVAL_SECONDS), random))

    async def _run_cycle(self) -> None:
        now = time.time()
        if await self.config.paused() or await self.config.cooldown_until() > now or self._store is None:
            return
        group_raw = await self.config.group()
        if not group_raw:
            return
        raw_maps = await self.config.mappings()
        active = [m for m in (TopicMapping.from_dict(r) for r in raw_maps.values()) if not m.paused and m.channel_id]
        if not active:
            return
        group = GroupInfo(**group_raw)
        totals = TopicStats()
        try:
            source = self._get_source()
            await source.connect()
            items, scan_max = await source.fetch_new(group, min(m.cursor for m in active))
            by_topic: dict = defaultdict(list)
            for item in items:
                by_topic[item.topic_id].append(item)

            per_hour, per_day = await self.config.max_per_hour(), await self.config.max_per_day()
            stamps = engine.prune_rate_log(await self.config.rate_log(), now)
            gap = (await self.config.file_gap_min(), await self.config.file_gap_max())
            x_button = await self.config.x_button()

            def reserve(n: int) -> bool:
                t = time.time()
                available = engine.slots_available(stamps, t, per_hour, per_day)
                if not engine.can_start_unit(available, n, per_hour):
                    return False
                stamps.extend([t] * n)
                return True

            random.shuffle(active)
            for index, mapping in enumerate(active):
                channel = self.bot.get_channel(mapping.channel_id)
                if channel is None:
                    await self._save_poll_fields(mapping, TopicStats(), error="destination channel not found")
                    continue
                if index:
                    await asyncio.sleep(random.uniform(*constants.TOPIC_STAGGER_RANGE))
                stats = TopicStats()
                deps = self._make_deps(source, group, channel, mapping, gap, reserve, x_button)
                stop = None
                try:
                    stop = await pipeline.process_topic(mapping, by_topic.get(mapping.topic_id, []), scan_max, deps, stats)
                finally:
                    totals.add(stats)
                    await self.config.rate_log.set(stamps)
                    await self._save_poll_fields(mapping, stats)
                if stop == "rate_cap":
                    break
        except SourceFlood as flood:
            await self._handle_flood(flood.seconds)
        except SourceAuthError as exc:
            await self._handle_auth(str(exc))
        except SourceError as exc:
            log.warning("tgfeed: Telegram error this cycle: %s", exc)
            self._last_cycle = {"error": str(exc)}
        else:
            self._last_cycle = {k: getattr(totals, k) for k in totals.__dataclass_fields__}
        self._last_cycle_ts = time.time()

    def _make_deps(self, source, group, channel, mapping, gap, reserve, x_button) -> pipeline.Deps:
        limit = engine.upload_limit(getattr(getattr(channel, "guild", None), "filesize_limit", None))

        async def send_batch(topic_id: int, files: list) -> int:
            discord_files = [
                discord.File(io.BytesIO(f.data), filename=f.name) if f.data is not None else discord.File(f.path, filename=f.name)
                for f in files
            ]
            message = await channel.send(
                files=discord_files, view=FeedPostView(self) if x_button else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return message.id

        async def record(discord_id: int, topic_id: int, tg_ids: list) -> None:
            await asyncio.to_thread(self._store.add, discord_id, channel.id, topic_id, tg_ids)

        async def shrink(src: str, dst: str, item, limit_bytes: int):
            return await transcode.shrink(src, dst, item.duration, item.height, limit_bytes)

        return pipeline.Deps(
            source=source, group=group, limit_bytes=limit, tmpdir=self._tmpdir, gap=gap, rng=random,
            sleep=asyncio.sleep, now=time.time, reserve=reserve, send_batch=send_batch, record=record,
            shrink=shrink, fails=self._fails,
        )

    async def _save_poll_fields(self, mapping: TopicMapping, stats: TopicStats, error: Optional[str] = None) -> None:
        """Merge only the fields the poll owns, so a `pausetopic`/`unmap` made while the
        cycle was running is not undone by this write."""
        async with self.config.mappings() as stored:
            raw = stored.get(str(mapping.topic_id))
            if raw is None:
                return
            raw["cursor"] = max(int(raw.get("cursor") or 0), mapping.cursor)
            raw["last_post_ts"] = max(float(raw.get("last_post_ts") or 0.0), mapping.last_post_ts)
            raw["last_error"] = error if error else mapping.last_error
            raw["posted_total"] = int(raw.get("posted_total") or 0) + stats.posted_files

    # -- Telegram trouble ---------------------------------------------------------------------

    async def _handle_flood(self, seconds: int) -> None:
        now = time.time()
        events = engine.prune_rate_log(await self.config.flood_events(), now, constants.FLOOD_WINDOW_SECONDS) + [now]
        await self.config.flood_events.set(events)
        await self.config.cooldown_until.set(now + engine.flood_sleep_seconds(seconds))
        log.warning("tgfeed: Telegram flood wait of %ss; cooling down", seconds)
        if engine.should_pause_after_flood(events, now):
            await self.config.paused.set(True)
            await self.config.pause_reason.set("Telegram rate-limited the account repeatedly")
            await self._log("⛔ tgfeed: Telegram rate-limited the account 3 times in 24h, so the feed is **paused** to protect it. "
                            "Lower `.tgfeed limits` / raise `.tgfeed interval`, then `.tgfeed resume`.")
        elif seconds >= constants.FLOOD_NOTIFY_THRESHOLD:
            await self._log(f"⚠️ tgfeed: Telegram asked us to wait {seconds}s; backing off until then.")

    async def _handle_auth(self, reason: str) -> None:
        await self.config.paused.set(True)
        await self.config.pause_reason.set(reason)
        if self._source is not None:
            await self._source.disconnect()
            self._source = None
        self._last_cycle = {"error": reason}
        if reason not in self._notified:
            self._notified.add(reason)
            await self._log(f"⛔ tgfeed paused: {reason} Fix it, then `.tgfeed resume`.")

    async def _log(self, text: str) -> None:
        channel_id = await self.config.log_channel_id()
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            log.warning("tgfeed: could not write to the log channel")

    async def _is_moderator(self, user) -> bool:
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and perms.manage_guild:
            return True
        try:
            return bool(await self.bot.is_mod(user))
        except Exception:  # noqa: BLE001 -- unknown means not allowed
            return False

    def _link_for(self, group_raw: dict, topic_id: int, msg_id: int) -> str:
        return engine.message_link(group_raw.get("username"), int(group_raw.get("id") or 0), topic_id, msg_id)

    # -- X button --------------------------------------------------------------------------------

    async def handle_feed_x(self, interaction) -> None:
        """Mod-only delete under a mirrored post; the Telegram origin goes to the mod log only."""
        if not await self._is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can remove feed posts.", ephemeral=True)
            return
        info = await asyncio.to_thread(self._store.get, interaction.message.id) if self._store else None
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(f"I couldn't delete it: {exc}", ephemeral=True)
            return
        await interaction.response.send_message("Removed.", ephemeral=True)
        await self._log_removal(interaction.user.display_name, interaction.message.channel.id, info)

    async def _log_removal(self, who: str, channel_id: int, info: Optional[dict]) -> None:
        origin = "origin unknown"
        if info:
            group_raw = await self.config.group()
            origin = " ".join(self._link_for(group_raw, info["topic_id"], i) for i in info["tg_ids"][:3])
        await self._log(f"✖️ {who} removed a feed post in <#{channel_id}> ({origin}).")

    # -- Command helpers ---------------------------------------------------------------------------

    async def _connected_source(self, ctx):
        """The live source, or None after telling the mod what is wrong."""
        try:
            source = self._get_source()
            await source.connect()
            return source
        except SourceError as exc:
            await ctx.send(f"Telegram isn't reachable: {exc}")
            return None

    async def _group_or_say(self, ctx) -> Optional[GroupInfo]:
        raw = await self.config.group()
        if not raw:
            await ctx.send("No group set. Use `.tgfeed group <invite link>` first.")
            return None
        return GroupInfo(**raw)

    async def _find_mapped(self, arg: str):
        """Mapping by topic id or exact title (no Telegram call)."""
        maps = [TopicMapping.from_dict(r) for r in (await self.config.mappings()).values()]
        text = (arg or "").strip()
        if text.isdigit():
            return next((m for m in maps if m.topic_id == int(text)), None)
        hits = [m for m in maps if m.title.strip().lower() == text.lower()]
        return hits[0] if len(hits) == 1 else None

    # -- Command tree --------------------------------------------------------------------------------

    @commands.group(name="tgfeed", invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    async def tgfeed(self, ctx: commands.Context) -> None:
        """Mirror a Telegram forum group's photos and videos into Discord."""
        await ctx.send_help(ctx.command)

    @tgfeed.command(name="version")
    async def tgfeed_version(self, ctx: commands.Context) -> None:
        """Version-probe command -- confirms a deploy actually took."""
        await ctx.send(f"tgfeed build: {constants.BUILD}")

    @tgfeed.command(name="group")
    async def tgfeed_group(self, ctx: commands.Context, link: str) -> None:
        """Set the Telegram group (an invite or t.me link of a group this account is already in)."""
        source = await self._connected_source(ctx)
        if source is None:
            return
        try:
            info = await source.resolve_group(link)
        except SourceFlood as flood:
            await self._handle_flood(flood.seconds)
            await ctx.send(f"Telegram asked us to wait {flood.seconds}s. Try again later.")
            return
        except SourceError as exc:
            await ctx.send(str(exc))
            return
        if not info.is_forum:
            await ctx.send(f"**{info.title}** isn't a forum-style group (it has no topics), so there is nothing to split into channels.")
            return
        current = await self.config.group()
        if current and current.get("id") != info.id and await self.config.mappings():
            await ctx.send("Topics from another group are still mapped. `.tgfeed unmap` them first.")
            return
        await self.config.group.set({"id": info.id, "title": info.title, "username": info.username, "is_forum": True})
        try:
            topics = await source.list_topics(info)
        except SourceError:
            topics = []
        await ctx.send(f"Group set: **{info.title}**, {len(topics)} topics. Next: `.tgfeed category <category>`, `.tgfeed topics`, `.tgfeed mapall`.")

    @tgfeed.command(name="topics")
    async def tgfeed_topics(self, ctx: commands.Context) -> None:
        """List the group's topics and where each one goes."""
        group = await self._group_or_say(ctx)
        source = await self._connected_source(ctx) if group else None
        if source is None:
            return
        try:
            topics = await source.list_topics(group)
        except SourceFlood as flood:
            await self._handle_flood(flood.seconds)
            await ctx.send(f"Telegram asked us to wait {flood.seconds}s. Try again later.")
            return
        except SourceError as exc:
            await ctx.send(str(exc))
            return
        maps = await self.config.mappings()
        lines = []
        for topic in topics:
            raw = maps.get(str(topic.id))
            where = f"<#{raw['channel_id']}>" + (" (paused)" if raw.get("paused") else "") if raw else "not mapped"
            lines.append(f"`{topic.id}` {topic.title} → {where}")
        for chunk in engine.chunk_lines(lines or ["No topics found."]):
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    @tgfeed.command(name="map")
    async def tgfeed_map(self, ctx: commands.Context, topic: str, channel: discord.TextChannel) -> None:
        """Map one topic (id or exact title, quote titles with spaces) to an existing channel."""
        group = await self._group_or_say(ctx)
        source = await self._connected_source(ctx) if group else None
        if source is None:
            return
        try:
            topics = await source.list_topics(group)
            found, reason = engine.resolve_topic(topic, topics)
            if found is None:
                await ctx.send(reason)
                return
            for raw in (await self.config.mappings()).values():
                if raw["channel_id"] == channel.id and raw["topic_id"] != found.id:
                    await ctx.send(f"{channel.mention} already receives another topic. One topic per channel.")
                    return
            cursor = await source.latest_id(group)
        except SourceFlood as flood:
            await self._handle_flood(flood.seconds)
            await ctx.send(f"Telegram asked us to wait {flood.seconds}s. Try again later.")
            return
        except SourceError as exc:
            await ctx.send(str(exc))
            return
        mapping = TopicMapping(topic_id=found.id, title=found.title, channel_id=channel.id, cursor=cursor, added_ts=time.time())
        async with self.config.mappings() as stored:
            stored[str(found.id)] = mapping.to_dict()
        await ctx.send(f"Mapped **{found.title}** → {channel.mention}. Only new posts from now on.")

    @tgfeed.command(name="mapall")
    async def tgfeed_mapall(self, ctx: commands.Context, confirm: str = "") -> None:
        """Create one hidden channel per unmapped topic. Without `yes` it only shows the plan."""
        group = await self._group_or_say(ctx)
        source = await self._connected_source(ctx) if group else None
        if source is None:
            return
        category_id = await self.config.category_id()
        category = ctx.guild.get_channel(category_id) if category_id else None
        if category is None:
            await ctx.send("Set the category first: `.tgfeed category <category>`.")
            return
        try:
            topics = await source.list_topics(group)
        except SourceFlood as flood:
            await self._handle_flood(flood.seconds)
            await ctx.send(f"Telegram asked us to wait {flood.seconds}s. Try again later.")
            return
        except SourceError as exc:
            await ctx.send(str(exc))
            return
        maps = await self.config.mappings()
        todo = [t for t in topics if str(t.id) not in maps]
        if not todo:
            await ctx.send("Every topic is already mapped.")
            return
        prefix = await self.config.name_prefix()
        plan = [(t, engine.slugify_channel_name(t.title, prefix, t.id)) for t in todo]
        if confirm.lower() != "yes":
            lines = [f"`{t.id}` {t.title} → #{name}" for t, name in plan]
            lines.append(f"\nThis would create {len(plan)} channels in **{category.name}**, hidden from @everyone. Run `.tgfeed mapall yes` to do it.")
            for chunk in engine.chunk_lines(lines):
                await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            return
        if len(plan) > 50:
            await ctx.send(f"That's {len(plan)} channels; I'll do the first 50 now. Run it again for the rest.")
            plan = plan[:50]
        try:
            cursor = await source.latest_id(group)
        except SourceError as exc:
            await ctx.send(str(exc))
            return
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        }
        for role_id in await self.config.view_role_ids():
            role = ctx.guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
        created = 0
        async with ctx.typing():
            for topic, name in plan:
                try:
                    channel = await ctx.guild.create_text_channel(
                        name, category=category, overwrites=overwrites, reason=f"tgfeed: Telegram topic {topic.id}",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    await ctx.send(f"Stopped at **{topic.title}**: I couldn't create the channel ({exc}). Created {created} so far.")
                    break
                mapping = TopicMapping(topic_id=topic.id, title=topic.title, channel_id=channel.id, cursor=cursor, added_ts=time.time())
                async with self.config.mappings() as stored:
                    stored[str(topic.id)] = mapping.to_dict()
                created += 1
                await asyncio.sleep(2)
        await ctx.send(f"Created and mapped {created} channel(s). They're hidden from @everyone; only new posts will appear.")

    @tgfeed.command(name="unmap")
    async def tgfeed_unmap(self, ctx: commands.Context, topic: str) -> None:
        """Stop mirroring a topic (the Discord channel is left alone)."""
        mapping = await self._find_mapped(topic)
        if mapping is None:
            await ctx.send("That topic isn't mapped.")
            return
        async with self.config.mappings() as stored:
            stored.pop(str(mapping.topic_id), None)
        await ctx.send(f"Unmapped **{mapping.title}**. Its channel <#{mapping.channel_id}> is untouched.")

    @tgfeed.command(name="pausetopic")
    async def tgfeed_pausetopic(self, ctx: commands.Context, topic: str) -> None:
        """Pause one topic."""
        mapping = await self._find_mapped(topic)
        if mapping is None:
            await ctx.send("That topic isn't mapped.")
            return
        async with self.config.mappings() as stored:
            stored[str(mapping.topic_id)]["paused"] = True
        await ctx.send(f"Paused **{mapping.title}**.")

    @tgfeed.command(name="resumetopic")
    async def tgfeed_resumetopic(self, ctx: commands.Context, topic: str) -> None:
        """Resume one topic from now (posts made while it was paused are not backfilled)."""
        mapping = await self._find_mapped(topic)
        group = await self._group_or_say(ctx) if mapping else None
        if mapping is None:
            await ctx.send("That topic isn't mapped.")
            return
        source = await self._connected_source(ctx) if group else None
        if source is None:
            return
        try:
            cursor = await source.latest_id(group)
        except SourceError as exc:
            await ctx.send(str(exc))
            return
        async with self.config.mappings() as stored:
            stored[str(mapping.topic_id)]["paused"] = False
            stored[str(mapping.topic_id)]["cursor"] = cursor
        await ctx.send(f"Resumed **{mapping.title}** from now.")

    @tgfeed.command(name="pause")
    async def tgfeed_pause(self, ctx: commands.Context) -> None:
        """Pause everything."""
        await self.config.paused.set(True)
        await self.config.pause_reason.set("paused by a moderator")
        await ctx.send("tgfeed paused. `.tgfeed resume` to continue.")

    @tgfeed.command(name="resume")
    async def tgfeed_resume(self, ctx: commands.Context, mode: str = "") -> None:
        """Resume everything. `.tgfeed resume skip` also jumps every topic to 'now' (no catch-up of what was missed)."""
        if mode.lower() == "skip":
            group = await self._group_or_say(ctx)
            source = await self._connected_source(ctx) if group else None
            if source is None:
                return
            try:
                cursor = await source.latest_id(group)
            except SourceError as exc:
                await ctx.send(str(exc))
                return
            async with self.config.mappings() as stored:
                for raw in stored.values():
                    raw["cursor"] = max(int(raw.get("cursor") or 0), cursor)
        await self.config.paused.set(False)
        await self.config.pause_reason.set(None)
        await self.config.cooldown_until.set(0.0)
        self._notified.clear()
        await ctx.send("tgfeed resumed." + (" Skipped everything posted while it was off." if mode.lower() == "skip" else ""))

    @tgfeed.command(name="list")
    async def tgfeed_list(self, ctx: commands.Context) -> None:
        """Mapped topics."""
        maps = [TopicMapping.from_dict(r) for r in (await self.config.mappings()).values()]
        if not maps:
            await ctx.send("Nothing mapped yet.")
            return
        lines = []
        for m in sorted(maps, key=lambda m: m.topic_id):
            last = f"<t:{int(m.last_post_ts)}:R>" if m.last_post_ts else "never"
            flag = " (paused)" if m.paused else ""
            err = f" ⚠ {m.last_error}" if m.last_error else ""
            lines.append(f"`{m.topic_id}` {m.title} → <#{m.channel_id}>{flag}: {m.posted_total} files, last {last}{err}")
        for chunk in engine.chunk_lines(lines):
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    @tgfeed.command(name="status")
    async def tgfeed_status(self, ctx: commands.Context) -> None:
        """Connection, throttle and last-cycle summary."""
        now = time.time()
        group = await self.config.group()
        stamps = await self.config.rate_log()
        per_hour, per_day = await self.config.max_per_hour(), await self.config.max_per_day()
        used_h = sum(1 for s in stamps if now - s < 3600)
        used_d = sum(1 for s in stamps if now - s < 86400)
        cooldown = await self.config.cooldown_until()
        session = os.path.exists(os.path.join(self._data_dir, constants.SESSION_BASENAME + ".session"))
        env_ok = bool(os.environ.get(constants.ENV_API_ID, "").strip().isdigit() and os.environ.get(constants.ENV_API_HASH, "").strip())
        lines = [
            f"Group: {group.get('title') if group else '(not set)'}",
            f"State: {'PAUSED (' + str(await self.config.pause_reason()) + ')' if await self.config.paused() else 'running'}"
            + (f", cooling down until <t:{int(cooldown)}:R>" if cooldown > now else ""),
            f"Storage: {'ok' if self._store is not None else 'NOT USABLE (check the data dir)'}",
            f"Credentials in env: {'yes' if env_ok else 'NO'} | session file: {'yes' if session else 'NO (run login.py)'} | ffmpeg: {'yes' if shutil.which('ffmpeg') else 'NO'}",
            f"Poll every ~{await self.config.poll_interval_seconds()}s (±{int(constants.POLL_JITTER_FRACTION * 100)}%), file gap {await self.config.file_gap_min()}-{await self.config.file_gap_max()}s",
            f"Downloads: {used_h}/{per_hour} this hour, {used_d}/{per_day} today",
            f"Topics mapped: {len(await self.config.mappings())} | X button: {'on' if await self.config.x_button() else 'off'}",
        ]
        if self._last_cycle:
            ago = f" (<t:{int(self._last_cycle_ts)}:R>)" if self._last_cycle_ts else ""
            if "error" in self._last_cycle:
                lines.append(f"Last cycle{ago}: error: {self._last_cycle['error']}")
            else:
                c = self._last_cycle
                lines.append(
                    f"Last cycle{ago}: posted {c['posted_files']} files in {c['posted_messages']} messages, shrunk {c['shrunk']}, "
                    f"skipped {c['skipped_oversize']} too big / {c['skipped_too_long']} unshrinkable / {c['skipped_failed']} failed, errors {c['errors']}"
                )
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @tgfeed.command(name="interval")
    async def tgfeed_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Seconds between polls (jittered, minimum 120)."""
        seconds = max(constants.MIN_POLL_INTERVAL_SECONDS, seconds)
        await self.config.poll_interval_seconds.set(seconds)
        await ctx.send(f"Polling about every {seconds}s.")

    @tgfeed.command(name="gap")
    async def tgfeed_gap(self, ctx: commands.Context, low: float, high: float) -> None:
        """Random pause range (seconds) between two downloads."""
        low = max(constants.MIN_FILE_GAP_SECONDS, low)
        high = max(low, high)
        await self.config.file_gap_min.set(low)
        await self.config.file_gap_max.set(high)
        await ctx.send(f"Downloads are spaced {low}-{high}s apart.")

    @tgfeed.command(name="limits")
    async def tgfeed_limits(self, ctx: commands.Context, per_hour: int, per_day: int) -> None:
        """Download caps per rolling hour and day (hour cap minimum 10)."""
        per_hour = max(constants.MIN_FILES_PER_HOUR, per_hour)
        per_day = max(per_hour, per_day)
        await self.config.max_per_hour.set(per_hour)
        await self.config.max_per_day.set(per_day)
        await ctx.send(f"Caps: {per_hour} files/hour, {per_day} files/day.")

    @tgfeed.command(name="xbutton")
    async def tgfeed_xbutton(self, ctx: commands.Context, state: str) -> None:
        """Turn the mod-only X under posts on or off."""
        if state.lower() not in ("on", "off"):
            await ctx.send("Say `on` or `off`.")
            return
        await self.config.x_button.set(state.lower() == "on")
        await ctx.send(f"X button {state.lower()} for new posts.")

    @tgfeed.command(name="logchannel")
    async def tgfeed_logchannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Where removals and Telegram warnings are logged."""
        await self.config.log_channel_id.set(channel.id)
        await ctx.send(f"Logging to {channel.mention}.")

    @tgfeed.command(name="category")
    async def tgfeed_category(self, ctx: commands.Context, category: discord.CategoryChannel) -> None:
        """Category where `mapall` creates channels."""
        await self.config.category_id.set(category.id)
        await ctx.send(f"`mapall` will create channels in **{category.name}**.")

    @tgfeed.command(name="prefix")
    async def tgfeed_prefix(self, ctx: commands.Context, *, prefix: str = "") -> None:
        """Text put in front of channel names `mapall` creates (empty clears it)."""
        await self.config.name_prefix.set(prefix.strip())
        await ctx.send(f"Prefix: `{prefix.strip()}`" if prefix.strip() else "Prefix cleared.")

    @tgfeed.command(name="viewrole")
    async def tgfeed_viewrole(self, ctx: commands.Context, *roles: discord.Role) -> None:
        """Roles that can see channels `mapall` creates (replaces the list). Default: the Mod role."""
        if roles:
            await self.config.view_role_ids.set([r.id for r in roles])
        current = await self.config.view_role_ids()
        await ctx.send("Created channels are visible to: " + (", ".join(f"<@&{i}>" for i in current) or "nobody but admins"),
                       allowed_mentions=discord.AllowedMentions.none())

    @tgfeed.command(name="trace")
    async def tgfeed_trace(self, ctx: commands.Context, message: discord.Message) -> None:
        """Show where a mirrored post came from (Telegram link). Mod-only, nothing is posted publicly."""
        info = await asyncio.to_thread(self._store.get, message.id) if self._store else None
        if info is None:
            await ctx.send("That message isn't in the takedown map (not a tgfeed post, or older than 180 days).")
            return
        group_raw = await self.config.group()
        links = " ".join(self._link_for(group_raw, info["topic_id"], i) for i in info["tg_ids"][:5])
        await ctx.send(f"Topic `{info['topic_id']}`, posted <t:{int(info['ts'])}:R>: {links}")

    @tgfeed.command(name="takedown")
    async def tgfeed_takedown(self, ctx: commands.Context, message: discord.Message) -> None:
        """Delete a mirrored post by its Discord link (for when someone asks for it to come down)."""
        info = await asyncio.to_thread(self._store.get, message.id) if self._store else None
        if info is None:
            await ctx.send("That message isn't in the takedown map, so I won't delete it.")
            return
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(f"I couldn't delete it: {exc}")
            return
        await asyncio.to_thread(self._store.delete, message.id)
        await ctx.send("Removed.")
        await self._log_removal(ctx.author.display_name, info["channel_id"], info)
