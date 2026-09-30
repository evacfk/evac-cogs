"""bumpreward: pay Red currency for successful Disboard /bump, auto-remind, post a daily bump board."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import discord
from discord.ext import tasks
from redbot.core import Config, bank, commands, errors

from . import engine

log = logging.getLogger("red.evac-cogs.bumpreward")

DEFAULT_ROLE_ID = 461511961494945812  # Bumper role pinged on reminders
SEEN_LIMIT = 100

DEFAULT_GUILD = {
    "enabled": False,
    "channel_id": None,            # bump channel; everything is ignored outside it
    "role_id": DEFAULT_ROLE_ID,
    "min_reward": 100,
    "max_reward": 250,
    "streak_bonus_pct": 10,        # extra % per bump in a row (same bumper, uninterrupted) after the first
    "streak_max": 5,               # max number of bonus steps (5 x 10% = +50%)
    "reminder_lead_seconds": 10,   # the single reminder goes out this many seconds before the server is bumpable (0 = exactly when ready)
    "mode": "free",                # "free" (2h cooldown) or "pro" (DISBOARD Pro: 30 min while <12 bumps/24h)
    "recent_bumps": [],            # timestamps of bumps we saw in the last 24h (Pro allowance tracking)
    "next_bump_at": 0.0,           # unix ts the server can be bumped again
    "reminded": True,              # True = nothing pending
    "last_bump_at": 0.0,
    "last_bumper": 0,              # who made the most recent bump (0 = unknown / run broken)
    "run_count": 0,                # how many bumps in a row last_bumper has made
    "daily": {"date": "", "counts": {}},     # bumps per member for the current LA day
    "weekly": {"key": "", "counts": {}},     # current ISO week (Mon-Sun, LA time)
    "monthly": {"key": "", "counts": {}},    # current calendar month (LA time)
}

DEFAULT_MEMBER = {
    "total_bumps": 0,              # all-time
}


class BumpReward(commands.Cog):
    """Currency rewards, bump-run bonuses, reminders and leaderboards for Disboard bumps."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xB0B0B0B1, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._seen: dict[int, None] = {}
        self._timers: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        self._loop.start()

    def cog_unload(self):
        self._loop.cancel()
        for task in self._timers.values():
            task.cancel()
        self._timers.clear()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _message_text(message: discord.Message) -> str:
        parts = [message.content or ""]
        for e in message.embeds:
            parts.append(e.title or "")
            parts.append(e.description or "")
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _bumper_id(message: discord.Message) -> int | None:
        meta = getattr(message, "interaction_metadata", None)
        user = getattr(meta, "user", None) if meta is not None else None
        if user is not None:
            return user.id
        inter = getattr(message, "interaction", None)  # older discord.py
        user = getattr(inter, "user", None) if inter is not None else None
        return user.id if user is not None else None

    def _remember(self, message_id: int) -> bool:
        """Return True if newly remembered, False if we've already handled this message."""
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.pop(next(iter(self._seen)))
        return True

    async def _send(self, guild: discord.Guild, channel_id: int, **kwargs):
        channel = guild.get_channel(channel_id)
        if channel is None:
            log.warning("bumpreward: channel %s not found in guild %s; message not sent", channel_id, guild.id)
            return None
        try:
            return await channel.send(**kwargs)
        except discord.HTTPException:
            log.exception("bumpreward: failed to post in %s", channel_id)
            return None

    async def _schedule_reminder(self, guild: discord.Guild, seconds: float, latency: float = 0.0):
        """Start a fresh reminder cycle. Caller holds the guild lock."""
        conf = self.config.guild(guild)
        await conf.next_bump_at.set(time.time() + seconds - latency)
        await conf.reminded.set(False)
        self._arm(guild)

    def _arm(self, guild: discord.Guild):
        """(Re)start the precise per-guild timer. The 15s loop is only a fallback."""
        old = self._timers.pop(guild.id, None)
        if old is not None and old is not asyncio.current_task():
            old.cancel()
        self._timers[guild.id] = asyncio.create_task(self._reminder_timer(guild))

    async def _reminder_timer(self, guild: discord.Guild):
        try:
            g = await self.config.guild(guild).all()
            if g["reminded"] or not g["next_bump_at"]:
                return
            fire_at = g["next_bump_at"] - g["reminder_lead_seconds"]
            await asyncio.sleep(max(fire_at - time.time(), 0))
            async with self._locks[guild.id]:
                await self._run_reminders(guild, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bumpreward: reminder timer failed for guild %s", guild.id)

    # ------------------------------------------------------------------ daily board

    async def _roll_day(self, guild: discord.Guild, today: str):
        """Post the finished day's board and start a fresh day. Caller holds the guild lock."""
        conf = self.config.guild(guild)
        daily = await conf.daily()
        old = daily.get("date", "")
        if old and old != today:
            if daily.get("counts"):
                await self._post_board(guild, daily)
            await conf.daily.set({"date": today, "counts": {}})
        elif not old:
            await conf.daily.set({"date": today, "counts": {}})

    async def _post_board(self, guild: discord.Guild, daily: dict):
        channel_id = await self.config.guild(guild).channel_id()
        if not channel_id:
            return
        counts = {k: v for k, v in daily["counts"].items() if v > 0}
        ranked = engine.rank_counts(counts, 10)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, n) in enumerate(ranked):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{prefix} <@{uid}> — **{n}** bump{'s' if n != 1 else ''}")
        total = sum(counts.values())
        embed = discord.Embed(
            title=f"🏆 Top bumpers — {daily['date']}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{total} bump{'s' if total != 1 else ''} today by {len(counts)} member{'s' if len(counts) != 1 else ''}")
        await self._send(guild, channel_id, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ reminders / board loop

    @tasks.loop(seconds=15)
    async def _loop(self):
        for guild in self.bot.guilds:
            try:
                await self._tick_guild(guild)
            except Exception:
                log.exception("bumpreward: tick failed for guild %s", guild.id)

    @_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_red_ready()

    async def _tick_guild(self, guild: discord.Guild):
        g = await self.config.guild(guild).all()
        if not g["enabled"] or not g["channel_id"]:
            return
        now = time.time()
        today = engine.day_key(now)
        needs_roll = g["daily"].get("date") != today
        pending = bool(g["next_bump_at"]) and not g["reminded"]
        if not needs_roll and not pending:
            return
        async with self._locks[guild.id]:
            if needs_roll:
                await self._roll_day(guild, today)
            if pending:
                await self._run_reminders(guild, now)
                timer = self._timers.get(guild.id)
                if (timer is None or timer.done()) and not await self.config.guild(guild).reminded():
                    self._arm(guild)   # e.g. after a restart/reload

    async def _run_reminders(self, guild: discord.Guild, now: float):
        """Send the single "bumpable soon" message once `now` is within the lead time. Caller holds the guild lock."""
        conf = self.config.guild(guild)
        g = await conf.all()
        if not g["enabled"] or not g["channel_id"] or g["reminded"] or not g["next_bump_at"]:
            return
        due = g["next_bump_at"]
        if now < due - g["reminder_lead_seconds"]:
            return
        await conf.reminded.set(True)   # first, so this can never send twice
        remaining = round(due - now)
        if remaining >= 1:
            text = f"⏳ The server can be bumped in {remaining} second{'s' if remaining != 1 else ''}! Use `/bump`."
        else:
            text = "🔔 The server can be bumped now! Use `/bump`."
        role_id = g["role_id"]
        ping = f"<@&{role_id}> " if role_id else ""
        allowed = discord.AllowedMentions(roles=[discord.Object(id=role_id)]) if role_id else None
        await self._send(guild, g["channel_id"], content=f"{ping}{text}", allowed_mentions=allowed)

    # ------------------------------------------------------------------ Disboard detection

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._handle(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Disboard defers its slash reply ("thinking...") and then edits in the real result.
        await self._handle(after)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.cached_message is not None or payload.guild_id is None:
            return  # on_message_edit covers cached messages
        data = payload.data or {}
        author = data.get("author") or {}
        if int(author.get("id", 0) or 0) != engine.DISBOARD_ID:
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        await self._handle(message)

    async def _handle(self, message: discord.Message):
        if message.guild is None or message.author.id != engine.DISBOARD_ID:
            return
        text = self._message_text(message)
        success = engine.is_success_text(text)
        cooldown_seconds = None if success else engine.parse_cooldown_seconds(text)
        if not success and cooldown_seconds is None:
            return

        guild = message.guild
        async with self._locks[guild.id]:
            g = await self.config.guild(guild).all()   # read under the lock so it can't be stale
            if not g["enabled"] or not g["channel_id"] or message.channel.id != g["channel_id"]:
                return
            if success:
                if not self._remember(message.id):
                    return
                await self._on_successful_bump(message, g)
            else:
                await self._on_cooldown_notice(guild, g, cooldown_seconds)

    async def _on_cooldown_notice(self, guild: discord.Guild, g: dict, cooldown_seconds: int):
        """Someone tried too early: use Disboard's own number to (re)sync the reminder timer."""
        target = time.time() + cooldown_seconds
        if g["reminded"] or abs(g["next_bump_at"] - target) > 180:
            await self._schedule_reminder(guild, cooldown_seconds)

    async def _on_successful_bump(self, message: discord.Message, g: dict):
        guild = message.guild
        uid = self._bumper_id(message)
        now = time.time()
        today = engine.day_key(now)

        # Always (re)start the reminder, even if we can't identify or pay the bumper.
        conf = self.config.guild(guild)
        recent = engine.prune_recent(await conf.recent_bumps(), now) + [now]
        await conf.recent_bumps.set(recent)
        cooldown = engine.cooldown_seconds(g["mode"], len(recent))
        created = getattr(message, "created_at", None)
        latency = engine.bump_latency(now, created.timestamp() if created else None)
        await self._schedule_reminder(guild, cooldown, latency)
        await conf.last_bump_at.set(now)
        await self._roll_day(guild, today)

        member = guild.get_member(uid) if uid else None
        if member is None:
            log.warning("bumpreward: could not resolve bumper for message %s (uid=%s)", message.id, uid)
            # Someone we can't identify bumped, so nobody's run continues.
            await conf.last_bumper.set(0)
            await conf.run_count.set(0)
            return

        run = engine.update_run(await conf.last_bumper(), await conf.run_count(), member.id)
        await conf.last_bumper.set(member.id)
        await conf.run_count.set(run)

        pct = engine.streak_bonus_pct(run, g["streak_bonus_pct"], g["streak_max"])
        base = engine.roll_reward(g["min_reward"], g["max_reward"])
        amount = engine.final_reward(base, pct)

        mconf = self.config.member(member)
        total = await mconf.total_bumps() + 1
        await mconf.total_bumps.set(total)

        daily = await conf.daily()
        counts = dict(daily.get("counts", {}))
        counts[str(member.id)] = counts.get(str(member.id), 0) + 1
        await conf.daily.set({"date": today, "counts": counts})
        await conf.weekly.set(engine.add_bump(await conf.weekly(), engine.week_key(now), member.id))
        await conf.monthly.set(engine.add_bump(await conf.monthly(), engine.month_key(now), member.id))

        currency = await bank.get_currency_name(guild)
        failure = None
        try:
            await bank.deposit_credits(member, amount)
        except errors.BalanceTooHigh:
            failure = "Couldn't pay out — their balance is at the maximum."
            log.warning("bumpreward: %s is at the max balance, reward skipped", member.id)
        except Exception:
            failure = "Couldn't pay out — something went wrong (it's been logged)."
            log.exception("bumpreward: deposit failed for %s", member.id)

        lines = [f"🎉 {member.mention} bumped the server!"]
        lines.append(failure or f"**+{amount:,} {currency}**")
        extras = []
        if run > 1:
            extras.append(f"🔥 {run} bumps in a row" + (f" · +{pct}% bonus" if pct else ""))
        extras.append(f"Bump #{total:,}")
        lines.append(" · ".join(extras))
        lines.append(f"⏰ Next bump reminder <t:{int(now + cooldown - latency)}:R>")
        if g["mode"] == "pro":
            lines.append(f"⚡ {len(recent)}/{engine.PRO_FAST_BUMP_LIMIT} bumps in the last 24h")
        embed = discord.Embed(description="\n".join(lines), color=discord.Color.green())
        await self._send(guild, g["channel_id"], embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ commands

    @commands.group(name="bumpreward", invoke_without_command=True)
    @commands.guild_only()
    async def bumpreward(self, ctx: commands.Context):
        """Bump rewards: leaderboards and (for mods) settings."""
        await ctx.send_help()

    @bumpreward.command(name="top")
    async def br_top(self, ctx: commands.Context, period: str = "all"):
        """Top bumpers. Use `all` (default), `week` or `month`."""
        p = period.lower()
        if p in ("all", "alltime", "all-time", "total"):
            data = await self.config.all_members(ctx.guild)
            counts = {str(uid): d.get("total_bumps", 0) for uid, d in data.items()}
            title, empty = "🏆 All-time top bumpers", "Nobody has bumped yet."
        elif p in ("week", "weekly"):
            counts = engine.bump_period(await self.config.guild(ctx.guild).weekly(), engine.week_key())
            title, empty = "🏆 Top bumpers — this week", "No bumps yet this week."
        elif p in ("month", "monthly"):
            counts = engine.bump_period(await self.config.guild(ctx.guild).monthly(), engine.month_key())
            title, empty = "🏆 Top bumpers — this month", "No bumps yet this month."
        else:
            return await ctx.send("Use `top`, `top week` or `top month`.")
        counts = {k: v for k, v in counts.items() if v > 0}
        if not counts:
            return await ctx.send(empty)
        ranked = engine.rank_counts(counts, 10)
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — **{n:,}** bump{'s' if n != 1 else ''}"
                 for i, (uid, n) in enumerate(ranked)]
        embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @bumpreward.command(name="status")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_status(self, ctx: commands.Context):
        """Last detected bump and the reminder timer (use this to check Disboard detection still works)."""
        g = await self.config.guild(ctx.guild).all()
        who = f"<@{g['last_bumper']}>" if g["last_bumper"] else "an unidentified member"
        last = f"<t:{int(g['last_bump_at'])}:R> by {who}" if g["last_bump_at"] else "none detected yet"
        if g["reminded"] or not g["next_bump_at"]:
            nxt = "nothing pending"
        else:
            nxt = f"<t:{int(g['next_bump_at'])}:R>"
        embed = discord.Embed(title="Bump status", color=discord.Color.blurple())
        embed.add_field(name="Last bump", value=last, inline=False)
        embed.add_field(name="Next reminder", value=nxt, inline=False)
        recent = engine.prune_recent(g["recent_bumps"], time.time())
        embed.add_field(name="Bumps in last 24h", value=f"{len(recent)} (seen by this cog; web bumps aren't visible)", inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @bumpreward.command(name="settings")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_settings(self, ctx: commands.Context):
        """Show current settings."""
        g = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="bumpreward settings", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value=str(g["enabled"]))
        embed.add_field(name="Channel", value=f"<#{g['channel_id']}>" if g["channel_id"] else "not set")
        embed.add_field(name="Ping role", value=f"<@&{g['role_id']}>" if g["role_id"] else "none")
        embed.add_field(name="Reward", value=f"{g['min_reward']:,}–{g['max_reward']:,}")
        embed.add_field(name="Streak bonus", value=f"+{g['streak_bonus_pct']}% per bump in a row, up to {g['streak_max']} steps "
                                                   f"(max +{g['streak_bonus_pct'] * g['streak_max']}%)")
        embed.add_field(name="Heads-up", value=f"{g['reminder_lead_seconds']}s before ready" if g["reminder_lead_seconds"] else "exactly when ready")
        embed.add_field(name="Mode", value=self._mode_text(g["mode"]))
        await ctx.send(embed=embed)

    @bumpreward.command(name="enabled")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_enabled(self, ctx: commands.Context, value: bool):
        """Turn rewards, reminders and the daily board on or off."""
        if value and not await self.config.guild(ctx.guild).channel_id():
            return await ctx.send("Set the bump channel first: `bumpreward channel #channel`.")
        await self.config.guild(ctx.guild).enabled.set(value)
        await ctx.send(f"bumpreward is now **{'on' if value else 'off'}**.")

    @bumpreward.command(name="channel")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the bump channel. Bumps anywhere else are ignored."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"Bump channel set to {channel.mention}.")

    @bumpreward.command(name="role")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_role(self, ctx: commands.Context, role: discord.Role = None):
        """Set the role pinged on reminders (omit to disable pings)."""
        await self.config.guild(ctx.guild).role_id.set(role.id if role else None)
        await ctx.send(f"Reminder role: {role.mention if role else 'none'}.", allowed_mentions=discord.AllowedMentions.none())

    @bumpreward.command(name="reward")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_reward(self, ctx: commands.Context, minimum: int, maximum: int):
        """Set the base reward range (before the in-a-row bonus)."""
        if minimum < 0 or maximum < 0:
            return await ctx.send("Rewards can't be negative.")
        lo, hi = sorted((minimum, maximum))
        await self.config.guild(ctx.guild).min_reward.set(lo)
        await self.config.guild(ctx.guild).max_reward.set(hi)
        await ctx.send(f"Base reward is now {lo:,}–{hi:,}.")

    @bumpreward.command(name="streak")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_streak(self, ctx: commands.Context, percent_per_bump: int, max_steps: int):
        """Set the bonus for bumps in a row: extra % per bump, and how many bumps it keeps growing."""
        if percent_per_bump < 0 or max_steps < 0:
            return await ctx.send("Values can't be negative.")
        await self.config.guild(ctx.guild).streak_bonus_pct.set(percent_per_bump)
        await self.config.guild(ctx.guild).streak_max.set(max_steps)
        await ctx.send(f"Bonus: +{percent_per_bump}% per bump in a row, capped at +{percent_per_bump * max_steps}%.")

    @bumpreward.command(name="lead")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_lead(self, ctx: commands.Context, seconds: int):
        """Set how many seconds before the server is bumpable the single reminder is sent (0 = exactly when ready)."""
        if not 0 <= seconds <= engine.MAX_LEAD_SECONDS:
            return await ctx.send(f"Seconds must be between 0 and {engine.MAX_LEAD_SECONDS}.")
        async with self._locks[ctx.guild.id]:
            await self.config.guild(ctx.guild).reminder_lead_seconds.set(seconds)
            if not await self.config.guild(ctx.guild).reminded():
                self._arm(ctx.guild)
        await ctx.send(f"Reminder will be sent **{seconds}s** before the server can be bumped." if seconds
                       else "Reminder will be sent exactly when the server can be bumped.")

    @staticmethod
    def _mode_text(mode: str) -> str:
        slow = engine.format_duration(engine.BUMP_COOLDOWN_SECONDS)
        if mode == "pro":
            fast = engine.format_duration(engine.PRO_FAST_COOLDOWN_SECONDS)
            return f"Pro — {fast} between bumps while under {engine.PRO_FAST_BUMP_LIMIT} bumps in 24h, otherwise {slow}"
        return f"Free — {slow} between bumps"

    @bumpreward.command(name="mode")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_mode(self, ctx: commands.Context, mode: str = None):
        """Show or set the server mode: `mode`, `mode free` or `mode pro` (DISBOARD Pro)."""
        conf = self.config.guild(ctx.guild)
        if mode is None:
            return await ctx.send(f"Mode: {self._mode_text(await conf.mode())}.")
        m = mode.lower()
        if m not in ("free", "pro"):
            return await ctx.send("Use `mode`, `mode free` or `mode pro`.")
        await conf.mode.set(m)
        await ctx.send(f"Mode set to **{self._mode_text(m)}**. Applies from the next bump.")

    @bumpreward.command(name="seed")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_seed(self, ctx: commands.Context, count: int):
        """Tell the cog how many bumps the server has had in the last 24h (web + pre-install bumps it can't see).

        Counted as happening now, so the cog stays on the slow cooldown until they age out (conservative).
        """
        if not 0 <= count <= 50:
            return await ctx.send("Count must be between 0 and 50.")
        async with self._locks[ctx.guild.id]:
            await self.config.guild(ctx.guild).recent_bumps.set([time.time()] * count)
        await ctx.send(f"Recording **{count}** bump{'s' if count != 1 else ''} in the last 24h. "
                       f"Pro's fast window reopens once they age out or the count drops below {engine.PRO_FAST_BUMP_LIMIT}.")

    @bumpreward.command(name="nexttimer")
    @commands.mod_or_permissions(manage_guild=True)
    async def br_nexttimer(self, ctx: commands.Context, minutes: int):
        """Hand over a running timer: remind in N minutes (use when switching off the YAGPDB reminder)."""
        if not 0 <= minutes <= 120:
            return await ctx.send("Minutes must be between 0 and 120.")
        async with self._locks[ctx.guild.id]:
            await self._schedule_reminder(ctx.guild, minutes * 60)
        await ctx.send(f"Reminder set for {minutes} minute{'s' if minutes != 1 else ''} from now.")
