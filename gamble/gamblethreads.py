from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from redbot.core import bank, commands, Config, checks
from redbot.core.bot import Red

# ---------------------------------------------------------------------------
# Wondercasino hub — how a "game" entry works
#
# Each registered game is a dict stored under Config guild key "games":
#   {
#       "emoji": "🃏",
#       "label": "Wonderjack",
#       "command": "wonderjack table",   # resolved via bot.get_command() at click time
#       "mode": "personal" | "shared" | "direct" | "modal",   # routing model, see below
#       "args": [{"label": "Amount"}, {"label": "Heads or Tails"}],  # modal mode only, 1-2 entries
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
# "direct":   no thread at all — the command is invoked immediately and
#             whatever it would normally ctx.send() is relayed back as an
#             ephemeral reply to the clicking user instead. For quick,
#             one-shot commands with no back-and-forth (Payday) where
#             opening/reusing a table would just be overhead.
# "modal":    same as "direct" (no thread, ephemeral reply), but for a
#             command that needs 1-2 arguments the hub can't supply on its
#             own (a recipient, an amount, a coinflip side, ...). Clicking
#             it pops a Discord modal collecting each argument as plain
#             text via `entry["args"]`; the raw text is appended to the
#             invocation exactly as if typed (e.g. `.bank transfer @user
#             100`), so Red's normal converters do the actual parsing —
#             see _ArgModal and `.gamblehub addmodalgame`.
#
# Session liveness for "shared" games is tracked purely off Discord's own
# thread state (does the stored thread ID still resolve, is it archived) —
# never by reading a specific game cog's internal Table/session objects.
# That's what keeps this generic across any future multiplayer cog: adding
# one costs a `.gamblehub addgame` (or `addmodalgame`) call, never new
# Python here.
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
    "payday": {
        "emoji": "\U0001F4B5",  # 💵
        "label": "Payday",
        "command": "payday",
        "mode": "direct",
    },
    "gamble": {
        "emoji": "\U0001F3B0",  # 🎰
        "label": "Open a Table",
        "command": "gamble",
        "mode": "personal",
    },
}

# Flagship keys get a dedicated, always-visible button on row 0 instead of
# being buried in the "More games…" dropdown. Order here also drives the
# button order (see HubView) — wonderjack, heist, payday, then "Open a
# Table" right before "Active Tables".
FLAGSHIP_KEYS = ("wonderjack", "heist", "payday", "gamble")

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
        label="Payday",
        emoji="\U0001F4B5",
        style=discord.ButtonStyle.blurple,
        custom_id="gamblehub:flagship:payday",
        row=0,
    )
    async def payday_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_hub_click(interaction, "payday")

    @discord.ui.button(
        label="Open a Table",
        emoji="\U0001F3B0",
        style=discord.ButtonStyle.blurple,
        custom_id="gamblehub:flagship:gamble",
        row=0,
    )
    async def gamble_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_hub_click(interaction, "gamble")

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


class _ArgModal(discord.ui.Modal):
    """Generic modal for hub games registered in "modal" mode — up to two
    typed arguments (a recipient, an amount, a coinflip side, ...) collected
    as plain text and appended to the command invocation exactly as if the
    player had typed them, e.g. `.bank transfer @Someone 100`. Red's own
    command converters (discord.Member, int, str, ...) do the actual
    parsing/validation from that point on; this class never converts
    anything itself, so it works unmodified for any command that takes at
    most two arguments — see `.gamblehub addmodalgame`."""

    def __init__(
        self,
        cog: "GambleThreads",
        member: discord.Member,
        command: commands.Command,
        entry: dict,
    ):
        super().__init__(title=entry["label"][:45])
        self.cog = cog
        self.member = member
        self.command = command
        self.entry = entry
        self.inputs: list[discord.ui.TextInput] = []
        for arg in entry.get("args", [])[:2]:
            text_input = discord.ui.TextInput(
                label=arg["label"][:45],
                placeholder=arg.get("placeholder", "")[:100],
                required=arg.get("required", True),
                max_length=200,
            )
            self.inputs.append(text_input)
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Blank/omitted optional fields are dropped rather than passed
        # through as empty tokens, so a command with one required + one
        # optional argument still resolves correctly when only the first
        # is filled in.
        extra_args = " ".join(i.value.strip() for i in self.inputs if i.value.strip())
        await self.cog._invoke_direct(
            interaction, self.member, self.command, extra_args=extra_args, entry=self.entry
        )


