import asyncio
import logging
import time
import weakref
from collections import OrderedDict
from typing import Dict, Optional, Tuple

import discord
from redbot.core import Config, commands

from . import constants, embeds, engine, views

log = logging.getLogger("red.evac-cogs.rejoinwatch")


class RejoinWatch(commands.Cog):
    """
    Track who leaves the server, and deal with people who keep doing it.

    - A voluntary leave is recorded (kicks, bans and prunes are not). Records expire after
      the retention window (default 180 days).
    - First rejoin after a leave: the member is DMed a warning (falling back to a ping in the
      fallback channel if their DMs are closed) and mods get a note.
    - 2nd+ leave: mods get an alert with Yes / No ban buttons. Nothing more is sent to the user.
    - Rejoin after 2+ leaves: mods get a note (with the same buttons). The user is not messaged.
    """

    # Overridable in tests; see constants.SETTLE_SECONDS.
    SETTLE_SECONDS = constants.SETTLE_SECONDS

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7310425908, force_registration=True)
        self.config.register_guild(**constants.GUILD_DEFAULTS)

        # A leave and a quick rejoin by the same person must be handled in order.
        self._user_locks: "weakref.WeakValueDictionary[Tuple[int, int], asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )
        # Guards the read-modify-write of the per-guild `leaves` dict.
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        # (guild_id, user_id) -> time of a ban event, waiting for its matching leave event.
        self._recent_bans: Dict[Tuple[int, int], float] = {}
        # Alert messages already acted on (or being acted on): a double click must not act twice.
        self._handled: "OrderedDict[int, None]" = OrderedDict()

        self._prune_task = self.bot.loop.create_task(self._prune_loop())

    async def cog_unload(self):
        self._prune_task.cancel()

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        try:
            for guild_id in list((await self.config.all_guilds()).keys()):
                async with self._guild_lock(guild_id):
                    cfg = self.config.guild_from_id(guild_id)
                    leaves = await cfg.leaves()
                    if leaves.pop(str(user_id), None) is not None:
                        await cfg.leaves.set(leaves)
        except Exception:
            log.exception("rejoinwatch: data deletion failed for %s", user_id)
            raise

    # ----------------------------------------------------------- helpers

    def _lock_for(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (guild_id, user_id)
        lock = self._user_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[key] = lock
        return lock

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = self._guild_locks[guild_id] = asyncio.Lock()
        return lock

    @staticmethod
    def _channel(guild, channel_id: Optional[int]):
        return guild.get_channel(channel_id) if channel_id else None

    def _claim(self, message_id: int) -> bool:
        """Mark an alert message as being handled. Synchronous on purpose: no await between
        the check and the set, so two simultaneous clicks cannot both get through."""
        if message_id in self._handled:
            return False
        self._handled[message_id] = None
        while len(self._handled) > constants.MAX_HANDLED_MESSAGES:
            self._handled.popitem(last=False)
        return True

    def _release(self, message_id: int):
        self._handled.pop(message_id, None)

    def _note_ban(self, guild_id: int, user_id: int):
        now = time.time()
        for key in [k for k, ts in self._recent_bans.items() if now - ts > constants.BAN_NOTE_TTL_SECONDS]:
            del self._recent_bans[key]
        self._recent_bans[(guild_id, user_id)] = now

    # --------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        self._note_ban(guild.id, user.id)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload):
        user = payload.user
        if getattr(user, "bot", False):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        try:
            await self._process_leave(guild, user)
        except Exception:
            log.exception("rejoinwatch: failed handling leave of %s in %s", user.id, guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot:
            return
        try:
            await self._process_join(member)
        except Exception:
            log.exception("rejoinwatch: failed handling join of %s in %s", member.id, member.guild.id)

    @commands.Cog.listener()
    async def on_interaction(self, interaction):
        data = getattr(interaction, "data", None) or {}
        parsed = engine.parse_custom_id(data.get("custom_id"))
        if parsed is None:
            return
        try:
            await self._handle_button(interaction, *parsed)
        except Exception:
            log.exception("rejoinwatch: button handler failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Something went wrong, please try again.", ephemeral=True)
                else:
                    await interaction.response.send_message("Something went wrong, please try again.", ephemeral=True)
            except Exception:
                pass

    # ------------------------------------------------------------ leaves

    async def _process_leave(self, guild, user):
        cfg = self.config.guild(guild)
        if not await cfg.enabled():
            return
        event_ts = time.time()
        async with self._lock_for(guild.id, user.id):
            # Held across the settle delay so a fast rejoin waits for this leave to be recorded.
            if self.SETTLE_SECONDS:
                await asyncio.sleep(self.SETTLE_SECONDS)
            if await self._removed_by_staff(guild, user.id):
                return
            retention = await cfg.retention_days()
            async with self._guild_lock(guild.id):
                async with cfg.leaves() as leaves:
                    stamps = engine.record_leave(leaves, user.id, event_ts, time.time(), retention)
        if engine.leave_action(len(stamps)) != "alert":
            return
        embed = embeds.leave_alert(user.id, user.name, stamps, retention)
        await self._post_mod(guild, embed, views.ban_view(user.id))

    async def _removed_by_staff(self, guild, user_id: int) -> bool:
        """True when this leave was a kick, ban or prune rather than the member's own choice."""
        now = time.time()
        noted = self._recent_bans.pop((guild.id, user_id), None)
        if noted is not None and engine.is_recent(now, noted, constants.BAN_NOTE_TTL_SECONDS):
            return True
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return False  # can't tell: counted as a leave. `.rejoinwatch settings` shows this.
        try:
            for action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                async for entry in guild.audit_logs(limit=10, action=action):
                    target_id = getattr(entry.target, "id", None)
                    if target_id == user_id and engine.is_recent(
                        now, entry.created_at.timestamp(), constants.AUDIT_WINDOW_SECONDS
                    ):
                        return True
            async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.member_prune):
                if engine.is_recent(now, entry.created_at.timestamp(), constants.AUDIT_WINDOW_SECONDS):
                    return True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("rejoinwatch: audit log lookup failed in %s; counting this leave", guild.id)
        return False

    # ------------------------------------------------------------- joins

    async def _process_join(self, member):
        guild = member.guild
        cfg = self.config.guild(guild)
        if not await cfg.enabled():
            return
        async with self._lock_for(guild.id, member.id):
            # Same lock as _process_leave: a leave still settling is recorded before we look.
            retention = await cfg.retention_days()
            stamps = engine.history(await cfg.leaves(), member.id, time.time(), retention)
        action = engine.rejoin_action(len(stamps))
        if action == "none":
            return
        created_ts = member.created_at.timestamp()
        if action == "warn":
            delivery = await self._warn_member(member)
            channel_id = await cfg.cuddle_channel_id()
            text = engine.delivery_phrase(delivery, f"<#{channel_id}>" if channel_id else None)
            embed = embeds.rejoin_warned(member.id, member.name, stamps, retention, text, created_ts)
            await self._post_mod(guild, embed)
        else:
            embed = embeds.rejoin_escalated(member.id, member.name, stamps, retention, created_ts)
            await self._post_mod(guild, embed, views.ban_view(member.id))

    async def _warn_member(self, member) -> str:
        """DM first, fallback channel second. Returns 'dm', 'channel' or 'failed'."""
        guild = member.guild
        cfg = self.config.guild(guild)
        text = engine.render_warning(await cfg.warning_text(), guild.name)
        try:
            await member.send(text)
            return "dm"
        except (discord.Forbidden, discord.HTTPException):
            pass
        channel = self._channel(guild, await cfg.cuddle_channel_id())
        if channel is None:
            log.warning("rejoinwatch: %s has DMs closed and no fallback channel is set", member.id)
            return "failed"
        try:
            await channel.send(
                f"{member.mention} {text}",
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
            return "channel"
        except (discord.Forbidden, discord.HTTPException):
            log.warning("rejoinwatch: could not post the fallback warning for %s", member.id)
            return "failed"

    async def _post_mod(self, guild, embed, view=None):
        channel = self._channel(guild, await self.config.guild(guild).mod_channel_id())
        if channel is None:
            log.warning("rejoinwatch: mod channel not found in %s; alert dropped", guild.id)
            return None
        kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
        if view is not None:
            kwargs["view"] = view
        try:
            return await channel.send(**kwargs)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("rejoinwatch: could not post to the mod channel in %s", guild.id)
            return None

    # ----------------------------------------------------------- buttons

    async def _handle_button(self, interaction, action: str, user_id: int):
        guild, message = interaction.guild, interaction.message
        if guild is None or message is None:
            return
        clicker = interaction.user
        cfg = self.config.guild(guild)
        perms = clicker.guild_permissions
        allowed = engine.can_resolve(
            {r.id for r in clicker.roles}, await cfg.mod_role_id(), perms.ban_members, perms.administrator
        )
        if not allowed:
            await interaction.response.send_message("Only moderators can use these buttons.", ephemeral=True)
            return
        if not self._claim(message.id):
            await interaction.response.send_message("Someone has already handled this.", ephemeral=True)
            return

        settled = False
        try:
            await interaction.response.defer()
            if action == "ban":
                retention = await cfg.retention_days()
                count = engine.count_leaves(await cfg.leaves(), user_id, time.time(), retention)
                ok, problem = await self._ban(guild, user_id, clicker, count, retention)
                if not ok:
                    await interaction.followup.send(problem, ephemeral=True)
                    return  # claim is released below, so the buttons stay usable
                text = f"Banned by {clicker.display_name}"
            else:
                text = f"Dismissed by {clicker.display_name}, no ban"
            settled = True
            embed = embeds.resolve(
                message.embeds[0] if message.embeds else discord.Embed(title="Repeat leaver"),
                text, banned=(action == "ban"),
            )
            try:
                await interaction.edit_original_response(embed=embed, view=None)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("rejoinwatch: could not update the alert message after %s", action)
        finally:
            if not settled:
                self._release(message.id)

    async def _ban(self, guild, user_id: int, moderator, count: int, retention: int) -> Tuple[bool, str]:
        reason = (
            f"Repeat leaver ({engine.count_phrase(count)} in {retention} days). "
            f"Banned by {moderator} ({moderator.id}) via rejoinwatch."
        )
        target = discord.Object(id=user_id)
        try:
            try:
                await guild.ban(target, reason=reason, delete_message_seconds=0)
            except TypeError:  # older discord.py spells it in days
                await guild.ban(target, reason=reason, delete_message_days=0)
        except discord.Forbidden:
            return False, "I couldn't ban them: I'm missing Ban Members, or their role is above mine."
        except discord.HTTPException as exc:
            return False, f"Discord refused the ban: {exc}"
        return True, ""

    # ------------------------------------------------------------- prune

    async def _prune_guild(self, guild):
        cfg = self.config.guild(guild)
        retention = await cfg.retention_days()
        async with self._guild_lock(guild.id):
            kept, changed = engine.prune(await cfg.leaves(), time.time(), retention)
            if changed:
                await cfg.leaves.set(kept)  # one write, however many expired
        return changed

    async def _prune_loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:
                    await self._prune_guild(guild)
                except asyncio.CancelledError:
                    return
                except Exception:
                    log.exception("rejoinwatch: prune failed in %s", guild.id)
            try:
                await asyncio.sleep(constants.PRUNE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

    # ---------------------------------------------------------- commands

    @commands.group(name="rejoinwatch", invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    async def rejoinwatch(self, ctx):
        """Repeat-leaver tracking. Try `.rejoinwatch settings`."""
        await ctx.send_help()

    @rejoinwatch.command(name="settings")
    @commands.mod_or_permissions(manage_guild=True)
    async def rw_settings(self, ctx):
        """Show the current settings and whether everything needed is in place."""
        guild = ctx.guild
        cfg = self.config.guild(guild)
        data = await cfg.all()
        mod_ch, cuddle_ch, mod_role = data["mod_channel_id"], data["cuddle_channel_id"], data["mod_role_id"]
        me = guild.me
        audit = bool(me and me.guild_permissions.view_audit_log)

        mod_text = "<#%s>" % mod_ch if self._channel(guild, mod_ch) else "NOT FOUND, alerts are dropped"
        cuddle_text = (
            "<#%s>" % cuddle_ch if self._channel(guild, cuddle_ch)
            else "NOT SET, use `.rejoinwatch fallback #cuddle`"
        )
        role_text = "<@&%s>" % mod_role if mod_role else "none"
        audit_text = (
            "yes, kicks and bans are ignored" if audit
            else "NO, kicks and prunes will be counted as leaves"
        )
        lines = [
            "**RejoinWatch** v%s" % constants.VERSION,
            "Tracking: %s" % ("on" if data["enabled"] else "OFF"),
            "Mod channel: %s" % mod_text,
            "Fallback channel (DMs closed): %s" % cuddle_text,
            "Mod role (can press the ban buttons): %s" % role_text,
            "Retention: %s days" % data["retention_days"],
            "People with leaves on record: %s" % len(data["leaves"]),
            "View Audit Log: %s" % audit_text,
            "Warning text: %s" % data["warning_text"],
        ]
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @rejoinwatch.command(name="check")
    @commands.mod_or_permissions(manage_guild=True)
    async def rw_check(self, ctx, user: discord.User):
        """Show a person's recorded leaves (inside the retention window)."""
        cfg = self.config.guild(ctx.guild)
        retention = await cfg.retention_days()
        stamps = engine.history(await cfg.leaves(), user.id, time.time(), retention)
        if not stamps:
            msg = f"{user.mention}: no leaves on record in the last {retention} days."
        else:
            dates = "\n".join(f"<t:{int(t)}:f>" for t in stamps[-constants.MAX_HISTORY_SHOWN:])
            msg = f"{user.mention} has left {engine.count_phrase(len(stamps))} in the last {retention} days:\n{dates}"
        await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())

    @rejoinwatch.command(name="clear")
    @commands.mod_or_permissions(manage_guild=True)
    async def rw_clear(self, ctx, user: discord.User):
        """Forgive someone: wipe their recorded leaves."""
        cfg = self.config.guild(ctx.guild)
        async with self._guild_lock(ctx.guild.id):
            leaves = await cfg.leaves()
            removed = leaves.pop(str(user.id), None)
            if removed is not None:
                await cfg.leaves.set(leaves)
        if removed is None:
            msg = f"{user.mention} had nothing on record."
        else:
            msg = f"Cleared {engine.count_phrase(len(removed))} for {user.mention}."
        await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())

    @rejoinwatch.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def rw_toggle(self, ctx):
        """Turn tracking on or off. Existing records are kept either way."""
        cfg = self.config.guild(ctx.guild)
        new = not await cfg.enabled()
        await cfg.enabled.set(new)
        await ctx.send(f"Tracking is now {'on' if new else 'OFF'}.")

    @rejoinwatch.command(name="modchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def rw_modchannel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Set where alerts go (default #mod-chat). With no channel, shows the current one."""
        cfg = self.config.guild(ctx.guild)
        if channel is None:
            current = await cfg.mod_channel_id()
            await ctx.send("Mod channel: %s" % ("<#%s>" % current if current else "not set"))
            return
        await cfg.mod_channel_id.set(channel.id)
        await ctx.send(f"Alerts will go to {channel.mention}.")

    @rejoinwatch.command(name="fallback")
    @commands.admin_or_permissions(manage_guild=True)
    async def rw_fallback(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Set the fallback channel for warnings when DMs are closed (#cuddle). No channel shows the current one."""
        cfg = self.config.guild(ctx.guild)
        if channel is None:
            current = await cfg.cuddle_channel_id()
            await ctx.send("Fallback channel: %s" % ("<#%s>" % current if current else "not set"))
            return
        await cfg.cuddle_channel_id.set(channel.id)
        await ctx.send(f"Warnings will fall back to {channel.mention} when a DM can't be sent.")

    @rejoinwatch.command(name="modrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def rw_modrole(self, ctx, role: Optional[discord.Role] = None):
        """Set the role allowed to press the ban buttons. With no role, shows the current one.
        Anyone with Ban Members or Administrator can press them regardless."""
        cfg = self.config.guild(ctx.guild)
        if role is None:
            current = await cfg.mod_role_id()
            await ctx.send(
                "Mod role: %s" % ("<@&%s>" % current if current else "not set"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await cfg.mod_role_id.set(role.id)
        await ctx.send(f"{role.mention} can now press the ban buttons.", allowed_mentions=discord.AllowedMentions.none())

    @rejoinwatch.command(name="retention")
    @commands.admin_or_permissions(manage_guild=True)
    async def rw_retention(self, ctx, days: int):
        """How many days a leave is remembered (7 to 730, default 180). Older leaves are deleted now."""
        if not constants.MIN_RETENTION_DAYS <= days <= constants.MAX_RETENTION_DAYS:
            await ctx.send(
                f"Pick between {constants.MIN_RETENTION_DAYS} and {constants.MAX_RETENTION_DAYS} days."
            )
            return
        await self.config.guild(ctx.guild).retention_days.set(days)
        await self._prune_guild(ctx.guild)
        await ctx.send(f"Leaves are now remembered for {days} days.")

    @rejoinwatch.command(name="warning")
    @commands.admin_or_permissions(manage_guild=True)
    async def rw_warning(self, ctx, *, text: Optional[str] = None):
        """Set the warning sent on a first rejoin. `{server}` becomes the server name.
        No text shows the current one; `reset` restores the default."""
        cfg = self.config.guild(ctx.guild)
        if text is None:
            await ctx.send(f"Current warning:\n{await cfg.warning_text()}")
            return
        if text.strip().lower() == "reset":
            await cfg.warning_text.set(constants.DEFAULT_WARNING)
            await ctx.send("Warning text reset to the default.")
            return
        await cfg.warning_text.set(text)
        await ctx.send(f"Warning text updated. A member would see:\n{engine.render_warning(text, ctx.guild.name)}")
