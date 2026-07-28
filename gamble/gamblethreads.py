import asyncio
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import commands, Config
from redbot.core.bot import Red


class GambleThreads(commands.Cog):
    """Per-user private gambling threads with an inactivity auto-close."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=845201937123, force_registration=True)
        default_guild = {
            "bot_channel_id": None,
            "timeout_minutes": 10,
            # user_id (str) -> {"thread_id": int, "last_activity": iso8601 str}
            "active_threads": {},
        }
        self.config.register_guild(**default_guild)
        self._cleanup_task = self.bot.loop.create_task(self._cleanup_loop())

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
            active = await self.config.guild(guild).active_threads()
            if not active:
                continue
            timeout_minutes = await self.config.guild(guild).timeout_minutes()
            now = datetime.now(timezone.utc)
            stale = []
            for user_id, info in active.items():
                last_activity = datetime.fromisoformat(info["last_activity"])
                if now - last_activity > timedelta(minutes=timeout_minutes):
                    stale.append((user_id, info["thread_id"]))
            if not stale:
                continue
            for user_id, thread_id in stale:
                thread = guild.get_thread(thread_id)
                if thread is None:
                    # Thread's already gone (manually deleted, etc.) — just drop tracking.
                    async with self.config.guild(guild).active_threads() as a:
                        a.pop(user_id, None)
                    continue
                try:
                    await thread.delete(reason="Gambling session timed out")
                except discord.Forbidden:
                    try:
                        await thread.send(
                            "⚠️ I'm missing the **Manage Threads** permission so I can't "
                            "delete this thread — someone will need to remove it manually."
                        )
                    except discord.HTTPException:
                        pass
                    # Leave it tracked so this doesn't spawn a duplicate table for the user
                    # and so it keeps retrying every cycle until permissions are fixed.
                    continue
                except discord.HTTPException:
                    continue
                async with self.config.guild(guild).active_threads() as a:
                    a.pop(user_id, None)

    # ---------- commands ----------

    @commands.command(name="gamble")
    @commands.guild_only()
    async def gamble(self, ctx: commands.Context):
        """Open a private gambling thread just for you."""
        guild = ctx.guild
        user_id = str(ctx.author.id)
        active = await self.config.guild(guild).active_threads()

        if user_id in active:
            existing = guild.get_thread(active[user_id]["thread_id"])
            if existing and not existing.archived:
                await ctx.send(
                    f"{ctx.author.mention} you already have an open table: {existing.mention}",
                    delete_after=10,
                )
                return
            async with self.config.guild(guild).active_threads() as a:
                a.pop(user_id, None)

        bot_channel_id = await self.config.guild(guild).bot_channel_id()
        channel = guild.get_channel(bot_channel_id) if bot_channel_id else ctx.channel
        if channel is None:
            channel = ctx.channel

        thread_name = f"gamble-{ctx.author.display_name}"[:100]
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
            reason=f"Gambling session for {ctx.author} ({ctx.author.id})",
        )

        try:
            await thread.add_user(ctx.author)
        except discord.HTTPException:
            pass

        timeout_minutes = await self.config.guild(guild).timeout_minutes()
        async with self.config.guild(guild).active_threads() as a:
            a[user_id] = {
                "thread_id": thread.id,
                "last_activity": datetime.now(timezone.utc).isoformat(),
            }

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
        user_id = str(message.author.id)
        active = await self.config.guild(message.guild).active_threads()
        info = active.get(user_id)
        if info and info["thread_id"] == message.channel.id:
            async with self.config.guild(message.guild).active_threads() as a:
                if user_id in a:
                    a[user_id]["last_activity"] = datetime.now(timezone.utc).isoformat()

    # ---------- admin config ----------

    @commands.group(name="gambleset")
    @commands.guild_only()
    async def gambleset(self, ctx: commands.Context):
        """Configure gambling threads."""

    @gambleset.command(name="channel")
    async def gambleset_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set which channel new gambling threads spawn from (usually bot-stuff)."""
        await self.config.guild(ctx.guild).bot_channel_id.set(channel.id)
        await ctx.send(f"Gambling threads will now be created in {channel.mention}.")

    @gambleset.command(name="timeout")
    async def gambleset_timeout(self, ctx: commands.Context, minutes: int):
        """Set the inactivity timeout in minutes (min 1)."""
        if minutes < 1:
            await ctx.send("Timeout must be at least 1 minute.")
            return
        await self.config.guild(ctx.guild).timeout_minutes.set(minutes)
        await ctx.send(f"Gambling threads now auto-close after {minutes} minute(s) of inactivity.")

    @gambleset.command(name="settings")
    async def gambleset_settings(self, ctx: commands.Context):
        """Show current settings."""
        channel_id = await self.config.guild(ctx.guild).bot_channel_id()
        timeout_minutes = await self.config.guild(ctx.guild).timeout_minutes()
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        await ctx.send(
            f"Spawn channel: {channel.mention if channel else 'not set (falls back to invoking channel)'}\n"
            f"Timeout: {timeout_minutes} minute(s)"
        )
