"""Discord side of `roomperms`: gives AutoRoom owners Manage Permissions and runs the room guard.

Mixed into the Wonderland cog (no separate cog, no extra install). PhasecoreX's AutoRoom cog is never
edited: this wraps one of its methods in memory, so `.cog update autoroom` can't undo it, and a failure
anywhere in here leaves owners with *less* than before, never more.

How the grant works: AutoRoom asks `get_autoroom_source_config(source)` for a permission set called
`perms["owner"]` both when it creates a room and when someone runs `.autoroom claim`. The wrapper adds
Manage Permissions to that one set. Nothing else in the config changes.

How the guard works: when an AutoRoom's overwrites change, it waits a moment, re-reads the room, and puts
back anything that would let in someone the Room Creator source denies (see roomperms.plan_guard).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Dict, Optional

import discord
from redbot.core import commands

from . import roomperms
from .constants import GUARD_DELAY, GUARD_LOG_COOLDOWN, GUARD_PASSES, GUARD_ROUNDS, MOD_LOG_CHANNEL_ID

log = logging.getLogger("red.evac-cogs.wonderland.roomperms")

AUTOROOM_COG = "AutoRoom"
PATCHED = "get_autoroom_source_config"
AUDIT_WINDOW_SECONDS = 30


def overwrite_map(overwrites) -> Dict[roomperms.Target, Dict[str, Optional[bool]]]:
    """discord overwrites -> the plain dicts roomperms works on (roles and members only)."""
    out: Dict[roomperms.Target, Dict[str, Optional[bool]]] = {}
    for target, overwrite in overwrites.items():
        if isinstance(target, discord.Role):
            kind = "role"
        elif isinstance(target, discord.Member):
            kind = "member"
        else:
            continue  # users who left show up as bare objects; nothing to guard
        out[(kind, target.id)] = {flag: getattr(overwrite, flag) for flag in roomperms.GUARDED_FLAGS}
    return out


class RoomPermsMixin:
    """Needs `self.bot` and `self.config` (with a guild value `roomperms`) from the cog it is mixed into."""

    def _rp_setup(self) -> None:
        self._rp_pending: set = set()               # rooms with a guard run in progress
        self._rp_dirty: set = set()                 # rooms edited again during that run: check once more at the end
        self._rp_enabled: Dict[int, bool] = {}      # guild id -> switch, so voice events don't each hit Config
        self._rp_last_log: Dict[int, float] = {}    # room id -> when the guard last reported it

    # ------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self._rp_patch()

    def cog_unload(self) -> None:
        self._rp_unpatch()

    @commands.Cog.listener()
    async def on_cog_add(self, cog) -> None:
        # AutoRoom loaded or reloaded after us: its class is new, so the wrapper has to go back on.
        if getattr(cog, "qualified_name", None) == AUTOROOM_COG:
            self._rp_patch()

    def _rp_autoroom_class(self):
        cog = self.bot.get_cog(AUTOROOM_COG)
        return type(cog) if cog is not None else None

    def _rp_patch(self) -> bool:
        cls = self._rp_autoroom_class()
        if cls is None:
            return False
        current = getattr(cls, PATCHED, None)
        if current is None:
            log.warning("AutoRoom has no %s any more; owners will NOT get Manage Permissions.", PATCHED)
            return False
        if getattr(current, "_wld_owner", None) is self:
            return True
        base = getattr(current, "_wld_base", current)  # an older copy of this cog's wrapper: start from the original
        owner = self

        @functools.wraps(base)
        async def wrapper(autoroom_self, autoroom_source):
            cfg = await base(autoroom_self, autoroom_source)
            return await owner._rp_apply(cfg, autoroom_source)

        wrapper._wld_base = base
        wrapper._wld_owner = self
        setattr(cls, PATCHED, wrapper)
        log.info("AutoRoom owner Manage Permissions: wrapper installed.")
        return True

    def _rp_unpatch(self) -> None:
        cls = self._rp_autoroom_class()
        if cls is None:
            return
        current = getattr(cls, PATCHED, None)
        if getattr(current, "_wld_owner", None) is self:
            setattr(cls, PATCHED, current._wld_base)

    def _rp_is_patched(self) -> bool:
        cls = self._rp_autoroom_class()
        return cls is not None and getattr(getattr(cls, PATCHED, None), "_wld_owner", None) is self

    async def _rp_apply(self, cfg, autoroom_source):
        """Runs on every AutoRoom source-config lookup. Any problem: hand back the config untouched."""
        try:
            guild = getattr(autoroom_source, "guild", None)
            if cfg and guild is not None:
                enabled = await self._rp_is_enabled(guild)
                can_manage_roles = bool(guild.me.guild_permissions.manage_roles)
                if roomperms.should_grant_owner_manage_permissions(cfg, enabled, can_manage_roles):
                    roomperms.grant_owner_manage_permissions(cfg)
        except Exception:
            log.exception("roomperms: could not adjust the owner permissions; leaving them as AutoRoom set them.")
        return cfg

    # ------------------------------------------------------------ status / switch

    async def _rp_is_enabled(self, guild) -> bool:
        if guild.id not in self._rp_enabled:
            settings = await self.config.guild(guild).roomperms() or {}
            self._rp_enabled[guild.id] = bool(settings.get("enabled", True))
        return self._rp_enabled[guild.id]

    async def _rp_set_enabled(self, guild, enabled: bool) -> None:
        settings = dict(await self.config.guild(guild).roomperms() or {})
        settings["enabled"] = enabled
        await self.config.guild(guild).roomperms.set(settings)
        self._rp_enabled[guild.id] = enabled

    async def _rp_status(self, guild) -> str:
        enabled = await self._rp_is_enabled(guild)
        autoroom = self.bot.get_cog(AUTOROOM_COG) is not None
        can = bool(guild.me.guild_permissions.manage_roles)
        lines = [
            f"Owners get Manage Permissions on new rooms: **{'on' if enabled else 'off'}**",
            f"AutoRoom loaded: **{'yes' if autoroom else 'no'}**, wrapper active: **{'yes' if self._rp_is_patched() else 'no'}**, "
            f"bot has Manage Roles: **{'yes' if can else 'no'}**",
            "Room guard (puts back denies on roles the Room Creator blocks): **always on**",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------ the guard

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after) -> None:
        try:
            if not isinstance(after, discord.VoiceChannel) or before.overwrites == after.overwrites:
                return
            autoroom = self.bot.get_cog(AUTOROOM_COG)
            if autoroom is None or not await autoroom.get_autoroom_info(after):
                return
            if after.id in self._rp_pending:
                self._rp_dirty.add(after.id)  # a run is in progress; it will look again before it finishes
                return
            self._rp_pending.add(after.id)
            try:
                for _ in range(GUARD_ROUNDS):
                    self._rp_dirty.discard(after.id)
                    await asyncio.sleep(GUARD_DELAY)  # lets rapid edits settle and the audit log catch up
                    for attempt in range(GUARD_PASSES):
                        if not await self._rp_guard_once(after.guild, after.id, first=attempt == 0):
                            break
                        await asyncio.sleep(GUARD_DELAY)  # let Discord's update for our own fix reach the cache first
                    if after.id not in self._rp_dirty:
                        break
            finally:
                self._rp_pending.discard(after.id)
                self._rp_dirty.discard(after.id)
        except Exception:
            log.exception("roomperms: guard failed")

    async def _rp_is_staff(self, member) -> bool:
        try:
            return bool(await self.bot.is_mod(member) or await self.bot.is_admin(member))
        except Exception:
            return False

    async def _rp_exempt_roles(self, guild, autoroom) -> Optional[set]:
        """Roles AutoRoom itself adds to every room on purpose (bot roles, and the staff roles). None = couldn't tell."""
        try:
            roles = list(await autoroom.get_bot_roles(guild))
            roles += list(await self.bot.get_mod_roles(guild))
            roles += list(await self.bot.get_admin_roles(guild))
            return {role.id for role in roles}
        except Exception:
            log.exception("roomperms: could not list the staff and bot roles; skipping this check")
            return None

    async def _rp_actor(self, guild, channel_id: int):
        """Who last edited this channel's overwrites, if the audit log says (best effort)."""
        try:
            wanted = {discord.AuditLogAction.overwrite_create, discord.AuditLogAction.overwrite_update,
                      discord.AuditLogAction.overwrite_delete}
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(limit=10):
                if entry.action not in wanted or getattr(entry.target, "id", None) != channel_id:
                    continue
                if (now - entry.created_at).total_seconds() > AUDIT_WINDOW_SECONDS:
                    continue
                return entry.user
        except Exception:
            pass
        return None

    async def _rp_guard_once(self, guild, channel_id: int, first: bool) -> bool:
        """One check of one room. True = it changed something (so check again to confirm)."""
        channel = guild.get_channel(channel_id)
        autoroom = self.bot.get_cog(AUTOROOM_COG)
        if autoroom is None or not isinstance(channel, discord.VoiceChannel):
            return False
        info = await autoroom.get_autoroom_info(channel)
        if not info:
            return False
        source = guild.get_channel(info.get("source_channel") or 0)
        if not isinstance(source, discord.VoiceChannel):
            return False
        exempt_roles = await self._rp_exempt_roles(guild, autoroom)
        if exempt_roles is None:
            return False

        room, src = overwrite_map(channel.overwrites), overwrite_map(source.overwrites)
        owner_id = info.get("owner")

        # Who has a personal "can join" allow, and would AutoRoom itself refuse them? Ask AutoRoom, so the
        # answer is exactly the one `.autoroom allow` would have given (roles that allow and deny resolve
        # the way Discord resolves them, not by guesswork here).
        blocked, exempt_members = set(), set()
        for (kind, member_id), overwrite in room.items():
            if kind != "member" or overwrite.get("connect") is not True:
                continue
            member = guild.get_member(member_id)
            if member is None:
                continue
            if member.bot or member_id == owner_id or member_id == guild.owner_id:
                exempt_members.add(member_id)
            elif not autoroom.check_if_member_or_role_allowed(source, member):
                blocked.add(member_id)
                if await self._rp_is_staff(member):
                    exempt_members.add(member_id)

        fixes = roomperms.plan_guard(room, src, guild.default_role.id, blocked, exempt_roles, exempt_members)
        if not fixes:
            return False

        actor = await self._rp_actor(guild, channel_id) if first else None
        actor_member = guild.get_member(actor.id) if actor is not None else None
        if actor_member is not None and not actor_member.bot and await self._rp_is_staff(actor_member):
            return False  # a mod or admin edited this room on purpose; not the guard's business

        changed = {}
        for (kind, target_id), changes in fixes.items():
            target = guild.get_role(target_id) if kind == "role" else guild.get_member(target_id)
            if target is None:
                continue
            overwrite = channel.overwrites_for(target)
            overwrite.update(**changes)
            try:
                await channel.set_permissions(
                    target, overwrite=None if overwrite.is_empty() else overwrite,
                    reason="Room guard: put back a deny the Room Creator requires")
                changed[(kind, target_id)] = changes
            except discord.HTTPException:
                log.warning("roomperms: could not restore %s %s in %s", kind, target_id, channel_id, exc_info=True)
        removed = await self._rp_disconnect(guild, channel, autoroom, source, changed, exempt_members, owner_id)
        if first:
            await self._rp_log(guild, channel, owner_id, actor, changed, removed, failed=not changed)
        return bool(changed)

    async def _rp_disconnect(self, guild, channel, autoroom, source, changed, exempt_members, owner_id) -> list:
        """Closing the door doesn't remove someone already inside. Like AutoRoom's own `deny`, disconnect the
        people this fix was about, but only those AutoRoom itself would refuse: never staff, bots, the room's
        owner or the server owner, and never someone the source lets in (a member whose allow beats a deny)."""
        role_ids = {target_id for (kind, target_id) in changed if kind == "role"}
        member_ids = {target_id for (kind, target_id) in changed if kind == "member"}
        if not role_ids and not member_ids:
            return []
        removed = []
        for member in list(getattr(channel, "members", [])):
            if member.bot or member.id in (owner_id, guild.owner_id) or member.id in exempt_members:
                continue
            affected = member.id in member_ids or any(role.id in role_ids for role in member.roles)
            if not affected or autoroom.check_if_member_or_role_allowed(source, member):
                continue
            if await self._rp_is_staff(member):
                continue
            try:
                await member.move_to(None, reason="Room guard: not allowed in this room")
                removed.append(member.id)
            except discord.HTTPException:
                log.warning("roomperms: could not disconnect %s from %s", member.id, channel.id, exc_info=True)
        return removed

    async def _rp_log(self, guild, channel, owner_id, actor, fixes, removed=(), failed: bool = False) -> None:
        now = time.monotonic()
        for room_id in [r for r, t in self._rp_last_log.items() if now - t > GUARD_LOG_COOLDOWN * 10]:
            del self._rp_last_log[room_id]            # forget rooms that have been quiet for a while
        if now - self._rp_last_log.get(channel.id, -1e9) < GUARD_LOG_COOLDOWN:
            return                                    # an owner mashing the same edit gets fixed every time, reported once
        self._rp_last_log[channel.id] = now
        log_channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
        if log_channel is None or not hasattr(log_channel, "send"):
            log.info("roomperms: guard restored %s in %s (no mod-log channel found)", list(fixes), channel.id)
            return
        who = actor.mention if actor is not None else "unknown (the audit log didn't say)"
        owner = f"<@{owner_id}>" if owner_id else "none"
        if failed:
            text = (f"⚠️ **Room guard could not fix** {channel.mention} (owner {owner}). Changed by {who}. "
                    "Discord refused the change; check the bot's permissions and role position.")
        else:
            lines = "\n".join(f"• {line}" for line in roomperms.describe_fixes(fixes))
            text = (f"🛡️ **Room guard** put back protected permissions in {channel.mention} "
                    f"(owner {owner}). Changed by {who}.\n{lines}")
            if removed:
                text += "\n• disconnected: " + ", ".join(f"<@{member_id}>" for member_id in removed)
        try:
            await log_channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("roomperms: could not post to the mod log", exc_info=True)
