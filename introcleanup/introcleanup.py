import asyncio
import discord
from datetime import datetime, timezone, timedelta
from redbot.core import commands, Config
from redbot.core.bot import Red

MAX_RETRIES = 8
DELETE_DELAY = 2.0  # seconds between individual deletes


async def _safe_delete(msg: discord.Message) -> bool:
    """Delete a single message, retrying on any rate limit. Returns True if deleted."""
    for attempt in range(MAX_RETRIES):
        try:
            await msg.delete()
            return True
        except discord.NotFound:
            return False
        except discord.RateLimited as e:
            # discord.py raises RateLimited when retry_after exceeds max_ratelimit_timeout
            await asyncio.sleep(e.retry_after + 1.0)
        except discord.HTTPException as e:
            if e.status == 429:
                # Global rate limit or one discord.py didn't auto-handle
                await asyncio.sleep(10.0)
            else:
                return False
    return False


async def _safe_bulk_delete(channel: discord.TextChannel, chunk: list) -> tuple[int, list]:
    """
    Bulk-delete a chunk of messages (2–100, all <14 days old).
    Returns (deleted_count, failed_messages) where failed_messages need
    individual deletion fallback.
    """
    for attempt in range(MAX_RETRIES):
        try:
            await channel.delete_messages(chunk)
            return len(chunk), []
        except discord.RateLimited as e:
            await asyncio.sleep(e.retry_after + 1.0)
        except discord.HTTPException as e:
            if e.status == 429:
                await asyncio.sleep(10.0)
            else:
                # Non-429 error — fall back to individual
                return 0, chunk
    return 0, chunk


