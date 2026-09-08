from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

# ---------------------------------------------------------------------------
# Wondercasino hub — how a "game" entry works
#
# Each registered game is a dict stored under Config guild key "games":
#   {
#       "emoji": "🃏",
#       "label": "Wonderjack",
#       "command": "wonderjack table",   # resolved via bot.get_command() at click time
#       "mode": "personal" | "shared",   # routing model, see below
#   }
#
# "personal": every player gets their own thread, reused across sessions
#             (same thread .gamble already manages). Heist and Gamble itself
#             live here.
# "shared":   one live thread per game at a time, guild-wide. The first
#             click creates it and starts the game; every subsequent click
#             on that button while the session is still live routes the
#             player into that SAME thread rather than fragmenting into
#             1-person tables. Wonderjack lives here.
#
# Session liveness for "shared" games is tracked purely off Discord's own
# thread state (does the stored thread ID still resolve, is it archived) —
# never by reading a specific game cog's internal Table/session objects.
# That's what keeps this generic across any future multiplayer cog: adding
# one costs a `.gamblehub addgame` call, never new Python here.
# ---------------------------------------------------------------------------

DEFAULT_GAMES = {
    "wonderjack": {
        "emoji": "\U0001F0CF",  # 🃏
        "label": "Wonderjack",
        "command": "wonderjack table",
        "mode": "shared",
    },
    "heist": {
        "emoji": "\U0001F3E6",  # 🏦
        "label": "Heist",
        "command": "heist start",
        "mode": "personal",
    },
    "gamble": {
        "emoji": "\U0001F3B0",  # 🎰
        "label": "Open a Table",
        "command": "gamble",
        "mode": "personal",
    },
}

FLAGSHIP_KEYS = ("wonderjack", "heist")
SHARED_THREAD_NAMES = {
    "wonderjack": "blackjack-table",
}


class HubView(discord.ui.View):
    """Persistent hub view. Registered once in cog_load so its fixed
    custom_ids keep routing correctly after a bot restart. The Select's
    options are rebuilt from Config and re-attached every time the hub
    message is actually rendered — the class-level custom_id is all that
    needs to survive a restart, not any particular option list."""

    def __init__(self, cog: "GambleThreads"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Wonderjack",
        emoji="\U0001F0CF",
        style=discord.ButtonStyle.blurple,
        custom_id="gamblehub:flagship:wonderjack",
        row=0,
    )
    async def wonderjack_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_hub_click(interaction, "wonderjack")

    @discord.ui.button(
        label="Heist",
        emoji="\U0001F3E6",
        style=discord.ButtonStyle.blurple,
        custom_id="gamblehub:flagship:heist",
        row=0,
    )
    async def heist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_hub_click(interaction, "heist")

    @discord.ui.button(
        label="Active Tables",
        emoji="\U0001F4CB",
        style=discord.ButtonStyle.grey,
        custom_id="gamblehub:active",
        row=0,
    )
    async def active_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_active_sessions(interaction)

    @discord.ui.select(
        placeholder="More games…",
        custom_id="gamblehub:select",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label="Loading…", value="__none__")],
        row=1,
    )
    async def game_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        value = select.values[0]
        if value == "__none__":
            await interaction.response.send_message(
                "No additional games are configured right now.", ephemeral=True
            )
            return
        await self.cog.handle_hub_click(interaction, value)


