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

from . import constants, engine, panel as panel_specs, pipeline, transcode
from .models import GroupInfo, TopicMapping, TopicStats
from .settings_panel import PanelError, open_panel
from .source import SourceAuthError, SourceError, SourceFlood, TelethonSource
from .views import FeedPostView

log = logging.getLogger("red.tgfeed")


class TGFeed(commands.Cog):
    """Telegram forum group -> Discord channels, media only."""

    _topics_cache = None   # (timestamp, [TopicInfo]) from the last explicit "Load Telegram topics"

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
            category_id=None,               # where `map` creates a channel when none is given
            name_prefix=constants.DEFAULT_CHANNEL_PREFIX,
            view_role_ids=[],               # extra roles that can see channels `map` creates (default: nobody but the creator)
            rate_log=[],                    # download timestamps, last 24h
            flood_events=[],                # FloodWait timestamps, last 24h
            cooldown_until=0.0,
        )
        self._source_factory = source_factory
        self._source = None
        self._storage_ok = False
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
            self._storage_ok = True
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
        if await self.config.paused() or await self.config.cooldown_until() > now or not self._storage_ok:
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

        async def shrink(src: str, dst: str, item, limit_bytes: int):
            return await transcode.shrink(src, dst, item.duration, item.height, limit_bytes)

        return pipeline.Deps(
            source=source, group=group, limit_bytes=limit, tmpdir=self._tmpdir, gap=gap, rng=random,
            sleep=asyncio.sleep, now=time.time, reserve=reserve, send_batch=send_batch,
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
                            "Lower the caps / raise the poll interval in `.tgfeed` (Pace & posts), then turn the feed back on in Connection.")
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
            await self._log(f"⛔ tgfeed paused: {reason} Fix it, then turn the feed back on in `.tgfeed` (Connection).")

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

    # -- X button --------------------------------------------------------------------------------

    async def handle_feed_x(self, interaction) -> None:
        """Mod-only delete under a mirrored post; the removal is logged to the log channel."""
        if not await self._is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can remove feed posts.", ephemeral=True)
            return
        channel_id = interaction.message.channel.id
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(f"I couldn't delete it: {exc}", ephemeral=True)
            return
        await interaction.response.send_message("Removed.", ephemeral=True)
        await self._log(f"\u2716\ufe0f {interaction.user.display_name} removed a feed post in <#{channel_id}>.")

    # -- Telegram helpers (they raise PanelError, which the panel shows to the admin) ---------------

    async def _connected_source(self):
        try:
            source = self._get_source()
            await source.connect()
            return source
        except SourceError as exc:
            raise PanelError(f"Telegram isn't reachable: {exc}")

    async def _telegram(self, call):
        """Await `call()` (a source call), turning Telegram trouble into a PanelError."""
        try:
            return await call()
        except SourceFlood as flood:
            await self._handle_flood(flood.seconds)
            raise PanelError(f"Telegram asked us to wait {flood.seconds}s. Try again later.")
        except SourceError as exc:
            raise PanelError(str(exc))

    async def _group_or_raise(self) -> GroupInfo:
        raw = await self.config.group()
        if not raw:
            raise PanelError("No group set. Set the Telegram group in the Connection tab first.")
        return GroupInfo(**raw)

    async def _find_mapped(self, arg: str):
        """Mapping by topic id or exact title (no Telegram call)."""
        maps = [TopicMapping.from_dict(r) for r in (await self.config.mappings()).values()]
        text = (arg or "").strip()
        if text.isdigit():
            return next((m for m in maps if m.topic_id == int(text)), None)
        hits = [m for m in maps if m.title.strip().lower() == text.lower()]
        return hits[0] if len(hits) == 1 else None

    async def _mapped_or_raise(self, arg: str) -> TopicMapping:
        mapping = await self._find_mapped(arg)
        if mapping is None:
            raise PanelError("That topic isn't mapped.")
        return mapping

    # -- Changes the panel makes (each returns the message to show, or raises PanelError) ---------------

    async def _apply_group(self, guild, member, link: str) -> str:
        """Set the Telegram group (an invite or t.me link of a group this account is already in)."""
        link = (link or "").strip()
        if not link:
            raise PanelError("Paste an invite or t.me link of a group this Telegram account is already in.")
        source = await self._connected_source()
        info = await self._telegram(lambda: source.resolve_group(link))
        if not info.is_forum:
            raise PanelError(f"**{info.title}** isn't a forum-style group (it has no topics), so there is nothing to split into channels.")
        current = await self.config.group()
        if current and current.get("id") != info.id and await self.config.mappings():
            raise PanelError("Topics from another group are still mapped. Unmap them first (Topics tab).")
        await self.config.group.set({"id": info.id, "title": info.title, "username": info.username, "is_forum": True})
        self._topics_cache = None
        try:
            topics = await source.list_topics(info)
        except SourceError:
            topics = []
        return f"Group set: **{info.title}**, {len(topics)} topics. Next: pick a category here, then map topics in the Topics tab."

    async def _apply_category(self, guild, member, category) -> str:
        """Category where new topic channels are created (None clears it)."""
        if category is None:
            await self.config.category_id.set(None)
            return "No category set: map topics to an existing channel, or set a category first."
        await self.config.category_id.set(category.id)
        return f"New topic channels will be created in {getattr(category, 'name', None) or category.id}."

    async def _apply_prefix(self, guild, member, prefix: str) -> str:
        """Text put in front of the names of channels created for topics (empty goes back to the default)."""
        prefix = (prefix or "").strip() or constants.DEFAULT_CHANNEL_PREFIX
        await self.config.name_prefix.set(prefix)
        return f"Prefix: `{prefix}`"

    async def _apply_view_roles(self, guild, member, roles) -> str:
        """Extra roles that can see channels created from now on (empty = just the person who mapped them)."""
        ids = [getattr(r, "id", r) for r in (roles or [])]
        await self.config.view_role_ids.set(ids)
        return ("New topic channels will also be visible to: " + ", ".join(f"<@&{i}>" for i in ids) + ". Existing channels keep their permissions."
                if ids else "New topic channels will be visible only to the person who maps them (and admins).")

    async def _apply_log_channel(self, guild, member, channel) -> str:
        """Where removals and Telegram warnings are logged."""
        if channel is None:
            raise PanelError("Pick a channel.")
        await self.config.log_channel_id.set(channel.id)
        return f"Logging to {getattr(channel, 'mention', channel.id)}."

    async def _apply_x_button(self, guild, member, on: bool) -> str:
        await self.config.x_button.set(bool(on))
        return f"X button {'on' if on else 'off'} for new posts."

    async def _apply_throttle(self, guild, member, values: dict) -> str:
        """Poll interval, the pause between downloads, and the hourly/daily download caps (all clamped to safe minimums)."""
        try:
            interval = int(float(values["interval"]))
            low, high = float(values["gap_min"]), float(values["gap_max"])
            per_hour, per_day = int(float(values["per_hour"])), int(float(values["per_day"]))
        except (KeyError, TypeError, ValueError):
            raise PanelError("Every box needs a plain number.")
        interval = max(constants.MIN_POLL_INTERVAL_SECONDS, interval)
        low = max(constants.MIN_FILE_GAP_SECONDS, low)
        high = max(low, high)
        per_hour = max(constants.MIN_FILES_PER_HOUR, per_hour)
        per_day = max(per_hour, per_day)
        await self.config.poll_interval_seconds.set(interval)
        await self.config.file_gap_min.set(low)
        await self.config.file_gap_max.set(high)
        await self.config.max_per_hour.set(per_hour)
        await self.config.max_per_day.set(per_day)
        return (f"Polling about every {interval}s, downloads {low:g}-{high:g}s apart, "
                f"caps {per_hour} files/hour and {per_day}/day.")

    async def _apply_running(self, guild, member, on: bool) -> str:
        """On = resume everything (no catch-up skipping), Off = pause everything."""
        if on:
            await self.config.paused.set(False)
            await self.config.pause_reason.set(None)
            await self.config.cooldown_until.set(0.0)
            self._notified.clear()
            return "tgfeed is running."
        await self.config.paused.set(True)
        await self.config.pause_reason.set("paused by a moderator")
        return "tgfeed paused."

    async def _apply_resume_skip(self, guild, member, _value=None) -> str:
        """Resume and jump every topic to 'now': nothing posted while the feed was off is caught up."""
        group = await self._group_or_raise()
        source = await self._connected_source()
        cursor = await self._telegram(lambda: source.latest_id(group))
        async with self.config.mappings() as stored:
            for raw in stored.values():
                raw["cursor"] = max(int(raw.get("cursor") or 0), cursor)
        await self._apply_running(guild, member, True)
        return "tgfeed is running. Skipped everything posted while it was off."

    async def _apply_load_topics(self, guild, member, _value=None) -> str:
        """Fetch the group's topics from Telegram so the Topics tab can list the unmapped ones too."""
        group = await self._group_or_raise()
        source = await self._connected_source()
        topics = await self._telegram(lambda: source.list_topics(group))
        self._topics_cache = (time.time(), list(topics))
        return f"Loaded {len(topics)} topics."

    async def _create_topic_channel(self, guild, member, topic):
        """A new text channel for one topic, visible only to the person who maps it
        (plus the bot, and any roles added under 'Roles that can see new channels')."""
        category_id = await self.config.category_id()
        category = guild.get_channel(category_id) if category_id else None
        if category is None:
            raise PanelError("Set a category first (Connection tab), or map this topic to an existing channel (Edit).")
        prefix = await self.config.name_prefix() or constants.DEFAULT_CHANNEL_PREFIX
        name = engine.slugify_channel_name(topic.title, prefix, topic.id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
            member: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
        }
        for role_id in await self.config.view_role_ids():
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
        try:
            return await guild.create_text_channel(
                name, category=category, overwrites=overwrites, reason=f"tgfeed: Telegram topic {topic.id}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise PanelError(f"I couldn't create the channel: {exc}")

    async def _apply_map(self, guild, member, topic: str, channel=None) -> str:
        """Map ONE topic (id or exact title). With a channel, use that existing channel (and re-point the topic if
        it is already mapped); without one, create a new channel in the category, visible only to you."""
        group = await self._group_or_raise()
        source = await self._connected_source()
        topics = await self._telegram(lambda: source.list_topics(group))
        found, reason = engine.resolve_topic(topic, topics)
        if found is None:
            raise PanelError(reason)
        maps = await self.config.mappings()
        existing = maps.get(str(found.id))
        if existing is not None and channel is None:
            raise PanelError(f"**{found.title}** is already mapped to <#{existing['channel_id']}>. Edit it to point at another channel, or Remove it first.")
        if channel is not None:
            for key, raw in maps.items():
                if raw["channel_id"] == channel.id and key != str(found.id):
                    raise PanelError(f"{getattr(channel, 'mention', channel.id)} already receives another topic. One topic per channel.")
            if existing is not None:
                async with self.config.mappings() as stored:
                    if str(found.id) in stored:
                        stored[str(found.id)]["channel_id"] = channel.id
                return f"**{found.title}** now goes to {getattr(channel, 'mention', channel.id)}."
        cursor = await self._telegram(lambda: source.latest_id(group))
        if channel is None:
            channel = await self._create_topic_channel(guild, member, found)
        mapping = TopicMapping(topic_id=found.id, title=found.title, channel_id=channel.id, cursor=cursor, added_ts=time.time())
        async with self.config.mappings() as stored:
            stored[str(found.id)] = mapping.to_dict()
        return f"Mapped **{found.title}** \u2192 {getattr(channel, 'mention', channel.id)}. Only new posts from now on."

    async def _apply_unmap(self, guild, member, topic: str) -> str:
        """Stop mirroring a topic (the Discord channel is left alone)."""
        mapping = await self._mapped_or_raise(topic)
        async with self.config.mappings() as stored:
            stored.pop(str(mapping.topic_id), None)
        return f"Unmapped **{mapping.title}**. Its channel <#{mapping.channel_id}> is untouched."

    async def _apply_pause_topic(self, guild, member, topic: str) -> str:
        mapping = await self._mapped_or_raise(topic)
        async with self.config.mappings() as stored:
            if str(mapping.topic_id) in stored:
                stored[str(mapping.topic_id)]["paused"] = True
        return f"Paused **{mapping.title}**."

    async def _apply_resume_topic(self, guild, member, topic: str) -> str:
        """Resume one topic from now (posts made while it was paused are not backfilled)."""
        mapping = await self._mapped_or_raise(topic)
        group = await self._group_or_raise()
        source = await self._connected_source()
        cursor = await self._telegram(lambda: source.latest_id(group))
        async with self.config.mappings() as stored:
            if str(mapping.topic_id) in stored:
                stored[str(mapping.topic_id)]["paused"] = False
                stored[str(mapping.topic_id)]["cursor"] = cursor
        return f"Resumed **{mapping.title}** from now."

    # -- Commands --------------------------------------------------------------------------------------------

    @commands.group(name="tgfeed", invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    async def tgfeed(self, ctx: commands.Context) -> None:
        """Telegram feed control panel (admins). Mods get the help list: status, version."""
        perms = ctx.author.guild_permissions
        if perms.administrator or perms.manage_guild:
            await open_panel(ctx, self, panel_specs.build_specs(), title="Telegram feed", intro=panel_specs.INTRO)
            return
        await ctx.send_help(ctx.command)

    @tgfeed.command(name="version")
    async def tgfeed_version(self, ctx: commands.Context) -> None:
        """Version-probe command -- confirms a deploy actually took."""
        await ctx.send(f"tgfeed build: {constants.BUILD}")

    @tgfeed.command(name="status")
    async def tgfeed_status(self, ctx: commands.Context) -> None:
        """Connection, throttle, last-cycle summary and the mapped topics."""
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
            f"Storage: {'ok' if self._storage_ok else 'NOT USABLE (check the data dir)'}",
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
        mapped = await self._mapped_lines()
        if mapped:
            lines.append("")
            lines.extend(mapped)
        for chunk in engine.chunk_lines(lines):
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    async def _mapped_lines(self) -> list:
        maps = [TopicMapping.from_dict(r) for r in (await self.config.mappings()).values()]
        lines = []
        for m in sorted(maps, key=lambda m: m.topic_id):
            last = f"<t:{int(m.last_post_ts)}:R>" if m.last_post_ts else "never"
            flag = " (paused)" if m.paused else ""
            err = f" \u26a0 {m.last_error}" if m.last_error else ""
            lines.append(f"`{m.topic_id}` {m.title} \u2192 <#{m.channel_id}>{flag}: {m.posted_total} files, last {last}{err}")
        return lines
