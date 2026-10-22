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
from . import panel as panel_specs
from .settings_panel import PanelError, open_panel
from .views import InterestView, InviteView, LeaveConfirmView, RabbitView, ReviewView

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
        self._review_locks: Dict[int, asyncio.Lock] = {}
        self._state: Dict[int, dict] = {}        # guild_id -> {interest_key: {str(uid): entry}}
        self._dirty: set = set()
        self._interest_views: Dict[int, InterestView] = {}
        self._task: Optional[asyncio.Task] = None
        self._drop_task: Optional[asyncio.Task] = None
        self._drop_locks: Dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------ lifecycle

    async def cog_load(self):
        self.bot.add_view(RabbitView(self))
        self.bot.add_view(InviteView(self))
        self.bot.add_view(ReviewView(self))
        self._task = asyncio.create_task(self._sweep_loop())
        self._drop_task = asyncio.create_task(self._drop_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()
        if self._drop_task:
            self._drop_task.cancel()
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

    # ------------------------------------------------------------ rabbit drops

    async def _drop_loop(self):
        """Every half minute: delete drops that have expired, post drops that are
        due. Cheap (one config read per guild) and wrapped so it never dies."""
        try:
            await self.bot.wait_until_ready()
        except asyncio.CancelledError:
            return
        while True:
            for guild in list(self.bot.guilds):
                try:
                    await self._drop_tick(guild)
                except asyncio.CancelledError:
                    return
                except Exception:
                    log.exception("afterdark: rabbit drop tick failed for guild %s", guild.id)
            try:
                await asyncio.sleep(C.DROP_TICK_SECONDS)
            except asyncio.CancelledError:
                return

    def _drop_lock(self, guild) -> asyncio.Lock:
        return self._drop_locks.setdefault(guild.id, asyncio.Lock())

    def _drop_channel(self, guild, cfg: dict):
        return guild.get_channel(cfg.get("drop_channel_id") or cfg["clue_channel_id"])

    def _schedule_next_drop(self, cfg: dict, state: dict, base: float) -> None:
        state["next_ts"] = base + engine.next_drop_gap_seconds(cfg["drop_min_minutes"], cfg["drop_max_minutes"], self._rng)

    async def _drop_tick(self, guild):
        async with self._drop_lock(guild):
            cfg = await self.config.guild(guild).all()
            state = dict(cfg["drop_state"])
            now = self._clock()
            action = engine.drop_action(state, cfg["drop_enabled"], now)
            if action is None:
                return
            if action == "delete":
                await self._drop_delete(guild, state)
            elif action == "schedule":
                self._schedule_next_drop(cfg, state, now)
            else:
                await self._drop_post(guild, cfg, state, now)
            await self.config.guild(guild).drop_state.set(state)

    async def _drop_delete(self, guild, state: dict) -> None:
        """Remove the live drop (if it is already gone, that is fine) and forget it."""
        channel = guild.get_channel(state.get("channel_id") or 0)
        message_id = state.get("message_id")
        if channel is not None and message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.info("afterdark: could not delete rabbit drop %s (already gone or no permission)", message_id)
        state.pop("message_id", None)
        state.pop("channel_id", None)
        state.pop("delete_ts", None)
        state.pop("claims", None)

    async def _drop_post(self, guild, cfg: dict, state: dict, now: float) -> bool:
        """Post one drop and plan the next. The next drop is always planned, even
        if this one failed, so a permission problem can't make the loop retry every tick."""
        channel = self._drop_channel(guild, cfg)
        self._schedule_next_drop(cfg, state, now)
        if channel is None:
            await self._log(guild, "⚠️ Rabbit drop skipped: no drop channel is set (open `.afterdark`, Drops).")
            return False
        variant = engine.pick_variant(len(C.DROP_VARIANTS), state.get("last_variant"), self._rng)
        try:
            message = await channel.send(embed=embeds.rabbit_embed(variant), view=RabbitView(self))
        except (discord.Forbidden, discord.HTTPException) as exc:
            await self._log(guild, f"⚠️ Rabbit drop failed in {channel.mention}: {exc}")
            return False
        state["message_id"], state["channel_id"] = message.id, channel.id
        state["delete_ts"] = now + cfg["drop_life_minutes"] * 60
        state["last_variant"] = variant
        state["claims"] = []
        return True

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

    def _interest_channels(self, guild, interest: Interest) -> list:
        """The interest's channels that still exist."""
        return [c for c in (guild.get_channel(cid) for cid in interest.channel_ids) if c is not None]

    def _has_any_overwrite(self, member, interest: Interest) -> bool:
        return any(self._has_overwrite(member, c) for c in self._interest_channels(member.guild, interest))

    def _has_access(self, member, interest: Interest) -> bool:
        """Any access at all (role, or an overwrite on at least one of its channels)."""
        if interest.role_id and any(r.id == interest.role_id for r in member.roles):
            return True
        return self._has_any_overwrite(member, interest)

    def _has_full_access(self, member, interest: Interest) -> bool:
        """Access to EVERY channel. A member who joined before a channel was added
        has access but not full access; clicking the button again completes it."""
        if interest.role_id and any(r.id == interest.role_id for r in member.roles):
            return True
        channels = self._interest_channels(member.guild, interest)
        return bool(channels) and all(self._has_overwrite(member, c) for c in channels)

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
        channels = self._interest_channels(guild, interest)
        if not channels:
            return False, "the interest channel no longer exists"
        added = []
        for channel in channels:
            if self._has_overwrite(member, channel):
                continue
            if len(channel.overwrites) >= C.OVERWRITE_SOFT_CAP:
                why = f"{channel.name} is near Discord's 100-overwrite limit"
            else:
                try:
                    await channel.set_permissions(
                        member,
                        overwrite=discord.PermissionOverwrite(
                            view_channel=True, read_message_history=True, add_reactions=True
                        ),
                        reason=f"AfterDark: joined {interest.key}",
                    )
                    added.append(channel)
                    continue
                except (discord.Forbidden, discord.HTTPException) as exc:
                    why = f"Discord refused the permission change in {channel.name} ({exc})"
            # All or nothing: undo what this call added so access never ends up half-done.
            for done in added:
                try:
                    await done.set_permissions(member, overwrite=None, reason="AfterDark: rolling back")
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("afterdark: rollback failed in %s for %s", done.id, member.id)
            return False, why
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
        for channel in self._interest_channels(guild, interest):
            if member in channel.overwrites:
                try:
                    await channel.set_permissions(member, overwrite=None, reason="AfterDark: access removed")
                    removed.append(f"{interest.key} overwrite ({channel.name})")
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("afterdark: could not clear %s overwrite in %s for %s",
                                interest.key, channel.id, member.id)
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
        message = getattr(interaction, "message", None)
        state = cfg["drop_state"]
        permanent = (cfg.get("rabbit_post") or {}).get("message_id")
        is_drop = message is not None and message.id != permanent      # any other 🐇 message is a drop
        if not is_drop:
            return await self._claim_rabbit(interaction, cfg)
        async with self._drop_lock(guild):          # slots are counted under a lock so the last one can't be double-taken
            state = dict(await self.config.guild(guild).drop_state())
            cap = cfg.get("drop_max_claims", 0)
            claims = list(state.get("claims", []))
            if state.get("message_id") != message.id or (cap and len(claims) >= cap):
                return await self._reply(interaction, "Too slow. The rabbit is gone.")
            if not await self._claim_rabbit(interaction, cfg):
                return
            claims.append(member.id)
            state["claims"] = claims
            if cap and len(claims) >= cap:
                await self._drop_delete(guild, state)      # full: take it down now
            await self.config.guild(guild).drop_state.set(state)

    async def _claim_rabbit(self, interaction, cfg: dict) -> bool:
        """Run the eligibility check and grant the role. True only if the member got in."""
        guild, member = interaction.guild, interaction.user
        code = await self._eligibility(member, cfg, check_level=True)
        if code != engine.OK:
            await self._reply(interaction, self._deny_text(code, cfg))
            return False
        ok, why = await self._grant_rabbit(member, via="button")
        if not ok:
            await self._log(guild, f"⚠️ Rabbit Hole button failed for {self._label(member)}: {why}")
            await self._reply(interaction, "Something went wrong. Ask a moderator.")
            return False
        await self._reply(interaction, self._welcome_text(cfg))
        return True

    async def handle_rabbit_leave(self, interaction):
        guild, member = interaction.guild, interaction.user
        if guild is None or not hasattr(member, "roles"):
            return await self._reply(interaction, "This only works inside the server.")
        role = guild.get_role(await self.config.guild(guild).rabbit_role_id())
        if role is None or role not in member.roles:
            return await self._reply(interaction, "You're not in the Rabbit Hole.")
        try:
            await interaction.response.send_message(
                "Leave the Rabbit Hole? This removes the role and **every** channel you joined. "
                "**You will not be let back in.**",
                view=LeaveConfirmView(self, member), ephemeral=True,
            )
        except (discord.HTTPException, discord.NotFound):
            log.warning("afterdark: could not show the leave confirmation")

    async def handle_rabbit_leave_confirm(self, interaction, member):
        if interaction.user.id != member.id:
            return await self._reply(interaction, "That's not yours.")
        # Leaving is final: record it like an exclusion so no route (button, drop, invite) lets them back in.
        excluded = dict(await self.config.guild(member.guild).excluded())
        excluded[str(member.id)] = ExcludeEntry(by=member.id, reason="left by choice", ts=self._clock()).to_dict()
        await self.config.guild(member.guild).excluded.set(excluded)
        invites = dict(await self.config.guild(member.guild).invites())
        if invites.pop(str(member.id), None) is not None:
            await self.config.guild(member.guild).invites.set(invites)
        removed = await self._revoke_all(member, reason="left by choice")
        await self._log(member.guild, f"\U0001F6AA {self._label(member)} left the Rabbit Hole for good (`.afterdark unexclude` to let them back)"
                        + (f" ({', '.join(removed)})." if removed else "."))
        try:
            await interaction.response.edit_message(
                content="You're out. \U0001F407" if removed else "You were already out.", view=None)
        except (discord.HTTPException, discord.NotFound):
            pass

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
        if interest is None or self._interest_hidden(interest, cfg):
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
            if self._has_full_access(member, interest):
                await self._strip_interest_unlocked(member, interest, state)
                await self._flush_state(guild.id)
                return await self._reply(interaction, f"You left {interest.name}.")
            ok, why = await self._grant_interest_access(member, interest, cfg)
            if not ok:
                await self._log(guild, f"⚠️ Interest join failed ({interest.key}) for {self._label(member)}: {why}")
                return await self._reply(interaction, "Something went wrong. Ask a moderator.")
            if str(member.id) not in state.get(key, {}):     # completing a partial join keeps the old clock
                state.setdefault(key, {})[str(member.id)] = engine.new_membership(self._clock())
                self._mark_dirty(guild.id)
            await self._flush_state(guild.id)
        await self._reply(interaction, (interest.join_message or "").strip()[:2000] or f"You're in {interest.name}.")

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
            if channel_id in interest.channel_ids:
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
            if self._interest_hidden(interest, cfg):
                continue          # nobody can post in a hidden channel, so nobody is warned or removed for it
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
                    if cfg["access_mode"] == "overwrites" and self._has_any_overwrite(member, interest):
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

    async def _report_sweep(self, guild, summary: dict, *, force: bool = False):
        """Sweeps run hourly but report once a day. Warned / removed / revoked
        members are queued and posted together as one digest. Aborts and errors
        need a mod now, so they post straight away. `force` (a manual
        `.afterdark sweep`) flushes the queue immediately."""
        if summary["aborted"] or summary["errors"]:
            await self._post_alert(guild, summary)
        if summary["aborted"]:
            return  # nothing was done, so there is nothing to put in a digest
        conf = self.config.guild(guild)
        pending = await conf.digest_pending()
        now = self._clock()
        if pending and bool(pending.get("dry")) != bool(summary["dry"]):
            await self._post_digest(guild, pending)  # mode flipped: never mix "would" and "did"
            pending = {}
        queued = pending or {"dry": bool(summary["dry"]), "revoke": [], "remove": [], "warn": []}
        for kind in ("revoke", "remove", "warn"):
            for line in summary[kind]:
                if line not in queued[kind]:  # a dry run finds the same members every sweep
                    queued[kind].append(line)
        if not any(queued[k] for k in ("revoke", "remove", "warn")):
            await conf.digest_pending.set({})
            return  # (daily invitations log themselves)
        if force or now - float(await conf.digest_last()) >= C.DIGEST_SECONDS:
            await self._post_digest(guild, queued)
        else:
            await conf.digest_pending.set(queued)

    async def _post_digest(self, guild, queued: dict):
        conf = self.config.guild(guild)
        dry = bool(queued.get("dry"))
        counts = {k: len(queued.get(k, [])) for k in ("revoke", "remove", "warn")}
        verb = {"revoke": "revoked", "remove": "removed", "warn": "warned"}
        if dry:
            verb = {"revoke": "would revoke", "remove": "would remove", "warn": "would warn"}
        head = "\U0001F9EA Sweep digest (dry-run, nothing changed)" if dry else "\U0001F9F9 Sweep digest"
        lines = [head, "• " + ", ".join(f"{counts[k]} {verb[k]}" for k in ("warn", "remove", "revoke"))]
        for kind in ("warn", "remove", "revoke"):
            for line in engine.cap_lines(queued.get(kind, []), C.DIGEST_LINE_CAP):
                lines.append(f"• {verb[kind]}: {line}")
        await self._log(guild, "\n".join(lines))
        await conf.digest_last.set(self._clock())
        await conf.digest_pending.set({})

    async def _post_alert(self, guild, summary: dict):
        """Circuit-breaker aborts and per-member errors: posted immediately."""
        lines = ["⛔ Sweep ABORTED: too many actions for the circuit breaker (nothing changed)"
                 if summary["aborted"] else "⚠️ Sweep finished with errors (the rest is in the next digest)"]
        if summary["aborted"]:
            for label, items in (("revoke", summary["revoke"]), ("remove", summary["remove"])):
                for line in engine.cap_lines(items, C.LOG_LINE_CAP):
                    lines.append(f"• would {label}: {line}")
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
        again), start today's review round, and keep an open round moving.
        Independent of dry-run."""
        now = self._clock()
        invites = dict(cfg["invites"])
        for uid in engine.expired_invites(invites, now, cfg["invite_ttl_days"]):
            inv = Invite.from_dict(invites.pop(uid))
            summary["expired_invites"] += 1
            await self._expire_invite_message(int(uid), inv)
        if invites != cfg["invites"]:
            await self.config.guild(guild).invites.set(invites)
        snoozed = engine.prune_snoozed(cfg["invite_snoozed"], now)
        if snoozed != cfg["invite_snoozed"]:
            await self.config.guild(guild).invite_snoozed.set(snoozed)
        if not cfg["invites_enabled"]:
            return
        today = engine.la_date(now)
        rnd = cfg["invite_round"]
        if cfg["last_invite_date"] != today or rnd.get("date") != today:
            await self._start_round(guild)
            summary["invited"] = 1          # a new review round started
            return
        if rnd.get("prompt") and not await self._prompt_exists(guild, rnd["prompt"]):
            await self._clear_prompt(guild)  # a mod deleted the prompt: put a fresh one up
        await self._post_next_review(guild)

    def _review_lock(self, guild) -> asyncio.Lock:
        lock = self._review_locks.get(guild.id)
        if lock is None:
            lock = self._review_locks[guild.id] = asyncio.Lock()
        return lock

    async def _prompt_exists(self, guild, prompt: dict) -> bool:
        channel = guild.get_channel(prompt.get("channel_id") or 0)
        if channel is None:
            return False
        try:
            await channel.fetch_message(prompt["message_id"])
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return True      # can't tell: don't spam a second prompt

    async def _clear_prompt(self, guild):
        rnd = dict(await self.config.guild(guild).invite_round())
        rnd["prompt"] = {}
        await self.config.guild(guild).invite_round.set(rnd)

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

    async def _start_round(self, guild) -> int:
        """Open a new review round: pick today's target (3-4 by default) and put the
        first candidate in front of the moderators. Any older open prompt is closed."""
        cfg = await self.config.guild(guild).all()
        now = self._clock()
        old = cfg["invite_round"].get("prompt")
        if old:
            await self._close_prompt(guild, old, "Replaced by a newer review round.")
        target = engine.invite_count(self._rng, cfg["invite_min"], cfg["invite_max"])
        await self.config.guild(guild).invite_round.set(engine.new_round(engine.la_date(now), target))
        await self.config.guild(guild).last_invite_date.set(engine.la_date(now))
        await self._post_next_review(guild)
        return target

    async def _close_prompt(self, guild, prompt: dict, text: str):
        channel = guild.get_channel(prompt.get("channel_id") or 0)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(prompt["message_id"])
            old = message.embeds[0] if message.embeds else None
            if old is not None:
                await message.edit(embed=embeds.review_result_embed(old, text), view=None)
        except Exception:
            log.debug("afterdark: could not close an old review prompt")

    async def _review_pool(self, guild, cfg: dict, now: float):
        """Eligible members, highest level first, snoozed ones left out."""
        levels: Dict[int, int] = {}
        for uid in await self._invite_candidates(guild, cfg):
            member = guild.get_member(uid)
            if member is not None:
                levels[uid] = await self._get_level(member) or 0
        verified = guild.get_role(cfg.get("verified_role_id") or 0)
        priority = [uid for uid in levels if verified is not None
                    and verified in getattr(guild.get_member(uid), "roles", [])]
        return engine.rank_by_level(levels, cfg["invite_snoozed"], now, priority=priority), levels

    async def _post_next_review(self, guild):
        """Put the next candidate in front of the moderators, unless a prompt is
        already waiting or today's round is finished."""
        async with self._review_lock(guild):
            cfg = await self.config.guild(guild).all()
            rnd = dict(cfg["invite_round"])
            if not engine.round_needs_prompt(rnd):
                return
            now = self._clock()
            channel = guild.get_channel(cfg["log_channel_id"] or 0)
            if channel is None:
                log.warning("afterdark: no log channel, so invitation reviews cannot be posted")
                return
            pool, levels = await self._review_pool(guild, cfg, now)
            if not pool:
                rnd["done"] = True
                await self.config.guild(guild).invite_round.set(rnd)
                await self._log(
                    guild,
                    f"\U0001F407 Invitation review: nobody left to suggest today ({rnd['sent']} of {rnd['target']} sent)."
                )
                return
            member = guild.get_member(pool[0])
            try:
                message = await channel.send(
                    embed=embeds.review_embed(
                        member, levels[member.id], rnd["sent"], rnd["target"],
                        verified=(None if not cfg.get("verified_role_id") else any(
                            r.id == cfg["verified_role_id"] for r in member.roles)),
                    ),
                    view=ReviewView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.warning("afterdark: could not post an invitation review prompt", exc_info=True)
                return
            rnd["prompt"] = {"message_id": message.id, "channel_id": channel.id, "user_id": member.id}
            await self.config.guild(guild).invite_round.set(rnd)

    async def _dm_invite(self, member, cfg: dict):
        try:
            return await member.send(
                embed=embeds.invite_embed(member.guild.name, cfg["invite_ttl_days"]), view=InviteView(self)
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def handle_review_stop(self, interaction):
        """A moderator ends today's invitations from the prompt itself."""
        guild, mod = interaction.guild, interaction.user
        if guild is None or not hasattr(mod, "roles"):
            return await self._reply(interaction, "This only works inside the server.")
        cfg = await self.config.guild(guild).all()
        if not self._is_staff(mod, cfg):
            return await self._reply(interaction, "Only moderators can answer this.")
        async with self._review_lock(guild):
            rnd = dict(await self.config.guild(guild).invite_round())
            prompt = rnd.get("prompt") or {}
            if not prompt or prompt.get("message_id") != interaction.message.id:
                return await self._reply(interaction, "That prompt is out of date.")
            await self.config.guild(guild).invite_round.set(engine.stop_round(rnd))
            result = (f"\u23f9\ufe0f Stopped by {mod.display_name}. No more invitations today "
                      f"({rnd.get('sent', 0)} of {rnd.get('target', 0)} sent). Tomorrow's round starts as usual.")
        old = interaction.message.embeds[0] if interaction.message.embeds else None
        try:
            if old is not None:
                await interaction.response.edit_message(embed=embeds.review_result_embed(old, result), view=None)
            else:
                await interaction.response.edit_message(content=result, view=None)
        except (discord.HTTPException, discord.NotFound):
            log.warning("afterdark: could not update the review prompt after Stop")

    async def handle_review(self, interaction, accept: bool):
        """A moderator answers the prompt: send the invitation, or 'not now'."""
        guild, mod = interaction.guild, interaction.user
        if guild is None or not hasattr(mod, "roles"):
            return await self._reply(interaction, "This only works inside the server.")
        cfg = await self.config.guild(guild).all()
        if not self._is_staff(mod, cfg):
            return await self._reply(interaction, "Only moderators can answer this.")
        async with self._review_lock(guild):
            cfg = await self.config.guild(guild).all()
            rnd = dict(cfg["invite_round"])
            prompt = rnd.get("prompt") or {}
            if not prompt or prompt.get("message_id") != interaction.message.id:
                return await self._reply(interaction, "That prompt is out of date.")
            rnd["prompt"] = {}                        # claimed before any further await
            await self.config.guild(guild).invite_round.set(rnd)
            now = self._clock()
            uid = int(prompt["user_id"])
            target = guild.get_member(uid)
            name = target.display_name if target else str(uid)
            snoozed = dict(cfg["invite_snoozed"])
            snooze_days = cfg["invite_snooze_days"]
            if not accept:
                snoozed[str(uid)] = engine.snooze_until(now, snooze_days)
                result = f"\u23ed\ufe0f Not now, by {mod.display_name}. {name} stays in the pool and can come up again in {int(snooze_days)} days."
            elif target is None or uid not in await self._invite_candidates(guild, cfg):
                result = f"\u26a0\ufe0f {name} is no longer eligible (left, already in, or something changed). Skipped."
            else:
                message = await self._dm_invite(target, cfg)
                if message is None:
                    snoozed[str(uid)] = engine.snooze_until(now, snooze_days)
                    result = f"\u26a0\ufe0f {name} has DMs closed, so no invitation went out. They stay in the pool; moving on."
                else:
                    invites = dict(cfg["invites"])
                    invites[str(uid)] = Invite(
                        ts=now, channel_id=getattr(message.channel, "id", None), message_id=message.id
                    ).to_dict()
                    await self.config.guild(guild).invites.set(invites)
                    rnd["sent"] = rnd.get("sent", 0) + 1
                    result = f"\u2705 Invitation sent to {name} by {mod.display_name}."
            await self.config.guild(guild).invite_snoozed.set(snoozed)
            await self.config.guild(guild).invite_round.set(rnd)
            if rnd.get("sent", 0) >= rnd.get("target", 0):
                result += f"\nThat's today's {rnd['target']} invitations."
        old = interaction.message.embeds[0] if interaction.message.embeds else None
        try:
            if old is not None:
                await interaction.response.edit_message(embed=embeds.review_result_embed(old, result), view=None)
            else:
                await interaction.response.edit_message(content=result, view=None)
        except (discord.HTTPException, discord.NotFound):
            log.warning("afterdark: could not update a review prompt")
        await self._post_next_review(guild)

    # --------------------------------------------------------- panel / posts

    @staticmethod
    def _interest_hidden(interest: Interest, cfg: dict) -> bool:
        """True while ANY of the interest's channels is hidden: its button goes away and nobody can join,
        because a join would give them a per-user allow on a channel that is meant to be hidden."""
        hidden = {int(c) for c in (cfg.get("hidden_channels") or {})}
        return any(c in hidden for c in interest.channel_ids)

    async def _register_interest_view(self, guild):
        cfg = await self.config.guild(guild).all()
        old = self._interest_views.pop(guild.id, None)
        if old is not None:
            old.stop()
        interests = list(self._interests(cfg).values())
        if not interests:
            return
        view = InterestView(self, [i for i in interests if not self._interest_hidden(i, cfg)])
        self._interest_views[guild.id] = view
        self.bot.add_view(view)

    async def _refresh_panel(self, guild) -> str:
        """Re-register the buttons and edit the posted member panel to match. Returns a sentence for the reply."""
        await self._register_interest_view(guild)
        cfg = await self.config.guild(guild).all()
        saved, view = cfg["panel_post"], self._interest_views.get(guild.id)
        if not saved or view is None:
            return ""
        channel = guild.get_channel(saved.get("channel_id") or 0)
        if channel is None:
            return " The member panel's channel is gone; re-post it with `.afterdark panel #channel`."
        visible = [i for i in self._interests(cfg).values() if not self._interest_hidden(i, cfg)]
        try:
            message = await channel.fetch_message(saved["message_id"])
            await message.edit(embed=embeds.panel_embed(visible, cfg["warn_days"], cfg["remove_days"]), view=view)
        except (discord.NotFound, discord.HTTPException, KeyError):
            return " I couldn't update the member panel message; re-post it with `.afterdark panel #channel`."
        return " Updated the buttons on the member panel."

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
        """Rabbit Hole control panel (admins). Mods get the help list: grant, revoke, exclude, clear, check."""
        perms = ctx.author.guild_permissions
        if perms.administrator or perms.manage_guild:
            return await open_panel(ctx, self, panel_specs.build_specs(), title="Rabbit Hole", intro=panel_specs.INTRO)
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
        if self._interest_hidden(interest, cfg):
            return await ctx.send(f"{interest.name} is hidden right now. Unhide the channels first (`.afterdark`, Interests & feeds).")
        rabbit = ctx.guild.get_role(cfg["rabbit_role_id"])
        if rabbit is None or rabbit not in member.roles:
            return await ctx.send("They aren't in Rabbit Hole yet. `.afterdark grant` first.")
        code = await self._eligibility(member, cfg, check_level=False)
        if code != engine.OK:
            return await ctx.send(f"Refused ({code}).")
        state = await self._get_state(ctx.guild)
        async with self._lock(member):
            if self._has_full_access(member, interest):
                return await ctx.send(f"{member.display_name} already has {interest.name}.")
            ok, why = await self._grant_interest_access(member, interest, cfg)
            if not ok:
                return await ctx.send(f"Failed: {why}")
            lapsed = engine.lapsed_remove(cfg["lapsed"], key, member.id)
            await self.config.guild(ctx.guild).lapsed.set(lapsed)
            if str(member.id) not in state.get(key, {}):
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
            snoozed = dict(cfg["invite_snoozed"])
            if snoozed.pop(str(member.id), None) is not None:
                await self.config.guild(ctx.guild).invite_snoozed.set(snoozed)
                cleared.append("'not now' snooze")
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
            ("Invitations", f"{'ON' if cfg['invites_enabled'] else 'OFF'}: you approve each one, {cfg['invite_min']}-{cfg['invite_max']}/day, highest level first, expire {cfg['invite_ttl_days']}d"),
            ("Not now (snoozed)", f"{len([1 for t in cfg['invite_snoozed'].values() if float(t) > self._clock()])} member(s), {cfg['invite_snooze_days']}d each"),
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
        await self._report_sweep(ctx.guild, summary, force=True)
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
            return await ctx.send("No channel. Pass one or set the clue channel in `.afterdark`.")
        message = await self._post_or_edit(
            ctx.guild, channel, cfg["rabbit_post"], embed=embeds.rabbit_embed(), view=RabbitView(self)
        )
        await self.config.guild(ctx.guild).rabbit_post.set({"channel_id": channel.id, "message_id": message.id})
        await ctx.send(f"Posted in {channel.mention}. Pin it if you want it to stay visible.")

    # ------------------------------------------------ rabbit drops (used by the panel)

    async def _apply_drops_enabled(self, guild, member, on: bool) -> str:
        if on:
            async with self._drop_lock(guild):
                cfg = await self.config.guild(guild).all()
                channel = self._drop_channel(guild, cfg)
                if channel is None:
                    raise PanelError("Set a drop channel (or the clue channel) first.")
                removed = ""
                saved = cfg["rabbit_post"]
                if saved and saved.get("message_id"):
                    old = guild.get_channel(saved.get("channel_id") or 0)
                    try:
                        message = await old.fetch_message(saved["message_id"]) if old is not None else None
                        if message is not None:
                            await message.delete()
                            removed = " The permanent \U0001F407 message was deleted."
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        removed = " (Couldn't delete the old permanent \U0001F407 message; remove it by hand.)"
                    await self.config.guild(guild).rabbit_post.set({})
                state = dict(cfg["drop_state"])
                self._schedule_next_drop(cfg, state, self._clock())
                await self.config.guild(guild).drop_state.set(state)
                await self.config.guild(guild).drop_enabled.set(True)
            return f"Drops are on in {channel.mention}. The first one is <t:{int(state['next_ts'])}:R>.{removed}"
        async with self._drop_lock(guild):
            state = dict(await self.config.guild(guild).drop_state())
            await self._drop_delete(guild, state)
            state.pop("next_ts", None)
            await self.config.guild(guild).drop_state.set(state)
            await self.config.guild(guild).drop_enabled.set(False)
        return "Drops are off."

    async def _apply_drop_now(self, guild, member, _value=None) -> str:
        async with self._drop_lock(guild):
            cfg = await self.config.guild(guild).all()
            state = dict(cfg["drop_state"])
            if state.get("message_id"):
                raise PanelError("A drop is already live.")
            ok = await self._drop_post(guild, cfg, state, self._clock())
            if not cfg["drop_enabled"]:
                state.pop("next_ts", None)       # a test must not start a schedule
            await self.config.guild(guild).drop_state.set(state)
        if not ok:
            raise PanelError("Couldn't drop (see the log channel).")
        return "Dropped. It disappears after its lifetime."

    async def _apply_drop_timing(self, guild, member, values: dict) -> str:
        minimum, maximum = self._whole(values, "min", "Shortest gap"), self._whole(values, "max", "Longest gap")
        lifetime, slots = self._whole(values, "life", "Lifetime"), self._whole(values, "slots", "Slots")
        error = engine.validate_drop_timing(minimum, maximum, lifetime, C.DROP_MAX_GAP_MINUTES)
        if error:
            raise PanelError(error)
        if not 0 <= slots <= 100:
            raise PanelError("Slots: pick a number from 0 to 100 (0 = unlimited).")
        conf = self.config.guild(guild)
        await conf.drop_min_minutes.set(minimum)
        await conf.drop_max_minutes.set(maximum)
        await conf.drop_life_minutes.set(lifetime)
        await conf.drop_max_claims.set(slots)
        return (f"Drops come every {minimum}-{maximum} minutes, stay {lifetime} minutes and let in "
                f"{slots if slots else 'unlimited'} people (gaps and lifetime apply from the next drop; slots apply to the live one too).")

    async def _apply_drop_next(self, guild, member, values: dict) -> str:
        minutes = self._whole(values, "minutes", "Minutes")
        if not 1 <= minutes <= C.DROP_MAX_GAP_MINUTES:
            raise PanelError(f"Pick 1 to {C.DROP_MAX_GAP_MINUTES} minutes.")
        if not await self.config.guild(guild).drop_enabled():
            raise PanelError("Drops are off. Turn them on first.")
        async with self._drop_lock(guild):
            state = dict(await self.config.guild(guild).drop_state())
            state["next_ts"] = self._clock() + minutes * 60
            await self.config.guild(guild).drop_state.set(state)
        return f"Next drop in {minutes} minutes (<t:{int(state['next_ts'])}:t>). Later gaps stay random."


    @afterdark.command(name="panel")
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_panel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Post (or update) the interest-buttons message."""
        cfg = await self.config.guild(ctx.guild).all()
        interests = self._interests(cfg)
        if not interests:
            return await ctx.send("Add an interest first in `.afterdark` (Interests).")
        channel = channel or ctx.channel
        await self._register_interest_view(ctx.guild)
        visible = [i for i in interests.values() if not self._interest_hidden(i, cfg)]
        view = self._interest_views.get(ctx.guild.id) or InterestView(self, visible)
        embed = embeds.panel_embed(visible, cfg["warn_days"], cfg["remove_days"])
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

    async def _apply_interest_save(self, guild, member, item_id, values: dict, picked) -> str:
        """Create an interest (item_id None) or replace an existing one's name, emoji, role, join
        message and channels. `picked` is the list of channels it opens."""
        interests = dict(await self.config.guild(guild).interests())
        name_text = (values.get("name") or "").strip()
        if item_id is None:
            key = engine.normalize_key(name_text)
            if not key:
                raise PanelError("That name isn't usable as a key (use letters, digits, dashes).")
            if key in interests:
                raise PanelError(f"There is already an interest called `{key}`. Pick it from the list to edit it.")
            if len(interests) >= InterestView.MAX_BUTTONS:
                raise PanelError("That's the maximum number of interests.")
            existing = None
        else:
            key = engine.normalize_key(item_id)
            if key not in interests:
                raise PanelError("No such interest any more.")
            existing = Interest.from_dict(interests[key])
        channel_ids = []
        for channel in picked or []:
            if channel.id not in channel_ids:
                channel_ids.append(channel.id)
        new_channel_name = (values.get("new_channel") or "").strip()
        if not channel_ids and not new_channel_name:
            raise PanelError("Pick at least one channel, or give a name for a new one.")
        role_text = (values.get("role") or "").strip()
        role_id = existing.role_id if existing is not None else None
        if role_text:
            digits = role_text[3:-1] if role_text.startswith("<@&") and role_text.endswith(">") else role_text
            if not digits.isdigit() or guild.get_role(int(digits)) is None:
                raise PanelError("I couldn't find that role. Paste its ID or leave it blank.")
            role_id = int(digits)
        elif existing is not None and "role" in values:
            role_id = None
        created = None
        if new_channel_name:
            created = await self._make_interest_channel(guild, member, new_channel_name)
            channel_ids.append(created.id)
        # The form has separate boxes, so a word typed in the emoji box is simply not an emoji:
        # drop it (Discord rejects the whole member panel on an invalid button emoji).
        emoji_text = (values.get("emoji") or "").strip()
        emoji, name = (emoji_text if engine.valid_emoji(emoji_text) else ""), name_text
        if existing is None:
            interest = Interest(key=key, name=name or key.title(), emoji=emoji, channel_id=channel_ids[0],
                                role_id=role_id, extra_channel_ids=channel_ids[1:])
            added = channel_ids
        else:
            interest = existing
            added = [c for c in channel_ids if c not in interest.channel_ids]
            interest.channel_id, interest.extra_channel_ids = channel_ids[0], channel_ids[1:]
            interest.emoji = emoji
            if name:
                interest.name = name
            interest.role_id = role_id
        if "join_message" in values:
            interest.join_message = (values.get("join_message") or "").strip()[:2000]
        interests[key] = interest.to_dict()
        await self.config.guild(guild).interests.set(interests)
        await self._register_interest_view(guild)
        mode = await self.config.guild(guild).access_mode()
        notes = ""
        if mode == "roles" and not interest.role_id:
            notes += " Warning: mode is roles but no role was given, so joins will fail until you add one."
        if not engine.valid_emoji(interest.emoji):
            notes += " (No valid emoji, so the button has none.)"
        if existing is not None and added:
            notes += " Members who already joined get the new channels when they click the button again, or use `.afterdark grantinterest`."
        listing = ", ".join(f"#{guild.get_channel(c).name}" if guild.get_channel(c) else str(c) for c in interest.channel_ids)
        if created is not None:
            notes = f" Created {created.mention} (age-restricted, hidden from everyone but you and the bot; members get in through the button)." + notes
        return (f"Interest {key} now opens {listing}. Re-post the member panel (`.afterdark panel #channel`) to refresh the buttons."
                + notes)

    async def _apply_interest_remove(self, guild, member, item_id) -> str:
        key = engine.normalize_key(item_id)
        interests = dict(await self.config.guild(guild).interests())
        if key not in interests:
            raise PanelError("No such interest.")
        gone = Interest.from_dict(interests.pop(key))
        await self.config.guild(guild).interests.set(interests)
        await self._register_interest_view(guild)
        still_used = {c for other in interests.values() for c in Interest.from_dict(other).channel_ids}
        exclusive = [c for c in gone.channel_ids if c not in still_used]
        paused = [label for _fid, label in await self._pause_feeds_on(guild, member, set(exclusive))]
        # Close the channels: a removed interest must not leave members with a view of its channel.
        hidden, failed, cleared = [], [], 0
        for channel_id in exclusive:
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            ok, count = await self._hide_channel(guild, channel, member)
            if ok:
                hidden.append(channel.mention)
                cleared += count
            else:
                failed.append(channel.mention)
        snaps = dict(await self.config.guild(guild).hidden_channels())
        forgot = [snaps.pop(str(c), None) is not None for c in exclusive]     # a list, so every channel is forgotten
        if any(forgot):
            await self.config.guild(guild).hidden_channels.set(snaps)
        # Forget who was tracked or lapsed under this key, so a later interest with the same key
        # starts clean and the sweep never warns people who no longer have the channel.
        state = await self._get_state(guild)
        if state.pop(key, None) is not None:
            self._mark_dirty(guild.id)
            await self._flush_state(guild.id)
        lapsed = dict(await self.config.guild(guild).lapsed())
        if lapsed.pop(key, None) is not None:
            await self.config.guild(guild).lapsed.set(lapsed)
        notes = ""
        if hidden:
            notes += (f" Hid {', '.join(hidden)} from everyone except you and the bot "
                      f"({cleared} member/role permission(s) cleared).")
        if failed:
            notes += (f" I could NOT change the permissions of {', '.join(failed)} (the bot needs Manage Channel "
                      "and Manage Permissions there), so people who joined can still see it.")
        if paused:
            notes += f" Paused {len(paused)} feed(s): {', '.join(paused)}."
        return f"Interest {key} removed.{notes} Re-post the member panel to refresh the buttons."

    # ------------------------------------------------------------------
    # Interests and feeds in one place (the panel calls these)
    # ------------------------------------------------------------------

    def _feed_cog(self, name: str, label: str):
        cog = self.bot.get_cog(name)
        if cog is None:
            raise PanelError(f"The {label} cog isn't loaded.")
        return cog

    @staticmethod
    async def _foreign(call):
        """Run a feed cog's own change. Each cog has its own PanelError class, so turn theirs into ours."""
        try:
            return await call
        except PanelError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "PanelError":
                raise PanelError(str(exc)) from exc
            raise

    async def _apply_interest_category(self, guild, member, category) -> str:
        await self.config.guild(guild).interest_category_id.set(category.id if category else 0)
        return (f"New interest channels are created in {category.mention}." if category
                else "No category set: you can't create new channels from here until you pick one.")

    async def _make_interest_channel(self, guild, member, name: str):
        """A new age-restricted text channel, hidden from everyone but the bot and the person making it.
        Members get in through the interest button (a per-member permission, no role)."""
        slug = engine.slugify_channel(name)
        if not slug:
            raise PanelError("That channel name isn't usable. Use letters, digits and dashes.")
        category_id = await self.config.guild(guild).interest_category_id()
        category = guild.get_channel(category_id) if category_id else None
        if category is None:
            raise PanelError("Pick a category for new channels first (the 'Category for new channels' setting).")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                  embed_links=True, read_message_history=True),
            member: discord.PermissionOverwrite(view_channel=True, read_message_history=True, add_reactions=True),
        }
        try:
            return await guild.create_text_channel(C.CHANNEL_PREFIX + slug, category=category, nsfw=True, overwrites=overwrites,
                                                   reason=f"AfterDark: new interest channel (by {member})")
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise PanelError(f"I couldn't create the channel: {exc}")

    async def _hide_channel(self, guild, channel, keep_member) -> Tuple[bool, int]:
        """Lock a channel down in ONE call: @everyone cannot see it, every member and role overwrite is
        dropped, and only the bot and `keep_member` (the admin who removed the interest) keep access.
        The bot and keep_member keep any overwrite they already had. Returns (ok, overwrites cleared)."""
        old = dict(channel.overwrites)
        bot_overwrite = old.get(guild.me) or discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)
        new = {guild.default_role: discord.PermissionOverwrite(view_channel=False), guild.me: bot_overwrite}
        if keep_member is not None and keep_member != guild.me:
            new[keep_member] = old.get(keep_member) or discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, add_reactions=True)
        cleared = len([target for target in old if target not in new])
        try:
            await channel.edit(overwrites=new, reason="AfterDark: interest removed, channel hidden")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("afterdark: could not hide %s: %s", channel.id, exc)
            return False, 0
        return True, cleared

    # ------------------------------------------------------------------
    # Hide every bot channel (name starts with the robot emoji), reversibly
    # ------------------------------------------------------------------

    @staticmethod
    def _with_view(overwrite, value):
        """A copy of an overwrite with only "View Channel" changed (None = neutral)."""
        new = discord.PermissionOverwrite.from_pair(*overwrite.pair())
        new.view_channel = value
        return new

    @staticmethod
    def _in_hide_scope(channel, category_id: int) -> bool:
        """Inside the NSFW category AND named with the robot emoji. Nothing else is ever hidden."""
        return bool(category_id) and getattr(channel, "category_id", None) == category_id \
            and channel.name.startswith(C.HIDE_PREFIX)

    def _prefixed_channels(self, guild, category_id: int) -> list:
        return sorted((c for c in guild.text_channels if self._in_hide_scope(c, category_id)), key=lambda c: c.name)

    def _hide_plan(self, guild, channel, keep_member):
        """(new overwrites, snapshot) for one channel. @everyone is denied, and every member or role that
        could see it gets "View Channel" turned OFF in place (their other flags stay), so unhiding can flip
        exactly those back. The bot, other bots, integration roles and `keep_member` keep their access."""
        old = dict(channel.overwrites)
        default_ow = old.get(guild.default_role)
        snapshot = {"flipped": [], "added": [],
                    "default_view": getattr(default_ow, "view_channel", None) if default_ow is not None else None}
        new = {}
        for target, ow in old.items():
            if target == guild.default_role:
                continue
            keep = (target == guild.me or target == keep_member
                    or getattr(target, "bot", False) or getattr(target, "managed", False))
            if not keep and getattr(ow, "view_channel", None) is True:
                new[target] = self._with_view(ow, False)
                snapshot["flipped"].append(target.id)
            else:
                new[target] = ow
        new[guild.default_role] = (self._with_view(default_ow, False) if default_ow is not None
                                   else discord.PermissionOverwrite(view_channel=False))
        for who, flags in ((guild.me, dict(view_channel=True, send_messages=True, attach_files=True,
                                           embed_links=True, read_message_history=True)),
                           (keep_member, dict(view_channel=True, read_message_history=True, add_reactions=True))):
            if who is None:
                continue
            ow = new.get(who)
            if ow is None:
                new[who] = discord.PermissionOverwrite(**flags)
                snapshot["added"].append(who.id)          # unhide takes this overwrite away again
            elif getattr(ow, "view_channel", None) is not True:
                new[who] = self._with_view(ow, True)
        return new, snapshot

    def _unhide_plan(self, guild, channel, snapshot: dict) -> dict:
        """Undo _hide_plan: flip back only the targets it flipped (and only if they are still denied)."""
        flipped = {int(i) for i in snapshot.get("flipped", [])}
        added = {int(i) for i in snapshot.get("added", [])}
        new = {}
        for target, ow in dict(channel.overwrites).items():
            if getattr(target, "id", None) in added and target != guild.default_role:
                continue              # hide created this one for the bot or you; it wasn't there before
            if target == guild.default_role:
                prior = snapshot.get("default_view")
                new[target] = ow if getattr(ow, "view_channel", None) == prior else self._with_view(ow, prior)
            elif getattr(target, "id", None) in flipped and getattr(ow, "view_channel", None) is False:
                new[target] = self._with_view(ow, True)
            else:
                new[target] = ow
        return new

    async def _edit_all(self, changes, reason: str):
        """Apply [(channel, old overwrites, new overwrites)] one by one; if any fails, put the finished
        ones back and raise, so a hide or unhide is all or nothing."""
        done = []
        for channel, old, new in changes:
            try:
                await channel.edit(overwrites=new, reason=reason)
            except (discord.Forbidden, discord.HTTPException) as exc:
                for back, previous, _n in reversed(done):
                    try:
                        await back.edit(overwrites=previous, reason="AfterDark: rolling back")
                    except (discord.Forbidden, discord.HTTPException):
                        log.warning("afterdark: rollback failed in %s", back.id)
                raise PanelError(f"I couldn't change #{channel.name} ({exc}). Nothing was changed. "
                                 "The bot needs Manage Channel and Manage Permissions there.")
            done.append((channel, old, new))

    async def _apply_hide_preview(self, guild, member, _value=None) -> str:
        """Dry run: list what a hide would do, change nothing, and arm the hide for 15 minutes."""
        cfg = await self.config.guild(guild).all()
        hidden = {int(c) for c in cfg["hidden_channels"]}
        category_id = int(cfg["hide_category_id"] or 0)
        category = guild.get_channel(category_id) if category_id else None
        if category is None:
            raise PanelError(f"The NSFW category (id {category_id}) doesn't exist here, so there is nothing to hide.")
        matches = self._prefixed_channels(guild, category_id)
        if not matches:
            raise PanelError(f"No text channel in {category.name} starts with {C.HIDE_PREFIX}.")
        todo = [c for c in matches if c.id not in hidden]
        if not todo:
            raise PanelError(f"All {len(matches)} {C.HIDE_PREFIX} channel(s) are already hidden.")
        await self.config.guild(guild).hide_preview.set({"ts": self._clock(), "ids": [c.id for c in todo]})
        interests = self._interests(cfg)
        known = {c for i in interests.values() for c in i.channel_ids}
        feeds = await self._feed_rows(guild)
        for _fid, _label, ids, _p, _a in feeds:
            known.update(ids)
        mine = [c for c in todo if c.id in known]
        other = [c for c in todo if c.id not in known]
        after = hidden | {c.id for c in todo}
        buttons = [i.name for i in interests.values()
                   if any(c in after for c in i.channel_ids) and not self._interest_hidden(i, cfg)]
        pausing = [label for _f, label, ids, is_paused, _a in feeds if not is_paused and all(c in after for c in ids)]

        def names(chs):
            return ", ".join(f"#{c.name}" for c in chs) or "none"
        skipped = sorted((c for c in guild.text_channels if getattr(c, "category_id", None) == category_id
                          and not c.name.startswith(C.HIDE_PREFIX)), key=lambda c: c.name)
        lines = [f"Preview only, nothing changed. Would hide {len(todo)} channel(s) in {category.name} from everyone except you and the bots.",
                 f"Feed or interest channels ({len(mine)}): {names(mine)}",
                 f"OTHER {C.HIDE_PREFIX} channels, not a feed or interest ({len(other)}), CHECK THESE: {names(other)}"]
        if skipped:
            lines.append(f"Left alone (in the category, no {C.HIDE_PREFIX}): {names(skipped)}")
        if buttons:
            lines.append("Buttons removed: " + ", ".join(buttons))
        if pausing:
            lines.append("Feeds paused: " + ", ".join(pausing))
        lines.append("To go ahead, pick \"Hide the channels from the last preview\" and confirm within 15 minutes.")
        return engine.clip_text("\n".join(lines), 1900)

    async def _apply_hide(self, guild, member, action: str) -> str:
        action = (action or "").strip().lower()
        if action == "unhide":
            return await self._unhide_all(guild, member)
        if action != "hide":
            raise PanelError("Pick hide or unhide.")
        cfg = await self.config.guild(guild).all()
        preview = cfg["hide_preview"]
        if not preview or self._clock() - float(preview.get("ts", 0)) > C.HIDE_PREVIEW_TTL:
            raise PanelError("Run the preview first. It lists what will be hidden, and a hide only uses a preview "
                             "from the last 15 minutes.")
        hidden = {int(c) for c in cfg["hidden_channels"]}
        channels = []
        for cid in preview.get("ids", []):
            ch = guild.get_channel(int(cid))
            if ch is not None and self._in_hide_scope(ch, int(cfg["hide_category_id"] or 0)) and ch.id not in hidden:
                channels.append(ch)
        if not channels:
            raise PanelError("Nothing from the preview is left to hide (renamed, deleted or already hidden). "
                             "Run the preview again.")
        changes, snaps = [], {}
        for ch in channels:
            new, snap = self._hide_plan(guild, ch, member)
            changes.append((ch, dict(ch.overwrites), new))
            snaps[str(ch.id)] = snap
        await self._edit_all(changes, "AfterDark: hiding bot channels")
        await self.config.guild(guild).hidden_channels.set({**cfg["hidden_channels"], **snaps})
        await self.config.guild(guild).hide_preview.set({})
        now_hidden = hidden | {c.id for c in channels}
        paused = await self._pause_feeds_on(guild, member, now_hidden)
        if paused:
            await self.config.guild(guild).hidden_feeds.set(list(cfg["hidden_feeds"]) + [fid for fid, _l in paused])
        interests = self._interests(cfg)
        gone = [i.name for i in interests.values() if any(c.id in {x.id for x in channels} for c in
                                                         self._interest_channels(guild, i)) and not self._interest_hidden(i, cfg)]
        note = await self._refresh_panel(guild)
        out = f"Hid {len(channels)} channel(s): " + ", ".join(f"#{c.name}" for c in channels) + "."
        out += " Everyone except you and the bots lost access; unhide restores it exactly."
        if paused:
            out += f" Paused {len(paused)} feed(s): {', '.join(l for _f, l in paused)}."
        if gone:
            out += f" Buttons removed: {', '.join(gone)}."
        return out + note

    async def _unhide_all(self, guild, member) -> str:
        cfg = await self.config.guild(guild).all()
        snaps = cfg["hidden_channels"]
        if not snaps:
            raise PanelError("Nothing is hidden.")
        changes, restored = [], []
        for cid_s, snap in snaps.items():
            ch = guild.get_channel(int(cid_s))
            if ch is None:
                continue            # deleted while hidden: nothing to restore
            changes.append((ch, dict(ch.overwrites), self._unhide_plan(guild, ch, snap)))
            restored.append(ch)
        await self._edit_all(changes, "AfterDark: unhiding bot channels")
        hidden = {int(c) for c in snaps}
        await self.config.guild(guild).hidden_channels.set({})
        resumed = []
        rows = {row[0]: row for row in await self._feed_rows(guild)}
        for fid in cfg["hidden_feeds"]:
            row = rows.get(fid)
            if row is not None and row[3]:      # still paused: resume it
                try:
                    await self._apply_feed_pause(guild, member, fid)
                    resumed.append(row[1])
                except PanelError as exc:
                    log.warning("afterdark: could not resume %s: %s", row[1], exc)
        await self.config.guild(guild).hidden_feeds.set([])
        # Nobody could post while it was hidden, so the inactivity clocks start fresh.
        state, now = await self._get_state(guild), self._clock()
        for key, interest in self._interests(cfg).items():
            if any(c in hidden for c in interest.channel_ids):
                for entry in state.get(key, {}).values():
                    entry["since"], entry["last"], entry["warned"] = now, now, 0.0
                self._mark_dirty(guild.id)
        await self._flush_state(guild.id)
        note = await self._refresh_panel(guild)
        out = f"Unhid {len(restored)} channel(s): " + ", ".join(f"#{c.name}" for c in restored) + "."
        if resumed:
            out += f" Resumed {len(resumed)} feed(s): {', '.join(resumed)}."
        return out + " Members' access is back as it was; inactivity clocks restarted." + note

    @staticmethod
    def _real_channels(guild, picked) -> list:
        out = []
        for channel in picked or []:
            real = guild.get_channel(channel.id) or channel
            if real.id not in [c.id for c in out]:
                out.append(real)
        return out

    def _interest_labels(self, interests: Dict[str, Interest], channel_ids) -> str:
        names = [i.name for i in interests.values() if any(c in i.channel_ids for c in channel_ids)]
        return ", ".join(names) if names else "no interest"

    async def _feed_rows(self, guild) -> list:
        """Every Reddit and Telegram feed on this server: (id, label, channel ids, paused, approval text)."""
        rows = []
        reddit = self.bot.get_cog("RedditFeed")
        if reddit is not None:
            for name, raw in sorted((await reddit.config.mappings()).items()):
                ids = [int(c) for c in raw.get("channel_ids", [])]
                if ids and any(guild.get_channel(c) for c in ids):
                    rows.append((f"r:{name}", f"r/{name}", ids, bool(raw.get("paused")), str(raw.get("approval", "manual"))))
        telegram = self.bot.get_cog("TGFeed")
        if telegram is not None:
            for key, raw in sorted((await telegram.config.mappings()).items(), key=lambda kv: int(kv[0])):
                cid = int(raw.get("channel_id") or 0)
                if cid and guild.get_channel(cid):
                    title = raw.get("title") or f"topic {key}"
                    rows.append((f"t:{key}", f"Telegram: {title}", [cid], bool(raw.get("paused")), ""))
        return rows

    async def _feed_items(self, guild) -> list:
        interests = self._interests(await self.config.guild(guild).all())
        out = []
        for item_id, label, ids, paused, approval in await self._feed_rows(guild):
            bits = ["→ " + " ".join(f"<#{c}>" for c in ids), self._interest_labels(interests, ids)]
            if approval:
                bits.append(f"{approval} approval")
            if paused:
                bits.append("PAUSED")
            out.append((item_id, label, " · ".join(bits)))
        return out

    async def _feed_record(self, guild, item_id: str) -> dict:
        for row_id, _label, ids, _paused, _approval in await self._feed_rows(guild):
            if row_id == item_id:
                return {"channel_ids": ids}
        raise PanelError("That feed no longer exists.")

    def _find_interest_key(self, interests: Dict[str, Interest], text: str) -> str:
        wanted = engine.normalize_key(text)
        for key, interest in interests.items():
            if key == wanted or engine.normalize_key(interest.name) == wanted:
                return key
        raise PanelError("I don't know an interest called that. Known: " + (", ".join(i.name for i in interests.values()) or "none yet") + ".")

    async def _apply_feed_save(self, guild, member, item_id, values: dict, picked) -> str:
        values = values or {}
        channels = self._real_channels(guild, picked)
        if item_id is not None:  # edit: move an existing feed to other channel(s)
            if not channels:
                raise PanelError("Pick at least one channel (use Remove to stop the feed).")
            kind, _, ident = str(item_id).partition(":")
            if kind == "r":
                cog = self._feed_cog("RedditFeed", "Reddit feed")
                mapping = await self._foreign(cog._mapping_or_raise(ident))
                return await self._foreign(cog._apply_feed(
                    guild, member, ident, ", ".join(mapping.require_keywords), ", ".join(mapping.block_keywords), channels))
            if len(channels) != 1:
                raise PanelError("A Telegram topic posts to exactly one channel.")
            cog = self._feed_cog("TGFeed", "Telegram feed")
            return await self._foreign(cog._apply_map(guild, member, ident, channels[0]))
        reddit_text = (values.get("reddit") or "").strip()
        topic_text = (values.get("telegram") or "").strip()
        if bool(reddit_text) == bool(topic_text):
            raise PanelError("Fill in a subreddit or a Telegram topic, one of the two.")
        interests = {k: Interest.from_dict(v) for k, v in (await self.config.guild(guild).interests()).items()}
        interest_text = (values.get("interest") or "").strip()
        interest_key = self._find_interest_key(interests, interest_text) if interest_text else None
        new_name = (values.get("new_channel") or "").strip()
        if not channels and not new_name:
            raise PanelError("Pick a channel, or give a name for a new one.")
        cog = self._feed_cog("RedditFeed", "Reddit feed") if reddit_text else self._feed_cog("TGFeed", "Telegram feed")
        if topic_text and len(channels) + (1 if new_name else 0) != 1:
            raise PanelError("A Telegram topic posts to exactly one channel: pick one, or name one new channel.")
        created = None
        if new_name:
            created = await self._make_interest_channel(guild, member, new_name)
            channels.append(created)
        try:
            if reddit_text:
                message = await self._foreign(cog._apply_add(guild, member, reddit_text, channels))
            else:
                message = await self._foreign(cog._apply_map(guild, member, topic_text, channels[0]))
        except Exception:
            if created is not None:  # nothing was mapped to it, so don't leave an empty channel behind
                try:
                    await created.delete(reason="AfterDark: the feed could not be set up")
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("afterdark: could not remove channel %s after a failed feed setup", created.id)
            raise
        if interest_key is not None:
            interest = interests[interest_key]
            for channel in channels:
                if channel.id not in interest.channel_ids:
                    interest.extra_channel_ids.append(channel.id)
            stored = dict(await self.config.guild(guild).interests())
            stored[interest_key] = interest.to_dict()
            await self.config.guild(guild).interests.set(stored)
            message += (f" Added to the {interest.name} interest. Members who already joined get the new channel when they "
                        "click the button again.")
        if created is not None:
            message += f" Created {created.mention} (age-restricted, hidden; members get in through the button)."
        return message

    async def _apply_feed_remove(self, guild, member, item_id) -> str:
        kind, _, ident = str(item_id).partition(":")
        if kind == "r":
            return await self._foreign(self._feed_cog("RedditFeed", "Reddit feed")._apply_remove(guild, member, ident))
        return await self._foreign(self._feed_cog("TGFeed", "Telegram feed")._apply_unmap(guild, member, ident))

    async def _apply_feed_pause(self, guild, member, item_id) -> str:
        """Pause an active feed, or resume a paused one."""
        kind, _, ident = str(item_id).partition(":")
        if kind == "r":
            cog = self._feed_cog("RedditFeed", "Reddit feed")
            mapping = await self._foreign(cog._mapping_or_raise(ident))
            return await self._foreign(cog._apply_pause(guild, member, ident, not mapping.paused))
        cog = self._feed_cog("TGFeed", "Telegram feed")
        mapping = await self._foreign(cog._mapped_or_raise(ident))
        call = cog._apply_resume_topic if mapping.paused else cog._apply_pause_topic
        return await self._foreign(call(guild, member, ident))

    async def _apply_feed_approval(self, guild, member, item_id) -> str:
        kind, _, ident = str(item_id).partition(":")
        if kind != "r":
            raise PanelError("Telegram feeds post straight away; only Reddit feeds have an approval queue.")
        return await self._foreign(self._feed_cog("RedditFeed", "Reddit feed")._apply_switch_mode(guild, member, ident))

    async def _pause_feeds_on(self, guild, member, channel_ids) -> list:
        """Pause every active feed that posts ONLY to these channels. Feeds that also post elsewhere are left alone."""
        if not channel_ids:
            return []
        paused = []     # (feed id, label)
        for item_id, label, ids, is_paused, _approval in await self._feed_rows(guild):
            if is_paused or not all(c in channel_ids for c in ids):
                continue
            try:
                await self._apply_feed_pause(guild, member, item_id)
                paused.append((item_id, label))
            except PanelError as exc:
                log.warning("afterdark: could not pause %s: %s", label, exc)
        return paused


    @afterdark_interest.command(name="list")
    async def interest_list(self, ctx):
        """List interests."""
        cfg = await self.config.guild(ctx.guild).all()
        interests = self._interests(cfg)
        if not interests:
            return await ctx.send("No interests yet.")
        state = await self._get_state(ctx.guild)
        lines = [
            f"{i.emoji if engine.valid_emoji(i.emoji) else ''} `{k}` -> "
            + ", ".join(f"<#{c}>" for c in i.channel_ids)
            + f"{f' role <@&{i.role_id}>' if i.role_id else ''} ({len(state.get(k, {}))} tracked)"
            + (" HIDDEN" if self._interest_hidden(i, cfg) else "")
            for k, i in interests.items()
        ]
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @afterdark_interest.command(name="members")
    async def interest_members(self, ctx, key: Optional[str] = None):
        """Who is in each interest, busiest first. Pass a key for just one.
        Counts come from the join records, so they show what is actually getting opted into."""
        cfg = await self.config.guild(ctx.guild).all()
        interests = self._interests(cfg)
        if key is not None:
            key = engine.normalize_key(key)
            if key not in interests:
                return await ctx.send("No such interest. See `.afterdark interest list`.")
            interests = {key: interests[key]}
        if not interests:
            return await ctx.send("No interests yet.")
        state = await self._get_state(ctx.guild)
        rows = []
        for k, interest in interests.items():
            names = []
            for uid in state.get(k, {}):
                member = ctx.guild.get_member(int(uid))
                names.append(member.display_name if member else f"left server ({uid})")
            rows.append((len(names), interest, sorted(names, key=str.lower)))
        rows.sort(key=lambda r: (-r[0], r[1].name.lower()))
        lines = []
        if key is None:
            rabbit = ctx.guild.get_role(cfg["rabbit_role_id"])
            holders = len(rabbit.members) if rabbit is not None else 0
            lines.append(f"**Rabbit Hole holders: {holders}**")
            lines.append("**Summary:** " + ", ".join(f"{i.name} {n}" for n, i, _ in rows))
            empty = [i.name for n, i, _ in rows if n == 0]
            if empty:
                lines.append("**Nobody yet:** " + ", ".join(empty))
            lines.append("")
        for n, interest, names in rows:
            if n == 0 and key is None:
                continue
            lines.append(f"**{interest.name}** ({n}): " + (", ".join(names) if names else "nobody"))
        for page in engine.chunk_text(lines):
            await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())

    @afterdark.group(name="invite", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def afterdark_invite(self, ctx):
        """Invitation tools."""
        await ctx.send_help()

    async def _apply_invite_now(self, guild, member, _value=None) -> str:
        target = await self._start_round(guild)
        return f"Review round started: up to {target} invitation(s). Check <#{await self.config.guild(guild).log_channel_id()}>."

    async def _apply_invite_stop(self, guild, member, _value=None) -> str:
        async with self._review_lock(guild):
            rnd = dict(await self.config.guild(guild).invite_round())
            prompt = rnd.get("prompt")
            await self.config.guild(guild).invite_round.set(engine.stop_round(rnd))
        if prompt:
            await self._close_prompt(guild, prompt, f"Stopped by {member.display_name}.")
        return "Today's invitations are stopped. A new round starts tomorrow, or start one from the panel."

    @afterdark_invite.command(name="now")
    async def invite_now(self, ctx):
        """Start a review round right now (ignores the once-a-day limit). The first
        candidate appears in the log channel; nothing is sent until you answer."""
        await ctx.send(await self._apply_invite_now(ctx.guild, ctx.author))

    @afterdark_invite.command(name="stop")
    async def invite_stop(self, ctx):
        """End today's invitations now and close any waiting prompt (a new round starts tomorrow, or run `invite now`)."""
        await ctx.send(await self._apply_invite_stop(ctx.guild, ctx.author))

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

    # ------------------------------------------------ settings (used by the panel)

    @staticmethod
    def _whole(values: dict, key: str, label: str) -> int:
        try:
            return int(str(values[key]).strip())
        except (KeyError, ValueError):
            raise PanelError(f"{label} must be a whole number.")

    @staticmethod
    def _number(values: dict, key: str, label: str) -> float:
        try:
            return float(str(values[key]).strip())
        except (KeyError, ValueError):
            raise PanelError(f"{label} must be a number.")

    async def _apply_dry_run(self, guild, member, mode: str) -> str:
        """`dry`: the sweep only reports. `live`: the sweep acts."""
        value = mode != "live"
        await self.config.guild(guild).dry_run.set(value)
        await self._log(guild, f"\u2699\ufe0f {member.display_name} set sweep dry-run {'ON' if value else 'OFF (LIVE)'}.")
        return f"Sweep is now {'DRY-RUN' if value else 'LIVE'}."

    async def _apply_invites_enabled(self, guild, member, value: bool) -> str:
        await self.config.guild(guild).invites_enabled.set(bool(value))
        return f"Daily invitations {'ON' if value else 'OFF'}."

    async def _apply_inactivity(self, guild, member, values: dict) -> str:
        warn_days, remove_days = self._number(values, "warn", "Warn days"), self._number(values, "remove", "Remove days")
        problem = engine.validate_warn_remove(warn_days, remove_days)
        if problem:
            raise PanelError(problem)
        await self.config.guild(guild).warn_days.set(warn_days)
        await self.config.guild(guild).remove_days.set(remove_days)
        return f"Warn after {warn_days:g} days, remove after {remove_days:g}."

    async def _apply_min_level(self, guild, member, values: dict) -> str:
        level = self._whole(values, "level", "Level")
        if level < 0:
            raise PanelError("Level can't be negative.")
        await self.config.guild(guild).min_level.set(level)
        return f"Minimum level is now {level}."

    async def _apply_access_mode(self, guild, member, mode: str) -> str:
        mode = str(mode).lower()
        if mode not in C.ACCESS_MODES:
            raise PanelError("Use roles or overwrites.")
        await self.config.guild(guild).access_mode.set(mode)
        return f"New interest joins use {mode}. Existing access of either kind keeps working and is cleaned up on removal."

    async def _apply_sweep_max(self, guild, member, values: dict) -> str:
        count = self._whole(values, "count", "Circuit breaker")
        if count < 1:
            raise PanelError("Must be at least 1.")
        await self.config.guild(guild).sweep_max.set(count)
        return f"Circuit breaker set to {count}."

    async def _apply_invite_snooze(self, guild, member, values: dict) -> str:
        days = self._whole(values, "days", "Days")
        if days < 0 or days > 365:
            raise PanelError("Pick between 0 and 365 days.")
        await self.config.guild(guild).invite_snooze_days.set(days)
        return f"\"Not now\" now skips a member for {days} day(s)."

    async def _apply_invites_per_day(self, guild, member, values: dict) -> str:
        low, high = self._whole(values, "low", "Low"), self._whole(values, "high", "High")
        if low < 0 or high < low:
            raise PanelError("Need 0 <= low <= high.")
        await self.config.guild(guild).invite_min.set(low)
        await self.config.guild(guild).invite_max.set(high)
        return f"{low}-{high} invitations per day."

    async def _apply_channel(self, guild, member, key: str, channel) -> str:
        """Set one of the channel settings (log_channel_id, clue_channel_id, drop_channel_id); None clears it."""
        if key not in ("log_channel_id", "clue_channel_id", "drop_channel_id"):
            raise PanelError("Unknown setting.")
        await getattr(self.config.guild(guild), key).set(channel.id if channel is not None else 0)
        if channel is None:
            return "Cleared (drops use the clue channel)." if key == "drop_channel_id" else "Cleared."
        return f"Set to {channel.mention}."

    async def _apply_role(self, guild, member, key: str, role) -> str:
        """Set one of the role settings (rabbit_role_id, adult_role_id, verified_role_id)."""
        if key not in ("rabbit_role_id", "adult_role_id", "verified_role_id"):
            raise PanelError("Unknown setting.")
        if role is None and key != "verified_role_id":
            raise PanelError("That role is required.")
        await getattr(self.config.guild(guild), key).set(role.id if role is not None else 0)
        if key == "verified_role_id":
            return (f"Age Verified members are now invited first ({role.name})." if role
                    else "No Age Verified priority: highest level first.")
        return f"Set to {role.name}."