class GambleThreads(commands.Cog):
    """Per-user private gambling threads with an inactivity auto-close,
    plus the Wondercasino hub: a read-only channel with a persistent
    button/dropdown menu that routes players into threads instead of
    letting them post directly in the gambling channel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=845201937123, force_registration=True)
        default_guild = {
            "bot_channel_id": None,
            "timeout_minutes": 10,
            # user_id (str) -> {"thread_id": int, "last_activity": iso8601 str}
            "active_threads": {},
            # hub message tracking + game registry
            "hub_message_id": None,
            "games": DEFAULT_GAMES,
            "shared_timeout_minutes": 15,
            # game_key (str) -> {"thread_id": int, "last_activity": iso8601 str}
            "shared_sessions": {},
        }
        self.config.register_guild(**default_guild)
        self._cleanup_task = self.bot.loop.create_task(self._cleanup_loop())

    async def cog_load(self):
        # Fixed custom_ids on HubView make this safe to register once at
        # startup — Discord will route interactions on any live hub
        # message back to this same view even after a restart.
        self.bot.add_view(HubView(self))

    def cog_unload(self):
        self._cleanup_task.cancel()

    # ---------- background cleanup ----------

    async def _cleanup_loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._check_all_guilds()
            except Exception:
                pass
            await asyncio.sleep(60)

    async def _check_all_guilds(self):
        for guild in self.bot.guilds:
            await self._cleanup_thread_map(
                guild,
                config_attr=self.config.guild(guild).active_threads,
                timeout_minutes=await self.config.guild(guild).timeout_minutes(),
                delete_reason="Gambling session timed out",
            )
            await self._cleanup_thread_map(
                guild,
                config_attr=self.config.guild(guild).shared_sessions,
                timeout_minutes=await self.config.guild(guild).shared_timeout_minutes(),
                delete_reason="Shared table timed out",
            )

    async def _cleanup_thread_map(self, guild: discord.Guild, config_attr, timeout_minutes: int, delete_reason: str):
        """Shared cleanup for both the per-user active_threads map and the
        per-game shared_sessions map — same shape (key -> {thread_id,
        last_activity}), same inactivity-then-delete behavior."""
        active = await config_attr()
        if not active:
            return
        now = datetime.now(timezone.utc)
        stale = []
        for key, info in active.items():
            last_activity = datetime.fromisoformat(info["last_activity"])
            if now - last_activity > timedelta(minutes=timeout_minutes):
                stale.append((key, info["thread_id"]))
        if not stale:
            return
        for key, thread_id in stale:
            thread = guild.get_thread(thread_id)
            if thread is None:
                async with config_attr() as a:
                    a.pop(key, None)
                continue
            try:
                await thread.delete(reason=delete_reason)
            except discord.Forbidden:
                try:
                    await thread.send(
                        "⚠️ I'm missing the **Manage Threads** permission so I can't "
                        "delete this thread — someone will need to remove it manually."
                    )
                except discord.HTTPException:
                    pass
                # Leave it tracked so this doesn't spawn a duplicate table/session
                # and so it keeps retrying every cycle until permissions are fixed.
                continue
            except discord.HTTPException:
                continue
            async with config_attr() as a:
                a.pop(key, None)

    # ---------- thread provisioning ----------

    async def _hub_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        channel_id = await self.config.guild(guild).bot_channel_id()
        return guild.get_channel(channel_id) if channel_id else None

    async def _get_or_create_personal_thread(
        self, guild: discord.Guild, member: discord.Member
    ) -> tuple[Optional[discord.Thread], bool]:
        """Returns (thread, created). Reused by .gamble directly and by
        every 'personal' mode hub game."""
        user_id = str(member.id)
        active = await self.config.guild(guild).active_threads()

        if user_id in active:
            existing = guild.get_thread(active[user_id]["thread_id"])
            if existing and not existing.archived:
                return existing, False
            async with self.config.guild(guild).active_threads() as a:
                a.pop(user_id, None)

        channel = await self._hub_channel(guild)
        if channel is None:
            return None, False

        thread_name = f"\U0001F3B2 {member.display_name}'s table"[:100]
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
            reason=f"Gambling session for {member} ({member.id})",
        )

        try:
            await thread.add_user(member)
        except discord.HTTPException:
            pass

        async with self.config.guild(guild).active_threads() as a:
            a[user_id] = {
                "thread_id": thread.id,
                "last_activity": datetime.now(timezone.utc).isoformat(),
            }

        return thread, True

    async def _get_or_create_shared_thread(
        self, guild: discord.Guild, game_key: str, label: str
    ) -> tuple[Optional[discord.Thread], bool]:
        """Returns (thread, created). One live thread per (guild, game_key)
        at a time — later clicks reuse it instead of spawning duplicates."""
        sessions = await self.config.guild(guild).shared_sessions()

        if game_key in sessions:
            existing = guild.get_thread(sessions[game_key]["thread_id"])
            if existing and not existing.archived:
                return existing, False
            async with self.config.guild(guild).shared_sessions() as s:
                s.pop(game_key, None)

        channel = await self._hub_channel(guild)
        if channel is None:
            return None, False

        thread_name = SHARED_THREAD_NAMES.get(game_key, f"{label.lower()}-table")[:100]
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
            reason=f"Shared {label} session",
        )

        async with self.config.guild(guild).shared_sessions() as s:
            s[game_key] = {
                "thread_id": thread.id,
                "last_activity": datetime.now(timezone.utc).isoformat(),
            }

        return thread, True

    # ---------- commands ----------

    @commands.command(name="gamble")
    @commands.guild_only()
    async def gamble(self, ctx: commands.Context):
        """Open a private gambling thread just for you."""
        guild = ctx.guild
        thread, created = await self._get_or_create_personal_thread(guild, ctx.author)
        if thread is None:
            await ctx.send(
                "No gambling channel is configured yet — ask a mod to run "
                "`.gambleset channel #wondercasino` first."
            )
            return

        if not created:
            await ctx.send(
                f"{ctx.author.mention} you already have an open table: {thread.mention}",
                delete_after=10,
            )
            return

        timeout_minutes = await self.config.guild(guild).timeout_minutes()
        await thread.send(
            f"🎰 {ctx.author.mention} this is your table. Run your usual game commands right "
            f"here. Closes automatically after {timeout_minutes} min of inactivity, or type "
            f"`.endgambling` to close it now."
        )
        await ctx.send(
            f"{ctx.author.mention} your table is ready: {thread.mention}", delete_after=10
        )

    @commands.command(name="endgambling")
    async def endgambling(self, ctx: commands.Context):
        """Close your gambling thread (run this inside the thread)."""
        guild = ctx.guild
        user_id = str(ctx.author.id)
        active = await self.config.guild(guild).active_threads()
        info = active.get(user_id)

        if not info or ctx.channel.id != info["thread_id"]:
            await ctx.send(
                "You don't have an open gambling table in this thread.", delete_after=10
            )
            return

        async with self.config.guild(guild).active_threads() as a:
            a.pop(user_id, None)

        try:
            await ctx.send("Closing this table. Thanks for playing! 🎲")
        except discord.HTTPException:
            pass
        try:
            await ctx.channel.delete(reason=f"Gambling session ended by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(
                "⚠️ I couldn't delete this thread — I'm missing the **Manage Threads** "
                "permission in this channel/category. Please grant that to Hana."
            )
        except discord.HTTPException as e:
            await ctx.send(f"⚠️ Discord rejected the delete request: `{e}`")

    # ---------- activity tracking ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        guild = message.guild
        thread_id = message.channel.id

        user_id = str(message.author.id)
        active = await self.config.guild(guild).active_threads()
        info = active.get(user_id)
        if info and info["thread_id"] == thread_id:
            async with self.config.guild(guild).active_threads() as a:
                if user_id in a:
                    a[user_id]["last_activity"] = datetime.now(timezone.utc).isoformat()
            return

        sessions = await self.config.guild(guild).shared_sessions()
        for key, session_info in sessions.items():
            if session_info["thread_id"] == thread_id:
                async with self.config.guild(guild).shared_sessions() as s:
                    if key in s:
                        s[key]["last_activity"] = datetime.now(timezone.utc).isoformat()
                break

    # ---------- hub dispatch ----------

    async def handle_hub_click(self, interaction: discord.Interaction, key: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        games = await self.config.guild(guild).games()
        entry = games.get(key)
        if entry is None:
            await interaction.response.send_message(
                "That game isn't configured right now.", ephemeral=True
            )
            return

        command = self.bot.get_command(entry["command"])
        if command is None:
            await interaction.response.send_message(
                "That game's command isn't currently available — let a mod know.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        member = interaction.user

        if entry["mode"] == "shared":
            thread, created = await self._get_or_create_shared_thread(guild, key, entry["label"])
        else:
            thread, created = await self._get_or_create_personal_thread(guild, member)

        if thread is None:
            await interaction.followup.send(
                "No gambling channel is configured yet — ask a mod to run "
                "`.gambleset channel #wondercasino` first.",
                ephemeral=True,
            )
            return

        try:
            await thread.add_user(member)
        except discord.HTTPException:
            pass

        # We always need one real Message object in this thread to build a
        # fake invocation off of (see _invoke_in_thread). On creation we
        # control that directly by sending an anchor message ourselves —
        # relying on thread.history() here would race a just-created,
        # possibly still-empty thread. On reuse, the thread already has
        # prior activity, so history is safe to fall back on.
        anchor_message = None
        if entry["mode"] == "personal" and created:
            timeout_minutes = await self.config.guild(guild).timeout_minutes()
            anchor_message = await thread.send(
                f"🎰 {member.mention} this is your table. Run your usual game commands right "
                f"here. Closes automatically after {timeout_minutes} min of inactivity, or type "
                f"`.endgambling` to close it now."
            )
        elif entry["mode"] == "shared" and created:
            anchor_message = await thread.send(f"🎲 Starting **{entry['label']}**…")

        # `gamble` itself IS the thread-provisioning step above — nothing
        # further to invoke, opening the thread is the whole command.
        # For a *reused* shared thread, skip invoking too: the game's own
        # command already guards against a second concurrent table in the
        # same thread (e.g. wonderjack replies "A table is already open in
        # this channel."), so re-invoking on every new joiner just spams
        # that message instead of leaving them at the existing Join button.
        should_invoke = command.qualified_name != "gamble" and (entry["mode"] == "personal" or created)
        if should_invoke:
            await self._invoke_in_thread(thread, member, command, anchor_message=anchor_message)

        await interaction.followup.send(f"You're set: {thread.mention}", ephemeral=True)

    async def _invoke_in_thread(
        self,
        thread: discord.Thread,
        member: discord.Member,
        command: commands.Command,
        anchor_message: Optional[discord.Message] = None,
    ):
        """Run `command` inside `thread` as if `member` had typed it there.
        Goes through bot.invoke() (not ctx.invoke()) so the full normal
        pipeline — checks, cooldowns, converters — applies exactly as it
        would for a real typed invocation, including surfacing a cooldown
        or permission error back to the clicking user rather than
        silently skipping it."""
        prefixes = await self.bot.get_prefix(thread)
        prefix = prefixes[0] if isinstance(prefixes, list) else prefixes

        reference_message = anchor_message
        if reference_message is None:
            try:
                reference_message = [msg async for msg in thread.history(limit=1)][0]
            except (discord.HTTPException, IndexError):
                reference_message = None

        if reference_message is None:
            await thread.send(
                "⚠️ I couldn't set up that command in this thread — try running "
                f"`{prefix}{command.qualified_name}` here directly."
            )
            return

        fake_message = copy.copy(reference_message)
        fake_message.author = member
        fake_message.channel = thread
        fake_message.guild = thread.guild
        fake_message.content = f"{prefix}{command.qualified_name}"

        ctx = await self.bot.get_context(fake_message)
        if not ctx.valid:
            await thread.send(
                f"⚠️ Couldn't start that here automatically — try `{prefix}{command.qualified_name}` "
                "directly in this thread."
            )
            return
        await self.bot.invoke(ctx)

    async def show_active_sessions(self, interaction: discord.Interaction):
        guild = interaction.guild
        sessions = await self.config.guild(guild).shared_sessions()
        games = await self.config.guild(guild).games()

        if not sessions:
            await interaction.response.send_message(
                "No shared tables are open right now — click a game to start one.",
                ephemeral=True,
            )
            return

        lines = []
        for key, info in sessions.items():
            thread = guild.get_thread(info["thread_id"])
            if thread is None or thread.archived:
                continue
            label = games.get(key, {}).get("label", key)
            member_count = thread.member_count if thread.member_count is not None else "?"
            lines.append(f"• **{label}** — {thread.mention} ({member_count} in thread)")

        if not lines:
            await interaction.response.send_message(
                "No shared tables are open right now — click a game to start one.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---------- hub rendering ----------

    def _build_hub_embed(self, games: dict) -> discord.Embed:
        embed = discord.Embed(
            title="\U0001F3B0 Wondercasino",
            description=(
                "Pick a game below to get your own table. This channel is view-only — "
                "everything happens in the thread you're dropped into."
            ),
            color=discord.Color.gold(),
        )
        extra = [
            f"{data['emoji']} **{data['label']}**"
            for key, data in games.items()
            if key not in FLAGSHIP_KEYS
        ]
        if extra:
            embed.add_field(name="Also in the dropdown", value="\n".join(extra), inline=False)
        return embed

    def _build_hub_view(self, games: dict) -> HubView:
        view = HubView(self)
        options = [
            discord.SelectOption(
                label=data["label"],
                value=key,
                emoji=data.get("emoji") or None,
            )
            for key, data in games.items()
            if key not in FLAGSHIP_KEYS
        ]
        if not options:
            options = [discord.SelectOption(label="No other games yet", value="__none__")]
        view.game_select.options = options[:25]

        # Flagship buttons reflect current labels/emoji even though their
        # custom_id (and therefore routing) is fixed.
        if "wonderjack" in games:
            view.wonderjack_button.label = games["wonderjack"]["label"]
            view.wonderjack_button.disabled = False
        else:
            view.wonderjack_button.disabled = True
        if "heist" in games:
            view.heist_button.label = games["heist"]["label"]
            view.heist_button.disabled = False
        else:
            view.heist_button.disabled = True

        return view

    async def _refresh_hub_message(self, guild: discord.Guild) -> Optional[str]:
        """Re-renders the hub embed/view onto the stored hub message.
        Returns an error string on failure, None on success."""
        channel = await self._hub_channel(guild)
        if channel is None:
            return "No hub channel is set — run `.gambleset channel #wondercasino` first."

        message_id = await self.config.guild(guild).hub_message_id()
        games = await self.config.guild(guild).games()
        embed = self._build_hub_embed(games)
        view = self._build_hub_view(games)

        message = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        try:
            if message is None:
                message = await channel.send(embed=embed, view=view)
                await self.config.guild(guild).hub_message_id.set(message.id)
            else:
                await message.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            # Most likely cause: a bad emoji/label on a recently-added game
            # that Discord rejects at render time rather than at save time.
            return f"Discord rejected the hub menu update: `{e}`. Check emoji/labels on recently added games."

        return None

    # ---------- admin config: gambling threads ----------

    @commands.group(name="gambleset")
    @commands.guild_only()
    async def gambleset(self, ctx: commands.Context):
        """Configure gambling threads."""

    @gambleset.command(name="channel")
    async def gambleset_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the Wondercasino hub channel — where personal/shared threads spawn from."""
        await self.config.guild(ctx.guild).bot_channel_id.set(channel.id)
        await ctx.send(
            f"Gambling threads will now be created in {channel.mention}. "
            f"Run `.gamblehub setup` next to post the menu there."
        )

    @gambleset.command(name="timeout")
    async def gambleset_timeout(self, ctx: commands.Context, minutes: int):
        """Set the personal-thread inactivity timeout in minutes (min 1)."""
        if minutes < 1:
            await ctx.send("Timeout must be at least 1 minute.")
            return
        await self.config.guild(ctx.guild).timeout_minutes.set(minutes)
        await ctx.send(f"Personal gambling threads now auto-close after {minutes} minute(s) of inactivity.")

    @gambleset.command(name="sharedtimeout")
    async def gambleset_sharedtimeout(self, ctx: commands.Context, minutes: int):
        """Set the shared-table (e.g. blackjack-table) inactivity timeout in minutes (min 1)."""
        if minutes < 1:
            await ctx.send("Timeout must be at least 1 minute.")
            return
        await self.config.guild(ctx.guild).shared_timeout_minutes.set(minutes)
        await ctx.send(f"Shared tables now auto-close after {minutes} minute(s) of inactivity.")

    @gambleset.command(name="settings")
    async def gambleset_settings(self, ctx: commands.Context):
        """Show current settings."""
        channel_id = await self.config.guild(ctx.guild).bot_channel_id()
        timeout_minutes = await self.config.guild(ctx.guild).timeout_minutes()
        shared_timeout = await self.config.guild(ctx.guild).shared_timeout_minutes()
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        await ctx.send(
            f"Hub channel: {channel.mention if channel else 'not set (falls back to invoking channel for `.gamble`)'}\n"
            f"Personal timeout: {timeout_minutes} minute(s)\n"
            f"Shared timeout: {shared_timeout} minute(s)"
        )

    # ---------- admin config: hub menu / game registry ----------

    @commands.group(name="gamblehub")
    @commands.guild_only()
    async def gamblehub(self, ctx: commands.Context):
        """Configure the Wondercasino hub menu."""

    @gamblehub.command(name="setup")
    @checks.mod_or_permissions(manage_guild=True)
    async def gamblehub_setup(self, ctx: commands.Context):
        """Post (or refresh) the hub menu in the configured hub channel."""
        error = await self._refresh_hub_message(ctx.guild)
        if error:
            await ctx.send(error)
            return
        await ctx.send("Hub menu posted/refreshed.")

    @gamblehub.command(name="addgame")
    @commands.is_owner()
    async def gamblehub_addgame(
        self,
        ctx: commands.Context,
        key: str,
        emoji: str,
        mode: str,
        command: str,
        *,
        label: str,
    ):
        """Register a game on the hub. Owner-only.

        `key` — short internal id, e.g. `casino-slots`
        `emoji` — shown on the button/dropdown entry
        `mode` — `personal` (own thread every time) or `shared` (one table, others join in)
        `command` — the exact bot command to run, quoted if it has a space, e.g. "wonderjack table"
        `label` — display name, can have spaces, goes last

        Example: .gamblehub addgame slots 🎰 personal "casino slots" Slot Machine
        """
        key = key.lower()
        mode = mode.lower()
        if mode not in ("personal", "shared"):
            await ctx.send("`mode` must be `personal` or `shared`.")
            return

        resolved = self.bot.get_command(command)
        if resolved is None:
            await ctx.send(
                f"`{command}` doesn't resolve to a real bot command — nothing was saved. "
                f"Check the spelling/quoting and try again."
            )
            return

        try:
            discord.PartialEmoji.from_str(emoji)
        except Exception:
            await ctx.send(f"`{emoji}` doesn't look like a valid emoji — nothing was saved.")
            return

        previous_entry = None
        async with self.config.guild(ctx.guild).games() as games:
            existed = key in games
            if existed:
                previous_entry = games[key]
            games[key] = {
                "emoji": emoji,
                "label": label,
                "command": resolved.qualified_name,
                "mode": mode,
            }

        error = await self._refresh_hub_message(ctx.guild)
        if error:
            # Roll back so a bad emoji/label doesn't leave a broken entry
            # sitting in the registry — restore the prior version if this
            # was an update, or drop it entirely if it was brand new.
            async with self.config.guild(ctx.guild).games() as games:
                if previous_entry is not None:
                    games[key] = previous_entry
                else:
                    games.pop(key, None)
            await ctx.send(f"{error}\nNothing was saved — fix the emoji/label and try again.")
            return

        await ctx.send(
            f"{'Updated' if existed else 'Added'} **{label}** (`{key}`) → `.{resolved.qualified_name}`, "
            f"{mode} mode. Hub menu refreshed."
        )

    @gamblehub.command(name="removegame")
    @commands.is_owner()
    async def gamblehub_removegame(self, ctx: commands.Context, key: str):
        """Remove a game from the hub. Owner-only."""
        key = key.lower()
        async with self.config.guild(ctx.guild).games() as games:
            if key not in games:
                await ctx.send(f"No game registered under `{key}`.")
                return
            removed = games.pop(key)

        error = await self._refresh_hub_message(ctx.guild)
        if error:
            await ctx.send(f"Removed **{removed['label']}** (`{key}`), but {error}")
            return
        await ctx.send(f"Removed **{removed['label']}** (`{key}`). Hub menu refreshed.")

    @gamblehub.command(name="lockdown")
    @checks.mod_or_permissions(manage_guild=True)
    async def gamblehub_lockdown(self, ctx: commands.Context):
        """Lock the hub channel down to view-only: denies @everyone Send
        Messages but explicitly allows Send Messages in Threads, so the
        hub menu never gets buried but hub-spawned threads stay usable."""
        channel = await self._hub_channel(ctx.guild)
        if channel is None:
            await ctx.send("No hub channel is set — run `.gambleset channel #wondercasino` first.")
            return

        everyone = ctx.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = False
        overwrite.send_messages_in_threads = True
        try:
            await channel.set_permissions(
                everyone, overwrite=overwrite, reason="Wondercasino hub lockdown"
            )
        except discord.Forbidden:
            await ctx.send(
                "I don't have permission to edit that channel's permissions — "
                "I need **Manage Channel/Permissions** there."
            )
            return

        await ctx.send(
            f"{channel.mention} is now view-only for @everyone: they can't post directly there, "
            f"but they can still send messages inside threads spawned from it."
        )

    @gamblehub.command(name="listgames")
    @checks.mod_or_permissions(manage_guild=True)
    async def gamblehub_listgames(self, ctx: commands.Context):
        """List every game currently registered on the hub."""
        games = await self.config.guild(ctx.guild).games()
        if not games:
            await ctx.send("No games registered.")
            return
        lines = [
            f"`{key}` — {data['emoji']} **{data['label']}** → `.{data['command']}` ({data['mode']}"
            f"{', flagship' if key in FLAGSHIP_KEYS else ''})"
            for key, data in games.items()
        ]
        await ctx.send("\n".join(lines))
