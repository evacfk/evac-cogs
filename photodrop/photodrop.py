"""photodrop: role-holder daily photo-drop job cog.

See photodrop-cog-design.md (evac.dev Bots project) for the full spec this
implements. Command group is `.pp` / `.pockypolice`.

The job role (`.pp set role`, hardcoded default DEFAULT_JOB_ROLE_ID) is the
single source of truth for who the job holder is -- there is no separately
tracked "assigned member" id. Anyone currently holding the role can `.pp
drop`; hire/fire/clearstrikes just add or remove that role. If more than one
member ever holds it at once (manual assignment permits this), each is
treated fully independently: their own streak, strikes, no-show checks, and
their own entry in the weekly poll -- never merged or compared against each
other. A poll only ever includes people who currently hold the role at the
moment it's built, so losing the role mid-week drops that person's entry
from that week's poll entirely, even for days they already submitted.
"""
from __future__ import annotations

import calendar as calendar_module
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import collage, embeds, models, storage
from .constants import (
    DEFAULT_GUILD,
    DEFAULT_MEMBER,
    DEFAULT_POLL_DURATION_HOURS,
    DEFAULT_TIMEZONE,
    DEFAULT_WEEKLY_POLL_HOUR,
    DEFAULT_WEEKLY_POLL_WEEKDAY,
    POLL_CLOSE_GRACE_MINUTES,
    POLL_LETTERS,
    STATUS_FULL,
    STATUS_TARDY,
    WEEKDAY_NAMES,
)

try:
    from discord.ext import tasks
except ImportError:  # pragma: no cover - only hit if discord.py lacks ext.tasks
    tasks = None

log = logging.getLogger("red.photodrop")


class PhotoDrop(commands.Cog):
    """Daily photo-drop job: streak, strikes, PTO, and a weekly photo poll."""

    def __init__(self, bot: Red):
        self.bot = bot
        self._log = log
        self.config = Config.get_conf(self, identifier=0xF0704404, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self.data_dir: Path = cog_data_path(self)

        if tasks is not None:
            self._rollover_loop.start()
            self._weekly_poll_loop.start()
            self._poll_close_loop.start()

    def cog_unload(self):
        if tasks is not None:
            self._rollover_loop.cancel()
            self._weekly_poll_loop.cancel()
            self._poll_close_loop.cancel()

    # ------------------------------------------------------------------
    # Role-holder lookups
    # ------------------------------------------------------------------

    async def _job_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = await self.config.guild(guild).job_role_id()
        return guild.get_role(role_id) if role_id else None

    async def _current_holders(self, guild: discord.Guild) -> list[discord.Member]:
        """Every guild member currently holding the job role, independent of
        how many there are -- zero, one, or several are all valid states.
        """
        role = await self._job_role(guild)
        if role is None:
            return []
        return list(role.members)

    async def _holds_role(self, member: discord.Member) -> bool:
        role = await self._job_role(member.guild)
        return role is not None and role in member.roles

    def _channel_mismatch_message(self, channel: discord.TextChannel) -> str:
        return f"Photo drops happen in {channel.mention}."

    async def _member_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = await self.config.guild(guild).channel_id()
        return guild.get_channel(channel_id) if channel_id else None

    # ------------------------------------------------------------------
    # Strike -> role removal
    # ------------------------------------------------------------------

    async def _check_and_apply_strikes(self, guild: discord.Guild, member: discord.Member) -> None:
        gconf = self.config.guild(guild)
        threshold = await gconf.strike_threshold()
        window = await gconf.strike_window_days()
        mconf = self.config.member(member)
        member_state = await mconf.all()
        if models.should_lose_role(member_state, threshold, window):
            role = await self._job_role(guild)
            if role is not None and role in member.roles:
                await member.remove_roles(role, reason="photodrop: strike threshold reached")

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------

    @commands.group(name="pockypolice", aliases=["pp"], invoke_without_command=True)
    @commands.guild_only()
    async def pp(self, ctx: commands.Context):
        """Photo-drop job commands. See `.pp drop`, `.pp status`, etc."""
        await ctx.send_help(ctx.command)

    # -- drop --------------------------------------------------------------

    @pp.command(name="drop")
    @commands.guild_only()
    async def pp_drop(self, ctx: commands.Context):
        """Submit today's photos. Attach the photos to this message."""
        guild = ctx.guild

        if not await self._holds_role(ctx.author):
            await ctx.send("This job isn't assigned to you.")
            return

        channel = await self._member_channel(guild)
        if channel is not None and ctx.channel.id != channel.id:
            await ctx.send(self._channel_mismatch_message(channel))
            return

        if not ctx.message.attachments:
            await ctx.send("Attach today's photos to this command.")
            return

        date_key = models.today_key(DEFAULT_TIMEZONE)
        mconf = self.config.member(ctx.author)
        member_state = await mconf.all()

        if date_key in member_state.get("history", {}):
            await ctx.send("Already logged a shift for today.")
            return

        quota = await self.config.guild(guild).quota()
        photos = ctx.message.attachments
        saved_paths: list[str] = []
        for index, attachment in enumerate(photos):
            data = await attachment.read()
            path = storage.save_photo_bytes(self.data_dir, ctx.author.id, date_key, index, attachment.filename, data)
            saved_paths.append(str(path))

        updated, outcome, strike_added = models.record_drop(member_state, date_key, len(photos), quota, saved_paths)
        await mconf.streak.set(updated["streak"])
        await mconf.strikes.set(updated["strikes"])
        await mconf.history.set(updated["history"])

        if strike_added:
            await self._check_and_apply_strikes(guild, ctx.author)

        embed = embeds.shift_report_embed(ctx.author.display_name, outcome, len(photos), quota, updated["streak"])
        await ctx.send(embed=embed)

    # -- pto -----------------------------------------------------------------

    @pp.command(name="pto")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def pp_pto(self, ctx: commands.Context, user: discord.Member, date: str | None = None):
        """Excuse `user` for `date` (YYYY-MM-DD, defaults to today). No strike, no streak impact."""
        date_key = date or models.today_key(DEFAULT_TIMEZONE)
        try:
            models.parse_day_key(date_key)
        except ValueError:
            await ctx.send("Date must be YYYY-MM-DD.")
            return

        mconf = self.config.member(user)
        member_state = await mconf.all()
        updated = models.record_pto(member_state, date_key)
        await mconf.history.set(updated["history"])
        await ctx.send(f"{user.display_name} is excused for {date_key}.")

    # -- restorestreak ---------------------------------------------------------

    @pp.command(name="restorestreak")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def pp_restorestreak(self, ctx: commands.Context, user: discord.Member, value: int):
        """Manually set `user`'s streak to `value`."""
        if value < 0:
            await ctx.send("Streak can't be negative.")
            return
        mconf = self.config.member(user)
        await mconf.streak.set(value)
        await ctx.send(f"{user.display_name}'s streak set to {value}.")

    # -- clearstrikes ------------------------------------------------------------

    @pp.command(name="clearstrikes")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def pp_clearstrikes(self, ctx: commands.Context, user: discord.Member):
        """Wipe `user`'s strikes and restore the job role."""
        mconf = self.config.member(user)
        await mconf.strikes.set([])
        role = await self._job_role(ctx.guild)
        if role is not None and role not in user.roles:
            await user.add_roles(role, reason="photodrop: clearstrikes")
        await ctx.send(f"Strikes cleared and role restored for {user.display_name}.")

    # -- hire / fire -------------------------------------------------------------

    @pp.command(name="hire")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def pp_hire(self, ctx: commands.Context, user: discord.Member):
        """Add the job role to `user`."""
        role = await self._job_role(ctx.guild)
        if role is None:
            await ctx.send("No job role configured. Set one with `.pp set role @role` first.")
            return
        await user.add_roles(role, reason="photodrop: hire")
        await ctx.send(f"{user.display_name} is hired.")

    @pp.command(name="fire")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def pp_fire(self, ctx: commands.Context, user: discord.Member):
        """Remove the job role from `user`, outside the strike system."""
        role = await self._job_role(ctx.guild)
        if role is not None and role in user.roles:
            await user.remove_roles(role, reason="photodrop: fire")
        await ctx.send(f"{user.display_name} is let go.")

    # -- view / calendar / status --------------------------------------------

    @pp.command(name="view")
    @commands.guild_only()
    async def pp_view(self, ctx: commands.Context, user: discord.Member, date: str):
        """Show the raw photos `user` submitted on `date` (YYYY-MM-DD)."""
        try:
            models.parse_day_key(date)
        except ValueError:
            await ctx.send("Date must be YYYY-MM-DD.")
            return

        paths = storage.list_photos(self.data_dir, user.id, date)
        if not paths:
            await ctx.send(f"No photos on file for {user.display_name} on {date}.")
            return

        files = [discord.File(str(p)) for p in paths]
        await ctx.send(content=f"{user.display_name} — {date}", files=files)

    @pp.command(name="calendar")
    @commands.guild_only()
    async def pp_calendar(self, ctx: commands.Context, user: discord.Member, month: int | None = None, year: int | None = None):
        """Month view of `user`'s history (full/tardy/no-show/PTO per day)."""
        now = models.now_local(DEFAULT_TIMEZONE)
        month = month or now.month
        year = year or now.year

        member_state = await self.config.member(user).all()
        days_in_month = calendar_module.monthrange(year, month)[1]
        entries = []
        for day_num in range(1, days_in_month + 1):
            date_key = f"{year:04d}-{month:02d}-{day_num:02d}"
            status = models.calendar_status(member_state, date_key)
            entries.append((day_num, status))

        month_label = f"{calendar_module.month_name[month]} {year}"
        embed = embeds.calendar_embed(user.display_name, month_label, entries)
        await ctx.send(embed=embed)

    @pp.command(name="status")
    @commands.guild_only()
    async def pp_status(self, ctx: commands.Context, user: discord.Member):
        """Current streak, strikes, and role status for `user`."""
        gconf = self.config.guild(ctx.guild)
        threshold = await gconf.strike_threshold()
        window = await gconf.strike_window_days()
        member_state = await self.config.member(user).all()
        strikes_in_window = models.strike_count(member_state, window)
        has_role = await self._holds_role(user)
        embed = embeds.status_embed(
            user.display_name,
            member_state.get("streak", 0),
            strikes_in_window,
            threshold,
            window,
            has_role,
        )
        await ctx.send(embed=embed)

    # -- pollday ---------------------------------------------------------------

    @pp.command(name="pollday")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def pp_pollday(self, ctx: commands.Context, user: discord.Member, date: str):
        """Manually run a poll for one specific person's one day, as individual lettered photo options."""
        try:
            models.parse_day_key(date)
        except ValueError:
            await ctx.send("Date must be YYYY-MM-DD.")
            return

        channel = await self._member_channel(ctx.guild)
        if channel is None:
            await ctx.send("No channel configured. Set one with `.pp set channel #channel` first.")
            return

        raw_paths = storage.list_photos(self.data_dir, user.id, date)
        if not raw_paths:
            await ctx.send(f"No photos on file for {user.display_name} on {date}.")
            return
        if len(raw_paths) > len(POLL_LETTERS):
            raw_paths = raw_paths[: len(POLL_LETTERS)]

        letters = POLL_LETTERS[: len(raw_paths)]
        labeled_dir = self.data_dir / "collages" / "pollday" / str(user.id) / date
        entries = []
        for i, (letter, path) in enumerate(zip(letters, raw_paths)):
            out_path = labeled_dir / f"{letter}.png"
            collage.save_collage([path], letter, out_path)
            entries.append((letter, f"Photo {i + 1}", out_path))

        await self._post_poll(
            channel,
            question=embeds.pollday_question_text(date),
            entries=entries,
            kind="pollday",
        )
        if channel.id != ctx.channel.id:
            await ctx.send(f"Poll posted in {channel.mention} for {user.display_name} — {date}.")
        else:
            await ctx.send(f"Poll posted for {user.display_name} — {date}.")

    # -- set (config) ------------------------------------------------------------

    @pp.group(name="set")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def pp_set(self, ctx: commands.Context):
        """Configure the photodrop cog."""
        await ctx.send_help(ctx.command)

    @pp_set.command(name="channel")
    async def pp_set_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the shared channel for drops, shift reports, and polls."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"Channel set to {channel.mention}.")

    @pp_set.command(name="role")
    async def pp_set_role(self, ctx: commands.Context, role: discord.Role):
        """Set the job role -- the single source of truth for who can `.pp drop`."""
        await self.config.guild(ctx.guild).job_role_id.set(role.id)
        await ctx.send(f"Job role set to {role.mention}.")

    @pp_set.command(name="quota")
    async def pp_set_quota(self, ctx: commands.Context, quota: int):
        """Set the daily photo quota."""
        if quota < 1:
            await ctx.send("Quota must be at least 1.")
            return
        await self.config.guild(ctx.guild).quota.set(quota)
        await ctx.send(f"Quota set to {quota}.")

    @pp_set.command(name="strikewindow")
    async def pp_set_strikewindow(self, ctx: commands.Context, days: int):
        """Set the rolling strike window, in days."""
        if days < 1:
            await ctx.send("Window must be at least 1 day.")
            return
        await self.config.guild(ctx.guild).strike_window_days.set(days)
        await ctx.send(f"Strike window set to {days} days.")

    @pp_set.command(name="strikethreshold")
    async def pp_set_strikethreshold(self, ctx: commands.Context, threshold: int):
        """Set how many strikes (within the window) cost the job role."""
        if threshold < 1:
            await ctx.send("Threshold must be at least 1.")
            return
        await self.config.guild(ctx.guild).strike_threshold.set(threshold)
        await ctx.send(f"Strike threshold set to {threshold}.")

    # ------------------------------------------------------------------
    # Poll posting + tracking
    # ------------------------------------------------------------------

    async def _post_poll(self, channel: discord.TextChannel, question: str, entries: list[tuple[str, str, Path]], kind: str) -> None:
        """entries: [(letter, label, image_path), ...]. Posts the photo embed
        message first, then the native poll with matching lettered options.
        """
        photo_embeds, files = embeds.build_photo_message(entries)
        await channel.send(embeds=photo_embeds, files=files)

        poll = discord.Poll(question=question, duration=DEFAULT_POLL_DURATION_HOURS)
        option_labels = {}
        for letter, label, _path in entries:
            poll.add_answer(text=f"{letter} — {label}")
            option_labels[letter] = label
        poll_message = await channel.send(poll=poll)

        closes_at = datetime.now(tz=ZoneInfo("UTC")) + timedelta(hours=DEFAULT_POLL_DURATION_HOURS)
        gconf = self.config.guild(channel.guild)
        async with gconf.active_polls() as active_polls:
            active_polls[str(poll_message.id)] = {
                "channel_id": channel.id,
                "kind": kind,
                "closes_at": closes_at.isoformat(),
                "option_labels": option_labels,
            }

    async def _close_poll(self, guild: discord.Guild, message_id: str, record: dict) -> None:
        gconf = self.config.guild(guild)
        channel = guild.get_channel(record["channel_id"])
        if channel is None:
            async with gconf.active_polls() as active_polls:
                active_polls.pop(message_id, None)
            return

        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            async with gconf.active_polls() as active_polls:
                active_polls.pop(message_id, None)
            return

        poll = message.poll
        results_embed = discord.Embed(title="Poll results", color=embeds.COLOR_NEUTRAL)
        if poll is None or not poll.answers:
            results_embed.description = "No results available."
        else:
            max_votes = max(answer.vote_count for answer in poll.answers)
            lines = []
            for answer in sorted(poll.answers, key=lambda a: a.vote_count, reverse=True):
                answer_text = getattr(answer, "text", None) or answer.media.text
                crown = "\N{CROWN} " if answer.vote_count == max_votes and max_votes > 0 else ""
                lines.append(f"{crown}{answer_text} — {answer.vote_count}")
            results_embed.description = "\n".join(lines)
            winners = [a for a in poll.answers if a.vote_count == max_votes]
            if max_votes > 0 and len(winners) > 1:
                results_embed.set_footer(text="It's a tie.")

        await channel.send(embed=results_embed)
        async with gconf.active_polls() as active_polls:
            active_polls.pop(message_id, None)

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

    if tasks is not None:

        @tasks.loop(minutes=15)
        async def _rollover_loop(self):
            for guild in self.bot.guilds:
                try:
                    await self._run_rollover_check(guild)
                except Exception:
                    self._log.exception("photodrop: rollover check failed for guild %s", guild.id)

        @tasks.loop(minutes=30)
        async def _weekly_poll_loop(self):
            for guild in self.bot.guilds:
                try:
                    await self._run_weekly_poll_check(guild)
                except Exception:
                    self._log.exception("photodrop: weekly poll check failed for guild %s", guild.id)

        @tasks.loop(minutes=5)
        async def _poll_close_loop(self):
            for guild in self.bot.guilds:
                try:
                    await self._run_poll_close_check(guild)
                except Exception:
                    self._log.exception("photodrop: poll close check failed for guild %s", guild.id)

        @_rollover_loop.before_loop
        @_weekly_poll_loop.before_loop
        @_poll_close_loop.before_loop
        async def _before_loops(self):
            await self.bot.wait_until_red_ready()

    async def _run_rollover_check(self, guild: discord.Guild) -> None:
        """Every current role-holder is checked independently: a day with no
        drop and no PTO for that specific person becomes a no-show for them,
        regardless of what any other current holder did that day.

        Walks every fully-elapsed day since the last time this ran, not just
        yesterday -- if the bot was offline across more than one rollover, a
        single catch-up run still flags every missed day instead of only the
        most recent one.

        Threshold is 1 strike by default, so a member can lose the role
        partway through a multi-day catch-up. `holders` is snapshotted once
        at the top, but role membership is re-checked live before each date
        for each member: once someone is fired mid-loop, no further missed
        days are recorded against them -- they're no longer the job holder,
        so a day after their firing isn't a day they failed at the job.
        """
        gconf = self.config.guild(guild)
        holders = await self._current_holders(guild)
        if not holders:
            return

        today = models.today_key(DEFAULT_TIMEZONE)
        last_rollover = await gconf.last_rollover_date()
        if last_rollover == today:
            return

        if last_rollover is not None:
            missed = models.missed_days(last_rollover, today)
            for member in holders:
                for date_key in missed:
                    if not await self._holds_role(member):
                        break
                    mconf = self.config.member(member)
                    member_state = await mconf.all()
                    if models.needs_no_show_check(member_state, date_key):
                        updated = models.record_no_show(member_state, date_key)
                        await mconf.streak.set(updated["streak"])
                        await mconf.strikes.set(updated["strikes"])
                        await mconf.history.set(updated["history"])
                        await self._check_and_apply_strikes(guild, member)

        await gconf.last_rollover_date.set(today)

    async def _run_weekly_poll_check(self, guild: discord.Guild) -> None:
        """One independent poll per current role-holder. Someone who lost the
        role mid-week is simply not in `holders` any more by Sunday, so their
        entry -- and any photos they submitted earlier in the week -- is left
        out of this week's poll entirely.
        """
        now = models.now_local(DEFAULT_TIMEZONE)
        if now.weekday() != DEFAULT_WEEKLY_POLL_WEEKDAY or now.hour < DEFAULT_WEEKLY_POLL_HOUR:
            return

        gconf = self.config.guild(guild)
        today = models.day_key(now)
        if await gconf.last_weekly_poll_date() == today:
            return

        channel = await self._member_channel(guild)
        holders = await self._current_holders(guild)
        if channel is None or not holders:
            await gconf.last_weekly_poll_date.set(today)
            return

        week_start = now - timedelta(days=now.weekday())

        for member in holders:
            member_state = await self.config.member(member).all()
            entries = []
            for i, day_dt in enumerate(models.week_dates(week_start)):
                date_key = models.day_key(day_dt)
                status = models.calendar_status(member_state, date_key)
                if status not in (STATUS_FULL, STATUS_TARDY):
                    continue
                raw_paths = storage.list_photos(self.data_dir, member.id, date_key)
                if not raw_paths:
                    continue
                letter = POLL_LETTERS[len(entries)]
                out_path = self.data_dir / "collages" / "weekly" / str(member.id) / today / f"{letter}.png"
                collage.save_collage(raw_paths, letter, out_path)
                entries.append((letter, WEEKDAY_NAMES[i], out_path))
                if len(entries) >= len(POLL_LETTERS):
                    break

            if entries:
                await self._post_poll(channel, embeds.poll_question_text(), entries, kind="weekly")

        await gconf.last_weekly_poll_date.set(today)

    async def _run_poll_close_check(self, guild: discord.Guild) -> None:
        """A small grace period after `closes_at` before fetching results.

        Discord finalizes a poll's tallies shortly after its expiry rather
        than the instant it passes, so reading `message.poll` right at
        expiry risks stale or incomplete vote counts. Waiting a few minutes
        gives Discord room to finish before the cog reads the results.
        """
        gconf = self.config.guild(guild)
        active_polls = await gconf.active_polls()
        now = datetime.now(tz=ZoneInfo("UTC"))
        for message_id, record in list(active_polls.items()):
            closes_at = datetime.fromisoformat(record["closes_at"])
            if now >= closes_at + timedelta(minutes=POLL_CLOSE_GRACE_MINUTES):
                await self._close_poll(guild, message_id, record)
