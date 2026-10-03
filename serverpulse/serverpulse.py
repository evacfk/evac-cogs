"""serverpulse: when is the server chatty, and when is it quiet?

Tracks human chat activity per clock hour (counts only, never message content)
and turns it into daily / weekly / monthly reports, hour-of-day averages, a
weekday x hour heatmap, a best-window finder, anomaly flags, a live board and
scheduled digests. Command group is `.pulse` (alias `.activity`), mods only, and
only usable in the configured mod channel.

Logic lives in pure modules (tracker / storage / engine / backfill / export);
this file is the Discord wiring.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import backfill as bf
from . import embeds, engine, heatmap, models, storage
from . import export as export_mod
from .constants import (
    BEST_WINDOW_HOURS,
    BEST_WINDOW_TOP_N,
    BOARD_HISTORY_DAYS,
    BOARD_INTERVAL_MINUTES,
    CONFIG_IDENTIFIER,
    DEFAULT_GUILD,
    DEFAULT_WINDOW_DAYS,
    DIGEST_CHECK_MINUTES,
    FLUSH_INTERVAL_SECONDS,
    TIMEZONE,
    USER_RETENTION_DAYS,
    WEEKDAY_NAMES,
)
from .tracker import GuildTracker, should_count

try:
    from discord.ext import tasks
except ImportError:  # pragma: no cover - only hit under the dev stub
    tasks = None

log = logging.getLogger("red.serverpulse")

MAX_DAYS_ARG = 730
METRIC_ALIASES = {
    "msgs": "msgs", "messages": "msgs", "message": "msgs", "chat": "msgs",
    "chatters": "users", "users": "users", "people": "users",
    "peak": "peak", "concurrent": "peak", "online": "peak",
}
METRIC_LABELS = {
    "msgs": "avg messages per hour",
    "users": "avg distinct chatters per hour",
    "peak": "avg peak chatting at once (5 min)",
}


@dataclass
class _Prep:
    guild: discord.Guild
    now_ts: float
    today: date
    coverage_start: int | None
    st: dict

    @property
    def has_data(self) -> bool:
        return self.coverage_start is not None and self.now_ts >= self.coverage_start + 3600


class ServerPulse(commands.Cog):
    """Server activity analytics: busy hours, quiet hours, daily/weekly/monthly reports."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        # guilds serverpulse is allowed to track/report in. None = not decided yet
        # (seeded on first run from the guild that contains the mod channel).
        self.config.register_global(allowed_guild_ids=None)
        self.data_dir = cog_data_path(self)
        self._allowed: set[int] | None = None  # None = open until seeded, never silently stop tracking

        self._trackers: dict[int, GuildTracker] = {}
        self._cache: dict[int, dict] = {}
        self._flushed: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._init_locks: dict[int, asyncio.Lock] = {}
        self._backfill_tasks: dict[int, asyncio.Task] = {}
        self._cancel: set[int] = set()

        if tasks is not None:
            self._flush_loop.start()
            self._board_loop.start()
            self._digest_loop.start()

    def cog_unload(self):
        if tasks is not None:
            self._flush_loop.cancel()
            self._board_loop.cancel()
            self._digest_loop.cancel()
        for task in self._backfill_tasks.values():
            task.cancel()
        try:
            self._flush_all_sync()  # synchronous on purpose: a reload must not lose the last few minutes
        except Exception:
            log.exception("serverpulse: final flush on unload failed")

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild_dir in (self.data_dir / "days").glob("*"):
            if guild_dir.name.isdigit():
                await asyncio.to_thread(storage.delete_user, self.data_dir, int(guild_dir.name), user_id)

    # ------------------------------------------------------------------
    # Per-guild state
    # ------------------------------------------------------------------

    def _guild_allowed(self, guild_id: int) -> bool:
        allowed = self._allowed
        return allowed is None or guild_id in allowed

    def _apply_allowed(self, allowed: set[int]) -> None:
        """Adopt a new allowlist and drop in-memory state for guilds that fell out of it."""
        self._allowed = allowed
        for gid in [g for g in self._trackers if g not in allowed]:
            self._trackers.pop(gid, None)
            self._cache.pop(gid, None)
            self._flushed.pop(gid, None)

    async def _load_allowed_guilds(self) -> None:
        """Load the allowlist. On the very first run, seed it with the guild(s) that
        actually contain the configured mod channel (i.e. Wonderland), so the bot being
        in other servers doesn't make serverpulse track them or warn about them.
        Idempotent. If nothing can be seeded yet, stay open rather than stop tracking."""
        stored = await self.config.allowed_guild_ids()
        if stored is None:
            seeded = []
            for guild in self.bot.guilds:
                channel_id = await self.config.guild(guild).mod_channel_id()
                if channel_id and guild.get_channel_or_thread(channel_id) is not None:
                    seeded.append(guild.id)
            if not seeded:
                return
            await self.config.allowed_guild_ids.set(seeded)
            stored = seeded
        self._apply_allowed({int(g) for g in stored})

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def _ensure_guild(self, guild: discord.Guild) -> dict:
        """Load settings, create + hydrate the tracker. Cached after the first call."""
        cached = self._cache.get(guild.id)
        if cached is not None:
            return cached
        async with self._init_locks.setdefault(guild.id, asyncio.Lock()):
            cached = self._cache.get(guild.id)
            if cached is not None:
                return cached
            gconf = self.config.guild(guild)
            data = await gconf.all()
            now = time.time()
            if data["live_since"] is None:
                data["live_since"] = models.ceil_hour(now)
                await gconf.live_since.set(data["live_since"])
            if data["coverage_start"] is None:
                data["coverage_start"] = data["live_since"]
                await gconf.coverage_start.set(data["coverage_start"])
            # a digest enabled "now" must not fire for a period that ended before we existed
            nowl = models.local_dt(time.time())
            for name, due in (("digest_weekly", engine.weekly_due), ("digest_monthly", engine.monthly_due)):
                if not data[name].get("last"):
                    data[name]["last"] = due(nowl)[0]
                    await getattr(gconf, name).set(data[name])

            tracker = GuildTracker(floor_ts=data["live_since"])
            await asyncio.to_thread(self._hydrate, tracker, guild.id, now)
            self._trackers[guild.id] = tracker
            self._flushed[guild.id] = tracker.changes
            self._cache[guild.id] = self._settings_from(data)
            return self._cache[guild.id]

    @staticmethod
    def _settings_from(data: dict) -> dict:
        return {
            "ignored": {int(c) for c in data["ignored_channels"]},
            "exclude_commands": bool(data["exclude_commands"]),
            "mod_channel": data["mod_channel_id"],
            "mod_role": data["mod_role_id"],
            "live_since": data["live_since"],
            "coverage_start": data["coverage_start"],
        }

    async def _refresh_settings(self, guild: discord.Guild) -> dict:
        data = await self.config.guild(guild).all()
        self._cache[guild.id] = self._settings_from(data)
        return self._cache[guild.id]

    def _hydrate(self, tracker: GuildTracker, guild_id: int, now: float) -> None:
        """Restore the in-progress hour from disk (blocking; run in a thread)."""
        state = storage.load_open_state(self.data_dir, guild_id)
        persisted = {(s["date"], s["key"]): s for s in state.get("slots", [])}
        current = models.slot_for_ts(now)
        wanted = {(current.date, current.key)} | set(persisted)
        for date_key, key in wanted:
            info = next((i for i in models.day_slots(date_key) if i.key == key), None)
            if info is None or info.end_ts <= now:
                continue
            rec = storage.load_day(self.data_dir, guild_id, date_key)["h"].get(key)
            if not rec:
                continue
            p = persisted.get((date_key, key), {})
            tracker.hydrate_slot(info, rec, p.get("users", ()), p.get("ch_users"))

    # ------------------------------------------------------------------
    # Flushing
    # ------------------------------------------------------------------

    def _write_snap(self, guild_id: int, snap) -> None:
        storage.apply_snapshot(self.data_dir, guild_id, snap)
        storage.save_open_state(self.data_dir, guild_id, snap.open_state)

    async def _flush_guild(self, guild_id: int) -> None:
        tracker = self._trackers.get(guild_id)
        if tracker is None:
            return
        async with self._lock(guild_id):
            changes = tracker.changes
            if changes == self._flushed.get(guild_id) and not any(a.end_ts <= time.time() for a in tracker.accs.values()):
                return
            snap = tracker.snapshot(time.time())
            await asyncio.to_thread(self._write_snap, guild_id, snap)
            tracker.commit(snap)
            self._flushed[guild_id] = changes

    def _flush_all_sync(self) -> None:
        for guild_id, tracker in self._trackers.items():
            if tracker.changes == self._flushed.get(guild_id):
                continue
            snap = tracker.snapshot(time.time())
            self._write_snap(guild_id, snap)
            tracker.commit(snap)
            self._flushed[guild_id] = tracker.changes

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @staticmethod
    def _channel_key(channel) -> int:
        """Threads count toward their parent channel."""
        return getattr(channel, "parent_id", None) or channel.id

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id:
            return
        if not self._guild_allowed(guild.id):
            return
        st = self._cache.get(guild.id) or await self._ensure_guild(guild)
        ts = message.created_at.timestamp()
        if ts < st["live_since"]:
            return
        content = message.content or ""
        prefixes: list[str] = []
        if st["exclude_commands"] and content and not content[0].isalnum():
            prefixes = await self.bot.get_valid_prefixes(guild)
        channel_key = self._channel_key(message.channel)
        if not should_count(
            is_bot=False,
            webhook_id=None,
            type_name=message.type.name,
            content=content,
            channel_key=channel_key,
            ignored=st["ignored"],
            exclude_commands=st["exclude_commands"],
            prefixes=prefixes,
        ):
            return
        self._trackers[guild.id].record(ts, message.author.id, str(channel_key))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not self._guild_allowed(member.guild.id):
            return
        await self._ensure_guild(member.guild)
        self._trackers[member.guild.id].record_join(time.time())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or not self._guild_allowed(member.guild.id):
            return
        await self._ensure_guild(member.guild)
        self._trackers[member.guild.id].record_leave(time.time())

    # ------------------------------------------------------------------
    # Access control: mods only, mod channel only
    # ------------------------------------------------------------------

    async def _is_mod(self, member: discord.Member) -> bool:
        st = self._cache.get(member.guild.id) or await self._ensure_guild(member.guild)
        if st["mod_role"] and member.get_role(st["mod_role"]) is not None:
            return True
        if member.guild_permissions.administrator:
            return True
        try:
            return bool(await self.bot.is_mod(member))
        except Exception:
            return False

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        if not self._guild_allowed(ctx.guild.id):
            return False  # serverpulse is switched off for this server: stay silent
        if not await self._is_mod(ctx.author):
            return False  # silent for non-mods
        st = self._cache.get(ctx.guild.id) or await self._ensure_guild(ctx.guild)
        channel_id = st["mod_channel"]
        if channel_id and self._channel_key(ctx.channel) != channel_id and not await self.bot.is_owner(ctx.author):
            # Red's help menu also evaluates cog_check for every command; only speak up when a
            # .pulse command is genuinely being invoked, or `.help` would spam the channel.
            if getattr(ctx, "cog", None) is self:
                await ctx.send(f"`.pulse` only works in <#{channel_id}>.", delete_after=15)
            return False
        return True

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        """Delete the mod's `.pulse ...` message the moment it is accepted, so only the reply remains.

        Runs after the checks, so a refused command (wrong channel, not a mod) is left alone.
        Needs Manage Messages; without it the command message simply stays.
        """
        if getattr(ctx, "interaction", None) is not None or ctx.guild is None:
            return
        try:
            await ctx.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _ack(self, ctx: commands.Context) -> None:
        """Confirmation for commands with no other output (the command message is already gone)."""
        try:
            await ctx.send("\N{WHITE HEAVY CHECK MARK} Done.", delete_after=8)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    async def _prepare(self, guild: discord.Guild) -> _Prep:
        st = await self._ensure_guild(guild)
        await self._flush_guild(guild.id)
        now = time.time()
        return _Prep(guild, now, models.local_dt(now).date(), st["coverage_start"], st)

    async def _days(self, guild_id: int, start: date, end: date) -> dict:
        return await asyncio.to_thread(storage.load_range, self.data_dir, guild_id, start, end)

    def _slots(self, prep: _Prep, days: dict, start: date, end: date, **kw):
        return engine.build_slots(days, start, end, prep.now_ts, prep.coverage_start, **kw)

    @staticmethod
    def _parse_args(args: tuple[str, ...]):
        """Loose argument soup -> (days:int|None, weekday:int|None, words:set[str], rest:list[str])."""
        days = weekday = None
        words: set[str] = set()
        rest: list[str] = []
        for a in args:
            low = a.lower()
            if low.isdigit():
                days = max(1, min(int(low), MAX_DAYS_ARG))
            elif models.parse_weekday(low) is not None:
                weekday = models.parse_weekday(low)
            elif low in ("all", "users"):
                words.add(low)
            else:
                rest.append(a)
        return days, weekday, words, rest

    def _window(self, prep: _Prep, days_n: int | None) -> tuple[date, date, str]:
        n = days_n or DEFAULT_WINDOW_DAYS
        return prep.today - timedelta(days=n - 1), prep.today, f"last {n} days"

    async def _no_data(self, ctx, prep: _Prep) -> bool:
        if prep.has_data:
            return False
        await ctx.send(embed=embeds.collecting_embed(prep.coverage_start, prep.now_ts))
        return True

    @staticmethod
    def _png_file(png: bytes, name: str):
        return discord.File(io.BytesIO(png), filename=name)

    # ------------------------------------------------------------------
    # Live view (board + overview)
    # ------------------------------------------------------------------

    def _live_view(self, prep: _Prep, days: dict) -> embeds.LiveView:
        tracker = self._trackers[prep.guild.id]
        start = prep.today - timedelta(days=BOARD_HISTORY_DAYS - 1)
        hist = self._slots(prep, days, start, prep.today)
        now = prep.now_ts
        local = models.local_dt(now)
        acc = tracker.open_acc(now)
        hour_msgs = acc.msgs if acc else 0
        hour_chatters = max(len(acc.users), acc.u_base) if acc else 0
        usual, _n = engine.usual_msgs(hist, local.weekday(), local.hour)
        elapsed = now - models.floor_hour(now)
        pace = None
        if usual and usual > 0 and elapsed >= 600:
            pace = (hour_msgs / (elapsed / 3600)) / usual
        today_key = prep.today.isoformat()
        today_slots = self._slots(prep, days, prep.today, prep.today, include_open=True)
        users_today = engine.user_counts(days.get(today_key) or {})
        # per-user counts for the not-yet-flushed tail are a rounding error; use disk
        joins, leaves = engine.joins_leaves(days, prep.today, prep.today)
        return embeds.LiveView(
            now_ts=now,
            last_msg_age_s=(now - tracker.last_message_ts) if tracker.last_message_ts else None,
            hour_msgs=hour_msgs,
            hour_chatters=hour_chatters,
            concurrency=tracker.concurrency_now(now),
            usual_hour_msgs=usual,
            pace_ratio=pace,
            today_msgs=sum(s.msgs for s in today_slots),
            today_chatters=len(users_today) if users_today else None,
            today_vs_usual=engine.today_pace(hist, prep.today),
            joins_today=joins,
            leaves_today=leaves,
            next_best=engine.upcoming_window(hist, now, BEST_WINDOW_HOURS, 24, "best"),
            next_quiet=engine.upcoming_window(hist, now, BEST_WINDOW_HOURS, 24, "quiet"),
            hours=engine.hour_table([s for s in hist if s.date >= (prep.today - timedelta(days=DEFAULT_WINDOW_DAYS - 1)).isoformat()]),
            history_days=BOARD_HISTORY_DAYS,
        )

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------

    @commands.group(name="pulse", aliases=["activity"], invoke_without_command=True)
    @commands.guild_only()
    async def pulse(self, ctx: commands.Context):
        """Server activity at a glance. See `.pulse day|week|month|hours|hour|heatmap|best|channels|top|anomalies|export`."""
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        start = prep.today - timedelta(days=BOARD_HISTORY_DAYS - 1)
        days = await self._days(ctx.guild.id, start, prep.today)
        live = self._live_view(prep, days)
        sentence = embeds.busy_quiet_sentence(live.hours)
        await ctx.send(embed=embeds.overview_embed(live, sentence, live.hours, DEFAULT_WINDOW_DAYS))

    @pulse.command(name="version")
    async def pulse_version(self, ctx: commands.Context):
        """Show the running build (use it to confirm a deploy)."""
        prep = await self._prepare(ctx.guild)
        tracker = self._trackers[ctx.guild.id]
        pending = sum(a.msgs for a in tracker.accs.values())
        dates = await asyncio.to_thread(storage.list_dates, self.data_dir, ctx.guild.id)
        state = await self._backfill_state(ctx.guild)
        await ctx.send(
            embed=embeds.version_embed(
                days_stored=len(dates), coverage_start_ts=prep.coverage_start, live_since_ts=prep.st["live_since"],
                pending_msgs=pending, backfill_status=state.get("status", "none"),
            )
        )

    # -- period reports -------------------------------------------------------

    async def _report_for(self, prep: _Prep, kind: str, resolved) -> engine.PeriodReport:
        start, end, label, ps, pe = resolved
        load_from = min(ps, start - timedelta(weeks=8))
        days = await self._days(prep.guild.id, load_from, end)
        report = engine.build_period_report(days, kind, start, end, label, ps, pe, prep.now_ts, prep.coverage_start)
        baseline = self._slots(prep, days, load_from, end)
        report.anomalies = engine.find_anomalies(baseline, start, end)
        report.baseline = baseline
        return report

    async def _send_period(self, ctx: commands.Context, kind: str, arg: str | None):
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        resolved = engine.resolve_period(kind, arg, prep.today)
        if resolved is None:
            usage = {"day": "`.pulse day [today|yesterday|YYYY-MM-DD]`", "week": "`.pulse week [this|last|YYYY-MM-DD]`", "month": "`.pulse month [this|last|YYYY-MM]`"}[kind]
            await ctx.send(f"I couldn't read that. Usage: {usage}")
            return
        async with ctx.typing():
            report = await self._report_for(prep, kind, resolved)
            embed = embeds.period_embed(report, window_days_note=f"{report.slots_n} hours of data")
            file = None
            if kind != "day" and report.slots_n:
                png = await asyncio.to_thread(
                    heatmap.render_heatmap, report.matrix,
                    title=f"{embeds._KIND_TITLE[kind]} heatmap", subtitle=report.label,
                )
                name = f"pulse_{kind}.png"
                file = self._png_file(png, name)
                embed.set_image(url=f"attachment://{name}")
        await ctx.send(embed=embed, **({"file": file} if file else {}))

    @pulse.command(name="day")
    async def pulse_day(self, ctx: commands.Context, when: str | None = None):
        """Daily report. `.pulse day`, `.pulse day yesterday`, `.pulse day 2026-09-12`."""
        await self._send_period(ctx, "day", when)

    @pulse.command(name="week")
    async def pulse_week(self, ctx: commands.Context, which: str | None = None):
        """Weekly report (Mon–Sun). `.pulse week` (so far), `.pulse week last`, `.pulse week 2026-09-12`."""
        await self._send_period(ctx, "week", which)

    @pulse.command(name="month")
    async def pulse_month(self, ctx: commands.Context, which: str | None = None):
        """Monthly report. `.pulse month` (so far), `.pulse month last`, `.pulse month 2026-08`."""
        await self._send_period(ctx, "month", which)

    # -- hour-of-day views ----------------------------------------------------

    @pulse.command(name="hours")
    async def pulse_hours(self, ctx: commands.Context, *args: str):
        """Average activity for each of the 24 hours. Optional: a weekday and/or number of days.

        `.pulse hours` · `.pulse hours fri` · `.pulse hours 60` · `.pulse hours all`
        """
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        days_n, weekday, words, _ = self._parse_args(args)
        start, end, note = self._window_all(prep, days_n, "all" in words)
        days = await self._days(ctx.guild.id, start, end)
        slots = self._slots(prep, days, start, end)
        await ctx.send(embed=embeds.hours_embed(engine.hour_table(slots, weekday), weekday, f"{note} · {len(slots)} hours sampled"))

    def _window_all(self, prep: _Prep, days_n: int | None, everything: bool) -> tuple[date, date, str]:
        if everything and prep.coverage_start is not None:
            first = models.local_dt(prep.coverage_start).date()
            return first, prep.today, f"all data since {first.isoformat()}"
        return self._window(prep, days_n)

    @pulse.command(name="hour")
    async def pulse_hour(self, ctx: commands.Context, *args: str):
        """Zoom in on one hour: where it ranks and how it looks on each weekday.

        `.pulse hour 8pm` · `.pulse hour 20` · `.pulse hour 8pm 60` (last 60 days)
        """
        parts = list(args)
        hour = models.parse_hour(parts[0]) if parts else None
        if hour is not None:
            parts = parts[1:]
        elif len(parts) > 1:
            hour = models.parse_hour(parts[0] + parts[1])
            if hour is not None:
                parts = parts[2:]
        if hour is None:
            await ctx.send("Which hour? Try `.pulse hour 8pm` or `.pulse hour 20`.")
            return
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        days_n, _wd, words, _ = self._parse_args(tuple(parts))
        start, end, note = self._window_all(prep, days_n, "all" in words)
        days = await self._days(ctx.guild.id, start, end)
        slots = self._slots(prep, days, start, end)
        await ctx.send(embed=embeds.hour_focus_embed(engine.hour_focus(slots, hour), note))

    @pulse.command(name="heatmap")
    async def pulse_heatmap(self, ctx: commands.Context, *args: str):
        """Weekday × hour heatmap image. Optional metric (`msgs`, `chatters`, `peak`) and days.

        `.pulse heatmap` · `.pulse heatmap chatters` · `.pulse heatmap peak 60`
        """
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        days_n, _wd, words, rest = self._parse_args(args)
        metric = next((METRIC_ALIASES[r.lower()] for r in rest if r.lower() in METRIC_ALIASES), "msgs")
        start, end, note = self._window_all(prep, days_n, "all" in words)
        days = await self._days(ctx.guild.id, start, end)
        slots = self._slots(prep, days, start, end)
        matrix, _counts = engine.weekday_hour_matrix(slots, metric)
        async with ctx.typing():
            png = await asyncio.to_thread(
                heatmap.render_heatmap, matrix, title="Server Pulse — when is chat busy?",
                subtitle=f"{note} · all times Pacific", value_label=METRIC_LABELS[metric],
            )
        embed = embeds._embed("🔥 Weekday × hour heatmap", embeds.busy_quiet_sentence(engine.hour_table(slots)))
        embed.set_image(url="attachment://pulse_heatmap.png")
        embeds._footer(embed, note)
        await ctx.send(embed=embed, file=self._png_file(png, "pulse_heatmap.png"))

    @pulse.command(name="best")
    async def pulse_best(self, ctx: commands.Context, *args: str):
        """Best (and quietest) windows for an event or announcement.

        `.pulse best` (2-hour windows) · `.pulse best 3` · `.pulse best 2 fri`
        """
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        width = BEST_WINDOW_HOURS
        weekday = None
        for a in args:
            if a.isdigit() and 1 <= int(a) <= 6:
                width = int(a)
            elif models.parse_weekday(a) is not None:
                weekday = models.parse_weekday(a)
        start = prep.today - timedelta(days=BOARD_HISTORY_DAYS - 1)
        days = await self._days(ctx.guild.id, start, prep.today)
        slots = self._slots(prep, days, start, prep.today)
        best = engine.find_windows(slots, width, BEST_WINDOW_TOP_N, "best", weekday)
        quiet = engine.find_windows(slots, width, BEST_WINDOW_TOP_N, "quiet", weekday)
        await ctx.send(embed=embeds.windows_embed(best, quiet, width, weekday, f"last {BOARD_HISTORY_DAYS} days"))

    # -- channels, people, oddities --------------------------------------------

    @pulse.command(name="channels")
    async def pulse_channels(self, ctx: commands.Context, days_n: int = 7):
        """Which channels carry the conversation. `.pulse channels [days]` (default 7)."""
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        days_n = max(1, min(days_n, MAX_DAYS_ARG))
        start = prep.today - timedelta(days=days_n - 1)
        days = await self._days(ctx.guild.id, start, prep.today)
        ranking = engine.channel_ranking(days, start, prep.today, {str(c) for c in prep.st["ignored"]})
        await ctx.send(embed=embeds.channels_embed(ranking, f"last {days_n} days"))

    @pulse.command(name="channel")
    async def pulse_channel(self, ctx: commands.Context, channel: discord.abc.GuildChannel, *args: str):
        """Hour-by-hour profile of a single channel. `.pulse channel #general [days]`."""
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        days_n, weekday, _w, _r = self._parse_args(args)
        start, end, note = self._window(prep, days_n)
        days = await self._days(ctx.guild.id, start, end)
        slots = self._slots(prep, days, start, end, channels={str(channel.id)})
        embed = embeds.hours_embed(engine.hour_table(slots, weekday), weekday, f"#{channel.name} · {note} · chatters are summed per hour")
        embed.title = f"🕒 #{channel.name} — hour by hour"
        await ctx.send(embed=embed)

    @pulse.command(name="top")
    async def pulse_top(self, ctx: commands.Context, days_n: int = 7):
        """Top chatters (mods only; counts, never message content). `.pulse top [days]`."""
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        days_n = max(1, min(days_n, USER_RETENTION_DAYS))
        start = prep.today - timedelta(days=days_n - 1)
        days = await self._days(ctx.guild.id, start, prep.today)
        rows = []
        for uid, n in engine.top_chatters(days, start, prep.today, 15):
            member = ctx.guild.get_member(uid)
            rows.append((member.display_name if member else f"User {uid}", n))
        await ctx.send(embed=embeds.top_chatters_embed(rows, f"last {days_n} days"))

    @pulse.command(name="anomalies")
    async def pulse_anomalies(self, ctx: commands.Context, days_n: int = 14):
        """Days/hours that were unusually busy or dead for their weekday. `.pulse anomalies [days]`."""
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        days_n = max(1, min(days_n, 90))
        start = prep.today - timedelta(days=days_n - 1)
        days = await self._days(ctx.guild.id, start - timedelta(weeks=8), prep.today)
        slots = self._slots(prep, days, start - timedelta(weeks=8), prep.today)
        found = engine.find_anomalies(slots, start, prep.today, limit=12)
        await ctx.send(embed=embeds.anomalies_embed(found, f"last {days_n} days"))

    @pulse.command(name="members")
    async def pulse_members(self, ctx: commands.Context, days_n: int = 14):
        """Joins and leaves per day. `.pulse members [days]`."""
        prep = await self._prepare(ctx.guild)
        days_n = max(1, min(days_n, 60))
        start = prep.today - timedelta(days=days_n - 1)
        days = await self._days(ctx.guild.id, start, prep.today)
        rows = [
            (d.isoformat(), int((days.get(d.isoformat()) or {}).get("joins", 0)), int((days.get(d.isoformat()) or {}).get("leaves", 0)))
            for d in models.date_range(start, prep.today)
            if (days.get(d.isoformat()) or {}).get("joins") or (days.get(d.isoformat()) or {}).get("leaves")
        ]
        await ctx.send(embed=embeds.members_embed(rows, f"last {days_n} days"))

    # -- export ---------------------------------------------------------------

    @pulse.command(name="export")
    async def pulse_export(self, ctx: commands.Context, *args: str):
        """JSON export for analysis (share the file with Claude). `.pulse export [hourly-days=90] [users]`.

        Summaries (hour-of-day, heatmap matrix, daily/weekly/monthly, channels,
        windows, anomalies) always cover all data; the raw per-hour table covers
        the last N days. Add `users` to include top-chatter ids (off by default).
        """
        prep = await self._prepare(ctx.guild)
        if await self._no_data(ctx, prep):
            return
        raw_days, _wd, words, _ = self._parse_args(args)
        raw_days = raw_days or 90
        first = models.local_dt(prep.coverage_start).date()
        async with ctx.typing():
            days = await self._days(ctx.guild.id, first, prep.today)
            names = {str(c.id): c.name for c in ctx.guild.channels}
            user_names = {}
            if "users" in words:
                for uid, _n in engine.top_chatters(days, first, prep.today, 25):
                    m = ctx.guild.get_member(uid)
                    user_names[uid] = m.display_name if m else None
            gconf = self.config.guild(ctx.guild)
            settings = {
                "ignored_channels": sorted(prep.st["ignored"]), "exclude_bot_commands": prep.st["exclude_commands"],
                "counts": "human messages only",
            }
            payload = await asyncio.to_thread(
                export_mod.build_export,
                guild_id=ctx.guild.id, guild_name=ctx.guild.name, now_ts=prep.now_ts, coverage_start_ts=prep.coverage_start,
                days=days, start=first, end=prep.today, raw_start=max(first, prep.today - timedelta(days=raw_days - 1)),
                channel_names=names, settings=settings, user_names=user_names, include_users="users" in words,
            )
            blob = await asyncio.to_thread(lambda: json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            if len(blob) > 8 * 1024 * 1024:
                payload["hourly"] = [h for h in payload["hourly"] if h["date"] >= (prep.today - timedelta(days=30)).isoformat()]
                payload["range"]["hourly_detail_from"] = (prep.today - timedelta(days=30)).isoformat()
                payload["range"]["note"] = "hourly detail truncated to 30 days to fit Discord's upload limit"
                blob = await asyncio.to_thread(lambda: json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        name = f"serverpulse-export-{prep.today.isoformat()}.json"
        await ctx.send(
            f"Export ready — {len(payload['daily'])} days of data, {len(payload['hourly'])} active hours of detail. Share this file with Claude for analysis.",
            file=discord.File(io.BytesIO(blob), filename=name),
        )

    # -- live board -----------------------------------------------------------

    @pulse.group(name="board", invoke_without_command=True)
    async def pulse_board(self, ctx: commands.Context):
        """Self-updating live board. `.pulse board start|stop|refresh`."""
        board = await self.config.guild(ctx.guild).board()
        if board.get("channel_id"):
            await ctx.send(f"Live board is running in <#{board['channel_id']}> (updates every {BOARD_INTERVAL_MINUTES} min). `.pulse board stop` removes it.")
        else:
            await ctx.send("No live board. `.pulse board start` posts one here and keeps it updated.")

    @pulse_board.command(name="start")
    async def pulse_board_start(self, ctx: commands.Context, channel: discord.TextChannel | None = None):
        """Post the live board in this (or the given) channel."""
        target = channel or ctx.channel
        gconf = self.config.guild(ctx.guild)
        old = await gconf.board()
        if old.get("channel_id") and old.get("message_id"):
            await self._delete_board_message(ctx.guild, old)
        await gconf.board.set({"channel_id": target.id, "message_id": None})
        await self._update_board(ctx.guild)
        await self._ack(ctx)

    @pulse_board.command(name="stop")
    async def pulse_board_stop(self, ctx: commands.Context):
        """Stop and delete the live board."""
        gconf = self.config.guild(ctx.guild)
        await self._delete_board_message(ctx.guild, await gconf.board())
        await gconf.board.set({"channel_id": None, "message_id": None})
        await self._ack(ctx)

    @pulse_board.command(name="refresh")
    async def pulse_board_refresh(self, ctx: commands.Context):
        """Update the board right now."""
        await self._update_board(ctx.guild)
        await self._ack(ctx)

    async def _delete_board_message(self, guild: discord.Guild, board: dict) -> None:
        channel = guild.get_channel_or_thread(board.get("channel_id") or 0)
        if channel is None or not board.get("message_id"):
            return
        try:
            await channel.get_partial_message(board["message_id"]).delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _update_board(self, guild: discord.Guild) -> None:
        gconf = self.config.guild(guild)
        board = await gconf.board()
        channel = guild.get_channel_or_thread(board.get("channel_id") or 0)
        if channel is None:
            return
        prep = await self._prepare(guild)
        if prep.has_data:
            start = prep.today - timedelta(days=BOARD_HISTORY_DAYS - 1)
            days = await self._days(guild.id, start, prep.today)
            embed = embeds.board_embed(self._live_view(prep, days))
        else:
            embed = embeds.collecting_embed(prep.coverage_start, prep.now_ts)
        message_id = board.get("message_id")
        if message_id:
            try:
                await channel.get_partial_message(message_id).edit(embed=embed)
                return
            except discord.NotFound:
                pass  # deleted by someone: post a fresh one below
        sent = await channel.send(embed=embed)
        await gconf.board.set({"channel_id": channel.id, "message_id": sent.id})

    # -- digests --------------------------------------------------------------

    @pulse.group(name="digest", invoke_without_command=True)
    async def pulse_digest(self, ctx: commands.Context):
        """Scheduled summaries in the mod channel. `.pulse digest weekly|monthly on|off`, `.pulse digest now weekly|monthly`."""
        gconf = self.config.guild(ctx.guild)
        w, m = await gconf.digest_weekly(), await gconf.digest_monthly()
        await ctx.send(
            f"Weekly (Mondays 9am Pacific): **{'on' if w['enabled'] else 'off'}** · Monthly (1st, 9am Pacific): **{'on' if m['enabled'] else 'off'}**\n"
            "Change with `.pulse digest weekly on|off` / `.pulse digest monthly on|off`; preview with `.pulse digest now weekly|monthly`."
        )

    async def _toggle_digest(self, ctx: commands.Context, name: str, due, enabled: bool):
        gconf = self.config.guild(ctx.guild)
        cfg = await getattr(gconf, name)()
        cfg["enabled"] = enabled
        if enabled:
            cfg["last"] = due(models.local_dt(time.time()))[0]  # next digest is the *next* due one, not a stale backlog
        await getattr(gconf, name).set(cfg)
        await self._ack(ctx)

    @pulse_digest.command(name="weekly")
    async def pulse_digest_weekly(self, ctx: commands.Context, state: bool):
        """Turn the weekly digest on or off."""
        await self._toggle_digest(ctx, "digest_weekly", engine.weekly_due, state)

    @pulse_digest.command(name="monthly")
    async def pulse_digest_monthly(self, ctx: commands.Context, state: bool):
        """Turn the monthly digest on or off."""
        await self._toggle_digest(ctx, "digest_monthly", engine.monthly_due, state)

    @pulse_digest.command(name="now")
    async def pulse_digest_now(self, ctx: commands.Context, kind: str = "weekly"):
        """Send the most recent digest right now (does not affect the schedule)."""
        kind = kind.lower()
        if kind not in ("weekly", "monthly"):
            await ctx.send("`.pulse digest now weekly` or `.pulse digest now monthly`.")
            return
        due = engine.weekly_due if kind == "weekly" else engine.monthly_due
        _key, start, end = due(models.local_dt(time.time()))
        sent = await self._send_digest(ctx.guild, "week" if kind == "weekly" else "month", start, end, channel=ctx.channel)
        if not sent:
            await ctx.send("Not enough data for that period yet.")

    async def _send_digest(self, guild: discord.Guild, kind: str, start: date, end: date, channel=None) -> bool:
        st = await self._ensure_guild(guild)
        channel = channel or guild.get_channel_or_thread(st["mod_channel"] or 0)
        if channel is None:
            log.warning("serverpulse: digest skipped, mod channel %s not found in guild %s", st["mod_channel"], guild.id)
            return False
        prep = await self._prepare(guild)
        resolved = engine.resolve_period(kind, start.isoformat(), prep.today)
        if resolved is None:
            return False
        report = await self._report_for(prep, kind, resolved)
        if report.slots_n < 24:
            return False
        best = engine.find_windows(report.baseline, BEST_WINDOW_HOURS, BEST_WINDOW_TOP_N, "best")
        embed = embeds.period_embed(report, digest=True, best=best, window_days_note=f"{report.slots_n} hours of data · human messages only")
        png = await asyncio.to_thread(
            heatmap.render_heatmap, report.matrix, title=f"{embeds._KIND_TITLE[kind]} heatmap", subtitle=report.label,
        )
        name = f"pulse_{kind}_digest.png"
        embed.set_image(url=f"attachment://{name}")
        await channel.send(embed=embed, file=self._png_file(png, name))
        return True

    async def _run_digest_check(self, guild: discord.Guild, nowl: datetime | None = None) -> None:
        await self._ensure_guild(guild)
        gconf = self.config.guild(guild)
        nowl = nowl or models.local_dt(time.time())
        for name, kind, due in (
            ("digest_weekly", "week", engine.weekly_due),
            ("digest_monthly", "month", engine.monthly_due),
        ):
            cfg = await getattr(gconf, name)()
            if not cfg.get("enabled"):
                continue
            key, start, end = due(nowl)
            if cfg.get("last") == key:
                continue
            # mark first: at-most-once beats spamming the channel if sending fails halfway
            cfg["last"] = key
            await getattr(gconf, name).set(cfg)
            await self._send_digest(guild, kind, start, end)

    # -- backfill -------------------------------------------------------------

    async def _backfill_state(self, guild: discord.Guild) -> dict:
        state = dict(await self.config.guild(guild).backfill())
        if not state:
            return {"status": "none"}
        task = self._backfill_tasks.get(guild.id)
        if state.get("status") == "running" and (task is None or task.done()):
            state["status"] = "cancelled"
            state["error"] = "interrupted (reload or restart)"
        return state

    @pulse.group(name="backfill", invoke_without_command=True)
    async def pulse_backfill(self, ctx: commands.Context, days: int = 30):
        """Import message history so reports work immediately. `.pulse backfill [days=30]`.

        Also: `.pulse backfill status|resume|cancel`. Safe to re-run (replaces,
        never double counts). Counts messages only — joins/leaves can't be recovered.
        """
        days = max(1, min(days, 365))
        await self._start_backfill(ctx, days, resume=False)

    @pulse_backfill.command(name="status")
    async def pulse_backfill_status(self, ctx: commands.Context):
        """Show backfill progress."""
        await ctx.send(embed=embeds.backfill_embed(await self._backfill_state(ctx.guild), time.time()))

    @pulse_backfill.command(name="resume")
    async def pulse_backfill_resume(self, ctx: commands.Context):
        """Continue an interrupted backfill (skips channels already read)."""
        await self._start_backfill(ctx, None, resume=True)

    @pulse_backfill.command(name="cancel")
    async def pulse_backfill_cancel(self, ctx: commands.Context):
        """Stop a running backfill (progress is kept; `resume` continues)."""
        task = self._backfill_tasks.get(ctx.guild.id)
        if task is None or task.done():
            await ctx.send("No backfill is running.")
            return
        self._cancel.add(ctx.guild.id)
        await ctx.send("Cancelling after the current batch — progress is kept, `.pulse backfill resume` continues.")

    async def _start_backfill(self, ctx: commands.Context, days: int | None, resume: bool):
        guild = ctx.guild
        task = self._backfill_tasks.get(guild.id)
        if task is not None and not task.done():
            await ctx.send("A backfill is already running — see `.pulse backfill status`.")
            return
        prep = await self._prepare(guild)
        st = prep.st
        if prep.now_ts < st["live_since"]:
            await ctx.send(f"Live tracking starts at {embeds.fmt_clock(st['live_since'])}. Run the backfill after that so the two don't overlap.")
            return
        stage = bf.Stage(self.data_dir, guild.id)
        if resume:
            meta = stage.meta()
            if not meta:
                await ctx.send("Nothing to resume — start one with `.pulse backfill 30`.")
                return
        else:
            start_ts = models.day_start_ts(prep.today - timedelta(days=days))
            meta = {"days": days, "start_ts": start_ts, "end_ts": st["live_since"], "started": prep.now_ts}
            stage.start(meta)
        await self.config.guild(guild).backfill.set(
            {"status": "running", "days": meta["days"], "started": meta["started"], "channels_total": 0,
             "channels_done": 0, "messages": 0, "current": "", "skipped": [], "error": ""}
        )
        self._cancel.discard(guild.id)
        await ctx.send(
            f"📥 {'Resuming' if resume else 'Starting'} backfill of the last **{meta['days']} days**. I'll post here when it's done; "
            "check `.pulse backfill status` any time. Chat tracking keeps running meanwhile."
        )
        task = asyncio.create_task(self._backfill_run(guild, ctx.channel, meta))
        self._backfill_tasks[guild.id] = task

    async def _sources_for(self, parent, start_dt: datetime):
        """The parent channel (if it has messages of its own) plus its threads, active and recently archived.

        Forum channels have no message history themselves, only posts (threads).
        """
        if hasattr(parent, "history"):
            yield parent
        seen = set()
        for thread in list(getattr(parent, "threads", []) or []):
            seen.add(thread.id)
            yield thread
        archived = getattr(parent, "archived_threads", None)
        if archived is None:
            return
        try:
            async for thread in archived(limit=None):
                if thread.id in seen:
                    continue
                if thread.archive_timestamp and thread.archive_timestamp < start_dt:
                    break  # newest-archived first: everything after this is older than the window
                yield thread
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _backfill_run(self, guild: discord.Guild, report_channel, meta: dict) -> None:
        gconf = self.config.guild(guild)
        stage = bf.Stage(self.data_dir, guild.id)
        state = {"status": "running", "days": meta["days"], "started": meta["started"], "channels_total": 0,
                 "channels_done": 0, "messages": 0, "current": "", "skipped": [], "error": ""}
        try:
            st = self._cache[guild.id]
            start_ts, end_ts = int(meta["start_ts"]), int(meta["end_ts"])
            start_dt = datetime.fromtimestamp(start_ts, TIMEZONE)
            end_dt = datetime.fromtimestamp(end_ts, TIMEZONE)
            prefixes = await self.bot.get_valid_prefixes(guild)
            parents = [
                c for c in (*guild.text_channels, *guild.voice_channels, *getattr(guild, "stage_channels", []), *getattr(guild, "forums", []))
                if c.id not in st["ignored"]
            ]
            done = stage.done_channels()
            state["channels_total"] = len(parents)
            state["channels_done"] = sum(1 for c in parents if str(c.id) in done)
            await gconf.backfill.set(state)
            last_save = time.monotonic()

            for parent in parents:
                if str(parent.id) in done:
                    continue
                if guild.id in self._cancel:
                    state["status"] = "cancelled"
                    break
                state["current"] = f"#{parent.name}"
                rows: list[tuple[float, int]] = []
                skipped = False
                async for source in self._sources_for(parent, start_dt):
                    try:
                        async for m in source.history(limit=None, after=start_dt, before=end_dt, oldest_first=True):
                            if not should_count(
                                is_bot=m.author.bot, webhook_id=m.webhook_id, type_name=m.type.name,
                                content=m.content or "", channel_key=parent.id, ignored=st["ignored"],
                                exclude_commands=st["exclude_commands"], prefixes=prefixes,
                            ):
                                continue
                            rows.append((m.created_at.timestamp(), m.author.id))
                            if len(rows) % 500 == 0:
                                if guild.id in self._cancel:
                                    break
                                await asyncio.sleep(0)
                    except (discord.Forbidden, discord.NotFound):
                        # no access, or the channel/thread was deleted mid-run (404 Unknown Channel):
                        # skip just that source and carry on with the rest instead of failing the whole run
                        if source is parent:
                            skipped = True
                            break
                        continue
                    if guild.id in self._cancel:
                        break
                if guild.id in self._cancel:
                    state["status"] = "cancelled"
                    break  # unfinished channel is not staged, so resume re-reads it whole
                if skipped:
                    state["skipped"].append(f"#{parent.name}")
                else:
                    await asyncio.to_thread(stage.write_channel, str(parent.id), rows)
                    state["messages"] += len(rows)
                state["channels_done"] += 1
                if time.monotonic() - last_save > 20:
                    await gconf.backfill.set(state)
                    last_save = time.monotonic()

            if state["status"] == "running":
                state["current"] = "merging…"
                await gconf.backfill.set(state)
                docs = await asyncio.to_thread(lambda: bf.build_days(stage.merged_rows(), start_ts, end_ts))
                async with self._lock(guild.id):
                    await asyncio.to_thread(bf.apply_backfill, self.data_dir, guild.id, docs, start_ts, end_ts)
                cov = await gconf.coverage_start()
                if cov is None or start_ts < cov:
                    await gconf.coverage_start.set(start_ts)
                await self._refresh_settings(guild)
                await asyncio.to_thread(stage.clear)
                state["status"], state["current"] = "done", ""
        except asyncio.CancelledError:
            state["status"], state["error"] = "cancelled", "interrupted (reload or restart)"
            await gconf.backfill.set(state)
            raise
        except Exception as exc:
            log.exception("serverpulse: backfill failed for guild %s", guild.id)
            state["status"], state["error"] = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self._cancel.discard(guild.id)
        await gconf.backfill.set(state)
        try:
            await report_channel.send(embed=embeds.backfill_embed(state, time.time()))
        except discord.HTTPException:
            log.warning("serverpulse: could not post backfill result")

    # -- ignore list & settings -------------------------------------------------

    @pulse.command(name="ignore")
    async def pulse_ignore(self, ctx: commands.Context, *channels: discord.abc.GuildChannel):
        """Stop counting a channel (threads follow their parent). `.pulse ignore #bot-spam`."""
        if not channels:
            await ctx.send("Which channel? `.pulse ignore #channel`")
            return
        async with self.config.guild(ctx.guild).ignored_channels() as ignored:
            for c in channels:
                if c.id not in ignored:
                    ignored.append(c.id)
        await self._refresh_settings(ctx.guild)
        await self._ack(ctx)

    @pulse.command(name="unignore")
    async def pulse_unignore(self, ctx: commands.Context, *channels: discord.abc.GuildChannel):
        """Count a channel again. `.pulse unignore #channel`."""
        if not channels:
            await ctx.send("Which channel? `.pulse unignore #channel`")
            return
        async with self.config.guild(ctx.guild).ignored_channels() as ignored:
            for c in channels:
                if c.id in ignored:
                    ignored.remove(c.id)
        await self._refresh_settings(ctx.guild)
        await self._ack(ctx)

    @pulse.command(name="ignored")
    async def pulse_ignored(self, ctx: commands.Context):
        """List ignored channels."""
        st = await self._ensure_guild(ctx.guild)
        await ctx.send(", ".join(f"<#{c}>" for c in sorted(st["ignored"])) or "No ignored channels.")

    @pulse.group(name="guilds", invoke_without_command=True)
    async def pulse_guilds(self, ctx: commands.Context):
        """Bot owner: show which servers serverpulse tracks. `.pulse guilds add|remove <server id>`."""
        if not await self.bot.is_owner(ctx.author):
            return
        allowed = self._allowed
        if allowed is None:
            await ctx.send("Allowlist not set yet: tracking every server the bot is in.")
            return
        lines = []
        for gid in sorted(allowed):
            g = self.bot.get_guild(gid)
            lines.append(f"`{gid}` {g.name if g else '(bot is not in this server)'}")
        await ctx.send("serverpulse is active in:\n" + ("\n".join(lines) if lines else "no servers"))

    @pulse_guilds.command(name="add")
    async def pulse_guilds_add(self, ctx: commands.Context, guild_id: int):
        """Bot owner: start tracking another server."""
        if not await self.bot.is_owner(ctx.author):
            return
        if self.bot.get_guild(guild_id) is None:
            await ctx.send("The bot isn't in a server with that ID.")
            return
        new = set(self._allowed or set()) | {guild_id}
        await self.config.allowed_guild_ids.set(sorted(new))
        self._apply_allowed(new)
        await self._ack(ctx)

    @pulse_guilds.command(name="remove")
    async def pulse_guilds_remove(self, ctx: commands.Context, guild_id: int):
        """Bot owner: stop tracking a server (its stored data is kept)."""
        if not await self.bot.is_owner(ctx.author):
            return
        if guild_id == ctx.guild.id:
            await ctx.send("Run this from a different allowed server, or `.pulse` stops working here.")
            return
        new = set(self._allowed or set()) - {guild_id}
        await self.config.allowed_guild_ids.set(sorted(new))
        self._apply_allowed(new)
        await self._ack(ctx)

    @pulse.group(name="set", invoke_without_command=True)
    async def pulse_set(self, ctx: commands.Context):
        """Settings: `.pulse set modchannel|modrole|commands`. Shows current settings."""
        await self.pulse_settings(ctx)

    @pulse_set.command(name="modchannel")
    async def pulse_set_modchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Where `.pulse` works and digests are posted."""
        await self.config.guild(ctx.guild).mod_channel_id.set(channel.id)
        await self._refresh_settings(ctx.guild)
        await self._ack(ctx)

    @pulse_set.command(name="modrole")
    async def pulse_set_modrole(self, ctx: commands.Context, role: discord.Role):
        """Role allowed to use `.pulse` (admins and Red mods always can)."""
        await self.config.guild(ctx.guild).mod_role_id.set(role.id)
        await self._refresh_settings(ctx.guild)
        await self._ack(ctx)

    @pulse_set.command(name="commands")
    async def pulse_set_commands(self, ctx: commands.Context, state: bool):
        """Ignore bot-command messages like `.gamble` (default on), so game nights don't read as chatty."""
        await self.config.guild(ctx.guild).exclude_commands.set(state)
        await self._refresh_settings(ctx.guild)
        await self._ack(ctx)

    @pulse.command(name="settings")
    async def pulse_settings(self, ctx: commands.Context):
        """Show current settings."""
        prep = await self._prepare(ctx.guild)
        gconf = self.config.guild(ctx.guild)
        dates = await asyncio.to_thread(storage.list_dates, self.data_dir, ctx.guild.id)
        await ctx.send(
            embed=embeds.settings_embed(
                ignored=sorted(prep.st["ignored"]), mod_channel=prep.st["mod_channel"], mod_role=prep.st["mod_role"],
                exclude_commands=prep.st["exclude_commands"], coverage_start_ts=prep.coverage_start,
                live_since_ts=prep.st["live_since"], board=await gconf.board(), digest_weekly=await gconf.digest_weekly(),
                digest_monthly=await gconf.digest_monthly(), days_stored=len(dates),
            )
        )

    # ------------------------------------------------------------------
    # Scheduled loops (every body wrapped per guild: one failure must not kill the loop)
    # ------------------------------------------------------------------

    if tasks is not None:

        @tasks.loop(seconds=FLUSH_INTERVAL_SECONDS)
        async def _flush_loop(self):
            for guild in self.bot.guilds:
                if not self._guild_allowed(guild.id):
                    continue
                try:
                    await self._ensure_guild(guild)
                    await self._flush_guild(guild.id)
                    await self._housekeeping(guild)
                except Exception:
                    log.exception("serverpulse: flush failed for guild %s", guild.id)

        @tasks.loop(minutes=BOARD_INTERVAL_MINUTES)
        async def _board_loop(self):
            for guild in self.bot.guilds:
                if not self._guild_allowed(guild.id):
                    continue
                try:
                    board = await self.config.guild(guild).board()
                    if board.get("channel_id"):
                        await self._update_board(guild)
                except Exception:
                    log.exception("serverpulse: board update failed for guild %s", guild.id)

        @tasks.loop(minutes=DIGEST_CHECK_MINUTES)
        async def _digest_loop(self):
            for guild in self.bot.guilds:
                if not self._guild_allowed(guild.id):
                    continue
                try:
                    await self._run_digest_check(guild)
                except Exception:
                    log.exception("serverpulse: digest check failed for guild %s", guild.id)

        @_flush_loop.before_loop
        @_board_loop.before_loop
        @_digest_loop.before_loop
        async def _before_loops(self):
            await self.bot.wait_until_red_ready()
            try:
                await self._load_allowed_guilds()
            except Exception:
                log.exception("serverpulse: could not load the guild allowlist; staying open")

    async def _housekeeping(self, guild: discord.Guild) -> None:
        """Once per local day: drop per-user counts older than the retention window."""
        gconf = self.config.guild(guild)
        today = models.local_dt(time.time()).date()
        if await gconf.last_purge() == today.isoformat():
            return
        cutoff = today - timedelta(days=USER_RETENTION_DAYS)
        async with self._lock(guild.id):
            await asyncio.to_thread(storage.purge_user_counts, self.data_dir, guild.id, cutoff)
        await gconf.last_purge.set(today.isoformat())
