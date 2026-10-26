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
from typing import Dict, Optional

import discord
from redbot.core import commands

from . import roomperms
from .constants import GUARD_DELAY, GUARD_PASSES, MOD_LOG_CHANNEL_ID

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
        self._rp_pending: set = set()

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
                settings = await self.config.guild(guild).roomperms()
                enabled = bool((settings or {}).get("enabled", True))
                can_manage_roles = bool(guild.me.guild_permissions.manage_roles)
                if roomperms.should_grant_owner_manage_permissions(cfg, enabled, can_manage_roles):
                    roomperms.grant_owner_manage_permissions(cfg)
        except Exception:
            log.exception("roomperms: could not adjust the owner permissions; leaving them as AutoRoom set them.")
        return cfg

    # ------------------------------------------------------------ status / switch

    async def _rp_set_enabled(self, guild, enabled: bool) -> None:
        settings = dict(await self.config.guild(guild).roomperms() or {})
        settings["enabled"] = enabled
        await self.config.guild(guild).roomperms.set(settings)

    async def _rp_status(self, guild) -> str:
        settings = await self.config.guild(guild).roomperms() or {}
        enabled = bool(settings.get("enabled", True))
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
                return  # already waiting; it re-reads the room fresh, so this edit is covered
            self._rp_pending.add(after.id)
            try:
                await asyncio.sleep(GUARD_DELAY)  # lets rapid edits settle and the audit log catch up
                for attempt in range(GUARD_PASSES):
                    if not await self._rp_guard_once(after.guild, after.id, first=attempt == 0):
                        break
            finally:
                self._rp_pending.discard(after.id)
        except Exception:
            log.exception("roomperms: guard failed")

    async def _rp_is_staff(self, member) -> bool:
        try:
            return bool(await self.bot.is_mod(member) or await self.bot.is_admin(member))
        except Exception:
            return False

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

        room, src = overwrite_map(channel.overwrites), overwrite_map(source.overwrites)
        everyone_id = guild.default_role.id
        protected = roomperms.protected_roles(src, everyone_id)
        owner_id = info.get("owner")

        member_roles, exempt = {}, set()
        for kind, member_id in room:
            if kind != "member":
                continue
            member = guild.get_member(member_id)
            if member is None:
                continue
            member_roles[member_id] = {role.id for role in member.roles}
            if member.bot or member_id == owner_id or member_id == guild.owner_id:
                exempt.add(member_id)
            elif protected.keys() & member_roles[member_id] and await self._rp_is_staff(member):
                exempt.add(member_id)

        fixes = roomperms.plan_guard(room, src, everyone_id, member_roles, exempt)
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
        if changed and first:
            await self._rp_log(guild, channel, owner_id, actor, changed)
        return bool(changed)

    async def _rp_log(self, guild, channel, owner_id, actor, fixes) -> None:
        log_channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
        if log_channel is None or not hasattr(log_channel, "send"):
            log.info("roomperms: guard restored %s in %s (no mod-log channel found)", list(fixes), channel.id)
            return
        who = actor.mention if actor is not None else "unknown (the audit log didn't say)"
        owner = f"<@{owner_id}>" if owner_id else "none"
        lines = "\n".join(f"• {line}" for line in roomperms.describe_fixes(fixes))
        try:
            await log_channel.send(
                f"🛡️ **Room guard** put back protected permissions in {channel.mention} "
                f"(owner {owner}). Changed by {who}.\n{lines}",
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("roomperms: could not post to the mod log", exc_info=True)