class _RunAgainView(discord.ui.View):
    """Attached to the ephemeral result of a "modal" mode game (see
    _ArgModal) so a player can immediately go again without re-opening the
    "More games…" dropdown and re-selecting the same entry from scratch
    every single time — built for repeat-play commands like Coinflip,
    where re-navigating the dropdown for every roll is the whole
    complaint. Deliberately NOT attached to "direct" mode results (Payday):
    those are cooldown-gated, so a one-click repeat wouldn't do anything
    useful. Non-persistent (5 min timeout) is fine here — it's only ever
    attached to an ephemeral followup, which stops being usable around the
    same window regardless (the interaction token expires), so there's
    nothing to survive a bot restart for."""

    def __init__(
        self,
        cog: "GambleThreads",
        member: discord.Member,
        command: commands.Command,
        entry: dict,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.member = member
        self.command = command
        self.entry = entry

    @discord.ui.button(label="Go Again", emoji="\U0001F501", style=discord.ButtonStyle.secondary)
    async def go_again(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                "This isn't your result to replay.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            _ArgModal(self.cog, self.member, self.command, self.entry)
        )


class _NoopTyping:
    """No-op stand-in for the object channel.typing() normally returns,
    usable as both `with` and `async with` since callers may use either."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _EphemeralRelay:
    """Stand-in for a real channel/thread when invoking a "direct" mode
    command (see DEFAULT_GAMES above). Covers everything a plain
    commands.Context / Red command-invocation pipeline touches on
    ctx.channel that doesn't go through ctx.send() itself — permission
    checks, typing indicators, name/mention in logging. The actual reply
    routing happens separately in _invoke_direct via ctx.interaction;
    this class's own .send() is only a fallback for the rare case
    something calls ctx.channel.send() directly instead of ctx.send()."""

    def __init__(self, interaction: discord.Interaction, retry_view: Optional[discord.ui.View] = None):
        self._interaction = interaction
        # Set only for "modal" mode games (see _invoke_direct) — attached
        # to whatever this relay ends up sending, so a repeat-play command
        # like Coinflip gets a "Go Again" button on its result instead of
        # forcing a full dropdown re-navigation for every single roll.
        # setdefault in send() below means a view the command supplies
        # itself always wins over this one.
        self._retry_view = retry_view
        self.guild = interaction.guild
        self.id = interaction.channel_id
        real_channel = interaction.channel
        self.name = getattr(real_channel, "name", "wondercasino")
        self.category_id = getattr(real_channel, "category_id", None)
        # Red's permission-rule resolution (requires.verify -> ...
        # _get_rule_from_ctx) reads ctx.channel.category directly, not just
        # .category_id — hit in production for Payday: any command gated by
        # Red's per-category/per-channel permission rules raised
        # AttributeError here and was silently swallowed by discord.py's
        # view on_error, so only users who never triggered that check (e.g.
        # while testing as the account with global admin) saw a reply.
        self.category = getattr(real_channel, "category", None)
        self.mention = f"<#{interaction.channel_id}>"
        # Red's Context.bot_permissions checks channel.type == private to
        # tell DMs apart from guild channels when there's no ctx.interaction
        # (which is the case here — we build ctx from a fake Message, not
        # from the interaction directly). Never a DM in this flow, so text
        # is always correct.
        self.type = getattr(real_channel, "type", discord.ChannelType.text)

    async def send(self, content=None, **kwargs):
        # Safety-net fallback only — normal flow never reaches this: with
        # ctx.interaction set (see _invoke_direct), Context.send() handles
        # the interaction directly and never calls channel.send() at all.
        # This only fires if something calls ctx.channel.send() itself.
        kwargs.pop("delete_after", None)
        kwargs.pop("reference", None)
        kwargs.pop("mention_author", None)
        kwargs.setdefault("ephemeral", True)
        if self._retry_view is not None:
            kwargs.setdefault("view", self._retry_view)
        if self._interaction.response.is_done():
            return await self._interaction.followup.send(content, **kwargs)
        return await self._interaction.response.send_message(content, **kwargs)

    async def trigger_typing(self):
        return None

    async def trigger_typing(self):
        return None

    def typing(self):
        return _NoopTyping()

    def permissions_for(self, _member):
        return discord.Permissions.all()


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
        if message.guild is None:
            return
        # Discord auto-posts a "<member> started a thread: ..." system
        # message in the parent channel whenever a thread is created via
        # the API without replying to an existing message — which is what
        # every hub click does. Left alone these pile up below the hub
        # embed and eventually bury it. The bot is always the author here
        # (it's the one creating the thread), so deleting its own message
        # needs no extra permissions.
        if message.type is discord.MessageType.thread_created:
            hub_channel = await self._hub_channel(message.guild)
            if hub_channel and message.channel.id == hub_channel.id:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            return
        if message.author.bot:
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
        member = interaction.user
        if entry["mode"] == "direct":
            # No defer here: direct-mode commands reply near-instantly, and
            # _invoke_direct makes the command's own first reply BE the
            # interaction's initial response — deferring first would only
            # add a pointless "thinking…" placeholder to clean up after.
            await self._invoke_direct(interaction, member, command)
            return
        if entry["mode"] == "modal":
            # Same reasoning as direct mode: the modal itself IS the
            # interaction's initial response, so no defer() here either —
            # _ArgModal.on_submit hands off to _invoke_direct once the
            # player fills it in, which does its own silent defer there.
            await interaction.response.send_modal(_ArgModal(self, member, command, entry))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if entry["mode"] == "shared":
            thread, created = await self._get_or_create_shared_thread(guild, key, entry["label"])
        else:
            thread, created = await self._get_or_create_personal_thread(guild, member)
        if thread is None:
            # edit_original_response (not followup.send) resolves the
            # "thinking…" placeholder from the defer() above in place,
            # instead of leaving it stuck alongside a brand-new message.
            await interaction.edit_original_response(
                content="No gambling channel is configured yet — ask a mod to run "
                "`.gambleset channel #wondercasino` first."
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
        await interaction.edit_original_response(content=f"You're set: {thread.mention}")

    async def _invoke_direct(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        command: commands.Command,
        extra_args: str = "",
        entry: Optional[dict] = None,
    ):
        """Run `command` with no thread at all (see "direct" mode above).
        Builds a fake invocation the same way _invoke_in_thread does, using
        an _EphemeralRelay as the channel — but deliberately leaves
        ctx.interaction unset (None), exactly like _invoke_in_thread does
        for every thread-based game. Red's hybrid-command permission check
        (used by some core commands, Payday included) special-cases
        ctx.interaction is not None by reading interaction._baton, a
        private discord.py field only populated by the library's own
        interaction-dispatch pipeline — never true for a synthetic
        invocation like this one. An earlier version of this method set
        ctx.interaction = interaction to route replies through it, which
        raised AttributeError('_MissingSentinel' object has no attribute
        'interaction') deep inside that check for exactly this reason.
        With ctx.interaction left None, Red treats this as a normal prefix
        invocation for permission purposes (safe — proven by every
        thread-based game already working this way); only ctx.send itself
        is redirected below, straight to _EphemeralRelay.send(), to reply
        through the interaction instead of posting to a real channel.

        `extra_args`, when set (modal mode — see _ArgModal), is appended
        to the invocation text verbatim, e.g. "@Someone 100" for a bank
        transfer. It's never parsed here; Red's own command converters do
        that exactly as they would for a real typed invocation, which is
        also why a bad value (an unresolvable member, a non-integer
        amount) surfaces as the command's normal argument-error reply
        rather than something this method has to anticipate.

        `entry`, also modal-mode only, is passed straight through to
        _EphemeralRelay so it can attach a "Go Again" button (see
        _RunAgainView) to whatever the command ends up sending — repeat-play
        games like Coinflip would otherwise need the dropdown re-opened and
        re-selected by hand for every single roll."""
        # A silent ack: no "thinking…" bubble, no visible change to the hub
        # message, but it satisfies Discord's 3-second response window —
        # which matters here, because checks/cooldowns/bank lookups inside
        # an arbitrary invoked command (Payday included) can occasionally
        # take longer than that. Once deferred, every reply below goes
        # through followup.send() instead of response.send_message().
        await interaction.response.defer(thinking=False)
        prefixes = await self.bot.get_prefix(interaction.channel)
        prefix = prefixes[0] if isinstance(prefixes, list) else prefixes
        # The hub message this button lives on doubles as our template
        # Message to build a fake invocation off of — always present for
        # any component interaction, no extra fetch needed.
        reference_message = interaction.message
        if reference_message is None:
            await interaction.followup.send(
                f"Couldn't run that here — try `{prefix}{command.qualified_name}` directly.",
                ephemeral=True,
            )
            return
        retry_view = None
        if entry is not None and entry.get("mode") == "modal":
            retry_view = _RunAgainView(self, member, command, entry)
        relay = _EphemeralRelay(interaction, retry_view=retry_view)
        fake_message = copy.copy(reference_message)
        fake_message.author = member
        fake_message.channel = relay
        fake_message.guild = interaction.guild
        fake_message.content = f"{prefix}{command.qualified_name}"
        if extra_args:
            fake_message.content += f" {extra_args}"
        ctx = await self.bot.get_context(fake_message)
        if not ctx.valid:
            await interaction.followup.send(
                f"Couldn't run that here — try `{prefix}{command.qualified_name}` directly.",
                ephemeral=True,
            )
            return
        ctx.send = relay.send

        # Recipient DM notification (see `.gamblehub notifyrecipient`) —
        # opt-in per game, and only meaningful when the game's first
        # argument is a recipient (Bank Transfer: yes; Coinflip: no, its
        # first arg is an amount, so this block just no-ops for it).
        # Detecting success by balance delta rather than parsing the
        # command's own reply text means this works for ANY currency-moving
        # command wired in this way, not just bank transfer specifically,
        # and it never misfires on a failed transfer (insufficient funds,
        # bad recipient) since the balance simply won't have moved.
        notify_target = None
        notify_before = None
        if entry is not None and entry.get("notify_recipient") and extra_args:
            first_token = extra_args.split(maxsplit=1)[0]
            try:
                candidate = await commands.MemberConverter().convert(ctx, first_token)
            except commands.BadArgument:
                candidate = None
            if candidate is not None and candidate.id != member.id:
                try:
                    notify_before = await bank.get_balance(candidate)
                    notify_target = candidate
                except Exception:
                    notify_target = None

        await self.bot.invoke(ctx)

        if notify_target is not None:
            try:
                notify_after = await bank.get_balance(notify_target)
            except Exception:
                notify_after = notify_before
            delta = notify_after - notify_before
            if delta > 0:
                currency = await bank.get_currency_name(interaction.guild)
                try:
                    await notify_target.send(
                        f"\U0001F4B0 **{member.display_name}** sent you **{delta:,}** "
                        f"{currency} in **{interaction.guild.name}**!"
                    )
                except discord.HTTPException:
                    pass  # DMs closed — no other notification channel to fall back to yet

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
        """Lists every currently-open gambling table — shared (e.g. the
        Wonderjack blackjack table) and personal (each player's own table,
        opened via Heist/Payday/Open a Table) alike."""
        guild = interaction.guild
        sessions = await self.config.guild(guild).shared_sessions()
        personal = await self.config.guild(guild).active_threads()
        games = await self.config.guild(guild).games()
        lines = []
        for key, info in sessions.items():
            thread = guild.get_thread(info["thread_id"])
            if thread is None or thread.archived:
                continue
            label = games.get(key, {}).get("label", key)
            member_count = thread.member_count if thread.member_count is not None else "?"
            lines.append(f"• **{label}** — {thread.mention} ({member_count} in thread)")
        for user_id, info in personal.items():
            thread = guild.get_thread(info["thread_id"])
            if thread is None or thread.archived:
                continue
            member = guild.get_member(int(user_id))
            owner_name = member.display_name if member else f"User {user_id}"
            member_count = thread.member_count if thread.member_count is not None else "?"
            lines.append(f"• **{owner_name}'s table** — {thread.mention} ({member_count} in thread)")
        if not lines:
            await interaction.response.send_message(
                "No active tables right now — click a game to start one.",
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
        if "payday" in games:
            view.payday_button.label = games["payday"]["label"]
            view.payday_button.disabled = False
        else:
            view.payday_button.disabled = True
        if "gamble" in games:
            view.gamble_button.label = games["gamble"]["label"]
            view.gamble_button.disabled = False
        else:
            view.gamble_button.disabled = True
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
        `mode` — `personal` (own thread every time), `shared` (one table, others join in),
                 or `direct` (no thread — just runs the command and relays its reply to
                 the clicking user; only safe for commands that take no arguments)
        `command` — the exact bot command to run, quoted if it has a space, e.g. "wonderjack table"
        `label` — display name, can have spaces, goes last

        Example: .gamblehub addgame slots 🎰 personal "casino slots" Slot Machine
        """
        key = key.lower()
        mode = mode.lower()
        if mode not in ("personal", "shared", "direct"):
            await ctx.send("`mode` must be `personal`, `shared`, or `direct`.")
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

    @gamblehub.command(name="addmodalgame")
    @commands.is_owner()
    async def gamblehub_addmodalgame(
        self,
        ctx: commands.Context,
        key: str,
        emoji: str,
        command: str,
        label: str,
        arg1_label: str,
        arg2_label: str = "",
    ):
        """Register a game that needs 1-2 arguments the hub can't supply on
        its own — a recipient, an amount, a coinflip side, and so on.
        Owner-only.

        Clicking it pops a modal with one text field per argument. Whatever
        gets typed is appended to the command exactly as if the player had
        typed it themselves (e.g. `.bank transfer @Someone 100`) — Red's own
        converters parse and validate from there, so a bad value (an
        unresolvable member, a non-integer amount) comes back as that
        command's normal argument error, same as typing it wrong manually.

        `key`/`emoji`/`command` — same as `.gamblehub addgame`
        `label` — display name shown on the dropdown/button, quote if it has spaces
        `arg1_label` — text shown above the first input field, e.g. "Recipient (mention or ID)"
        `arg2_label` — optional second input field, e.g. "Amount" — omit for single-argument commands

        For a recipient argument, tell players to paste a mention or user ID
        rather than typing a bare username — modals are plain text fields,
        so a name only resolves if it's an exact/unambiguous match.

        Examples:
        .gamblehub addmodalgame banktransfer 💸 "bank transfer" "Bank Transfer" "Recipient (mention or ID)" "Amount"
        .gamblehub addmodalgame allin 🎲 allin "All In" "Amount"
        .gamblehub addmodalgame coinflip 🪙 coin "Coinflip" "Amount" "Heads or Tails"

        Re-running this on an existing `key` updates it in place and keeps
        whatever `.gamblehub notifyrecipient` setting it already had.
        """
        key = key.lower()
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
        args = [{"label": arg1_label}]
        if arg2_label:
            args.append({"label": arg2_label})
        previous_entry = None
        async with self.config.guild(ctx.guild).games() as games:
            existed = key in games
            if existed:
                previous_entry = games[key]
            games[key] = {
                "emoji": emoji,
                "label": label,
                "command": resolved.qualified_name,
                "mode": "modal",
                "args": args,
                # Preserved across re-registration rather than reset to
                # off, so re-running this to tweak a label/arg doesn't
                # silently turn recipient notifications back off.
                "notify_recipient": bool(previous_entry.get("notify_recipient")) if existed else False,
            }
        error = await self._refresh_hub_message(ctx.guild)
        if error:
            async with self.config.guild(ctx.guild).games() as games:
                if previous_entry is not None:
                    games[key] = previous_entry
                else:
                    games.pop(key, None)
            await ctx.send(f"{error}\nNothing was saved — fix the emoji/label and try again.")
            return
        await ctx.send(
            f"{'Updated' if existed else 'Added'} **{label}** (`{key}`) → `.{resolved.qualified_name}`, "
            f"modal mode ({len(args)} argument{'s' if len(args) != 1 else ''}). Hub menu refreshed."
        )

    @gamblehub.command(name="notifyrecipient")
    @commands.is_owner()
    async def gamblehub_notifyrecipient(self, ctx: commands.Context, key: str, on_off: str):
        """Toggle a DM to whoever receives currency through a "modal" mode
        game (see `.gamblehub addmodalgame`) — e.g. "hey, evac sent you 100
        coins" after a Bank Transfer. Owner-only.

        Only meaningful when the game's FIRST argument is the recipient
        (a member), since that's the raw text this resolves and watches
        for a balance increase around the command running — Bank Transfer
        qualifies, Coinflip doesn't (its first argument is an amount, so
        turning this on for it just never fires — harmless, but pointless).
        Detection is by balance delta, not by reading the command's own
        reply, so a failed transfer (insufficient funds, bad recipient)
        correctly sends no notification.

        If the recipient has server/bot DMs closed, the notification is
        silently skipped — there's no other delivery path wired up yet.

        `on_off` — `on` or `off`

        Example: .gamblehub notifyrecipient banktransfer on
        """
        key = key.lower()
        on_off = on_off.lower()
        if on_off not in ("on", "off"):
            await ctx.send("Second argument must be `on` or `off`.")
            return
        async with self.config.guild(ctx.guild).games() as games:
            if key not in games:
                await ctx.send(f"No game registered under `{key}`.")
                return
            if games[key]["mode"] != "modal":
                await ctx.send(f"`{key}` isn't a modal-mode game — nothing to notify on.")
                return
            games[key]["notify_recipient"] = on_off == "on"
            label = games[key]["label"]
        await ctx.send(
            f"Recipient notifications for **{label}** are now {'on' if on_off == 'on' else 'off'}."
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
        lines = []
        for key, data in games.items():
            mode_suffix = data["mode"]
            if data["mode"] == "modal" and data.get("args"):
                arg_labels = ", ".join(a["label"] for a in data["args"])
                mode_suffix = f"modal: {arg_labels}"
                if data.get("notify_recipient"):
                    mode_suffix += ", notifies recipient"
            lines.append(
                f"`{key}` — {data['emoji']} **{data['label']}** → `.{data['command']}` ({mode_suffix}"
                f"{', flagship' if key in FLAGSHIP_KEYS else ''})"
            )
        await ctx.send("\n".join(lines))
