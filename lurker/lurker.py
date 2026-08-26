import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

import discord
from redbot.core import Config, checks, commands
from redbot.core.utils.chat_formatting import humanize_list

log = logging.getLogger("red.evac-cogs.lurker")


class Lurker(commands.Cog):
    """
    Auto-hide long-inactive members behind a Lurker role.

    - Members who go `threshold_days` without a message or reaction get every
      role stripped (except exempt roles) and are given the Lurker role,
      which is denied View Channel everywhere except the reactivation channel.
    - Posting in the reactivation channel deletes the message and instantly
      restores their prior roles.
    - New joins get a full threshold window before they can be flagged.
    - Rejoining resets their clock.
    """

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=928374651, force_registration=True)

        self.config.register_guild(
            lurker_role_id=None,
            lurker_channel_id=None,
            exempt_role_ids=[],
            threshold_days=30,
            last_active={},  # str(user_id) -> unix timestamp; periodically flushed from cache
        )
        self.config.register_member(
            stored_roles=[],
            flagged=False,
        )

        # in-memory cache: {guild_id: {user_id: timestamp}} — avoids a disk write per message
        self._cache: Dict[int, Dict[int, float]] = {}
        self._loaded_guilds: Set[int] = set()

        # ExtendedModLog logs an INFO line per role-change audit-reason lookup, which
        # floods the logs when we bulk-strip/restore roles. Quiet it to WARNING+ only.
        logging.getLogger("red.trusty-cogs.ExtendedModLog").setLevel(logging.WARNING)

        self._flush_task = self.bot.loop.create_task(self._flush_loop())
        self._daily_task = self.bot.loop.create_task(self._daily_loop())

    def cog_unload(self):
        self._flush_task.cancel()
        self._daily_task.cancel()

    # ---------------------------------------------------------------- cache

    async def _ensure_loaded(self, guild_id: int):
        if guild_id in self._loaded_guilds:
            return
        stored = await self.config.guild_from_id(guild_id).last_active()
        self._cache[guild_id] = {int(uid): ts for uid, ts in stored.items()}
        self._loaded_guilds.add(guild_id)

    def _touch(self, guild_id: int, user_id: int):
        self._cache.setdefault(guild_id, {})[user_id] = datetime.now(timezone.utc).timestamp()

    async def _flush_loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await asyncio.sleep(300)  # 5 minutes
                await self._flush_all()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Lurker flush loop error")

    async def _flush_all(self):
        for guild_id, users in list(self._cache.items()):
            str_map = {str(uid): ts for uid, ts in users.items()}
            await self.config.guild_from_id(guild_id).last_active.set(str_map)

    # -------------------------------------------------------- daily sweep

    async def _daily_loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._run_sweep()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Lurker daily sweep error")
            await asyncio.sleep(86400)  # 24 hours

    async def _run_sweep(self):
        for guild in self.bot.guilds:
            role_id = await self.config.guild(guild).lurker_role_id()
            if not role_id:
                continue
            lurker_role = guild.get_role(role_id)
            if not lurker_role:
                continue

            await self._ensure_loaded(guild.id)
            threshold_days = await self.config.guild(guild).threshold_days()
            exempt_ids = set(await self.config.guild(guild).exempt_role_ids())
            cutoff = datetime.now(timezone.utc).timestamp() - threshold_days * 86400
            cache = self._cache.get(guild.id, {})

            for member in guild.members:
                if member.bot:
                    continue
                if lurker_role in member.roles:
                    continue
                if exempt_ids & {r.id for r in member.roles}:
                    continue

                last_active = cache.get(member.id)
                if last_active is None:
                    last_active = member.joined_at.timestamp() if member.joined_at else 0

                if last_active < cutoff:
                    try:
                        await self._flag_member(member, lurker_role, exempt_ids)
                    except discord.Forbidden:
                        log.warning(f"Missing permissions to flag {member} in {guild}")
                    except Exception:
                        log.exception(f"Failed to flag {member} in {guild}")
                    await asyncio.sleep(1)  # gentle pacing against rate limits

    # ------------------------------------------------------- flag/unflag

    async def _flag_member(self, member: discord.Member, lurker_role: discord.Role, exempt_ids: Set[int]):
        guild = member.guild
        removable = [
            r for r in member.roles
            if r != guild.default_role
            and not r.managed
            and r.id not in exempt_ids
            and r < guild.me.top_role
        ]
        skipped = [
            r for r in member.roles
            if r != guild.default_role
            and not r.managed
            and r.id not in exempt_ids
            and r not in removable
        ]
        if skipped:
            log.warning(
                f"Could not strip {[r.name for r in skipped]} from {member} in {guild} "
                f"(role above bot's top role) — bot's role needs to be moved higher."
            )

        stored_ids = [r.id for r in removable]
        await self.config.member(member).stored_roles.set(stored_ids)
        if removable:
            await member.remove_roles(*removable, reason="Lurker: inactive")
        await member.add_roles(lurker_role, reason="Lurker: inactive")
        await self.config.member(member).flagged.set(True)

    async def _unflag_member(self, member: discord.Member, lurker_role: discord.Role):
        guild = member.guild
        stored_ids = await self.config.member(member).stored_roles()
        roles = [guild.get_role(rid) for rid in stored_ids if guild.get_role(rid)]

        if lurker_role in member.roles:
            await member.remove_roles(lurker_role, reason="Lurker: reactivated")
        if roles:
            await member.add_roles(*roles, reason="Lurker: restoring prior roles")

        await self.config.member(member).stored_roles.set([])
        await self.config.member(member).flagged.set(False)
        self._touch(guild.id, member.id)

    # ---------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        guild = message.guild
        await self._ensure_loaded(guild.id)
        lurker_channel_id = await self.config.guild(guild).lurker_channel_id()

        if lurker_channel_id and message.channel.id == lurker_channel_id:
            role_id = await self.config.guild(guild).lurker_role_id()
            lurker_role = guild.get_role(role_id) if role_id else None
            if lurker_role and lurker_role in message.author.roles:
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound):
                    pass
                try:
                    await self._unflag_member(message.author, lurker_role)
                except discord.Forbidden as e:
                    log.error(
                        f"FORBIDDEN restoring roles for {message.author} ({message.author.id}) "
                        f"in {guild}: {e}. Check bot role position vs the stored roles."
                    )
                except Exception:
                    log.exception(
                        f"Failed to unflag {message.author} ({message.author.id}) in {guild}"
                    )
            return

        self._touch(guild.id, message.author.id)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user):
        if user.bot or not reaction.message.guild:
            return
        await self._ensure_loaded(reaction.message.guild.id)
        self._touch(reaction.message.guild.id, user.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        await self._ensure_loaded(member.guild.id)
        self._touch(member.guild.id, member.id)

    # -------------------------------------------------------- config cmds

    @checks.admin_or_permissions(manage_roles=True)
    @commands.group()
    async def lurkerset(self, ctx):
        """Configure the Lurker inactivity system."""

    @lurkerset.command(name="role")
    async def lurkerset_role(self, ctx, role: discord.Role):
        """Set the role applied to inactive members."""
        await self.config.guild(ctx.guild).lurker_role_id.set(role.id)
        await ctx.send(f"Lurker role set to {role.mention}.")

    @lurkerset.command(name="channel")
    async def lurkerset_channel(self, ctx, channel: discord.TextChannel):
        """Set the channel where lurkers can post to reactivate themselves."""
        await self.config.guild(ctx.guild).lurker_channel_id.set(channel.id)
        await ctx.send(f"Reactivation channel set to {channel.mention}.")

    @lurkerset.command(name="exempt")
    async def lurkerset_exempt(self, ctx, *roles: discord.Role):
        """Set roles that are never flagged as lurkers (e.g. Mod, Booster)."""
        ids = [r.id for r in roles]
        await self.config.guild(ctx.guild).exempt_role_ids.set(ids)
        names = humanize_list([r.name for r in roles]) if roles else "none"
        await ctx.send(f"Exempt roles set to: {names}")

    @lurkerset.command(name="threshold")
    async def lurkerset_threshold(self, ctx, days: int):
        """Set the number of inactive days before a member is flagged."""
        await self.config.guild(ctx.guild).threshold_days.set(days)
        await ctx.send(f"Inactivity threshold set to {days} days.")

    @lurkerset.command(name="settings")
    async def lurkerset_settings(self, ctx):
        """Show current configuration."""
        data = await self.config.guild(ctx.guild).all()
        role = ctx.guild.get_role(data["lurker_role_id"]) if data["lurker_role_id"] else None
        channel = ctx.guild.get_channel(data["lurker_channel_id"]) if data["lurker_channel_id"] else None
        exempt = [ctx.guild.get_role(rid) for rid in data["exempt_role_ids"]]
        exempt = [r.name for r in exempt if r]
        msg = (
            f"Role: {role.mention if role else 'not set'}\n"
            f"Reactivation channel: {channel.mention if channel else 'not set'}\n"
            f"Threshold: {data['threshold_days']} days\n"
            f"Exempt roles: {humanize_list(exempt) if exempt else 'none'}"
        )
        await ctx.send(msg)

    # ------------------------------------------------------- manual test

    @checks.mod_or_permissions(manage_roles=True)
    @commands.command(name="lurker")
    async def lurker_manual(self, ctx, member: discord.Member):
        """Immediately flag a member as a lurker (for testing)."""
        role_id = await self.config.guild(ctx.guild).lurker_role_id()
        if not role_id:
            await ctx.send("Lurker role not configured. Use `.lurkerset role` first.")
            return
        lurker_role = ctx.guild.get_role(role_id)
        if not lurker_role:
            await ctx.send("Configured Lurker role no longer exists.")
            return
        if lurker_role in member.roles:
            await ctx.send(f"{member} is already flagged.")
            return

        exempt_ids = set(await self.config.guild(ctx.guild).exempt_role_ids())
        if exempt_ids & {r.id for r in member.roles}:
            await ctx.send(f"{member} has an exempt role and cannot be flagged.")
            return

        await self._flag_member(member, lurker_role, exempt_ids)
        await ctx.send(f"{member} flagged as a Lurker. Have them post in the reactivation channel to test recovery.")

    @checks.mod_or_permissions(manage_roles=True)
    @commands.command(name="lurkerstatus")
    async def lurker_status(self, ctx, member: discord.Member):
        """Show a member's Lurker state for debugging."""
        role_id = await self.config.guild(ctx.guild).lurker_role_id()
        lurker_role = ctx.guild.get_role(role_id) if role_id else None
        flagged = await self.config.member(member).flagged()
        stored_ids = await self.config.member(member).stored_roles()

        resolved, missing = [], []
        for rid in stored_ids:
            role = ctx.guild.get_role(rid)
            (resolved if role else missing).append(role.name if role else str(rid))

        has_lurker_role = bool(lurker_role and lurker_role in member.roles)
        bot_top = ctx.guild.me.top_role
        above_bot = [r.name for r in member.roles if r.position >= bot_top.position and r != ctx.guild.default_role]

        msg = (
            f"**{member}** ({member.id})\n"
            f"Flagged in config: {flagged}\n"
            f"Has Lurker role right now: {has_lurker_role}\n"
            f"Stored roles to restore: {humanize_list(resolved) if resolved else 'none'}\n"
            f"Stored role IDs no longer valid: {humanize_list(missing) if missing else 'none'}\n"
            f"Bot's top role: {bot_top.name} (position {bot_top.position})\n"
            f"Member's current roles at/above bot's position (can't be touched by bot): "
            f"{humanize_list(above_bot) if above_bot else 'none'}"
        )
        await ctx.send(msg)

    @checks.mod_or_permissions(manage_roles=True)
    @commands.command(name="lurkerunflag")
    async def lurker_unflag(self, ctx, member: discord.Member):
        """Manually restore a lurker's roles without them needing to post."""
        role_id = await self.config.guild(ctx.guild).lurker_role_id()
        if not role_id:
            await ctx.send("Lurker role not configured. Use `.lurkerset role` first.")
            return
        lurker_role = ctx.guild.get_role(role_id)
        if not lurker_role:
            await ctx.send("Configured Lurker role no longer exists.")
            return

        stored_ids = await self.config.member(member).stored_roles()
        await ctx.send(f"Attempting restore for {member}. Stored role IDs: {stored_ids or 'none'}")

        try:
            await self._unflag_member(member, lurker_role)
        except discord.Forbidden as e:
            await ctx.send(
                f"**Forbidden** restoring roles for {member}: `{e}`\n"
                f"This means the bot's role sits below one of the roles it's trying to restore. "
                f"Move the bot's role higher in Server Settings > Roles."
            )
            return
        except Exception as e:
            await ctx.send(f"**Unexpected error** restoring roles for {member}: `{type(e).__name__}: {e}`")
            log.exception(f"lurkerunflag failed for {member} in {ctx.guild}")
            return

        await ctx.send(f"Restored {member}'s roles and removed Lurker.")

    # ------------------------------------------------------------ backfill

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerbackfill")
    async def lurker_backfill(self, ctx, confirm: Optional[str] = None):
        """
        Scan the last 30 days of messages and flag everyone else as a Lurker.

        Run with no arguments for a dry-run report.
        Run `.lurkerbackfill confirm` to actually execute.
        """
        role_id = await self.config.guild(ctx.guild).lurker_role_id()
        if not role_id:
            await ctx.send("Lurker role not configured. Use `.lurkerset role` first.")
            return
        lurker_role = ctx.guild.get_role(role_id)
        if not lurker_role:
            await ctx.send("Configured Lurker role no longer exists.")
            return

        await ctx.send("Scanning last 30 days of message history — this may take a while...")
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=30)
        active_ids: Set[int] = set()

        for channel in ctx.guild.text_channels:
            perms = channel.permissions_for(ctx.guild.me)
            if not perms.read_message_history:
                continue
            try:
                async for message in channel.history(limit=None, after=cutoff_dt):
                    if not message.author.bot:
                        active_ids.add(message.author.id)
            except discord.Forbidden:
                continue
            except Exception:
                log.exception(f"Error scanning {channel}")

        exempt_ids = set(await self.config.guild(ctx.guild).exempt_role_ids())
        to_flag = []
        for member in ctx.guild.members:
            if member.bot:
                continue
            if member.id in active_ids:
                continue
            if lurker_role in member.roles:
                continue
            if exempt_ids & {r.id for r in member.roles}:
                continue
            to_flag.append(member)

        if confirm != "confirm":
            await ctx.send(
                f"Dry run complete.\n"
                f"Active in last 30 days: {len(active_ids)}\n"
                f"Would be flagged as Lurkers: {len(to_flag)}\n"
                f"Run `.lurkerbackfill confirm` to execute."
            )
            return

        await ctx.send(f"Flagging {len(to_flag)} members. I'll report back when done.")
        flagged_count = 0
        for member in to_flag:
            try:
                await self._flag_member(member, lurker_role, exempt_ids)
                self._cache.setdefault(ctx.guild.id, {})[member.id] = 0  # long-inactive marker
                flagged_count += 1
            except discord.Forbidden:
                log.warning(f"Missing permissions to flag {member}")
            except Exception:
                log.exception(f"Failed to flag {member} during backfill")
            await asyncio.sleep(1.2)  # rate limit pacing

        await self._flush_all()
        await ctx.send(f"Backfill complete. Flagged {flagged_count}/{len(to_flag)} members.")
