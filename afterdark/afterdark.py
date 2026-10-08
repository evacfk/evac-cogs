"""AfterDark: gated Rabbit Hole access (button + invitations), interest opt-ins,
inactivity cleanup, exclude list. Pure rules live in engine.py; this file owns
all Discord / Config I/O.

Design notes
- Rabbit Hole access is a single role. Interest access is a role OR a per-user
  channel overwrite (`access_mode`); removal always cleans up BOTH so flipping
  the mode never strands access.
- Level is only required to be *admitted*. Retention checks never look at it.
- Members flagged by the Lurker cog (they hold its Lurker role) are never
  touched: Lurker strips every role and restores them later, and that must not
  look like a revocation.
- The sweep is DRY-RUN by default and has a circuit breaker.
"""
import asyncio
import logging
import random
import time
from typing import Dict, List, Optional, Tuple

import discord
from redbot.core import Config, commands

from . import constants as C
from . import embeds, engine
from .models import ExcludeEntry, Interest, Invite
from .views import InterestView, InviteView, RabbitView

log = logging.getLogger("red.evac-cogs.afterdark")


class AfterDark(commands.Cog):
    """Gated access to the Rabbit Hole (adult image channels)."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=C.CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**{k: (v.copy() if hasattr(v, "copy") else v) for k, v in C.GUILD_DEFAULTS.items()})

        self._clock = time.time          # overridable in tests
        self._rng = random.Random()      # overridable in tests
        self._locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._state: Dict[int, dict] = {}        # guild_id -> {interest_key: {str(uid): entry}}
        self._dirty: set = set()
        self._interest_views: Dict[int, InterestView] = {}
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ lifecycle

    async def cog_load(self):
        self.bot.add_view(RabbitView(self))
        self.bot.add_view(InviteView(self))
        self._task = asyncio.create_task(self._sweep_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()
        for view in self._interest_views.values():
            view.stop()
        self._interest_views.clear()
        await self._flush_all()

    async def _sweep_loop(self):
        """Hourly maintenance. Wrapped per guild so one bad guild (or one bad
        iteration) can never kill the loop silently."""
        try:
            await self.bot.wait_until_ready()
            for guild in self.bot.guilds:
                try:
                    await self._register_interest_view(guild)
                except Exception:
                    log.exception("afterdark: could not register interest view for %s", guild.id)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        while True:
            interval = 60
            for guild in list(self.bot.guilds):
                try:
                    cfg = await self.config.guild(guild).all()
                    interval = max(5, int(cfg.get("sweep_interval_minutes", 60)))
                    summary = await self._sweep_guild(guild)
                    await self._report_sweep(guild, summary)
                    await self._flush_state(guild.id)
                except asyncio.CancelledError:
                    return
                except Exception:
                    log.exception("afterdark: sweep failed for guild %s", guild.id)
            try:
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------- plumbing

    def _lock(self, member) -> asyncio.Lock:
        key = (member.guild.id, member.id)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def _log(self, guild, text: str):
        channel_id = await self.config.guild(guild).log_channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            log.info("afterdark: %s", text)
            return
        try:
            await channel.send(text[:1990], allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            log.warning("afterdark: could not post to the log channel: %s", text)

    @staticmethod
    def _label(member) -> str:
        return f"{member.display_name} ({member.id})"

    def _interests(self, cfg: dict) -> Dict[str, Interest]:
        out = {}
        for key, data in cfg.get("interests", {}).items():
            try:
                out[key] = Interest.from_dict(data)
            except (KeyError, ValueError, TypeError):
                log.warning("afterdark: ignoring malformed interest %r", key)
        return out

    async def _get_state(self, guild) -> dict:
        if guild.id not in self._state:
            self._state[guild.id] = await self.config.guild(guild).interest_state()
        return self._state[guild.id]

    async def _flush_state(self, guild_id: int):
        if guild_id in self._dirty and guild_id in self._state:
            await self.config.guild_from_id(guild_id).interest_state.set(self._state[guild_id])
            self._dirty.discard(guild_id)

    async def _flush_all(self):
        for guild_id in list(self._dirty):
            try:
                await self._flush_state(guild_id)
            except Exception:
                log.exception("afterdark: flush failed for %s", guild_id)

    def _mark_dirty(self, guild_id: int):
        self._dirty.add(guild_id)

    # --------------------------------------------------------------- facts

    async def _get_level(self, member) -> Optional[int]:
        """Level via the LevelUp cog's own database. None = unreadable (fails
        closed for admission). Read-only: never creates a profile."""
        cog = self.bot.get_cog("LevelUp")
        if cog is None:
            return None
        try:
            conf = cog.db.get_conf(member.guild)
            profile = conf.users.get(member.id)
            return int(profile.level) if profile is not None else 0
        except Exception:
            log.warning("afterdark: could not read LevelUp level for %s", member.id, exc_info=True)
            return None

    async def _lurker_role_id(self, guild) -> Optional[int]:
        cog = self.bot.get_cog("Lurker")
        if cog is None:
            return None
        try:
            return await cog.config.guild(guild).lurker_role_id()
        except Exception:
            return None

    @staticmethod
    def _is_paused(member, lurker_role_id: Optional[int], cfg: dict) -> bool:
        """Lurker-flagged (or manually 'paused') members are left strictly alone."""
        role_ids = {r.id for r in member.roles}
        if lurker_role_id and lurker_role_id in role_ids:
            return True
        return bool(role_ids & set(cfg.get("skip_role_ids", [])))

    async def _eligibility(self, member, cfg: dict, *, check_level: bool) -> str:
        level = await self._get_level(member) if check_level else None
        return engine.eligibility(
            role_ids=[r.id for r in member.roles],
            excluded=str(member.id) in cfg["excluded"],
            underage_ids=cfg["underage_role_ids"],
            age_ids=cfg["adult_age_role_ids"],
            adult_role_id=cfg["adult_role_id"],
            level=level,
            min_level=cfg["min_level"],
            check_level=check_level,
        )

    @staticmethod
    def _is_staff(member, cfg: dict) -> bool:
        if {r.id for r in member.roles} & set(cfg["exempt_role_ids"]):
            return True
        perms = member.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    @staticmethod
    def _can_manage(guild, role) -> bool:
        return role is not None and not role.managed and role < guild.me.top_role

    # ------------------------------------------------------- access helpers

    def _has_overwrite(self, member, channel) -> bool:
        return channel is not None and member in channel.overwrites

    def _has_access(self, member, interest: Interest) -> bool:
        if interest.role_id and any(r.id == interest.role_id for r in member.roles):
            return True
        return self._has_overwrite(member, member.guild.get_channel(interest.channel_id))

    async def _grant_interest_access(self, member, interest: Interest, cfg: dict) -> Tuple[bool, str]:
        guild = member.guild
        if cfg["access_mode"] == "roles":
            role = guild.get_role(interest.role_id) if interest.role_id else None
            if role is None:
                return False, "no role is configured for this interest"
            if not self._can_manage(guild, role):
                return False, "the bot cannot manage that role (hierarchy or managed role)"
            try:
                await member.add_roles(role, reason=f"AfterDark: joined {interest.key}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                return False, f"Discord refused the role change ({exc})"
            return True, ""
        channel = guild.get_channel(interest.channel_id)
        if channel is None:
            return False, "the interest channel no longer exists"
        if len(channel.overwrites) >= C.OVERWRITE_SOFT_CAP:
            return False, f"{channel.name} is near Discord's 100-overwrite limit"
        try:
            await channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, add_reactions=True
                ),
                reason=f"AfterDark: joined {interest.key}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            return False, f"Discord refused the permission change ({exc})"
        return True, ""

    async def _strip_interest_unlocked(self, member, interest: Interest, state: dict) -> List[str]:
        """Remove role AND overwrite for one interest, drop tracking. Caller holds the lock."""
        guild = member.guild
        removed: List[str] = []
        role = guild.get_role(interest.role_id) if interest.role_id else None
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="AfterDark: access removed")
                removed.append(f"{interest.key} role")
            except (discord.Forbidden, discord.HTTPException):
                log.warning("afterdark: could not remove %s role from %s", interest.key, member.id)
        channel = guild.get_channel(interest.channel_id)
        if channel is not None and member in channel.overwrites:
            try:
                await channel.set_permissions(member, overwrite=None, reason="AfterDark: access removed")
                removed.append(f"{interest.key} overwrite")
            except (discord.Forbidden, discord.HTTPException):
                log.warning("afterdark: could not clear %s overwrite for %s", interest.key, member.id)
        if state.get(interest.key, {}).pop(str(member.id), None) is not None:
            self._mark_dirty(guild.id)
        return removed

    # ----------------------------------------------------- grant / revoke

    async def _grant_rabbit(self, member, *, via: str, by: Optional[int] = None) -> Tuple[bool, str]:
        guild = member.guild
        async with self._lock(member):
            cfg = await self.config.guild(guild).all()
            role = guild.get_role(cfg["rabbit_role_id"])
            if role is None:
                return False, "the Rabbit Hole role does not exist"
            if role in member.roles:
                return True, "already"
            if not self._can_manage(guild, role):
                return False, "the bot cannot manage the Rabbit Hole role (move the bot role above it)"
            try:
                await member.add_roles(role, reason=f"AfterDark: Rabbit Hole via {via}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                return False, f"Discord refused the role change ({exc})"
            granted = dict(cfg["granted"])
            granted[str(member.id)] = {"ts": self._clock(), "via": via, "by": by}
            invites = dict(cfg["invites"])
            invites.pop(str(member.id), None)
            await self.config.guild(guild).granted.set(granted)
            await self.config.guild(guild).invites.set(invites)
        await self._log(guild, f"\U0001F407 {self._label(member)} entered Rabbit Hole via {via}.")
        return True, ""

    async def _revoke_all(self, member, *, reason: str) -> List[str]:
        """Strip Rabbit Hole + every interest. Never touches excluded/declined/lapsed records."""
        guild = member.guild
        async with self._lock(member):
            cfg = await self.config.guild(guild).all()
            state = await self._get_state(guild)
            removed: List[str] = []
            role = guild.get_role(cfg["rabbit_role_id"])
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason=f"AfterDark: {reason}")
                    removed.append("Rabbit Hole")
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("afterdark: could not remove Rabbit Hole from %s", member.id)
            for interest in self._interests(cfg).values():
                removed += await self._strip_interest_unlocked(member, interest, state)
            granted = dict(cfg["granted"])
            if granted.pop(str(member.id), None) is not None:
                await self.config.guild(guild).granted.set(granted)
            await self._flush_state(guild.id)
        return removed

    # ---------------------------------------------------------- interactions

    async def _reply(self, interaction, text: str):
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except (discord.HTTPException, discord.NotFound):
            log.warning("afterdark: could not reply to an interaction")

    async def handle_rabbit_claim(self, interaction):
        guild, member = interaction.guild, interaction.user
        if guild is None or not hasattr(member, "roles"):
            return await self._reply(interaction, "This only works inside the server.")
        cfg = await self.config.guild(guild).all()
        role = guild.get_role(cfg["rabbit_role_id"])
        if role is not None and role in member.roles:
            return await self._reply(interaction, "You're already through. \U0001F407")
        code = await self._eligibility(member, cfg, check_level=True)
        if code != engine.OK:
            return await self._reply(interaction, self._deny_text(code, cfg))
        ok, why = await self._grant_rabbit(member, via="button")
        if not ok:
            await self._log(guild, f"⚠️ Rabbit Hole button failed for {self._label(member)}: {why}")
            return await self._reply(interaction, "Something went wrong. Ask a moderator.")
        await self._reply(interaction, self._welcome_text(cfg))

    @staticmethod
    def _deny_text(code: str, cfg: dict) -> str:
        """Player-facing refusal. Deliberately vague about age and about exactly
        which requirement failed, except the 'not yet' nudge for level."""
        if code == engine.EXCLUDED:
            return C.CONTACT_TEXT
        if code == engine.LEVEL_LOW:
            return "Not yet. The rabbit only shows itself to regulars. Keep chatting."
        if code == engine.LEVEL_UNKNOWN:
            return "The rabbit can't find you right now. Try again later or ask a moderator."
        return "Not for you."

    @staticmethod
    def _welcome_text(cfg: dict) -> str:
        return "\U0001F407 The rabbit nods. You're through. Look for the new channels."

    async def handle_interest_toggle(self, interaction, key: str):
        guild, member = interaction.guild, interaction.user
        if guild is None or not hasattr(member, "roles"):
            return await self._reply(interaction, "This only works inside the server.")
        cfg = await self.config.guild(guild).all()
        interest = self._interests(cfg).get(key)
        if interest is None:
            return await self._reply(interaction, "That one is closed.")
        rabbit = guild.get_role(cfg["rabbit_role_id"])
        if rabbit is None or rabbit not in member.roles:
            return await self._reply(interaction, "You haven't found the rabbit yet.")
        code = await self._eligibility(member, cfg, check_level=False)
        if code != engine.OK:
            return await self._reply(interaction, self._deny_text(code, cfg))
        if engine.lapsed_has(cfg["lapsed"], key, member.id):
            return await self._reply(
                interaction, f"Your access to {interest.name} lapsed for inactivity. {C.CONTACT_TEXT}"
            )
        state = await self._get_state(guild)
        async with self._lock(member):
            if self._has_access(member, interest):
                await self._strip_interest_unlocked(member, interest, state)
                await self._flush_state(guild.id)
                return await self._reply(interaction, f"You left {interest.name}.")
            ok, why = await self._grant_interest_access(member, interest, cfg)
            if not ok:
                await self._log(guild, f"⚠️ Interest join failed ({interest.key}) for {self._label(member)}: {why}")
                return await self._reply(interaction, "Something went wrong. Ask a moderator.")
            state.setdefault(key, {})[str(member.id)] = engine.new_membership(self._clock())
            self._mark_dirty(guild.id)
            await self._flush_state(guild.id)
        await self._reply(interaction, f"You're in {interest.name}.")

    async def _find_invite_guild(self, user_id: int):
        for guild in self.bot.guilds:
            invites = await self.config.guild(guild).invites()
            if str(user_id) in invites:
                return guild, invites
        return None, {}

    async def handle_invite_accept(self, interaction):
        user = interaction.user
        guild, invites = await self._find_invite_guild(user.id)
        if guild is None:
            return await self._finish_invite(interaction, "expired")
        member = guild.get_member(user.id)
        if member is None:
            return await self._finish_invite(interaction, "expired")
        cfg = await self.config.guild(guild).all()
        code = await self._eligibility(member, cfg, check_level=True)
        if code != engine.OK:
            return await self._reply(interaction, self._deny_text(code, cfg))
        ok, why = await self._grant_rabbit(member, via="invitation")
        if not ok:
            await self._log(guild, f"⚠️ Invitation accept failed for {self._label(member)}: {why}")
            return await self._reply(interaction, "Something went wrong. Ask a moderator.")
        await self._finish_invite(interaction, "accepted")

    async def handle_invite_decline(self, interaction):
        user = interaction.user
        guild, invites = await self._find_invite_guild(user.id)
        if guild is None:
            return await self._finish_invite(interaction, "expired")
        declined = list(await self.config.guild(guild).declined())
        if user.id not in declined:
            declined.append(user.id)
        invites = dict(invites)
        invites.pop(str(user.id), None)
        await self.config.guild(guild).declined.set(declined)
        await self.config.guild(guild).invites.set(invites)
        await self._finish_invite(interaction, "declined")

    async def _finish_invite(self, interaction, kind: str):
        try:
            await interaction.response.edit_message(embed=embeds.invite_result_embed(kind), view=None)
        except (discord.HTTPException, discord.NotFound):
            log.warning("afterdark: could not edit the invitation message")

    # ------------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        guild = member.guild
        cfg = await self.config.guild(guild).all()
        state = await self._get_state(guild)
        changed = False
        for entries in state.values():
            if entries.pop(str(member.id), None) is not None:
                changed = True
        if changed:
            self._mark_dirty(guild.id)
            await self._flush_state(guild.id)
        if str(member.id) in cfg["invites"]:
            invites = dict(cfg["invites"])
            invites.pop(str(member.id), None)
            await self.config.guild(guild).invites.set(invites)
        if str(member.id) in cfg["granted"]:
            granted = dict(cfg["granted"])
            granted.pop(str(member.id), None)
            await self.config.guild(guild).granted.set(granted)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if after.bot or before.roles == after.roles:
            return
        guild = after.guild
        cfg = await self.config.guild(guild).all()
        lurker_rid = await self._lurker_role_id(guild)
        if self._is_paused(after, lurker_rid, cfg):
            return
        rabbit = guild.get_role(cfg["rabbit_role_id"])
        has_rabbit = rabbit is not None and rabbit in after.roles
        interests = self._interests(cfg)
        if has_rabbit:
            code = await self._eligibility(after, cfg, check_level=False)
            if code != engine.OK:
                removed = await self._revoke_all(after, reason=f"no longer eligible ({code})")
                if removed:
                    await self._log(
                        guild, f"\U0001F6AB Revoked {', '.join(removed)} from {self._label(after)}: {code}."
                    )
            return
        # No Rabbit Hole role: interest roles must not survive without it.
        held = [
            i for i in interests.values()
            if i.role_id and any(r.id == i.role_id for r in after.roles)
        ]
        if held:
            state = await self._get_state(guild)
            async with self._lock(after):
                for interest in held:
                    await self._strip_interest_unlocked(after, interest, state)
                await self._flush_state(guild.id)
            await self._log(
                guild,
                f"\U0001F6AB Removed {', '.join(i.key for i in held)} from {self._label(after)}: no Rabbit Hole role.",
            )

    def _interest_for_channel(self, cfg_interests: Dict[str, Interest], channel_id: int) -> Optional[Interest]:
        for interest in cfg_interests.values():
            if interest.channel_id == channel_id:
                return interest
        return None

    async def _touch(self, guild, user_id: int, channel_id: int):
        # Runs on every server message: read only the small interests map, not
        # the whole guild config.
        interests = self._interests({"interests": await self.config.guild(guild).interests()})
        if not interests:
            return
        interest = self._interest_for_channel(interests, channel_id)
        if interest is None:
            thread = guild.get_thread(channel_id) if hasattr(guild, "get_thread") else None
            if thread is not None and getattr(thread, "parent_id", None):
                interest = self._interest_for_channel(interests, thread.parent_id)
        if interest is None:
            return
        state = await self._get_state(guild)
        entry = state.get(interest.key, {}).get(str(user_id))
        if entry is None:
            return  # only members with tracked access count
        now = self._clock()
        if engine.should_record(entry.get("last"), now):
            entry["last"] = now
            entry["warned"] = 0.0
            self._mark_dirty(guild.id)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.author.bot:
            return
        try:
            await self._touch(message.guild, message.author.id, message.channel.id)
        except Exception:
            log.exception("afterdark: activity tracking failed")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        try:
            await self._touch(guild, payload.user_id, payload.channel_id)
        except Exception:
            log.exception("afterdark: reaction tracking failed")

    # ------------------------------------------------------------------ sweep

    async def _sweep_guild(self, guild, *, dry: Optional[bool] = None) -> dict:
        cfg = await self.config.guild(guild).all()
        dry = cfg["dry_run"] if dry is None else dry
        now = self._clock()
        summary = {
            "dry": dry, "revoke": [], "remove": [], "warn": [], "errors": [],
            "aborted": False, "expired_invites": 0, "invited": 0, "adopted": 0,
        }
        rabbit = guild.get_role(cfg["rabbit_role_id"])
        interests = self._interests(cfg)
        state = await self._get_state(guild)
        lurker_rid = await self._lurker_role_id(guild)

        plan_revoke: List[Tuple[object, str]] = []
        revoke_ids = set()
        for member in list(rabbit.members) if rabbit is not None else []:
            if member.bot or self._is_paused(member, lurker_rid, cfg):
                continue
            code = await self._eligibility(member, cfg, check_level=False)
            if code != engine.OK:
                plan_revoke.append((member, code))
                revoke_ids.add(member.id)

        plan_remove: List[Tuple[object, Interest, str]] = []
        plan_warn: List[Tuple[object, Interest, float]] = []
        for key, interest in interests.items():
            entries = state.setdefault(key, {})
            role = guild.get_role(interest.role_id) if interest.role_id else None
            for member in list(role.members) if role is not None else []:
                if member.bot or str(member.id) in entries:
                    continue
                if not dry:
                    entries[str(member.id)] = engine.new_membership(now)
                    self._mark_dirty(guild.id)
                summary["adopted"] += 1
            for uid_s, entry in list(entries.items()):
                member = guild.get_member(int(uid_s))
                if member is None:
                    if not dry:
                        entries.pop(uid_s, None)
                        self._mark_dirty(guild.id)
                    continue
                if member.id in revoke_ids:
                    continue
                if self._is_paused(member, lurker_rid, cfg):
                    if cfg["access_mode"] == "overwrites" and self._has_overwrite(
                        member, guild.get_channel(interest.channel_id)
                    ):
                        # A per-user allow beats the Lurker role's deny, so lurkers
                        # must not keep one. They re-pick after they return.
                        plan_remove.append((member, interest, "lurker"))
                    elif not dry:
                        entry["since"], entry["warned"] = now, 0.0  # fresh clock on return
                        self._mark_dirty(guild.id)
                    continue
                if rabbit is None or rabbit not in member.roles:
                    plan_remove.append((member, interest, "no_rabbit"))
                    continue
                if self._is_staff(member, cfg):
                    continue
                status = engine.inactivity_status(entry, now, cfg["warn_days"], cfg["remove_days"])
                if status == engine.STATUS_REMOVE:
                    plan_remove.append((member, interest, "inactive"))
                elif status == engine.STATUS_WARN:
                    plan_warn.append((member, interest, engine.days_left(entry, now, cfg["remove_days"])))

        total = len(plan_revoke) + len(plan_remove)
        summary["revoke"] = [f"{self._label(m)}: {code}" for m, code in plan_revoke]
        summary["remove"] = [f"{self._label(m)} -> {i.key} ({kind})" for m, i, kind in plan_remove]
        summary["warn"] = [f"{self._label(m)} -> {i.key}" for m, i, _ in plan_warn]

        if not dry and engine.circuit_open(total, cfg["sweep_max"]):
            summary["aborted"] = True
        elif not dry:
            for member, code in plan_revoke:
                try:
                    await self._revoke_all(member, reason=f"sweep: {code}")
                except Exception as exc:
                    summary["errors"].append(f"{self._label(member)}: {exc}")
            for member, interest, kind in plan_remove:
                try:
                    await self._remove_interest(member, interest, kind, notify=kind == "inactive")
                except Exception as exc:
                    summary["errors"].append(f"{self._label(member)}: {exc}")

        if not dry:
            for member, interest, left in plan_warn:
                entry = state.get(interest.key, {}).get(str(member.id))
                if entry is None:
                    continue
                if await self._dm(member, embeds.warning_text(interest.name, engine.humanize_days(left))):
                    entry["warned"] = now
                    self._mark_dirty(guild.id)

        await self._invite_maintenance(guild, cfg, summary)
        await self._flush_state(guild.id)
        await self.config.guild(guild).last_sweep.set(
            {"ts": now, "dry": dry, "revoke": len(plan_revoke), "remove": len(plan_remove),
             "warn": len(plan_warn), "aborted": summary["aborted"], "errors": len(summary["errors"])}
        )
        return summary

    async def _remove_interest(self, member, interest: Interest, kind: str, *, notify: bool):
        """Remove one interest. kind == 'inactive' also records the lapse so the
        member needs a moderator to come back."""
        guild = member.guild
        state = await self._get_state(guild)
        async with self._lock(member):
            await self._strip_interest_unlocked(member, interest, state)
            if kind == "inactive":
                lapsed = engine.lapsed_add(await self.config.guild(guild).lapsed(), interest.key, member.id)
                await self.config.guild(guild).lapsed.set(lapsed)
            await self._flush_state(guild.id)
        if notify:
            await self._dm(member, embeds.removal_text(interest.name))

    async def _dm(self, member, text: str) -> bool:
        try:
            await member.send(text)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _report_sweep(self, guild, summary: dict):
        if not (summary["revoke"] or summary["remove"] or summary["warn"] or summary["errors"]
                or summary["aborted"]):
            return  # (daily invitations log themselves)
        head = "\U0001F9EA Sweep (dry-run, nothing changed)" if summary["dry"] else "\U0001F9F9 Sweep"
        if summary["aborted"]:
            head = "⛔ Sweep ABORTED: too many actions for the circuit breaker (nothing changed)"
        lines = [head]
        verb = "would " if summary["dry"] or summary["aborted"] else ""
        for label, items in (("revoke", summary["revoke"]), ("remove", summary["remove"])):
            for line in engine.cap_lines(items, C.LOG_LINE_CAP):
                lines.append(f"• {verb}{label}: {line}")
        if summary["warn"]:
            lines.append(f"• {'would warn' if summary['dry'] else 'warned'}: {len(summary['warn'])} member(s)")
        for err in summary["errors"][:5]:
            lines.append(f"• error: {err}")
        await self._log(guild, "\n".join(lines))

    # ------------------------------------------------------------ invitations

    async def _invite_candidates(self, guild, cfg: dict) -> List[int]:
        adult = guild.get_role(cfg["adult_role_id"])
        rabbit = guild.get_role(cfg["rabbit_role_id"])
        lurker_rid = await self._lurker_role_id(guild)
        declined = {int(u) for u in cfg["declined"]}
        outstanding = {int(u) for u in cfg["invites"]}
        out: List[int] = []
        for member in list(adult.members) if adult is not None else []:
            if member.bot or member.id in declined or member.id in outstanding:
                continue
            if rabbit is not None and rabbit in member.roles:
                continue
            if self._is_paused(member, lurker_rid, cfg):
                continue
            if await self._eligibility(member, cfg, check_level=True) == engine.OK:
                out.append(member.id)
        return out

    async def _invite_maintenance(self, guild, cfg: dict, summary: dict):
        """Expire old invitations (no cooldown: an expired member is eligible
        again) and send today's batch. Independent of dry-run."""
        now = self._clock()
        invites = dict(cfg["invites"])
        for uid in engine.expired_invites(invites, now, cfg["invite_ttl_days"]):
            inv = Invite.from_dict(invites.pop(uid))
            summary["expired_invites"] += 1
            await self._expire_invite_message(int(uid), inv)
        if invites != cfg["invites"]:
            await self.config.guild(guild).invites.set(invites)
        if cfg["invites_enabled"] and cfg["last_invite_date"] != engine.la_date(now):
            summary["invited"] = await self._send_invites(guild)

    async def _expire_invite_message(self, user_id: int, inv: Invite):
        if not inv.message_id:
            return
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            dm = user.dm_channel or await user.create_dm()
            message = await dm.fetch_message(inv.message_id)
            await message.edit(embed=embeds.invite_result_embed("expired"), view=None)
        except Exception:
            log.debug("afterdark: could not edit expired invitation for %s", user_id)

    async def _send_invites(self, guild) -> int:
        """Send today's batch (3-4 by default). Closed DMs are skipped and the
        next candidate is tried. Returns how many invitations went out."""
        cfg = await self.config.guild(guild).all()
        now = self._clock()
        wanted = engine.invite_count(self._rng, cfg["invite_min"], cfg["invite_max"])
        order = engine.shuffled_candidates(await self._invite_candidates(guild, cfg), self._rng)
        invites = dict(cfg["invites"])
        sent = skipped = 0
        for uid in order:
            if sent >= wanted:
                break
            member = guild.get_member(uid)
            if member is None:
                continue
            try:
                message = await member.send(
                    embed=embeds.invite_embed(guild.name, cfg["invite_ttl_days"]), view=InviteView(self)
                )
            except (discord.Forbidden, discord.HTTPException):
                skipped += 1
                continue
            invites[str(uid)] = Invite(
                ts=now, channel_id=getattr(message.channel, "id", None), message_id=message.id
            ).to_dict()
            sent += 1
        await self.config.guild(guild).invites.set(invites)
        await self.config.guild(guild).last_invite_date.set(engine.la_date(now))
        await self._log(
            guild, f"\U0001F407 Daily invitations: sent {sent} of {wanted} wanted ({skipped} had DMs closed)."
        )
        return sent

    # --------------------------------------------------------- panel / posts

    async def _register_interest_view(self, guild):
        cfg = await self.config.guild(guild).all()
        old = self._interest_views.pop(guild.id, None)
        if old is not None:
            old.stop()
        interests = list(self._interests(cfg).values())
        if not interests:
            return
        view = InterestView(self, interests)
        self._interest_views[guild.id] = view
        self.bot.add_view(view)

    async def _post_or_edit(self, guild, channel, saved: dict, *, embed, view) -> "discord.Message":
        """Edit the previously posted message if it still exists, else send a new one."""
        if saved and saved.get("channel_id") == channel.id and saved.get("message_id"):
            try:
                message = await channel.fetch_message(saved["message_id"])
                await message.edit(embed=embed, view=view)
                return message
            except (discord.NotFound, discord.HTTPException):
                pass
        return await channel.send(embed=embed, view=view)

    # ------------------------------------------------------------- commands

    @commands.group(name="afterdark", invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_roles=True)
    async def afterdark(self, ctx):
        """Rabbit Hole access: grant, revoke, exclude, clear, check."""
        await ctx.send_help()

    @afterdark.command(name="version")
    async def afterdark_version(self, ctx):
        """Show the running build (version probe)."""
        await ctx.send(f"{C.BUILD}")

    @afterdark.command(name="check")
    async def afterdark_check(self, ctx, member: discord.Member):
        """Explain exactly where a member stands."""
        cfg = await self.config.guild(ctx.guild).all()
        role_ids = {r.id for r in member.roles}
        rabbit = ctx.guild.get_role(cfg["rabbit_role_id"])
        level = await self._get_level(member)
        code = await self._eligibility(member, cfg, check_level=True)
        rows = [
            (str(member.id) not in cfg["excluded"], "not on the exclude list"),
            (not role_ids & set(cfg["underage_role_ids"]), "no underage role"),
            (bool(role_ids & set(cfg["adult_age_role_ids"])), "has an adult age role"),
            (cfg["adult_role_id"] in role_ids, "has Adult Chat"),
            (level is not None and level >= cfg["min_level"],
             f"level {level if level is not None else 'unreadable'} (needs {cfg['min_level']})"),
            (rabbit is not None and rabbit in member.roles, "holds Rabbit Hole"),
            (member.id not in {int(u) for u in cfg["declined"]}, "has not declined an invitation"),
            (str(member.id) not in cfg["invites"], "no outstanding invitation"),
        ]
        for key, interest in self._interests(cfg).items():
            lapsed = engine.lapsed_has(cfg["lapsed"], key, member.id)
            rows.append((not lapsed, f"{key}: {'LAPSED (ModMail to return)' if lapsed else 'not lapsed'}"
                         f"{' / has access' if self._has_access(member, interest) else ''}"))
        await ctx.send(embeds.check_lines(f"{member.display_name} -> admission: {code}", rows),
                       allowed_mentions=discord.AllowedMentions.none())

    @afterdark.command(name="grant")
    async def afterdark_grant(self, ctx, member: discord.Member):
        """Grant Rabbit Hole (skips the level check, still needs Adult Chat + adult age role)."""
        cfg = await self.config.guild(ctx.guild).all()
        code = await self._eligibility(member, cfg, check_level=False)
        reasons = {
            engine.EXCLUDED: "They're on the exclude list. `.afterdark unexclude` first.",
            engine.UNDERAGE: "They hold an underage role. Refused.",
            engine.NO_AGE_ROLE: "They have no adult age role. Refused.",
            engine.NO_ADULT_CHAT: "They don't have Adult Chat. Give them that first.",
        }
        if code != engine.OK:
            return await ctx.send(reasons.get(code, f"Refused ({code})."))
        ok, why = await self._grant_rabbit(member, via="moderator", by=ctx.author.id)
        if ok and why == "already":
            return await ctx.send(f"{member.display_name} already has Rabbit Hole.")
        await ctx.send(f"Granted Rabbit Hole to {member.display_name}." if ok else f"Failed: {why}")

    @afterdark.command(name="revoke")
    async def afterdark_revoke(self, ctx, member: discord.Member, *, reason: str = "moderator"):
        """Remove Rabbit Hole and every interest (they can re-enter; use exclude to block)."""
        removed = await self._revoke_all(member, reason=f"revoked by {ctx.author}: {reason}")
        await self._log(ctx.guild, f"\U0001F6AB {ctx.author.display_name} revoked {self._label(member)}: {reason}.")
        await ctx.send(f"Removed: {', '.join(removed)}." if removed else "They had nothing to remove.")

    @afterdark.command(name="grantinterest")
    async def afterdark_grantinterest(self, ctx, member: discord.Member, key: str):
        """Manually give one member access to one interest channel (per-user, no role needed).
        They must already be in Rabbit Hole. Also clears a lapse for that interest."""
        key = engine.normalize_key(key)
        cfg = await self.config.guild(ctx.guild).all()
        interest = self._interests(cfg).get(key)
        if interest is None:
            return await ctx.send("No such interest. See `.afterdark interest list`.")
        rabbit = ctx.guild.get_role(cfg["rabbit_role_id"])
        if rabbit is None or rabbit not in member.roles:
            return await ctx.send("They aren't in Rabbit Hole yet. `.afterdark grant` first.")
        code = await self._eligibility(member, cfg, check_level=False)
        if code != engine.OK:
            return await ctx.send(f"Refused ({code}).")
        state = await self._get_state(ctx.guild)
        async with self._lock(member):
            if self._has_access(member, interest):
                return await ctx.send(f"{member.display_name} already has {interest.name}.")
            ok, why = await self._grant_interest_access(member, interest, cfg)
            if not ok:
                return await ctx.send(f"Failed: {why}")
            lapsed = engine.lapsed_remove(cfg["lapsed"], key, member.id)
            await self.config.guild(ctx.guild).lapsed.set(lapsed)
            state.setdefault(key, {})[str(member.id)] = engine.new_membership(self._clock())
            self._mark_dirty(ctx.guild.id)
            await self._flush_state(ctx.guild.id)
        await self._log(ctx.guild, f"\u2795 {ctx.author.display_name} gave {self._label(member)} access to {interest.name}.")
        await ctx.send(f"{member.display_name} now has {interest.name}.")

    @afterdark.command(name="revokeinterest")
    async def afterdark_revokeinterest(self, ctx, member: discord.Member, key: str):
        """Take one interest away from one member (not recorded as a lapse)."""
        key = engine.normalize_key(key)
        cfg = await self.config.guild(ctx.guild).all()
        interest = self._interests(cfg).get(key)
        if interest is None:
            return await ctx.send("No such interest. See `.afterdark interest list`.")
        state = await self._get_state(ctx.guild)
        async with self._lock(member):
            removed = await self._strip_interest_unlocked(member, interest, state)
            await self._flush_state(ctx.guild.id)
        await self._log(ctx.guild, f"\u2796 {ctx.author.display_name} removed {self._label(member)} from {interest.name}.")
        await ctx.send(f"Removed: {', '.join(removed)}." if removed else "They didn't have it.")

    @afterdark.command(name="exclude")
    async def afterdark_exclude(self, ctx, member: discord.Member, *, reason: str = "no reason given"):
        """Block a member from every route AND revoke existing access immediately."""
        excluded = dict(await self.config.guild(ctx.guild).excluded())
        excluded[str(member.id)] = ExcludeEntry(by=ctx.author.id, reason=reason, ts=self._clock()).to_dict()
        await self.config.guild(ctx.guild).excluded.set(excluded)
        invites = dict(await self.config.guild(ctx.guild).invites())
        if invites.pop(str(member.id), None) is not None:
            await self.config.guild(ctx.guild).invites.set(invites)
        removed = await self._revoke_all(member, reason="excluded")
        await self._log(
            ctx.guild,
            f"\U0001F6D1 {ctx.author.display_name} excluded {self._label(member)}: {reason}."
            + (f" Revoked {', '.join(removed)}." if removed else ""),
        )
        await ctx.send(f"{member.display_name} is excluded." + (f" Revoked: {', '.join(removed)}." if removed else ""))

    @afterdark.command(name="unexclude")
    async def afterdark_unexclude(self, ctx, member: discord.Member):
        """Remove a member from the exclude list."""
        excluded = dict(await self.config.guild(ctx.guild).excluded())
        if excluded.pop(str(member.id), None) is None:
            return await ctx.send("They aren't on the exclude list.")
        await self.config.guild(ctx.guild).excluded.set(excluded)
        await self._log(ctx.guild, f"✅ {ctx.author.display_name} un-excluded {self._label(member)}.")
        await ctx.send(f"{member.display_name} is no longer excluded.")

    @afterdark.command(name="excluded")
    async def afterdark_excluded(self, ctx):
        """List excluded members."""
        excluded = await self.config.guild(ctx.guild).excluded()
        if not excluded:
            return await ctx.send("Nobody is excluded.")
        lines = []
        for uid, raw in excluded.items():
            entry = ExcludeEntry.from_dict(raw)
            who = ctx.guild.get_member(int(uid))
            lines.append(f"{who.display_name if who else 'left the server'} ({uid}): {entry.reason}")
        await ctx.send("\n".join(engine.cap_lines(lines, 40)), allowed_mentions=discord.AllowedMentions.none())

    @afterdark.command(name="clear")
    async def afterdark_clear(self, ctx, member: discord.Member, what: str):
        """Clear a lapse (`clear @user feet`), a declined invitation (`invite`) or both (`all`)."""
        cfg = await self.config.guild(ctx.guild).all()
        what = engine.normalize_key(what)
        cleared = []
        lapsed = cfg["lapsed"]
        keys = [k for k in cfg["interests"] if what in (k, "all")] if what != "invite" else []
        if what == "all":
            keys = list({*cfg["interests"], *lapsed})
        for key in keys:
            if engine.lapsed_has(lapsed, key, member.id):
                lapsed = engine.lapsed_remove(lapsed, key, member.id)
                cleared.append(f"lapse on {key}")
        if lapsed != cfg["lapsed"]:
            await self.config.guild(ctx.guild).lapsed.set(lapsed)
        if what in ("invite", "all"):
            declined = [int(u) for u in cfg["declined"]]
            if member.id in declined:
                declined.remove(member.id)
                await self.config.guild(ctx.guild).declined.set(declined)
                cleared.append("declined invitation")
        if not cleared:
            return await ctx.send("Nothing to clear for that member.")
        await self._log(ctx.guild, f"♻️ {ctx.author.display_name} cleared {', '.join(cleared)} for {self._label(member)}.")
        await ctx.send(f"Cleared {', '.join(cleared)}.")

    @afterdark.command(name="status")
    async def afterdark_status(self, ctx):
        """Show configuration and health."""
        guild = ctx.guild
        cfg = await self.config.guild(guild).all()
        rabbit = guild.get_role(cfg["rabbit_role_id"])
        adult = guild.get_role(cfg["adult_role_id"])
        interests = self._interests(cfg)
        lurker = await self._lurker_role_id(guild)
        level = await self._get_level(ctx.author)
        last = cfg["last_sweep"] or {}
        pairs = [
            ("Build", C.BUILD),
            ("Rabbit Hole role", f"{rabbit.name if rabbit else 'MISSING'}  (bot can manage: {self._can_manage(guild, rabbit)})"),
            ("Adult Chat role", adult.name if adult else "MISSING"),
            ("Min level", cfg["min_level"]),
            ("LevelUp readable", "yes" if level is not None else "NO (admissions fail closed)"),
            # Read-only: afterdark never assigns the Lurker role. It only leaves anyone who
            # holds it alone, because the Lurker cog strips every role (Adult Chat and the
            # rabbit included) and restores them on return, which must not read as a revocation.
            ("Lurker protection", f"members holding Lurker role {lurker} are skipped" if lurker
             else "OFF: Lurker cog not found, its role-stripping could be mistaken for a revocation"),
            ("Access mode", cfg["access_mode"]),
            ("Inactivity", f"warn {cfg['warn_days']}d / remove {cfg['remove_days']}d"),
            ("Sweep", f"{'DRY-RUN' if cfg['dry_run'] else 'LIVE'}, every {cfg['sweep_interval_minutes']}m, max {cfg['sweep_max']} actions"),
            ("Last sweep", last or "never"),
            ("Invitations", f"{'ON' if cfg['invites_enabled'] else 'OFF'}: {cfg['invite_min']}-{cfg['invite_max']}/day, expire {cfg['invite_ttl_days']}d"),
            ("Rabbit Hole holders", len(rabbit.members) if rabbit else 0),
            ("Outstanding invites", len(cfg["invites"])),
            ("Declined / excluded", f"{len(cfg['declined'])} / {len(cfg['excluded'])}"),
            ("Interests", ", ".join(interests) or "none yet"),
        ]
        await ctx.send(f"```\n{embeds.status_lines(pairs)}\n```")

    @afterdark.command(name="sweep")
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_sweep(self, ctx, mode: str = ""):
        """Run the sweep now. Uses the configured dry-run setting unless you say `dry`."""
        dry = True if mode.lower() == "dry" else None
        async with ctx.typing():
            summary = await self._sweep_guild(ctx.guild, dry=dry)
        await self._report_sweep(ctx.guild, summary)
        await ctx.send(
            f"Sweep done ({'dry-run' if summary['dry'] else 'live'}): "
            f"{len(summary['revoke'])} revoke, {len(summary['remove'])} remove, {len(summary['warn'])} warn"
            f"{', ABORTED by circuit breaker' if summary['aborted'] else ''}. Details in the log channel."
        )

    @afterdark.command(name="post")
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_post(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Post (or update) the 🐇 button message. Defaults to the clue channel."""
        cfg = await self.config.guild(ctx.guild).all()
        channel = channel or ctx.guild.get_channel(cfg["clue_channel_id"])
        if channel is None:
            return await ctx.send("No channel. Pass one or `.afterdark set cluechannel #channel`.")
        message = await self._post_or_edit(
            ctx.guild, channel, cfg["rabbit_post"], embed=embeds.rabbit_embed(), view=RabbitView(self)
        )
        await self.config.guild(ctx.guild).rabbit_post.set({"channel_id": channel.id, "message_id": message.id})
        await ctx.send(f"Posted in {channel.mention}. Pin it if you want it to stay visible.")

    @afterdark.command(name="panel")
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_panel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Post (or update) the interest-buttons message."""
        cfg = await self.config.guild(ctx.guild).all()
        interests = self._interests(cfg)
        if not interests:
            return await ctx.send("Add an interest first: `.afterdark interest add`.")
        channel = channel or ctx.channel
        await self._register_interest_view(ctx.guild)
        view = self._interest_views.get(ctx.guild.id) or InterestView(self, interests.values())
        embed = embeds.panel_embed(interests.values(), cfg["warn_days"], cfg["remove_days"])
        try:
            message = await self._post_or_edit(ctx.guild, channel, cfg["panel_post"], embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("afterdark: panel post failed", exc_info=True)
            return await ctx.send(f"Discord refused the panel: {exc}")
        await self.config.guild(ctx.guild).panel_post.set({"channel_id": channel.id, "message_id": message.id})
        await ctx.send(f"Panel posted in {channel.mention}.")

    @afterdark.group(name="interest", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_interest(self, ctx):
        """Manage interest channels."""
        await ctx.send_help()

    @afterdark_interest.command(name="add")
    async def interest_add(self, ctx, key: str, channel: discord.TextChannel, role: Optional[discord.Role] = None,
                           emoji: str = "", *, name: str = ""):
        """`.afterdark interest add feet #feet \U0001F9B6 Feet`. A role is only needed in `roles` mode."""
        key = engine.normalize_key(key)
        if not key:
            return await ctx.send("That key isn't usable (letters, digits, dashes).")
        interests = dict(await self.config.guild(ctx.guild).interests())
        if key not in interests and len(interests) >= InterestView.MAX_BUTTONS:
            return await ctx.send("That's the maximum number of interests.")
        # A word typed where the emoji goes ("Feet") becomes part of the name,
        # because Discord rejects the whole panel on an invalid button emoji.
        emoji, name = engine.split_emoji_and_name(emoji, name)
        interest = Interest(key=key, name=name or key.title(), emoji=emoji,
                            channel_id=channel.id, role_id=role.id if role else None)
        interests[key] = interest.to_dict()
        await self.config.guild(ctx.guild).interests.set(interests)
        await self._register_interest_view(ctx.guild)
        mode = await self.config.guild(ctx.guild).access_mode()
        notes = ""
        if mode == "roles" and role is None:
            notes += " Warning: mode is `roles` but no role was given, so joins will fail until you add one."
        if not emoji:
            notes += " (No valid emoji, so the button has none.)"
        await ctx.send(f"Interest `{key}` saved -> {channel.mention}, button \"{interest.name}\". "
                       f"Re-run `.afterdark panel` to refresh the buttons.{notes}")

    @afterdark_interest.command(name="remove")
    async def interest_remove(self, ctx, key: str):
        """Remove an interest (existing access is left alone; revoke manually if needed)."""
        key = engine.normalize_key(key)
        interests = dict(await self.config.guild(ctx.guild).interests())
        if interests.pop(key, None) is None:
            return await ctx.send("No such interest.")
        await self.config.guild(ctx.guild).interests.set(interests)
        await self._register_interest_view(ctx.guild)
        await ctx.send(f"Interest `{key}` removed. Re-run `.afterdark panel` to refresh the buttons.")

    @afterdark_interest.command(name="list")
    async def interest_list(self, ctx):
        """List interests."""
        cfg = await self.config.guild(ctx.guild).all()
        interests = self._interests(cfg)
        if not interests:
            return await ctx.send("No interests yet.")
        state = await self._get_state(ctx.guild)
        lines = [
            f"{i.emoji if engine.valid_emoji(i.emoji) else ''} `{k}` -> <#{i.channel_id}>"
            f"{f' role <@&{i.role_id}>' if i.role_id else ''} ({len(state.get(k, {}))} tracked)"
            for k, i in interests.items()
        ]
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @afterdark.group(name="invite", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_invite(self, ctx):
        """Invitation tools."""
        await ctx.send_help()

    @afterdark_invite.command(name="now")
    async def invite_now(self, ctx):
        """Send a batch of invitations right now (ignores the once-a-day limit)."""
        sent = await self._send_invites(ctx.guild)
        await ctx.send(f"Sent {sent} invitation(s).")

    @afterdark_invite.command(name="list")
    async def invite_list(self, ctx):
        """Show outstanding invitations."""
        cfg = await self.config.guild(ctx.guild).all()
        if not cfg["invites"]:
            return await ctx.send("No outstanding invitations.")
        now = self._clock()
        lines = []
        for uid, raw in cfg["invites"].items():
            who = ctx.guild.get_member(int(uid))
            left = cfg["invite_ttl_days"] - (now - Invite.from_dict(raw).ts) / C.DAY
            lines.append(f"{who.display_name if who else uid}: expires in {engine.humanize_days(max(0, left))}")
        await ctx.send("\n".join(engine.cap_lines(lines, 40)), allowed_mentions=discord.AllowedMentions.none())

    @afterdark.group(name="set", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_set(self, ctx):
        """Change settings."""
        await ctx.send_help()

    @afterdark_set.command(name="dryrun")
    async def set_dryrun(self, ctx, state: str):
        """`on`: the sweep only reports. `off`: the sweep acts."""
        value = engine.parse_toggle(state)
        if value is None:
            return await ctx.send("Use `on` or `off`.")
        await self.config.guild(ctx.guild).dry_run.set(value)
        await self._log(ctx.guild, f"⚙️ {ctx.author.display_name} set sweep dry-run {'ON' if value else 'OFF (LIVE)'}.")
        await ctx.send(f"Sweep is now {'DRY-RUN' if value else 'LIVE'}.")

    @afterdark_set.command(name="invites")
    async def set_invites(self, ctx, state: str):
        """Turn daily invitations on or off."""
        value = engine.parse_toggle(state)
        if value is None:
            return await ctx.send("Use `on` or `off`.")
        await self.config.guild(ctx.guild).invites_enabled.set(value)
        await ctx.send(f"Daily invitations {'ON' if value else 'OFF'}.")

    @afterdark_set.command(name="inactivity")
    async def set_inactivity(self, ctx, warn_days: float, remove_days: float):
        """`.afterdark set inactivity 7 14`: warn then remove (interest channels only)."""
        problem = engine.validate_warn_remove(warn_days, remove_days)
        if problem:
            return await ctx.send(problem)
        await self.config.guild(ctx.guild).warn_days.set(warn_days)
        await self.config.guild(ctx.guild).remove_days.set(remove_days)
        await ctx.send(f"Warn after {warn_days:g} days, remove after {remove_days:g}.")

    @afterdark_set.command(name="minlevel")
    async def set_minlevel(self, ctx, level: int):
        """Minimum LevelUp level to be admitted."""
        if level < 0:
            return await ctx.send("Level can't be negative.")
        await self.config.guild(ctx.guild).min_level.set(level)
        await ctx.send(f"Minimum level is now {level}.")

    @afterdark_set.command(name="mode")
    async def set_mode(self, ctx, mode: str):
        """`roles` (decoy-named roles) or `overwrites` (per-user channel permissions)."""
        mode = mode.lower()
        if mode not in C.ACCESS_MODES:
            return await ctx.send("Use `roles` or `overwrites`.")
        await self.config.guild(ctx.guild).access_mode.set(mode)
        await ctx.send(f"New interest joins use `{mode}`. Existing access of either kind keeps working and is cleaned up on removal.")

    @afterdark_set.command(name="sweepmax")
    async def set_sweepmax(self, ctx, count: int):
        """Circuit breaker: a live sweep acting on more members than this aborts."""
        if count < 1:
            return await ctx.send("Must be at least 1.")
        await self.config.guild(ctx.guild).sweep_max.set(count)
        await ctx.send(f"Circuit breaker set to {count}.")

    @afterdark_set.command(name="invitesperday")
    async def set_invites_per_day(self, ctx, low: int, high: int):
        """`.afterdark set invitesperday 3 4`."""
        if low < 0 or high < low:
            return await ctx.send("Need 0 <= low <= high.")
        await self.config.guild(ctx.guild).invite_min.set(low)
        await self.config.guild(ctx.guild).invite_max.set(high)
        await ctx.send(f"{low}-{high} invitations per day.")

    @afterdark_set.command(name="logchannel")
    async def set_logchannel(self, ctx, channel: discord.TextChannel):
        """Where grants, revokes and sweep reports are posted."""
        await self.config.guild(ctx.guild).log_channel_id.set(channel.id)
        await ctx.send(f"Logging to {channel.mention}.")

    @afterdark_set.command(name="cluechannel")
    async def set_cluechannel(self, ctx, channel: discord.TextChannel):
        """Where `.afterdark post` puts the 🐇 button by default."""
        await self.config.guild(ctx.guild).clue_channel_id.set(channel.id)
        await ctx.send(f"The rabbit lives in {channel.mention}.")

    @afterdark_set.command(name="rabbitrole")
    async def set_rabbitrole(self, ctx, role: discord.Role):
        """The Rabbit Hole role."""
        await self.config.guild(ctx.guild).rabbit_role_id.set(role.id)
        await ctx.send(f"Rabbit Hole role set to {role.name}.")

    @afterdark_set.command(name="adultrole")
    async def set_adultrole(self, ctx, role: discord.Role):
        """The Adult Chat role (prerequisite)."""
        await self.config.guild(ctx.guild).adult_role_id.set(role.id)
        await ctx.send(f"Adult Chat role set to {role.name}.")