class IntroCleanup(commands.Cog):
    """Deletes intro messages from members who leave, are kicked, or banned."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x696E74726F, force_registration=True)
        self.config.register_guild(
            intros_channel_id=None,
            log_channel_id=None,
        )

    # ------------------------------------------------------------------
    # Event listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        try:
            intros_id = await self.config.guild(guild).intros_channel_id()
            log_id = await self.config.guild(guild).log_channel_id()

            if not intros_id:
                return

            intros_channel = guild.get_channel(intros_id)
            if not intros_channel:
                return

            deleted_count = await self._delete_member_messages(intros_channel, member.id)

            if deleted_count and log_id:
                log_channel = guild.get_channel(log_id)
                if log_channel:
                    await self._post_log(log_channel, member, deleted_count, trigger="left/kicked/banned")
        except Exception:
            self.bot.logger.exception(
                "IntroCleanup: error handling member_remove for %s (%d)", member, member.id
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name="introcleanup", aliases=["ic"], invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def introcleanup(self, ctx: commands.Context):
        """Manage the IntroCleanup cog."""
        await ctx.send_help(ctx.command)

    @introcleanup.command(name="setchannel")
    async def setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the intros channel to watch."""
        await self.config.guild(ctx.guild).intros_channel_id.set(channel.id)
        await ctx.send(f"✅ Intros channel set to {channel.mention}.")

    @introcleanup.command(name="setlog")
    async def setlog(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the mod log channel for cleanup events."""
        await self.config.guild(ctx.guild).log_channel_id.set(channel.id)
        await ctx.send(f"✅ Log channel set to {channel.mention}.")

    @introcleanup.command(name="settings")
    async def settings(self, ctx: commands.Context):
        """Show current IntroCleanup configuration."""
        intros_id = await self.config.guild(ctx.guild).intros_channel_id()
        log_id = await self.config.guild(ctx.guild).log_channel_id()

        intros_mention = f"<#{intros_id}>" if intros_id else "not set"
        log_mention = f"<#{log_id}>" if log_id else "not set"

        embed = discord.Embed(title="IntroCleanup Settings", color=discord.Color.blurple())
        embed.add_field(name="Intros Channel", value=intros_mention, inline=True)
        embed.add_field(name="Log Channel", value=log_mention, inline=True)
        await ctx.send(embed=embed)

    @introcleanup.command(name="sweep")
    @commands.max_concurrency(1, per=commands.BucketType.guild)
    async def sweep(self, ctx: commands.Context, dry_run: bool = False):
        """
        Scan the entire intros channel history and delete messages from users
        who are no longer in the server.

        Pass `True` as the argument for a dry run (no deletions, just a count).

        Example:
            .introcleanup sweep
            .introcleanup sweep True
        """
        intros_id = await self.config.guild(ctx.guild).intros_channel_id()
        log_id = await self.config.guild(ctx.guild).log_channel_id()

        if not intros_id:
            return await ctx.send("❌ No intros channel configured. Use `.introcleanup setchannel #channel` first.")

        intros_channel = ctx.guild.get_channel(intros_id)
        if not intros_channel:
            return await ctx.send("❌ Configured intros channel not found.")

        mode = "**DRY RUN** — no messages will be deleted." if dry_run else "Deleting messages, this may take a while…"
        status_msg = await ctx.send(f"🔍 Sweep started. {mode}")

        # Collect current member IDs for fast lookup
        member_ids = {m.id for m in ctx.guild.members}

        # Group messages by author (skip bots, skip current members)
        to_delete: dict[int, list[discord.Message]] = {}
        async for msg in intros_channel.history(limit=None, oldest_first=True):
            if msg.author.bot:
                continue
            if msg.author.id in member_ids:
                continue
            to_delete.setdefault(msg.author.id, []).append(msg)

        if not to_delete:
            return await status_msg.edit(content="✅ Sweep complete — no stale intro messages found.")

        total_msgs = sum(len(v) for v in to_delete.values())
        total_users = len(to_delete)

        if dry_run:
            lines = [f"**Dry run result:** {total_msgs} message(s) from {total_users} former member(s) would be deleted."]
            for uid, msgs in to_delete.items():
                lines.append(f"• <@{uid}> (ID: {uid}) — {len(msgs)} message(s)")
            chunks = []
            current = []
            for line in lines:
                if sum(len(x) for x in current) + len(line) > 1900:
                    chunks.append("\n".join(current))
                    current = [line]
                else:
                    current.append(line)
            if current:
                chunks.append("\n".join(current))
            await status_msg.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await ctx.send(chunk)
            return

        # Actual deletion
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        deleted_total = 0

        for uid, msgs in to_delete.items():
            bulk = [m for m in msgs if m.created_at > cutoff]
            old = [m for m in msgs if m.created_at <= cutoff]

            # Bulk delete recent messages (≤100 at a time, requires 2+)
            for i in range(0, len(bulk), 100):
                chunk = bulk[i:i + 100]
                if len(chunk) == 1:
                    if await _safe_delete(chunk[0]):
                        deleted_total += 1
                    await asyncio.sleep(DELETE_DELAY)
                    continue
                count, fallback = await _safe_bulk_delete(intros_channel, chunk)
                deleted_total += count
                for m in fallback:
                    if await _safe_delete(m):
                        deleted_total += 1
                    await asyncio.sleep(DELETE_DELAY)
                if not fallback:
                    await asyncio.sleep(1.0)

            # Individual delete for old messages (>14 days, can't bulk)
            for m in old:
                if await _safe_delete(m):
                    deleted_total += 1
                await asyncio.sleep(DELETE_DELAY)

            # Log this user
            if log_id:
                log_channel = ctx.guild.get_channel(log_id)
                if log_channel:
                    embed = discord.Embed(
                        title="IntroCleanup — Sweep",
                        color=discord.Color.orange(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.add_field(name="User ID", value=str(uid), inline=True)
                    embed.add_field(name="Messages Deleted", value=str(len(msgs)), inline=True)
                    embed.add_field(name="Trigger", value="manual sweep", inline=True)
                    embed.set_footer(text=f"Swept by {ctx.author} ({ctx.author.id})")
                    try:
                        await log_channel.send(embed=embed)
                    except discord.HTTPException:
                        pass

        await status_msg.edit(
            content=f"✅ Sweep complete. Deleted **{deleted_total}** message(s) from **{total_users}** former member(s)."
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _delete_member_messages(self, channel: discord.TextChannel, user_id: int) -> int:
        """Delete all non-bot messages from user_id in channel. Returns count deleted."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        to_bulk: list[discord.Message] = []
        to_single: list[discord.Message] = []

        async for msg in channel.history(limit=None):
            if msg.author.id != user_id:
                continue
            if msg.author.bot:
                continue
            if msg.created_at > cutoff:
                to_bulk.append(msg)
            else:
                to_single.append(msg)

        deleted = 0

        for i in range(0, len(to_bulk), 100):
            chunk = to_bulk[i:i + 100]
            if len(chunk) == 1:
                if await _safe_delete(chunk[0]):
                    deleted += 1
                await asyncio.sleep(DELETE_DELAY)
                continue
            count, fallback = await _safe_bulk_delete(channel, chunk)
            deleted += count
            for m in fallback:
                if await _safe_delete(m):
                    deleted += 1
                await asyncio.sleep(DELETE_DELAY)
            if not fallback:
                await asyncio.sleep(1.0)

        for m in to_single:
            if await _safe_delete(m):
                deleted += 1
            await asyncio.sleep(DELETE_DELAY)

        return deleted

    async def _post_log(
        self,
        log_channel: discord.TextChannel,
        member: discord.Member,
        count: int,
        trigger: str,
    ):
        embed = discord.Embed(
            title="IntroCleanup — Member Left",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        avatar_url = member.display_avatar.url if member.display_avatar else None
        embed.set_author(name=str(member), icon_url=avatar_url)
        embed.add_field(name="User", value=f"{member.mention} ({member.id})", inline=False)
        embed.add_field(name="Messages Deleted", value=str(count), inline=True)
        embed.add_field(name="Trigger", value=trigger, inline=True)
        embed.set_footer(text=f"Guild: {log_channel.guild.name}")
        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass
