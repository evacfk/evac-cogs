"""MinigameHub -- main cog class.

Ties together everything else in this package: Config registration, the
scheduler that decides when/what to spawn, activity tracking wiring, and the
full `.minigamehub` / `.mgh` admin command tree. See the individual modules
(pacing.py, activity.py, stats.py, games/*) and the design doc
(claude/minigamehub-cog-design.md) for the reasoning behind each piece --
this file is mostly plumbing.
"""
import copy
import io
import json
import logging
import random
import time
from datetime import datetime
from typing import Optional

import discord
from discord.ext import tasks
from redbot.core import Config, bank, commands
from redbot.core.utils.chat_formatting import box, humanize_list, pagify

from . import pacing, scenarios as scenario_data, stats
from .activity import ActivityTracker, is_channel_active
from .config_schema import DEFAULT_GUILD, DEFAULT_MEMBER
from .constants import CONFIG_IDENTIFIER, GAME_KEYS, MOD_ROLE_ID, RESET_TIMEZONE, SCHEDULER_TICK_SECONDS
from .games import GAME_REGISTRY
from .settings_ui import ConfigView, HuntAnimalsView

log = logging.getLogger("red.minigamehub")

def _mod_check():
    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        if ctx.guild is None:
            return False
        if ctx.author.guild_permissions.manage_guild:
            return True
        role = ctx.guild.get_role(MOD_ROLE_ID)
        return role is not None and role in ctx.author.roles

    return commands.check(predicate)


def _fmt_range(lo, hi) -> str:
    return f"{lo:,}-{hi:,}"


def _stagger(games: dict, now: float, sep: float, reroll=()) -> None:
    """Give every enabled game its own future slot, at least `sep` apart.

    - Games in `reroll` get a fresh random time from their own min/max window.
    - Any other enabled game that is overdue (e.g. piled up while chat was
      quiet or another game was running) gets a fresh short random time
      instead of firing back-to-back.
    - Then timers are walked in order and any two closer than `sep` are
      nudged apart, so no two games land on top of each other.
    Mutates `games` in place.
    """
    for k, g in games.items():
        if not g.get("enabled"):
            continue
        lo, hi = g["min_frequency"], g["max_frequency"]
        if k in reroll:
            g["next_spawn"] = now + random.uniform(lo, hi)
        elif g.get("next_spawn", 0) <= now:
            g["next_spawn"] = now + random.uniform(sep, max(sep, lo))
    order = sorted((k for k, g in games.items() if g.get("enabled")), key=lambda k: games[k]["next_spawn"])
    prev = None
    for k in order:
        t = games[k]["next_spawn"]
        if prev is not None and t < prev + sep:
            t = prev + sep + random.uniform(0, sep)
            games[k]["next_spawn"] = t
        prev = t


def _schedule_lines(games: dict, now: float) -> list:
    rows = []
    for k in GAME_KEYS:
        g = games[k]
        if g.get("enabled"):
            rows.append((g.get("next_spawn", 0), k))
    rows.sort()
    lines = []
    for ts, k in rows:
        d = ts - now
        lines.append(f"{k:<12} {'due now' if d <= 0 else 'in ' + _fmt_secs(d)}")
    off = [k for k in GAME_KEYS if not games[k].get("enabled")]
    if off:
        lines.append(f"{'(off)':<12} {', '.join(off)}")
    return lines


class ScheduleView(discord.ui.View):
    """`.mgh schedule` output with a Shuffle button that re-rolls every timer."""

    def __init__(self, cog: "MinigameHub", guild: discord.Guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        role = self.guild.get_role(MOD_ROLE_ID)
        ok = (
            await self.cog.bot.is_owner(member)
            or getattr(member, "guild_permissions", None) and member.guild_permissions.manage_guild
            or (role is not None and role in getattr(member, "roles", []))
        )
        if not ok:
            await interaction.response.send_message("Mods only.", ephemeral=True)
        return bool(ok)

    @discord.ui.button(label="Shuffle", emoji="\U0001F500", style=discord.ButtonStyle.primary)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        lines = await self.cog._shuffle(self.guild)
        await interaction.response.edit_message(content=box("\n".join(lines), lang="text"), view=self)


def _fmt_secs(seconds) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    minutes = seconds / 60
    if minutes < 120:
        return f"{minutes:.1f}m"
    return f"{minutes / 60:.1f}h"


class MinigameHub(commands.Cog):
    """Unified, config-driven minigame spawner."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)

        self.active_game: dict = {}       # guild_id -> game key currently running
        self.trackers: dict = {}          # guild_id -> ActivityTracker
        self._seeded_guilds: set = set()  # guild_ids seeded this process, avoids re-checking every tick

        self.scheduler_loop.start()

    def cog_unload(self):
        self.scheduler_loop.cancel()

    # ------------------------------------------------------------------ #
    # Setup / seeding
    # ------------------------------------------------------------------ #

    async def _seed_scenarios(self, guild: discord.Guild) -> None:
        """Populate lootdrop/boss scenario pools from the built-in seed data
        (only if empty -- never clobbers a guild's edits), and backfill any
        game-config field that's been added to config_schema.py since this
        guild was first set up.

        Red's Config only applies registered defaults to a key that was never
        set at all -- once a guild has a non-empty `games` dict on disk (true
        for every guild that's ever toggled MinigameHub on), a brand new field
        added to DEFAULT_GUILD["games"][key] later (e.g. hunt's `trigger_mode`)
        will NOT retroactively appear for them, and code that does plain
        `game_conf[field]` indexing (the settings UI) will KeyError. Filling
        in anything missing here, once per process per guild, avoids that.
        """
        async with self.config.guild(guild).games() as games:
            for key in GAME_KEYS:
                defaults = DEFAULT_GUILD["games"][key]
                game_conf = games.setdefault(key, {})
                for field, default_val in defaults.items():
                    if field == "scenarios":
                        continue  # handled below, conditional on being empty
                    if field not in game_conf:
                        game_conf[field] = copy.deepcopy(default_val)
            if not games["lootdrop"]["scenarios"]:
                games["lootdrop"]["scenarios"] = copy.deepcopy(scenario_data.SEED_LOOTDROP_SCENARIOS)
            if not games["boss"]["scenarios"]:
                games["boss"]["scenarios"] = copy.deepcopy(scenario_data.SEED_BOSS_SCENARIOS)
        self._seeded_guilds.add(guild.id)

    def _get_tracker(self, guild: discord.Guild) -> ActivityTracker:
        tracker = self.trackers.get(guild.id)
        if tracker is None:
            tracker = ActivityTracker(self.config, guild)
            self.trackers[guild.id] = tracker
        return tracker

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._seed_scenarios(guild)

    # ------------------------------------------------------------------ #
    # Activity tracking wiring
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        spawn_channel_id = await self.config.guild(message.guild).spawn_channel()
        if message.channel.id != spawn_channel_id:
            return
        tracker = self._get_tracker(message.guild)
        tracker.on_message(message)
        await tracker.maybe_flush()

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #

    @tasks.loop(seconds=SCHEDULER_TICK_SECONDS)
    async def scheduler_loop(self):
        for guild in list(self.bot.guilds):
            try:
                await self._tick_guild(guild)
            except Exception:
                log.exception("Error ticking MinigameHub scheduler for guild %s", guild.id)

    @scheduler_loop.before_loop
    async def _before_scheduler(self):
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        if guild.id not in self._seeded_guilds:
            await self._seed_scenarios(guild)

        guild_conf = await self.config.guild(guild).all()
        if not guild_conf["enabled"]:
            return
        if self.active_game.get(guild.id):
            return  # a game is currently running -- one at a time (locked decision #7)

        channel = guild.get_channel(guild_conf["spawn_channel"])
        if channel is None:
            return

        tracker = self._get_tracker(guild)
        if not tracker.last_message_at:
            # No live data yet this process -- try a one-time bounded history seed
            # so the gate isn't permanently closed after every restart.
            await tracker.seed_from_history(channel)
        if not await is_channel_active(self.config, channel, tracker):
            return  # stay quiet in a dead channel; re-check next tick

        enabled_games = [k for k in GAME_KEYS if guild_conf["games"][k]["enabled"]]
        if not enabled_games:
            return

        # Each game type has its own next_spawn -- a long boss cooldown must
        # not block a short lootdrop cooldown (or vice versa). Only games
        # that are individually due are eligible this tick.
        now = time.time()
        due_games = [k for k in enabled_games if now >= guild_conf["games"][k].get("next_spawn", 0)]
        if not due_games:
            return
        candidates = [k for k in due_games if k != guild_conf["last_game"]] or due_games
        key = random.choice(candidates)
        game_conf = guild_conf["games"][key]

        # Claim the slot now so the next tick can't fire a second game before
        # the game handler sets active_game itself.
        self.active_game[guild.id] = key
        await self.config.guild(guild).last_game.set(key)
        self.bot.loop.create_task(self._run_game(guild, channel, key, game_conf))

    async def _shuffle(self, guild: discord.Guild) -> list:
        """Re-roll every enabled game's timer from its own window, spaced apart."""
        sep = await self.config.guild(guild).min_separation()
        now = time.time()
        async with self.config.guild(guild).games() as games:
            _stagger(games, now, sep, reroll=set(games))
            lines = _schedule_lines(games, now)
        return lines

    async def _run_game(self, guild: discord.Guild, channel: discord.TextChannel, key: str, game_conf: dict) -> None:
        try:
            await GAME_REGISTRY[key](self, channel, game_conf)
        except Exception:
            log.exception("MinigameHub game %r crashed in guild %s", key, guild.id)
        finally:
            # Hold the slot until timers are rewritten so a tick landing during
            # these awaits can't see stale timers and fire again immediately.
            self.active_game[guild.id] = key
            try:
                sep = await self.config.guild(guild).min_separation()
                async with self.config.guild(guild).games() as games:
                    _stagger(games, time.time(), sep, reroll={key})
            finally:
                self.active_game.pop(guild.id, None)

    # ------------------------------------------------------------------ #
    # Admin command tree
    # ------------------------------------------------------------------ #

    @commands.group(name="minigamehub", aliases=["mgh"])
    @commands.guild_only()
    @_mod_check()
    async def minigamehub(self, ctx: commands.Context):
        """Manage the unified minigame spawner."""

    @minigamehub.command(name="toggle")
    async def mgh_toggle(self, ctx: commands.Context):
        """Enable or disable spawning in this server."""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        state = "enabled" if not current else "disabled"
        if not current:
            await self._seed_scenarios(ctx.guild)
            # Fresh, spaced-out timers for every game rather than whatever
            # was left over from before.
            await self._shuffle(ctx.guild)
        await ctx.send(f"MinigameHub is now **{state}** in this server.")

    @minigamehub.command(name="schedule")
    async def mgh_schedule(self, ctx: commands.Context):
        """Show when each game fires next, with a Shuffle button."""
        games = await self.config.guild(ctx.guild).games()
        lines = _schedule_lines(games, time.time())
        await ctx.send(box("\n".join(lines), lang="text"), view=ScheduleView(self, ctx.guild))

    @minigamehub.command(name="shuffle")
    async def mgh_shuffle(self, ctx: commands.Context):
        """Re-roll every game's timer to a new random time from its own frequency, spaced apart."""
        lines = await self._shuffle(ctx.guild)
        await ctx.send(box("\n".join(lines), lang="text"), view=ScheduleView(self, ctx.guild))

    @minigamehub.command(name="spacing")
    async def mgh_spacing(self, ctx: commands.Context, minutes: Optional[float] = None):
        """Show or set the minimum minutes between any two games' scheduled times."""
        if minutes is None:
            sep = await self.config.guild(ctx.guild).min_separation()
            await ctx.send(f"Minimum spacing between games: {sep / 60:g} min.")
            return
        if minutes < 0:
            await ctx.send("Spacing can't be negative.")
            return
        await self.config.guild(ctx.guild).min_separation.set(round(minutes * 60))
        await ctx.send(f"Minimum spacing between games set to {minutes:g} min.")

    @minigamehub.command(name="channel")
    async def mgh_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Show or set the fixed spawn channel."""
        if channel is None:
            current_id = await self.config.guild(ctx.guild).spawn_channel()
            current = ctx.guild.get_channel(current_id)
            await ctx.send(f"Current spawn channel: {current.mention if current else f'`{current_id}` (not found)'}")
            return
        await self.config.guild(ctx.guild).spawn_channel.set(channel.id)
        await ctx.send(f"Spawn channel set to {channel.mention}.")

    @minigamehub.command(name="activitywindow")
    async def mgh_activitywindow(self, ctx: commands.Context, minutes: Optional[float] = None):
        """Show or set how recently a message must have appeared for spawns to fire (in minutes)."""
        if minutes is None:
            current = await self.config.guild(ctx.guild).activity_window()
            await ctx.send(f"Current activity window: {current / 60:.2f} min ({_fmt_secs(current)}).")
            return
        seconds = round(minutes * 60)
        if seconds < 10:
            await ctx.send("That's too short -- pick at least 1/6 of a minute (10 seconds).")
            return
        await self.config.guild(ctx.guild).activity_window.set(seconds)
        await ctx.send(f"Activity window set to {minutes:g} min ({_fmt_secs(seconds)}).")

    @minigamehub.command(name="settings")
    async def mgh_settings(self, ctx: commands.Context):
        """Browse and edit game settings with dropdowns (game -> parameter -> Set Value)."""
        view = ConfigView(self.config, ctx.guild)
        embed = await view.build_embed()
        await ctx.send(embed=embed, view=view)

    @minigamehub.command(name="huntanimals")
    async def mgh_huntanimals(self, ctx: commands.Context):
        """Add/edit/remove hunt's animal pool (emoji + spawn text) with a GUI --
        no JSON, no exact command syntax to remember."""
        games = await self.config.guild(ctx.guild).games()
        view = HuntAnimalsView(self.config, ctx.guild, games["hunt"]["animals"])
        await ctx.send(embed=view.build_embed(), view=view)

    @minigamehub.command(name="test")
    async def mgh_test(self, ctx: commands.Context, game_key: str):
        """Spawn one game right here for testing -- no currency or stats change.

        Runs in this channel regardless of the configured spawn channel or
        activity gate, so you can test from anywhere. Only one game (real or
        test) can be active per server at a time.
        """
        game_key = game_key.lower()
        if game_key not in GAME_KEYS:
            await ctx.send(f"Unknown game key. Choose from: {humanize_list(GAME_KEYS)}")
            return
        if self.active_game.get(ctx.guild.id):
            await ctx.send("A game (real or test) is already active in this server -- wait for it to resolve first.")
            return

        guild_conf = await self.config.guild(ctx.guild).all()
        game_conf = guild_conf["games"][game_key]
        if game_key in ("lootdrop", "boss") and not game_conf.get("scenarios"):
            await ctx.send(f"No scenarios configured for `{game_key}` yet -- run `.minigamehub toggle` once first so scenarios get seeded, or check `.minigamehub {'scenario' if game_key == 'lootdrop' else 'bossscenario'} list`.")
            return

        await ctx.send(f"\U0001F9EA Test-spawning `{game_key}` here -- no currency or stats will actually change.")
        await GAME_REGISTRY[game_key](self, ctx.channel, game_conf, dry_run=True)

    # -- game subgroup ------------------------------------------------- #

    @minigamehub.group(name="game")
    async def mgh_game(self, ctx: commands.Context):
        """Per-game-type settings."""

    @mgh_game.command(name="list")
    async def mgh_game_list(self, ctx: commands.Context):
        """Show every game type's current status."""
        guild_conf = await self.config.guild(ctx.guild).all()
        lines = []
        for key in GAME_KEYS:
            g = guild_conf["games"][key]
            status = "on" if g["enabled"] else "off"
            freq = f"{_fmt_secs(g['min_frequency'])}-{_fmt_secs(g['max_frequency'])}"
            lines.append(f"{key:<12} [{status:>3}]  every {freq}")
        await ctx.send(box("\n".join(lines), lang="text"))

    @mgh_game.command(name="toggle")
    async def mgh_game_toggle(self, ctx: commands.Context, game_key: str):
        """Enable or disable one game type."""
        game_key = game_key.lower()
        if game_key not in GAME_KEYS:
            await ctx.send(f"Unknown game key. Choose from: {humanize_list(GAME_KEYS)}")
            return
        sep = await self.config.guild(ctx.guild).min_separation()
        async with self.config.guild(ctx.guild).games() as games:
            games[game_key]["enabled"] = not games[game_key]["enabled"]
            state = "enabled" if games[game_key]["enabled"] else "disabled"
            if games[game_key]["enabled"]:
                _stagger(games, time.time(), sep, reroll={game_key})
        await ctx.send(f"`{game_key}` is now **{state}**.")

    @mgh_game.command(name="frequency")
    async def mgh_game_frequency(self, ctx: commands.Context, game_key: str, min_minutes: float, max_minutes: float):
        """Set how often (in minutes) one game type can spawn -- a random point
        between min and max is picked each time. Checked against the activity
        window separately, so this is "how often it's eligible", not a guarantee."""
        game_key = game_key.lower()
        if game_key not in GAME_KEYS:
            await ctx.send(f"Unknown game key. Choose from: {humanize_list(GAME_KEYS)}")
            return
        min_seconds, max_seconds = round(min_minutes * 60), round(max_minutes * 60)
        if min_seconds <= 0 or max_seconds < min_seconds:
            await ctx.send("min_minutes must be positive and max_minutes >= min_minutes.")
            return
        sep = await self.config.guild(ctx.guild).min_separation()
        async with self.config.guild(ctx.guild).games() as games:
            games[game_key]["min_frequency"] = min_seconds
            games[game_key]["max_frequency"] = max_seconds
            # Re-roll this game against its new window so an old timer doesn't linger.
            _stagger(games, time.time(), sep, reroll={game_key})
        await ctx.send(f"`{game_key}` frequency set to {min_minutes:g}-{max_minutes:g} min ({_fmt_secs(min_seconds)}-{_fmt_secs(max_seconds)}).")

    @mgh_game.command(name="reward")
    async def mgh_game_reward(self, ctx: commands.Context, game_key: str, min_amount: int, max_amount: int):
        """Set the reward range for one game type (boss uses `tier` instead)."""
        game_key = game_key.lower()
        if game_key not in GAME_KEYS:
            await ctx.send(f"Unknown game key. Choose from: {humanize_list(GAME_KEYS)}")
            return
        if game_key == "boss":
            await ctx.send("Boss rewards are per-tier -- use `.minigamehub game boss tier` instead.")
            return
        if min_amount < 0 or max_amount < min_amount:
            await ctx.send("min_amount must be >= 0 and max_amount >= min_amount.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            games[game_key]["reward_range"] = [min_amount, max_amount]
        await ctx.send(f"`{game_key}` reward range set to {_fmt_range(min_amount, max_amount)}.")

    @mgh_game.command(name="tier")
    async def mgh_game_tier(
        self, ctx: commands.Context, tier: str, hp_min: int, hp_max: int,
        reward_min: int, reward_max: int, penalty_min: int, penalty_max: int, weight: int,
    ):
        """Set a boss difficulty tier (`weak`/`medium`/`strong`)."""
        tier = tier.lower()
        async with self.config.guild(ctx.guild).games() as games:
            if tier not in games["boss"]["tiers"]:
                await ctx.send(f"Unknown tier. Choose from: {humanize_list(list(games['boss']['tiers'].keys()))}")
                return
            games["boss"]["tiers"][tier] = {
                "hp": [hp_min, hp_max],
                "reward": [reward_min, reward_max],
                "penalty": [penalty_min, penalty_max],
                "weight": weight,
            }
        await ctx.send(f"Boss tier `{tier}` updated.")

    @mgh_game.command(name="settings")
    async def mgh_game_settings(self, ctx: commands.Context, game_key: str, *, json_patch: Optional[str] = None):
        """View a game's full settings, or merge in a JSON object to change fields
        not covered by the other subcommands (e.g. `hunt`'s animal pool).

        Example: `.minigamehub game hunt settings {"shoot_word": "pew"}`
        """
        game_key = game_key.lower()
        if game_key not in GAME_KEYS:
            await ctx.send(f"Unknown game key. Choose from: {humanize_list(GAME_KEYS)}")
            return

        if json_patch is None:
            async with self.config.guild(ctx.guild).games() as games:
                current = games[game_key]
            dump = json.dumps(current, indent=2, ensure_ascii=False)
            for page in pagify(dump, delims=["\n"], page_length=1900):
                await ctx.send(box(page, lang="json"))
            return

        try:
            patch = json.loads(json_patch)
        except json.JSONDecodeError as e:
            await ctx.send(f"That's not valid JSON: {e}")
            return
        if not isinstance(patch, dict):
            await ctx.send("The patch must be a JSON object.")
            return

        async with self.config.guild(ctx.guild).games() as games:
            games[game_key].update(patch)
        await ctx.send(f"`{game_key}` settings updated: {humanize_list(list(patch.keys()))}.")

    # -- pacing subgroup ------------------------------------------------ #

    @minigamehub.group(name="pacing")
    async def mgh_pacing(self, ctx: commands.Context):
        """Per-user daily payout cap settings."""

    @mgh_pacing.command(name="show")
    async def mgh_pacing_show(self, ctx: commands.Context):
        conf = await self.config.guild(ctx.guild).payout_pacing()
        await ctx.send(box(json.dumps(conf, indent=2), lang="json"))

    @mgh_pacing.command(name="dailycap")
    async def mgh_pacing_dailycap(self, ctx: commands.Context, amount: int):
        """Set the per-user daily payout cap (across every game type combined)."""
        if amount <= 0:
            await ctx.send("Must be positive.")
            return
        async with self.config.guild(ctx.guild).payout_pacing() as pc:
            pc["daily_cap_per_user"] = amount
        await ctx.send(f"Daily payout cap set to {amount:,} per user.")

    @mgh_pacing.command(name="taperstart")
    async def mgh_pacing_taperstart(self, ctx: commands.Context, pct: float):
        """% of the daily cap at which rewards start tapering down."""
        if not 0 <= pct <= 100:
            await ctx.send("Must be between 0 and 100.")
            return
        async with self.config.guild(ctx.guild).payout_pacing() as pc:
            pc["taper_start_pct"] = pct
        await ctx.send(f"Taper start set to {pct}% of daily cap.")

    @mgh_pacing.command(name="taperfloor")
    async def mgh_pacing_taperfloor(self, ctx: commands.Context, pct: float):
        """Minimum reward multiplier (as a %) once a user is at/over their cap."""
        if not 0 <= pct <= 100:
            await ctx.send("Must be between 0 and 100.")
            return
        async with self.config.guild(ctx.guild).payout_pacing() as pc:
            pc["taper_floor_pct"] = pct
        await ctx.send(f"Taper floor set to {pct}%.")

    # -- stats / leaderboard -------------------------------------------- #

    @minigamehub.command(name="stats")
    async def mgh_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show a member's minigame stats."""
        member = member or ctx.author
        summary = await stats.get_member_summary(self.config, member)
        today_total, daily_cap = await pacing.payout_status(self.config, member)

        lines = [f"**{member.display_name}**'s minigame stats:"]
        for key in GAME_KEYS:
            entry = summary["games"].get(key)
            if not entry:
                continue
            lines.append(f"- {key}: {entry['good']} good / {entry['bad']} bad (best streak: {entry['highest_streak']})")
        if summary["boss_damage"]:
            lines.append(f"- Lifetime boss damage: {summary['boss_damage']:,}")
        lines.append(f"- Today's payouts: {today_total:,} / {daily_cap:,} daily cap")
        await ctx.send("\n".join(lines))

    @minigamehub.command(name="leaderboard")
    async def mgh_leaderboard(self, ctx: commands.Context, game_key: Optional[str] = None):
        """Server leaderboard. With no argument, shows top players per game type
        (most pets, most math questions solved, etc) plus overall and boss damage.
        With a game key, shows a single expanded top-15 for that game only."""
        all_members = await self.config.all_members(ctx.guild)
        if not all_members:
            await ctx.send("No stats recorded yet.")
            return

        if game_key:
            game_key = game_key.lower()
            if game_key not in GAME_KEYS:
                await ctx.send(f"Unknown game key. Choose from: {humanize_list(GAME_KEYS)}")
                return
            rows = []
            for member_id, data in all_members.items():
                member = ctx.guild.get_member(member_id)
                if member is None:
                    continue
                entry = data.get("games", {}).get(game_key)
                if entry and entry.get("good", 0) > 0:
                    rows.append((member, entry["good"], entry.get("highest_streak", 0)))
            if not rows:
                await ctx.send(f"No `{game_key}` stats recorded yet.")
                return
            rows.sort(key=lambda r: r[1], reverse=True)
            lines = [f"{i+1}. {m.display_name} -- {s} (best streak: {st})" for i, (m, s, st) in enumerate(rows[:15])]
            await ctx.send(f"**Leaderboard: {game_key}**\n" + "\n".join(lines))
            return

        # No game key -- one embed, one category per game type plus overall/boss damage.
        per_game: dict = {k: [] for k in GAME_KEYS}
        overall: list = []
        boss_damage: list = []
        for member_id, data in all_members.items():
            member = ctx.guild.get_member(member_id)
            if member is None:
                continue
            games_data = data.get("games", {})
            total = 0
            for key in GAME_KEYS:
                entry = games_data.get(key)
                if entry and entry.get("good", 0) > 0:
                    per_game[key].append((member, entry["good"]))
                    total += entry["good"]
            if total > 0:
                overall.append((member, total))
            if data.get("boss_damage", 0) > 0:
                boss_damage.append((member, data["boss_damage"]))

        if not overall:
            await ctx.send("No stats recorded yet.")
            return

        embed = discord.Embed(title="MinigameHub Leaderboards", color=discord.Color.gold())
        overall.sort(key=lambda r: r[1], reverse=True)
        embed.add_field(
            name="\U0001F3C6 Overall (total wins)",
            value="\n".join(f"{i+1}. {m.display_name} -- {s}" for i, (m, s) in enumerate(overall[:5])) or "--",
            inline=False,
        )
        for key in GAME_KEYS:
            rows = sorted(per_game[key], key=lambda r: r[1], reverse=True)
            if not rows:
                continue
            embed.add_field(
                name=f"{key}",
                value="\n".join(f"{i+1}. {m.display_name} -- {s}" for i, (m, s) in enumerate(rows[:5])),
                inline=True,
            )
        if boss_damage:
            boss_damage.sort(key=lambda r: r[1], reverse=True)
            embed.add_field(
                name="\U0001F5E1️ Boss damage (lifetime)",
                value="\n".join(f"{i+1}. {m.display_name} -- {s:,}" for i, (m, s) in enumerate(boss_damage[:5])),
                inline=True,
            )
        embed.set_footer(text="Use .minigamehub leaderboard <game> for a full top-15 of one game.")
        await ctx.send(embed=embed)

    # -- wipe (owner only) ------------------------------------------------ #

    @minigamehub.command(name="wipedata")
    @commands.is_owner()
    async def mgh_wipedata(self, ctx: commands.Context):
        """Reset this server's entire MinigameHub config to defaults. Bot owner only."""
        await ctx.send("This wipes ALL guild config (settings, scenarios, everything) back to defaults. Type `yes` to confirm.")
        try:
            msg = await self.bot.wait_for(
                "message",
                check=lambda m: m.author == ctx.author and m.channel == ctx.channel,
                timeout=20,
            )
        except Exception:
            await ctx.send("Timed out, nothing changed.")
            return
        if msg.content.strip().lower() != "yes":
            await ctx.send("Cancelled.")
            return
        await self.config.guild(ctx.guild).clear()
        self._seeded_guilds.discard(ctx.guild.id)
        await self._seed_scenarios(ctx.guild)
        await ctx.send("Guild config reset to defaults.")

    @minigamehub.command(name="wipestats")
    @commands.is_owner()
    async def mgh_wipestats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Wipe one member's stats, or every member's if none given. Bot owner only."""
        if member is not None:
            await self.config.member(member).clear()
            await ctx.send(f"Wiped stats for {member.display_name}.")
            return
        await ctx.send("This wipes stats for **every member** in this server. Type `yes` to confirm.")
        try:
            msg = await self.bot.wait_for(
                "message",
                check=lambda m: m.author == ctx.author and m.channel == ctx.channel,
                timeout=20,
            )
        except Exception:
            await ctx.send("Timed out, nothing changed.")
            return
        if msg.content.strip().lower() != "yes":
            await ctx.send("Cancelled.")
            return
        all_members = await self.config.all_members(ctx.guild)
        for member_id in list(all_members.keys()):
            fake = discord.Object(id=member_id)
            fake.guild = ctx.guild  # config.member() just needs .id and .guild
            await self.config.member(fake).clear()
        await ctx.send(f"Wiped stats for {len(all_members)} member(s).")

    # -- scenario CRUD (lootdrop) ---------------------------------------- #

    @minigamehub.group(name="scenario")
    async def mgh_scenario(self, ctx: commands.Context):
        """Manage lootdrop scenarios."""

    @mgh_scenario.command(name="list")
    async def mgh_scenario_list(self, ctx: commands.Context):
        async with self.config.guild(ctx.guild).games() as games:
            scenarios = games["lootdrop"]["scenarios"]
        lines = [f"{i}: {s['start'][:60]}" for i, s in enumerate(scenarios)]
        if not lines:
            await ctx.send("No lootdrop scenarios configured.")
            return
        for page in pagify("\n".join(lines), page_length=1900):
            await ctx.send(box(page, lang="text"))

    @mgh_scenario.command(name="remove")
    async def mgh_scenario_remove(self, ctx: commands.Context, index: int):
        async with self.config.guild(ctx.guild).games() as games:
            scenarios = games["lootdrop"]["scenarios"]
            if not 0 <= index < len(scenarios):
                await ctx.send("Index out of range -- see `.minigamehub scenario list`.")
                return
            removed = scenarios.pop(index)
        await ctx.send(f"Removed scenario {index}: {removed['start'][:60]}")

    @mgh_scenario.command(name="add")
    async def mgh_scenario_add(self, ctx: commands.Context, *, json_body: str):
        """Add one scenario. Needs `start`, `good`, `bad`, `button_text`, `button_emoji` keys as a JSON object."""
        try:
            scenario = json.loads(json_body)
        except json.JSONDecodeError as e:
            await ctx.send(f"That's not valid JSON: {e}")
            return
        required = {"start", "good", "bad", "button_text", "button_emoji"}
        missing = required - scenario.keys()
        if missing:
            await ctx.send(f"Missing keys: {humanize_list(list(missing))}")
            return
        async with self.config.guild(ctx.guild).games() as games:
            games["lootdrop"]["scenarios"].append(scenario)
        await ctx.send("Scenario added.")

    @mgh_scenario.command(name="export")
    async def mgh_scenario_export(self, ctx: commands.Context):
        async with self.config.guild(ctx.guild).games() as games:
            scenarios = games["lootdrop"]["scenarios"]
        buf = io.BytesIO(json.dumps(scenarios, indent=2, ensure_ascii=False).encode("utf-8"))
        await ctx.send(file=discord.File(buf, filename="lootdrop_scenarios.json"))

    @mgh_scenario.command(name="import")
    async def mgh_scenario_import(self, ctx: commands.Context, replace: bool = False):
        """Attach a JSON file (a list of scenario objects) to this command.
        `replace=True` overwrites the current pool instead of appending."""
        if not ctx.message.attachments:
            await ctx.send("Attach a `.json` file with this command.")
            return
        raw = await ctx.message.attachments[0].read()
        try:
            new_scenarios = json.loads(raw)
        except json.JSONDecodeError as e:
            await ctx.send(f"That's not valid JSON: {e}")
            return
        if not isinstance(new_scenarios, list):
            await ctx.send("The file must contain a JSON list of scenario objects.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            if replace:
                games["lootdrop"]["scenarios"] = new_scenarios
            else:
                games["lootdrop"]["scenarios"].extend(new_scenarios)
        await ctx.send(f"Imported {len(new_scenarios)} scenario(s){' (replaced pool)' if replace else ''}.")

    # -- scenario CRUD (boss) --------------------------------------------- #

    @minigamehub.group(name="bossscenario")
    async def mgh_bossscenario(self, ctx: commands.Context):
        """Manage boss-battle scenarios."""

    @mgh_bossscenario.command(name="list")
    async def mgh_bossscenario_list(self, ctx: commands.Context):
        async with self.config.guild(ctx.guild).games() as games:
            scenarios = games["boss"]["scenarios"]
        lines = [f"{i}: {s['start'][:60]} (hp {s['hp'][0]}-{s['hp'][1]})" for i, s in enumerate(scenarios)]
        if not lines:
            await ctx.send("No boss scenarios configured.")
            return
        for page in pagify("\n".join(lines), page_length=1900):
            await ctx.send(box(page, lang="text"))

    @mgh_bossscenario.command(name="remove")
    async def mgh_bossscenario_remove(self, ctx: commands.Context, index: int):
        async with self.config.guild(ctx.guild).games() as games:
            scenarios = games["boss"]["scenarios"]
            if not 0 <= index < len(scenarios):
                await ctx.send("Index out of range -- see `.minigamehub bossscenario list`.")
                return
            removed = scenarios.pop(index)
        await ctx.send(f"Removed boss scenario {index}: {removed['start'][:60]}")

    @mgh_bossscenario.command(name="add")
    async def mgh_bossscenario_add(self, ctx: commands.Context, *, json_body: str):
        """Add one boss scenario. Needs the lootdrop scenario keys plus `hp: [min, max]`."""
        try:
            scenario = json.loads(json_body)
        except json.JSONDecodeError as e:
            await ctx.send(f"That's not valid JSON: {e}")
            return
        required = {"start", "good", "bad", "button_text", "button_emoji", "hp"}
        missing = required - scenario.keys()
        if missing:
            await ctx.send(f"Missing keys: {humanize_list(list(missing))}")
            return
        async with self.config.guild(ctx.guild).games() as games:
            games["boss"]["scenarios"].append(scenario)
        await ctx.send("Boss scenario added.")

    @mgh_bossscenario.command(name="export")
    async def mgh_bossscenario_export(self, ctx: commands.Context):
        async with self.config.guild(ctx.guild).games() as games:
            scenarios = games["boss"]["scenarios"]
        buf = io.BytesIO(json.dumps(scenarios, indent=2, ensure_ascii=False).encode("utf-8"))
        await ctx.send(file=discord.File(buf, filename="boss_scenarios.json"))

    @mgh_bossscenario.command(name="import")
    async def mgh_bossscenario_import(self, ctx: commands.Context, replace: bool = False):
        if not ctx.message.attachments:
            await ctx.send("Attach a `.json` file with this command.")
            return
        raw = await ctx.message.attachments[0].read()
        try:
            new_scenarios = json.loads(raw)
        except json.JSONDecodeError as e:
            await ctx.send(f"That's not valid JSON: {e}")
            return
        if not isinstance(new_scenarios, list):
            await ctx.send("The file must contain a JSON list of scenario objects.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            if replace:
                games["boss"]["scenarios"] = new_scenarios
            else:
                games["boss"]["scenarios"].extend(new_scenarios)
        await ctx.send(f"Imported {len(new_scenarios)} boss scenario(s){' (replaced pool)' if replace else ''}.")

    # -- safe-animal CRUD (hunt) ------------------------------------------ #

    @minigamehub.group(name="huntsafe")
    async def mgh_huntsafe(self, ctx: commands.Context):
        """Manage hunt's safe animals -- the penalty for shooting one and the
        reward for saluting it instead."""

    @mgh_huntsafe.command(name="list")
    async def mgh_huntsafe_list(self, ctx: commands.Context):
        """Show every safe animal with its shoot penalty and salute reward."""
        async with self.config.guild(ctx.guild).games() as games:
            safe_animals = games["hunt"]["safe_animals"]
        if not safe_animals:
            await ctx.send("No safe animals configured -- every animal in the pool can be shot freely.")
            return
        lines = []
        for key, conf in safe_animals.items():
            pct = conf.get("penalty_pct", 0)
            lo, hi = conf.get("salute_reward", [0, 0])
            lines.append(f"{key:<12} penalty {pct:g}% of balance | salute reward {_fmt_range(lo, hi)}")
        await ctx.send(box("\n".join(lines), lang="text"))

    @mgh_huntsafe.command(name="add")
    async def mgh_huntsafe_add(
        self, ctx: commands.Context, animal_key: str, penalty_pct: float, reward_min: int, reward_max: int,
    ):
        """Mark an animal in hunt's pool as safe: shooting it costs `penalty_pct`
        of the shooter's balance, saluting it instead pays out `reward_min`-`reward_max`.

        Example: `.minigamehub huntsafe add eagle 8 50 200`
        """
        animal_key = animal_key.lower()
        if penalty_pct < 0 or penalty_pct > 100:
            await ctx.send("penalty_pct must be between 0 and 100.")
            return
        if reward_min < 0 or reward_max < reward_min:
            await ctx.send("reward_min must be >= 0 and reward_max >= reward_min.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            if animal_key not in games["hunt"]["animals"]:
                await ctx.send(
                    f"`{animal_key}` isn't in hunt's animal pool -- see `.minigamehub game hunt settings` "
                    "for the current pool, or add it there first."
                )
                return
            games["hunt"]["safe_animals"][animal_key] = {
                "penalty_pct": penalty_pct,
                "salute_reward": [reward_min, reward_max],
            }
        await ctx.send(
            f"`{animal_key}` is now safe: shooting it costs {penalty_pct:g}% of balance, "
            f"saluting it pays {_fmt_range(reward_min, reward_max)}."
        )

    @mgh_huntsafe.command(name="remove")
    async def mgh_huntsafe_remove(self, ctx: commands.Context, animal_key: str):
        """Unmark an animal as safe -- it goes back to a normal reward-only shoot."""
        animal_key = animal_key.lower()
        async with self.config.guild(ctx.guild).games() as games:
            if animal_key not in games["hunt"]["safe_animals"]:
                await ctx.send(f"`{animal_key}` isn't currently marked safe.")
                return
            del games["hunt"]["safe_animals"][animal_key]
        await ctx.send(f"`{animal_key}` is no longer safe.")

    @mgh_huntsafe.command(name="penalty")
    async def mgh_huntsafe_penalty(self, ctx: commands.Context, animal_key: str, penalty_pct: float):
        """Change just the shoot penalty for an already-safe animal."""
        animal_key = animal_key.lower()
        if penalty_pct < 0 or penalty_pct > 100:
            await ctx.send("penalty_pct must be between 0 and 100.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            if animal_key not in games["hunt"]["safe_animals"]:
                await ctx.send(f"`{animal_key}` isn't marked safe yet -- use `.minigamehub huntsafe add` first.")
                return
            games["hunt"]["safe_animals"][animal_key]["penalty_pct"] = penalty_pct
        await ctx.send(f"`{animal_key}` shoot penalty set to {penalty_pct:g}% of balance.")

    @mgh_huntsafe.command(name="reward")
    async def mgh_huntsafe_reward(self, ctx: commands.Context, animal_key: str, reward_min: int, reward_max: int):
        """Change just the salute reward range for an already-safe animal."""
        animal_key = animal_key.lower()
        if reward_min < 0 or reward_max < reward_min:
            await ctx.send("reward_min must be >= 0 and reward_max >= reward_min.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            if animal_key not in games["hunt"]["safe_animals"]:
                await ctx.send(f"`{animal_key}` isn't marked safe yet -- use `.minigamehub huntsafe add` first.")
                return
            games["hunt"]["safe_animals"][animal_key]["salute_reward"] = [reward_min, reward_max]
        await ctx.send(f"`{animal_key}` salute reward set to {_fmt_range(reward_min, reward_max)}.")

    # -- migration from the four old cogs -------------------------------- #

    # -- diagnostics -------------------------------------------------------#

    @minigamehub.command(name="diagnostics")
    async def mgh_diagnostics(self, ctx: commands.Context):
        """Analyze recent activity in the spawn channel and suggest pacing."""
        guild_conf = await self.config.guild(ctx.guild).all()
        tracking = guild_conf["activity_tracking"]
        buckets = tracking.get("hourly_buckets", {})
        sampling_since = tracking.get("sampling_since")

        if not buckets:
            await ctx.send(
                "No activity data yet. Enable MinigameHub with `.minigamehub toggle` and let it run for a "
                "few days, or make sure the bot has read-message-history in the spawn channel for the initial seed."
            )
            return

        rows = []
        total_messages = 0
        for hour in range(24):
            b = buckets.get(str(hour))
            if not b or not b["message_count"]:
                rows.append((hour, 0, 0.0, 0))
                continue
            avg_concurrency = (b["concurrency_sum"] / b["concurrency_n"]) if b.get("concurrency_n") else 0.0
            rows.append((hour, b["message_count"], avg_concurrency, int(b.get("max_gap_seconds", 0))))
            total_messages += b["message_count"]

        lines = [f"{h:02d}:00  msgs={c:<5} avg_concurrent={ac:.1f}  max_gap={_fmt_secs(mg)}" for h, c, ac, mg in rows]
        since_txt = datetime.fromtimestamp(sampling_since, RESET_TIMEZONE).strftime("%Y-%m-%d") if sampling_since else "unknown"

        # Very rough heuristic: an hour is "busy" if it's within 40% of the
        # single busiest hour's message count. Suggest frequencies scaled so a
        # busy hour sees a handful of spawns and a dead hour sees none (the
        # activity gate handles dead hours anyway, so this mostly matters for
        # separating "busy" from "quiet-but-present").
        busiest = max((c for _, c, _, _ in rows), default=0)
        busy_hours = sum(1 for _, c, _, _ in rows if busiest and c >= busiest * 0.4)
        active_hours = sum(1 for _, c, _, _ in rows if c > 0)

        currency = await bank.get_currency_name(ctx.guild)
        current_caps = guild_conf["payout_pacing"]

        suggestion = {
            "activity_window": 300 if busy_hours >= 4 else 600,
            "payout_pacing": {
                "daily_cap_per_user": current_caps["daily_cap_per_user"],
            },
            "note": (
                f"Sampling since {since_txt}, {total_messages} messages seen in the spawn channel. "
                f"{active_hours}/24 hours have any activity, {busy_hours}/24 look 'busy' by this rough heuristic. "
                "Frequencies aren't auto-changed -- review and apply with "
                "`.minigamehub game <key> frequency <min> <max>`."
            ),
        }

        embed = discord.Embed(
            title="MinigameHub diagnostics",
            description=f"Currency: {currency} | Sampling since: {since_txt}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Hourly activity (America/Los_Angeles) -- 00:00-11:00",
            value=box("\n".join(lines[:12]), lang="text"),
            inline=False,
        )
        embed.add_field(
            name="Hourly activity (America/Los_Angeles) -- 12:00-23:00",
            value=box("\n".join(lines[12:]), lang="text"),
            inline=False,
        )
        embed.add_field(name="Suggestion", value=suggestion["note"], inline=False)

        payload = {
            "hourly_buckets": {str(h): {"message_count": c, "avg_concurrency": ac, "max_gap_seconds": mg} for h, c, ac, mg in rows},
            "sampling_since": since_txt,
            "current_config": guild_conf["games"],
            "current_pacing": current_caps,
            "suggestion": suggestion,
        }
        buf = io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
        await ctx.send(embed=embed, file=discord.File(buf, filename="minigamehub_diagnostics.json"))
