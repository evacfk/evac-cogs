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

log = logging.getLogger("red.minigamehub")

# The four cogs this replaces, for `.minigamehub migrate` -- best-effort only,
# since this environment has no way to inspect their exact Config identifiers
# offline. If they're still loaded, we read through the live cog instance's
# own `.config` attribute instead of guessing an identifier.
_OLD_COG_NAMES = {
    "cashdrop": "mathdrop",
    "hunting": "hunt",
    "lootdrop": "lootdrop",
    "pupper": "pet",
}


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
        """Populate lootdrop/boss scenario pools from the built-in seed data,
        but only if they're empty -- never clobbers a guild's edits."""
        async with self.config.guild(guild).games() as games:
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
        if time.time() < guild_conf["next_spawn"]:
            return

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
        candidates = [k for k in enabled_games if k != guild_conf["last_game"]] or enabled_games
        key = random.choice(candidates)
        game_conf = guild_conf["games"][key]

        await self.config.guild(guild).last_game.set(key)
        self.bot.loop.create_task(self._run_game(guild, channel, key, game_conf))

    async def _run_game(self, guild: discord.Guild, channel: discord.TextChannel, key: str, game_conf: dict) -> None:
        try:
            await GAME_REGISTRY[key](self, channel, game_conf)
        except Exception:
            log.exception("MinigameHub game %r crashed in guild %s", key, guild.id)
        finally:
            self.active_game.pop(guild.id, None)
            min_f, max_f = game_conf["min_frequency"], game_conf["max_frequency"]
            next_spawn = time.time() + random.uniform(min_f, max_f)
            await self.config.guild(guild).next_spawn.set(next_spawn)

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
            # Kick the first spawn window off shortly rather than making
            # people wait out whatever next_spawn was left over from before.
            await self.config.guild(ctx.guild).next_spawn.set(time.time() + 30)
        await ctx.send(f"MinigameHub is now **{state}** in this server.")

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
    async def mgh_activitywindow(self, ctx: commands.Context, seconds: Optional[int] = None):
        """Show or set how recently a message must have appeared for spawns to fire."""
        if seconds is None:
            current = await self.config.guild(ctx.guild).activity_window()
            await ctx.send(f"Current activity window: {_fmt_secs(current)}.")
            return
        if seconds < 10:
            await ctx.send("That's too short -- pick at least 10 seconds.")
            return
        await self.config.guild(ctx.guild).activity_window.set(seconds)
        await ctx.send(f"Activity window set to {_fmt_secs(seconds)}.")

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
        async with self.config.guild(ctx.guild).games() as games:
            games[game_key]["enabled"] = not games[game_key]["enabled"]
            state = "enabled" if games[game_key]["enabled"] else "disabled"
        await ctx.send(f"`{game_key}` is now **{state}**.")

    @mgh_game.command(name="frequency")
    async def mgh_game_frequency(self, ctx: commands.Context, game_key: str, min_seconds: int, max_seconds: int):
        """Set how often (in seconds) one game type can spawn."""
        game_key = game_key.lower()
        if game_key not in GAME_KEYS:
            await ctx.send(f"Unknown game key. Choose from: {humanize_list(GAME_KEYS)}")
            return
        if min_seconds <= 0 or max_seconds < min_seconds:
            await ctx.send("min_seconds must be positive and max_seconds >= min_seconds.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            games[game_key]["min_frequency"] = min_seconds
            games[game_key]["max_frequency"] = max_seconds
        await ctx.send(f"`{game_key}` frequency set to {_fmt_secs(min_seconds)}-{_fmt_secs(max_seconds)}.")

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
        """Server leaderboard, overall or for one game type."""
        all_members = await self.config.all_members(ctx.guild)
        if not all_members:
            await ctx.send("No stats recorded yet.")
            return

        rows = []
        for member_id, data in all_members.items():
            member = ctx.guild.get_member(member_id)
            if member is None:
                continue
            if game_key:
                entry = data.get("games", {}).get(game_key.lower())
                score = entry["good"] if entry else 0
            else:
                score = sum(e.get("good", 0) for e in data.get("games", {}).values())
            if score > 0:
                rows.append((member, score))

        if not rows:
            await ctx.send("No stats recorded yet.")
            return
        rows.sort(key=lambda r: r[1], reverse=True)
        title = f"Leaderboard ({game_key})" if game_key else "Leaderboard (all games)"
        lines = [f"{i+1}. {m.display_name} -- {s} wins" for i, (m, s) in enumerate(rows[:15])]
        await ctx.send(f"**{title}**\n" + "\n".join(lines))

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

    # -- migration from the four old cogs -------------------------------- #

    @minigamehub.command(name="migrate")
    async def mgh_migrate(self, ctx: commands.Context):
        """Best-effort port of settings from cashdrop/hunting/lootdrop/pupper,
        if they're still loaded. Run this BEFORE unloading them."""
        ported = []
        skipped = []

        old_lootdrop = self.bot.get_cog("LootDrop") or self.bot.get_cog("Lootdrop")
        if old_lootdrop and hasattr(old_lootdrop, "config"):
            try:
                old_scenarios = await old_lootdrop.config.guild(ctx.guild).scenarios()
                if old_scenarios:
                    async with self.config.guild(ctx.guild).games() as games:
                        games["lootdrop"]["scenarios"] = old_scenarios
                    ported.append(f"lootdrop: {len(old_scenarios)} scenario(s)")
            except Exception:
                skipped.append("lootdrop (config shape didn't match, ported nothing)")
        else:
            skipped.append("lootdrop (cog not loaded)")

        for cog_name, new_key in (("CashDrop", "mathdrop"), ("Hunting", "hunt"), ("Pupper", "pet")):
            old_cog = self.bot.get_cog(cog_name)
            if old_cog is None or not hasattr(old_cog, "config"):
                skipped.append(f"{new_key} ({cog_name} not loaded)")
                continue
            skipped.append(f"{new_key} ({cog_name} found, but its settings shape isn't known here -- copy manually via `.minigamehub game {new_key} settings`)")

        msg = "**Migration report**\n"
        if ported:
            msg += "Ported:\n" + "\n".join(f"- {p}" for p in ported) + "\n"
        if skipped:
            msg += "Needs manual review:\n" + "\n".join(f"- {s}" for s in skipped)
        await ctx.send(msg)

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
        embed.add_field(name="Hourly activity (America/Los_Angeles)", value=box("\n".join(lines), lang="text"), inline=False)
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
