import asyncio
import discord
from datetime import datetime, timezone, timedelta
from redbot.core import commands, Config
from redbot.core.bot import Red


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
            # chunk to avoid 2000-char limit
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

            # Bulk delete (≤100 at a time, <14 days)
            for i in range(0, len(bulk), 100):
                chunk = bulk[i:i + 100]
                try:
                    await intros_channel.delete_messages(chunk)
                    deleted_total += len(chunk)
                except discord.HTTPException:
                    # Fall back to individual on error
                    for m in chunk:
                        try:
                            await m.delete()
                            deleted_total += 1
                        except discord.NotFound:
                            pass
                        await asyncio.sleep(1.1)
                await asyncio.sleep(0.5)

            # Individual delete for old messages
            for m in old:
                try:
                    await m.delete()
                    deleted_total += 1
                except discord.NotFound:
                    pass
                await asyncio.sleep(1.1)

            # Log each user
            if log_id:
                log_channel = ctx.guild.get_channel(log_id)
                if log_channel:
                    count = len(msgs)
                    embed = discord.Embed(
                        title="IntroCleanup — Sweep",
                        color=discord.Color.orange(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.add_field(name="User ID", value=str(uid), inline=True)
                    embed.add_field(name="Messages Deleted", value=str(count), inline=True)
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

        # Bulk delete in batches of 100
        for i in range(0, len(to_bulk), 100):
            chunk = to_bulk[i:i + 100]
            try:
                await channel.delete_messages(chunk)
                deleted += len(chunk)
            except discord.HTTPException:
                for m in chunk:
                    try:
                        await m.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    await asyncio.sleep(1.1)
            await asyncio.sleep(0.5)

        # Individual delete for old messages
        for m in to_single:
            try:
                await m.delete()
                deleted += 1
            except discord.NotFound:
                pass
            await asyncio.sleep(1.1)

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
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention} ({member.id})", inline=False)
        embed.add_field(name="Messages Deleted", value=str(count), inline=True)
        embed.add_field(name="Trigger", value=trigger, inline=True)
        embed.set_footer(text=f"Guild: {log_channel.guild.name}")
        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass
