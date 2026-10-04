import asyncio
import io
import logging
import weakref
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import discord
from redbot.core import Config, checks, commands
from redbot.core.utils.chat_formatting import humanize_list

from . import engine

log = logging.getLogger("red.evac-cogs.lurker")

VERSION = "2.2.0"


class Lurker(commands.Cog):
    """
    Auto-hide long-inactive members behind a Lurker role.

    - Members who go `threshold_days` without activity (message, reaction,
      slash command/button, or joining voice) get every strippable role removed
      and are given the Lurker role, which is denied View Channel everywhere
      except the reactivation channel.
    - Posting in the reactivation channel deletes the message and instantly
      restores their prior roles.
    - New joins get a full threshold window before they can be flagged.
    - Rejoining resets their clock.
    - A weekly digest of flags/restores is posted to a mod channel.
    - Year club: every active member holds exactly one "N year club!" role for
      their time in the server, swapped on each anniversary (`.yearclub`).
      Lurkers are skipped; un-lurking restores their *current* club role.
    """

    BACKFILL_TTL = 86400  # 24h -- dry-run scan stays valid for one day, survives restarts
    UNDO_TTL = 86400
    CHECKPOINT_EVERY = 25  # persist progress every N members processed
    # Red's JSON backend rewrites the whole settings file on every save, so flush
    # rarely. A crash loses at most this much activity, which is harmless against
    # a 30-day threshold; cog_unload still flushes immediately.
    FLUSH_INTERVAL = 3600
    DEFAULT_SWEEP_MAX = 250  # circuit breaker: abort a sweep that would flag more than this

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=928374651, force_registration=True)

        self.config.register_guild(
            lurker_role_id=None,
            lurker_channel_id=None,
            exempt_role_ids=[],
            threshold_days=30,
            last_active={},  # str(user_id) -> unix timestamp; periodically flushed from cache
            enabled=False,   # automatic daily sweep is OFF until explicitly enabled
            last_sweep_ts=0,  # persisted so a cog reload never re-triggers an early sweep
            backfill_scan={},  # {"remaining_ids": [...], "exempt_ids": [...], "ts": float, "total": int}
            undo_scan={},      # {"remaining_ids": [...], "ts": float, "total": int}
            # --- weekly mod report (v2) ---
            report_channel_id=None,
            report_interval_days=7,
            last_report_ts=0,
            report_events=[],   # individually listed flag/unflag events since the last report
            report_counts={},   # bulk (backfill/undo) counters since the last report
            last_sweep={},      # {"ts","candidates","flagged","skipped","errors","aborted"}
            sweep_max=self.DEFAULT_SWEEP_MAX,
            # --- year club (v2.2) ---
            yearclub_roles={},       # str(years) -> role id
            yearclub_enabled=False,  # hourly upkeep (anniversaries); `.yearclub sync` works regardless
            yearclub_last={},        # {"ts","changed","skipped_lurkers","blocked","errors","aborted"}
        )
        self.config.register_member(
            stored_roles=[],
            flagged=False,
        )

        # in-memory cache: {guild_id: {user_id: timestamp}} -- avoids a disk write per message
        self._cache: Dict[int, Dict[int, float]] = {}
        self._loaded_guilds: Set[int] = set()
        self._dirty: Set[int] = set()  # guilds whose last_active changed since last flush

        # report buffers, flushed to Config with the activity cache
        self._events: Dict[int, List[dict]] = {}
        self._counts: Dict[int, Dict[str, int]] = {}
        self._events_lock = asyncio.Lock()

        # per-member lock: flag/unflag are check-then-act and must never interleave
        self._locks: "weakref.WeakValueDictionary[Tuple[int, int], asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )
        # guilds with a backfill / undo-all / manual sweep currently running
        self._bulk_running: Set[int] = set()
        # guilds with a year-club sync/upkeep pass currently running
        self._yearclub_running: Set[int] = set()

        # ExtendedModLog logs an INFO line per role-change audit-reason lookup, which
        # floods the logs when we bulk-strip/restore roles. Quiet it to WARNING+ only.
        logging.getLogger("red.trusty-cogs.ExtendedModLog").setLevel(logging.WARNING)

        self._flush_task = self.bot.loop.create_task(self._flush_loop())
        self._daily_task = self.bot.loop.create_task(self._daily_loop())

    async def cog_unload(self):
        self._flush_task.cancel()
        self._daily_task.cancel()
        # Persist activity + report buffers so a reload never loses up to 5 minutes
        # of activity (which could make a freshly reactivated member look inactive).
        try:
            await self._flush_all()
        except Exception:
            log.exception("Lurker: final flush on unload failed")

    # ---------------------------------------------------------------- cache

    async def _ensure_loaded(self, guild_id: int):
        if guild_id in self._loaded_guilds:
            return
        stored = await self.config.guild_from_id(guild_id).last_active()
        loaded = {int(uid): ts for uid, ts in stored.items()}
        # merge (not replace): a touch that landed before the load finished must survive
        self._cache[guild_id] = engine.merge_last_active(loaded, self._cache.get(guild_id, {}))
        self._loaded_guilds.add(guild_id)

    def _touch(self, guild_id: int, user_id: int):
        now = datetime.now(timezone.utc).timestamp()
        users = self._cache.setdefault(guild_id, {})
        # skip no-op refreshes: a fresh-enough timestamp needs no write at all
        if not engine.should_record(users.get(user_id), now):
            return
        users[user_id] = now
        self._dirty.add(guild_id)

    def _lock_for(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (guild_id, user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _flush_loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await asyncio.sleep(self.FLUSH_INTERVAL)
                await self._flush_all()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Lurker flush loop error")

    async def _flush_all(self):
        for guild_id in set(self._events) | set(self._counts):
            await self._flush_events(guild_id)
        # only flush guilds whose cache was fully loaded -- a partial cache written
        # over Config would wipe everyone's stored activity
        for guild_id in list(self._loaded_guilds):
            if guild_id not in self._dirty:
                continue
            self._dirty.discard(guild_id)
            users = self._cache.get(guild_id, {})
            str_map = {str(uid): ts for uid, ts in users.items()}
            try:
                await self.config.guild_from_id(guild_id).last_active.set(str_map)
            except Exception:
                self._dirty.add(guild_id)
                raise

    # --------------------------------------------------------- report events

    def _record_event(self, guild_id: int, kind: str, member: discord.Member, src: str):
        if src in engine.BULK_SOURCES:
            counts = self._counts.setdefault(guild_id, {})
            counts[src] = counts.get(src, 0) + 1
            return
        self._events.setdefault(guild_id, []).append({
            "uid": member.id,
            "name": member.display_name,
            "ts": datetime.now(timezone.utc).timestamp(),
            "kind": kind,
            "src": src,
        })

    async def _flush_events(self, guild_id: int):
        async with self._events_lock:
            buf = self._events.get(guild_id) or []
            counts = self._counts.get(guild_id) or {}
            if not buf and not counts:
                return
            # swap first so anything recorded during the awaits below is kept
            self._events[guild_id] = []
            self._counts[guild_id] = {}
            group = self.config.guild_from_id(guild_id)
            try:
                async with group.report_events() as stored:
                    stored.extend(buf)
                    kept, dropped = engine.trim_events(list(stored))
                    stored[:] = kept
                    if dropped:
                        counts = dict(counts)
                        counts["dropped"] = counts.get("dropped", 0) + dropped
                if counts:
                    async with group.report_counts() as stored_counts:
                        for key, value in counts.items():
                            stored_counts[key] = stored_counts.get(key, 0) + value
            except Exception:
                # put everything back so a failed write loses nothing
                self._events[guild_id] = buf + self._events.get(guild_id, [])
                restored = self._counts.setdefault(guild_id, {})
                for key, value in counts.items():
                    restored[key] = restored.get(key, 0) + value
                raise

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
            try:
                await self._maybe_send_reports()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Lurker report error")
            try:
                await self._run_yearclub_upkeep()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Lurker year club upkeep error")
            await asyncio.sleep(3600)  # check hourly; actual 24h / weekly gating is persisted

    async def _run_sweep(self):
        now = datetime.now(timezone.utc).timestamp()
        for guild in self.bot.guilds:
            # one guild's failure must never stop the others (or kill the loop)
            try:
                await self._sweep_guild(guild, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(f"Lurker sweep failed for {guild}")

    def _sweep_candidates(
        self,
        guild: discord.Guild,
        lurker_role: discord.Role,
        exempt_ids: Set[int],
        cutoff: float,
    ) -> List[discord.Member]:
        cache = self._cache.get(guild.id, {})
        out = []
        for member in guild.members:
            if member.bot or lurker_role in member.roles:
                continue
            if exempt_ids & {r.id for r in member.roles}:
                continue
            joined = member.joined_at.timestamp() if member.joined_at else None
            if engine.is_inactive(cache.get(member.id), joined, cutoff):
                out.append(member)
        return out

    async def _sweep_guild(self, guild: discord.Guild, now: float, force: bool = False) -> Optional[dict]:
        cfg = self.config.guild(guild)
        if not force and not await cfg.enabled():
            return None
        role_id = await cfg.lurker_role_id()
        lurker_role = guild.get_role(role_id) if role_id else None
        if lurker_role is None:
            return None
        if not force and now - await cfg.last_sweep_ts() < engine.DAY:
            return None  # not due yet -- survives reloads, only a real 24h gap triggers a run
        if guild.id in self._bulk_running:
            return None

        self._bulk_running.add(guild.id)
        try:
            await self._ensure_loaded(guild.id)
            exempt_ids = set(await cfg.exempt_role_ids())
            cutoff = engine.cutoff_ts(now, await cfg.threshold_days())
            sweep_max = await cfg.sweep_max()
            candidates = self._sweep_candidates(guild, lurker_role, exempt_ids, cutoff)
            stats = {
                "ts": now, "candidates": len(candidates), "flagged": 0,
                "skipped": 0, "errors": 0, "aborted": False,
            }

            if len(candidates) > sweep_max:
                # Circuit breaker: a sudden mass-flag almost always means lost/missing
                # activity data, not 1000 people going quiet overnight.
                stats["aborted"] = True
                log.warning(
                    f"Lurker sweep ABORTED in {guild}: {len(candidates)} candidates exceeds "
                    f"sweep_max={sweep_max}."
                )
                await cfg.last_sweep_ts.set(now)
                await cfg.last_sweep.set(stats)
                await self._send_alert(
                    guild,
                    f"⚠️ **Lurker sweep aborted.** It would have flagged "
                    f"**{len(candidates)}** members, above the safety limit of **{sweep_max}**. "
                    f"Nobody was flagged. Review with `.lurkersweeppreview`; if it looks right, "
                    f"raise the limit with `.lurkerset sweepmax <n>` and run `.lurkersweeprun`.",
                )
                return stats

            for member in candidates:
                try:
                    result = await self._flag_member(
                        member, lurker_role, exempt_ids, cutoff=cutoff, source="sweep"
                    )
                except discord.Forbidden:
                    stats["errors"] += 1
                    log.warning(f"Missing permissions to flag {member} in {guild}")
                    await asyncio.sleep(1)
                except Exception:
                    stats["errors"] += 1
                    log.exception(f"Failed to flag {member} in {guild}")
                    await asyncio.sleep(1)
                else:
                    if result == "flagged":
                        stats["flagged"] += 1
                        await asyncio.sleep(1)  # gentle pacing against rate limits
                    else:
                        stats["skipped"] += 1

            await cfg.last_sweep_ts.set(now)
            await cfg.last_sweep.set(stats)
            log.info(
                f"Lurker sweep done in {guild}: {stats['flagged']} flagged of "
                f"{stats['candidates']} candidates ({stats['skipped']} skipped, {stats['errors']} errors)."
            )
            return stats
        finally:
            self._bulk_running.discard(guild.id)

    # ------------------------------------------------------- flag/unflag

    async def _flag_member(
        self,
        member: discord.Member,
        lurker_role: discord.Role,
        exempt_ids: Set[int],
        *,
        cutoff: Optional[float] = None,
        source: str = "manual",
    ) -> str:
        """Flag one member. Returns "flagged", "already", "exempt" or "active".

        Everything is decided under a per-member lock, using *current* state:
        - already flagged -> skipped (never overwrite stored_roles with the post-flag set)
        - exempt role granted since a scan was taken -> skipped
        - if `cutoff` is given, the member's live activity is re-checked, so a stale
          scan/backfill list can never re-flag someone who has since reactivated
        """
        guild = member.guild
        async with self._lock_for(guild.id, member.id):
            if lurker_role in member.roles:
                return "already"
            if exempt_ids & {r.id for r in member.roles}:
                return "exempt"
            if cutoff is not None:
                await self._ensure_loaded(guild.id)
                joined = member.joined_at.timestamp() if member.joined_at else None
                cached = self._cache.get(guild.id, {}).get(member.id)
                if not engine.is_inactive(cached, joined, cutoff):
                    return "active"

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
                    f"(role above bot's top role) -- bot's role needs to be moved higher."
                )

            # Union with anything stored by an earlier half-finished attempt so a retry
            # can never shrink the record of the member's real roles. Transient roles
            # (year club, today's birthday role) are stripped but never stored: they
            # are recomputed on restore, so a stale one can never come back.
            transient = await self._transient_role_ids(guild)
            prior = await self.config.member(member).stored_roles()
            stored_ids = [
                rid for rid in dict.fromkeys(list(prior) + [r.id for r in removable])
                if rid not in transient
            ]
            await self.config.member(member).stored_roles.set(stored_ids)

            # Add Lurker FIRST: if this fails the member keeps every role and nothing
            # is lost. (Old order stripped roles first and could strand a member with
            # no roles at all.)
            await member.add_roles(lurker_role, reason=f"Lurker: inactive ({source})")
            await self.config.member(member).flagged.set(True)
            if removable:
                await member.remove_roles(*removable, reason=f"Lurker: inactive ({source})")

            self._record_event(guild.id, "flag", member, source)
            return "flagged"

    async def _unflag_member(
        self, member: discord.Member, lurker_role: discord.Role, *, source: str = "mod"
    ) -> int:
        """Restore a member's roles and remove Lurker. Returns the number of roles restored."""
        guild = member.guild
        await self._ensure_loaded(guild.id)
        async with self._lock_for(guild.id, member.id):
            was_flagged = await self.config.member(member).flagged()
            stored_ids = await self.config.member(member).stored_roles()
            transient = await self._transient_role_ids(guild)
            stored_ids = [rid for rid in stored_ids if rid not in transient]
            club_role_id = await self._club_role_for(member)
            if club_role_id is not None:
                stored_ids.append(club_role_id)  # their *current* year, not what they had

            top = guild.me.top_role
            to_add, blocked = [], []
            for rid in stored_ids:
                role = guild.get_role(rid)
                if role is None:
                    continue  # role was deleted since
                if role in member.roles:
                    continue
                if role.managed or role >= top:
                    blocked.append(role.name)
                    continue
                to_add.append(role)
            if blocked:
                log.warning(
                    f"Could not restore {blocked} to {member} in {guild} "
                    f"(managed or above the bot's top role)."
                )

            # Restore roles FIRST, then drop Lurker: if restoring fails the member is
            # still safely flagged with stored_roles intact, so a retry just works.
            if to_add:
                await member.add_roles(*to_add, reason=f"Lurker: restoring prior roles ({source})")
            if lurker_role in member.roles:
                await member.remove_roles(lurker_role, reason=f"Lurker: reactivated ({source})")

            # One write (and no leftover default-valued entry) instead of two.
            # Runs only after roles were restored and Lurker removed, same order as before.
            await self.config.member(member).clear()
            self._touch(guild.id, member.id)
            if was_flagged:
                self._record_event(guild.id, "unflag", member, source)
            return len(to_add)

    # ---------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
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
                    await self._unflag_member(message.author, lurker_role, source="post")
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
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # raw event: the non-raw one only fires for messages still in the bot's cache
        if payload.guild_id is None:
            return
        if payload.member is not None and payload.member.bot:
            return
        await self._ensure_loaded(payload.guild_id)
        self._touch(payload.guild_id, payload.user_id)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # slash commands and button presses (casino/blackjack etc.) never fire on_message
        if interaction.guild_id is None or interaction.user is None or interaction.user.bot:
            return
        await self._ensure_loaded(interaction.guild_id)
        self._touch(interaction.guild_id, interaction.user.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if member.bot:
            return
        if after.channel is not None and before.channel != after.channel:
            await self._ensure_loaded(member.guild.id)
            self._touch(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        await self._ensure_loaded(member.guild.id)
        self._touch(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        # A stale flagged/stored_roles record must not follow someone who leaves and rejoins.
        # Only touch Config when a record exists: every clear() rewrites the whole file.
        try:
            cfg = self.config.member(member)
            if await cfg.flagged() or await cfg.stored_roles():
                await cfg.clear()
        except Exception:
            log.exception(f"Failed to clear lurker state for departed {member.id}")
        if member.guild.id in self._loaded_guilds:
            self._cache.get(member.guild.id, {}).pop(member.id, None)
            self._dirty.add(member.guild.id)

    # ----------------------------------------------------------- year club

    async def _transient_role_ids(self, guild: discord.Guild) -> Set[int]:
        """Roles stripped on flag but never stored/restored (recomputed instead)."""
        ids: Set[int] = set()
        cfg = self.config.guild(guild)
        if await cfg.yearclub_enabled():
            ids |= set(engine.club_mapping(await cfg.yearclub_roles()).values())
        celebrations = self.bot.get_cog("Celebrations") if hasattr(self.bot, "get_cog") else None
        getter = getattr(celebrations, "transient_role_ids", None)
        if getter is not None:
            try:
                ids |= set(await getter(guild))
            except Exception:
                log.exception("Lurker: could not read Celebrations' transient roles")
        return ids

    async def _club_role_for(self, member: discord.Member) -> Optional[int]:
        """The club role id this member should hold right now (None if off / under a year)."""
        cfg = self.config.guild(member.guild)
        if not await cfg.yearclub_enabled() or member.joined_at is None:
            return None
        mapping = engine.club_mapping(await cfg.yearclub_roles())
        years = engine.years_completed(member.joined_at.timestamp(), datetime.now(timezone.utc).timestamp())
        return engine.club_target(years, mapping)

    def _yearclub_plans(self, guild: discord.Guild, mapping: Dict[int, int], lurker_role_id: Optional[int], now: float):
        """Every member who needs a change: [(member, add_role_or_None, [roles_to_remove])], plus stats."""
        club_ids = set(mapping.values())
        top = guild.me.top_role
        plans, stats = [], {"skipped_lurkers": 0, "blocked": 0}
        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue
            current = {r.id for r in member.roles}
            if lurker_role_id and lurker_role_id in current:
                stats["skipped_lurkers"] += 1
                continue
            years = engine.years_completed(member.joined_at.timestamp(), now)
            add_id, remove_ids = engine.club_plan(current, engine.club_target(years, mapping), club_ids)
            if add_id is None and not remove_ids:
                continue
            add = guild.get_role(add_id) if add_id else None
            remove = [r for r in member.roles if r.id in remove_ids]
            if (add_id and add is None) or any(r >= top or r.managed for r in ([add] if add else []) + remove):
                stats["blocked"] += 1
                continue
            plans.append((member, add, remove))
        return plans, stats

    async def _apply_club_plan(self, member: discord.Member, mapping: Dict[int, int], lurker_role_id: Optional[int]) -> bool:
        """Re-check under the member lock (a flag may have landed meanwhile), then add-then-remove."""
        guild = member.guild
        async with self._lock_for(guild.id, member.id):
            current = {r.id for r in member.roles}
            if lurker_role_id and lurker_role_id in current:
                return False
            years = engine.years_completed(member.joined_at.timestamp(), datetime.now(timezone.utc).timestamp())
            add_id, remove_ids = engine.club_plan(current, engine.club_target(years, mapping), set(mapping.values()))
            add = guild.get_role(add_id) if add_id else None
            remove = [r for r in member.roles if r.id in remove_ids]
            if add is not None:
                await member.add_roles(add, reason=f"Year club: {years} year(s) in the server")
            if remove:
                await member.remove_roles(*remove, reason="Year club: replaced by current year")
            return add is not None or bool(remove)

    async def _yearclub_pass(self, guild: discord.Guild, *, auto: bool, progress=None) -> Optional[dict]:
        cfg = self.config.guild(guild)
        mapping = engine.club_mapping(await cfg.yearclub_roles())
        if not mapping or guild.id in self._yearclub_running:
            return None
        self._yearclub_running.add(guild.id)
        try:
            lurker_role_id = await cfg.lurker_role_id()
            now = datetime.now(timezone.utc).timestamp()
            plans, stats = self._yearclub_plans(guild, mapping, lurker_role_id, now)
            stats.update({"ts": now, "planned": len(plans), "changed": 0, "errors": 0, "aborted": False})
            if auto and len(plans) > engine.YEARCLUB_AUTO_MAX:
                stats["aborted"] = True
                await cfg.yearclub_last.set(stats)
                await self._send_alert(
                    guild,
                    f"⚠️ **Year club upkeep paused.** {len(plans)} members need a role change, more than "
                    f"an hourly anniversary pass should ever see ({engine.YEARCLUB_AUTO_MAX}). Nothing was "
                    "changed. Check `.yearclub`, then run `.yearclub sync` to review and apply.",
                )
                return stats
            for i, (member, _add, _remove) in enumerate(plans, 1):
                try:
                    if await self._apply_club_plan(member, mapping, lurker_role_id):
                        stats["changed"] += 1
                except Exception:
                    stats["errors"] += 1
                    log.exception(f"Year club: failed to update {member} in {guild}")
                await asyncio.sleep(0.5)  # gentle pacing against role-edit rate limits
                if progress is not None and i % 25 == 0:
                    await progress(i, len(plans))
            if not auto or plans:  # an hourly no-op pass must not rewrite the settings file
                await cfg.yearclub_last.set(stats)
            return stats
        finally:
            self._yearclub_running.discard(guild.id)

    async def _run_yearclub_upkeep(self):
        for guild in self.bot.guilds:
            try:
                if await self.config.guild(guild).yearclub_enabled():
                    await self._yearclub_pass(guild, auto=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(f"Year club upkeep failed for {guild}")

    @checks.admin_or_permissions(manage_roles=True)
    @commands.group(name="yearclub", invoke_without_command=True)
    async def yearclub(self, ctx):
        """Year club roles: one "N year club!" role per member, swapped on each join anniversary."""
        cfg = self.config.guild(ctx.guild)
        mapping = engine.club_mapping(await cfg.yearclub_roles())
        lines = [f"Automatic anniversary upkeep: **{'ON' if await cfg.yearclub_enabled() else 'off'}** (checks hourly)"]
        if not mapping:
            lines.append("No club roles mapped yet. Run `.yearclub autodetect` (finds roles named like `5 year club!`).")
        for years in sorted(mapping):
            role = ctx.guild.get_role(mapping[years])
            lines.append(f"`{years:>2}` year → {role.mention + f' ({len(role.members)} members)' if role else '⚠️ role deleted'}")
        last = await cfg.yearclub_last()
        if last:
            note = " — PAUSED by safety limit" if last.get("aborted") else ""
            lines.append(
                f"Last pass: <t:{int(last['ts'])}:R>: {last.get('changed', 0)} updated, "
                f"{last.get('skipped_lurkers', 0)} lurkers skipped, {last.get('blocked', 0)} blocked, "
                f"{last.get('errors', 0)} errors{note}"
            )
        lines.append("Commands: `autodetect`, `setrole <years> @role`, `clearrole <years>`, `check @member`, `sync`, `enable`, `disable`.")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @yearclub.command(name="autodetect")
    async def yearclub_autodetect(self, ctx):
        """Map every role named like `N year club!` to N years."""
        found = {}
        for role in ctx.guild.roles:
            n = engine.parse_club_role_name(role.name)
            if n is not None and n not in found:
                found[n] = role.id
        if not found:
            await ctx.send("No roles named like `5 year club!` found. Use `.yearclub setrole <years> @role`.")
            return
        await self.config.guild(ctx.guild).yearclub_roles.set({str(k): v for k, v in found.items()})
        listed = ", ".join(f"{n}→<@&{found[n]}>" for n in sorted(found))
        await ctx.send(f"Mapped {len(found)} club roles: {listed}\nNext: `.yearclub sync` to preview the one-time fix.",
                       allowed_mentions=discord.AllowedMentions.none())

    @yearclub.command(name="setrole")
    async def yearclub_setrole(self, ctx, years: int, role: discord.Role):
        """Map one club role by hand. `.yearclub setrole 13 @13 year club!`"""
        if not 1 <= years <= 50:
            await ctx.send("Years must be between 1 and 50.")
            return
        async with self.config.guild(ctx.guild).yearclub_roles() as roles:
            roles[str(years)] = role.id
        await ctx.send(f"{years} year → {role.mention}", allowed_mentions=discord.AllowedMentions.none())

    @yearclub.command(name="clearrole")
    async def yearclub_clearrole(self, ctx, years: int):
        """Unmap one club role (the role itself is not deleted)."""
        async with self.config.guild(ctx.guild).yearclub_roles() as roles:
            roles.pop(str(years), None)
        await ctx.send(f"Removed the {years}-year mapping.")

    @yearclub.command(name="check")
    async def yearclub_check(self, ctx, member: discord.Member):
        """Show a member's years in the server and what club role they should hold."""
        cfg = self.config.guild(ctx.guild)
        mapping = engine.club_mapping(await cfg.yearclub_roles())
        years = engine.years_completed(member.joined_at.timestamp(), datetime.now(timezone.utc).timestamp())
        target = engine.club_target(years, mapping)
        held = [r.mention for r in member.roles if r.id in set(mapping.values())]
        lurker_id = await cfg.lurker_role_id()
        note = " (lurker: skipped until they come back; they get this role on restore)" if lurker_id and member.get_role(lurker_id) else ""
        await ctx.send(
            f"{member.display_name} joined <t:{int(member.joined_at.timestamp())}:D> → **{years}** full year(s).\n"
            f"Should hold: {f'<@&{target}>' if target else 'no club role'}{note}\n"
            f"Holds now: {', '.join(held) if held else 'none'}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @yearclub.command(name="sync")
    async def yearclub_sync(self, ctx, confirm: Optional[str] = None):
        """Preview the one-time fix for every member; `.yearclub sync confirm` applies it.

        Each active member ends with exactly one club role (their current year).
        Lurkers are skipped: they get the right one back when they reactivate.
        Safe to re-run any time; it only touches members who are wrong.
        """
        cfg = self.config.guild(ctx.guild)
        mapping = engine.club_mapping(await cfg.yearclub_roles())
        if not mapping:
            await ctx.send("No club roles mapped. Run `.yearclub autodetect` first.")
            return
        if ctx.guild.id in self._yearclub_running:
            await ctx.send("A year club pass is already running.")
            return
        plans, stats = self._yearclub_plans(ctx.guild, mapping, await cfg.lurker_role_id(), datetime.now(timezone.utc).timestamp())
        adds: Dict[int, int] = {}
        removes = 0
        for _m, add, remove in plans:
            if add is not None:
                adds[add.id] = adds.get(add.id, 0) + 1
            removes += len(remove)
        summary = (
            f"**{len(plans)}** members need a change: {sum(adds.values())} role adds, {removes} role removals.\n"
            + "".join(f"• +<@&{rid}> ×{n}\n" for rid, n in sorted(adds.items(), key=lambda kv: -kv[1]))
            + f"Skipped: {stats['skipped_lurkers']} lurkers (fixed on reactivation), {stats['blocked']} blocked "
            "(role above the bot / managed / deleted)."
        )
        if (confirm or "").lower() != "confirm":
            await ctx.send(summary + f"\nApply with `.yearclub sync confirm` (~{max(1, len(plans) // 120)} min).",
                           allowed_mentions=discord.AllowedMentions.none())
            return
        if not plans:
            await ctx.send("Everyone already holds the right club role.")
            return
        status = await ctx.send(f"Applying… 0/{len(plans)}")

        async def progress(done, total):
            try:
                await status.edit(content=f"Applying… {done}/{total}")
            except discord.HTTPException:
                pass

        result = await self._yearclub_pass(ctx.guild, auto=False, progress=progress)
        if result is None:
            await ctx.send("Could not start (another pass is running, or no roles mapped).")
            return
        await status.edit(content=(
            f"✅ Year club sync done: {result['changed']} members updated, {result['errors']} errors, "
            f"{result['skipped_lurkers']} lurkers skipped, {result['blocked']} blocked."
        ))

    @yearclub.command(name="enable")
    async def yearclub_enable(self, ctx):
        """Turn on hourly anniversary upkeep (and recomputed club roles on un-lurk)."""
        await self.config.guild(ctx.guild).yearclub_enabled.set(True)
        await ctx.send("Year club upkeep ON. Anniversaries are applied within the hour (Pacific dates).")

    @yearclub.command(name="disable")
    async def yearclub_disable(self, ctx):
        """Turn off hourly upkeep (roles are left as they are)."""
        await self.config.guild(ctx.guild).yearclub_enabled.set(False)
        await ctx.send("Year club upkeep off.")

    # -------------------------------------------------------- config cmds

    @checks.admin_or_permissions(manage_roles=True)
    @commands.group(invoke_without_command=True)
    async def lurkerset(self, ctx):
        """Configure the Lurker inactivity system."""
        await ctx.send_help()

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
        if days < 1:
            await ctx.send("Threshold must be at least 1 day.")
            return
        await self.config.guild(ctx.guild).threshold_days.set(days)
        await ctx.send(f"Inactivity threshold set to {days} days.")

    @lurkerset.command(name="enable")
    async def lurkerset_enable(self, ctx):
        """Turn ON the automatic daily inactivity sweep for this server."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await self.config.guild(ctx.guild).last_sweep_ts.set(datetime.now(timezone.utc).timestamp())
        await ctx.send(
            "Automatic daily sweep enabled. First run will happen in ~24 hours, "
            "not immediately, and every 24h after that regardless of reloads."
        )

    @lurkerset.command(name="disable")
    async def lurkerset_disable(self, ctx):
        """Turn OFF the automatic daily inactivity sweep for this server."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("Automatic daily sweep disabled. Manual commands (.lurker, .lurkerbackfill) still work.")

    @lurkerset.command(name="sweepmax")
    async def lurkerset_sweepmax(self, ctx, limit: int):
        """Safety limit: a sweep that would flag more than this many members aborts and alerts mods."""
        if not 1 <= limit <= 5000:
            await ctx.send("Pick a limit between 1 and 5000.")
            return
        await self.config.guild(ctx.guild).sweep_max.set(limit)
        await ctx.send(f"Sweep safety limit set to {limit} members per sweep.")

    @lurkerset.command(name="reportchannel")
    async def lurkerset_reportchannel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Set the channel for the weekly mod report. Run with no channel to turn reports off."""
        cfg = self.config.guild(ctx.guild)
        if channel is None:
            await cfg.report_channel_id.set(None)
            await ctx.send("Weekly report disabled.")
            return
        await cfg.report_channel_id.set(channel.id)
        if not await cfg.last_report_ts():
            await cfg.last_report_ts.set(datetime.now(timezone.utc).timestamp())
        days = await cfg.report_interval_days()
        await ctx.send(
            f"Mod report channel set to {channel.mention}. One digest every {days} days "
            f"(first one {days} days from now). Preview any time with `.lurkerreport`."
        )

    @lurkerset.command(name="reportdays")
    async def lurkerset_reportdays(self, ctx, days: int):
        """Set how many days between mod reports (default 7)."""
        if not 1 <= days <= 30:
            await ctx.send("Pick between 1 and 30 days.")
            return
        await self.config.guild(ctx.guild).report_interval_days.set(days)
        await ctx.send(f"Mod report will post every {days} days.")

    @lurkerset.command(name="settings")
    async def lurkerset_settings(self, ctx):
        """Show current configuration and sweep status."""
        cfg = self.config.guild(ctx.guild)
        role_id = await cfg.lurker_role_id()
        chan_id = await cfg.lurker_channel_id()
        report_id = await cfg.report_channel_id()
        role = ctx.guild.get_role(role_id) if role_id else None
        channel = ctx.guild.get_channel(chan_id) if chan_id else None
        report = ctx.guild.get_channel(report_id) if report_id else None
        exempt = [ctx.guild.get_role(rid) for rid in await cfg.exempt_role_ids()]
        exempt = [r.name for r in exempt if r]
        enabled = await cfg.enabled()
        last_sweep_ts = await cfg.last_sweep_ts()
        last_sweep = await cfg.last_sweep()
        await self._ensure_loaded(ctx.guild.id)

        lines = [
            f"Role: {role.mention if role else 'not set'}",
            f"Reactivation channel: {channel.mention if channel else 'not set'}",
            f"Threshold: {await cfg.threshold_days()} days",
            f"Automatic sweep: {'ENABLED' if enabled else 'disabled'}",
        ]
        if enabled and last_sweep_ts:
            lines.append(f"Next sweep due: <t:{int(last_sweep_ts + engine.DAY)}:R>")
        if last_sweep:
            note = " (ABORTED by safety limit)" if last_sweep.get("aborted") else ""
            lines.append(
                f"Last sweep: <t:{int(last_sweep['ts'])}:R> — {last_sweep.get('flagged', 0)} flagged "
                f"of {last_sweep.get('candidates', 0)} candidates{note}"
            )
        else:
            lines.append("Last sweep: never")
        lines += [
            f"Sweep safety limit: {await cfg.sweep_max()} flags per sweep",
            f"Exempt roles: {humanize_list(exempt) if exempt else 'none'}",
            f"Weekly report: {report.mention if report else 'off'} "
            f"(every {await cfg.report_interval_days()} days)",
            f"Members with tracked activity: {len(self._cache.get(ctx.guild.id, {}))}",
        ]
        if not exempt:
            lines.append("⚠️ No exempt roles set — mods/boosters can be flagged. Use `.lurkerset exempt`.")
        await ctx.send("\n".join(lines))

    @lurkerset.command(name="postinfo")
    async def lurkerset_postinfo(self, ctx):
        """Post and pin the explainer embed in the configured lurker channel."""
        channel_id = await self.config.guild(ctx.guild).lurker_channel_id()
        if not channel_id:
            await ctx.send("Set the lurker channel first with `.lurkerset channel`.")
            return
        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            await ctx.send("Configured lurker channel no longer exists.")
            return

        threshold_days = await self.config.guild(ctx.guild).threshold_days()
        embed = discord.Embed(
            title="You've been moved here for inactivity",
            description=(
                f"You haven't posted or reacted anywhere in the server for {threshold_days}+ days, "
                "so you've been moved here to keep things tidy for active members. This channel is "
                "the only thing you can see right now."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Are my roles gone?",
            value="No. Every role you had is safely stored — nothing was deleted.",
            inline=False,
        )
        embed.add_field(
            name="How do I get everything back?",
            value=(
                "Just type anything in this channel. Your roles are restored instantly and "
                "automatically — no need to ping anyone."
            ),
            inline=False,
        )
        embed.set_footer(text="This process is fully automatic.")

        try:
            msg = await channel.send(embed=embed)
            await msg.pin()
        except discord.Forbidden:
            await ctx.send("Missing permissions to send or pin a message in that channel.")
            return

        await ctx.send(f"Posted and pinned in {channel.mention}.")

    # ------------------------------------------------------- manual / debug

    @checks.mod_or_permissions(manage_roles=True)
    @commands.command(name="lurkerversion")
    async def lurker_version(self, ctx):
        """Show the running Lurker cog version (deploy probe)."""
        await ctx.send(f"Lurker cog v{VERSION}")

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

        exempt_ids = set(await self.config.guild(ctx.guild).exempt_role_ids())
        result = await self._flag_member(member, lurker_role, exempt_ids, source="manual")
        if result == "already":
            await ctx.send(f"{member} is already flagged.")
        elif result == "exempt":
            await ctx.send(f"{member} has an exempt role and cannot be flagged.")
        else:
            await ctx.send(f"{member} flagged as a Lurker. Have them post in the reactivation channel to test recovery.")

    @checks.mod_or_permissions(manage_roles=True)
    @commands.command(name="lurkerstatus")
    async def lurker_status(self, ctx, member: discord.Member):
        """Show a member's Lurker state for debugging."""
        cfg = self.config.guild(ctx.guild)
        role_id = await cfg.lurker_role_id()
        lurker_role = ctx.guild.get_role(role_id) if role_id else None
        flagged = await self.config.member(member).flagged()
        stored_ids = await self.config.member(member).stored_roles()
        await self._ensure_loaded(ctx.guild.id)

        resolved, missing = [], []
        for rid in stored_ids:
            role = ctx.guild.get_role(rid)
            (resolved if role else missing).append(role.name if role else str(rid))

        has_lurker_role = bool(lurker_role and lurker_role in member.roles)
        bot_top = ctx.guild.me.top_role
        above_bot = [r.name for r in member.roles if r.position >= bot_top.position and r != ctx.guild.default_role]

        tracked = self._cache.get(ctx.guild.id, {}).get(member.id)
        joined = member.joined_at.timestamp() if member.joined_at else None
        cutoff = engine.cutoff_ts(datetime.now(timezone.utc).timestamp(), await cfg.threshold_days())
        exempt_ids = set(await cfg.exempt_role_ids())
        is_exempt = bool(exempt_ids & {r.id for r in member.roles})
        would_flag = (
            not has_lurker_role and not is_exempt and not member.bot
            and engine.is_inactive(tracked, joined, cutoff)
        )

        if tracked:
            activity = f"<t:{int(tracked)}:R>"
        elif joined:
            activity = f"none recorded (judged by join date <t:{int(joined)}:d>)"
        else:
            activity = "none recorded"

        msg = (
            f"**{member}** ({member.id})\n"
            f"Flagged in config: {flagged}\n"
            f"Has Lurker role right now: {has_lurker_role}\n"
            f"Last tracked activity: {activity}\n"
            f"Exempt: {is_exempt} | Would be flagged by next sweep: {would_flag}\n"
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
            await self._unflag_member(member, lurker_role, source="mod")
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

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerexemptaudit")
    async def lurker_exempt_audit(self, ctx):
        """List flagged members who hold (or held) an exempt role and shouldn't be flagged."""
        cfg = self.config.guild(ctx.guild)
        role_id = await cfg.lurker_role_id()
        lurker_role = ctx.guild.get_role(role_id) if role_id else None
        if not lurker_role:
            await ctx.send("Lurker role not configured.")
            return
        exempt_ids = set(await cfg.exempt_role_ids())
        if not exempt_ids:
            await ctx.send("No exempt roles are configured. Set them with `.lurkerset exempt`.")
            return

        all_cfg = await self.config.all_members(ctx.guild)
        hits = []
        for m in ctx.guild.members:
            if lurker_role not in m.roles:
                continue
            held = {r.id for r in m.roles} | set(all_cfg.get(m.id, {}).get("stored_roles", []))
            if held & exempt_ids:
                hits.append(m)

        if not hits:
            await ctx.send("No flagged members hold an exempt role.")
            return
        shown = ", ".join(f"{m} (`{m.id}`)" for m in hits[:25])
        extra = f" …and {len(hits) - 25} more" if len(hits) > 25 else ""
        await ctx.send(
            f"**{len(hits)}** flagged member(s) hold an exempt role. Restore each with "
            f"`.lurkerunflag @user`:\n{shown}{extra}"
        )

    # ------------------------------------------------------------ undo-all

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerundoall")
    async def lurker_undo_all(self, ctx):
        """Show how many members currently have the Lurker role."""
        role_id = await self.config.guild(ctx.guild).lurker_role_id()
        if not role_id:
            await ctx.send("Lurker role not configured.")
            return
        lurker_role = ctx.guild.get_role(role_id)
        if not lurker_role:
            await ctx.send("Configured Lurker role no longer exists.")
            return

        flagged_members = [m for m in ctx.guild.members if lurker_role in m.roles]
        await self.config.guild(ctx.guild).undo_scan.set({
            "remaining_ids": [m.id for m in flagged_members],
            "ts": datetime.now(timezone.utc).timestamp(),
            "total": len(flagged_members),
        })
        await ctx.send(
            f"{len(flagged_members)} members currently have the Lurker role.\n"
            f"Run `.lurkerundoallconfirm` within 24h to restore all of their prior roles and "
            f"remove Lurker from everyone. This is safe to resume if interrupted."
        )

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerundoallconfirm")
    async def lurker_undo_all_confirm(self, ctx):
        """Execute (or resume) the most recent .lurkerundoall check."""
        scan = await self.config.guild(ctx.guild).undo_scan()
        if not scan:
            await ctx.send("No recent check found. Run `.lurkerundoall` first.")
            return
        if (datetime.now(timezone.utc).timestamp() - scan.get("ts", 0)) >= self.UNDO_TTL:
            await self.config.guild(ctx.guild).undo_scan.set({})
            await ctx.send("Check expired (older than 24h). Run `.lurkerundoall` again.")
            return

        role_id = await self.config.guild(ctx.guild).lurker_role_id()
        lurker_role = ctx.guild.get_role(role_id) if role_id else None
        if not lurker_role:
            await ctx.send("Configured Lurker role no longer exists.")
            return
        if ctx.guild.id in self._bulk_running:
            await ctx.send("A bulk Lurker operation (backfill, undo or sweep) is already running here.")
            return

        pending = list(scan["remaining_ids"])
        total = scan.get("total", len(pending))
        already_done = total - len(pending)

        if already_done:
            await ctx.send(
                f"Resuming: {already_done}/{total} already restored in a previous run, "
                f"{len(pending)} left. I'll report back when done."
            )
        else:
            await ctx.send(f"Restoring {len(pending)} members. I'll report back when done.")

        self._bulk_running.add(ctx.guild.id)
        restored = 0
        try:
            for i, member_id in enumerate(pending):
                member = ctx.guild.get_member(member_id)
                worked = False  # True whenever we hit the API (success or error) -> pace
                if member is not None and lurker_role in member.roles:
                    worked = True
                    try:
                        await self._unflag_member(member, lurker_role, source="undo")
                        restored += 1
                    except discord.Forbidden:
                        log.warning(f"Missing permissions to restore {member}")
                    except Exception:
                        log.exception(f"Failed to restore {member} during undo-all")

                if (i + 1) % self.CHECKPOINT_EVERY == 0:
                    await self.config.guild(ctx.guild).undo_scan.set({
                        "remaining_ids": pending[i + 1:],
                        "ts": datetime.now(timezone.utc).timestamp(),  # refresh TTL on progress
                        "total": total,
                    })
                if worked:
                    await asyncio.sleep(1.2)  # rate limit pacing
        finally:
            self._bulk_running.discard(ctx.guild.id)

        await self._flush_all()
        await self.config.guild(ctx.guild).undo_scan.set({})
        await ctx.send(f"Done. Restored {restored} members this run ({total} total).")

    # ------------------------------------------------------------ backfill

    async def _scan_targets(self, guild: discord.Guild, cutoff_dt: datetime) -> list:
        """Every messageable place a member could have been active: text channels,
        voice/stage text chat, active threads, archived threads and forum posts.
        (`guild.text_channels` alone misses threads, forums and voice-text chat.)"""
        targets: dict = {}

        def add(ch):
            targets.setdefault(ch.id, ch)

        for ch in guild.text_channels:
            add(ch)
        for ch in guild.voice_channels:
            add(ch)
        for ch in getattr(guild, "stage_channels", []):
            add(ch)
        for th in guild.threads:
            add(th)

        parents = list(guild.text_channels) + list(getattr(guild, "forums", []))
        for parent in parents:
            try:
                if not parent.permissions_for(guild.me).read_message_history:
                    continue
            except Exception:
                continue
            is_forum = not isinstance(parent, discord.TextChannel)
            for private in ((False,) if is_forum else (False, True)):
                try:
                    kwargs = {"limit": None}
                    if not is_forum:
                        kwargs["private"] = private
                    async for th in parent.archived_threads(**kwargs):
                        archived_at = getattr(th, "archive_timestamp", None)
                        if archived_at is not None and archived_at < cutoff_dt:
                            break  # newest-archived first: everything after is older
                        add(th)
                except (discord.Forbidden, discord.HTTPException):
                    continue  # private archived threads need Manage Threads
                except Exception:
                    log.debug(f"archived_threads failed for {parent}", exc_info=True)
        return list(targets.values())

    async def _scan_history(self, guild: discord.Guild, cutoff_dt: datetime) -> Tuple[Dict[int, float], dict]:
        """Newest message timestamp per author since cutoff_dt, across every scan target."""
        latest: Dict[int, float] = {}
        scanned = skipped = 0
        for target in await self._scan_targets(guild, cutoff_dt):
            if not hasattr(target, "history"):
                continue
            try:
                perms = target.permissions_for(guild.me)
                if not (perms.view_channel and perms.read_message_history):
                    skipped += 1
                    continue
                async for message in target.history(limit=None, after=cutoff_dt):
                    if message.author.bot:
                        continue
                    ts = message.created_at.timestamp()
                    if ts > latest.get(message.author.id, 0.0):
                        latest[message.author.id] = ts
                scanned += 1
            except discord.Forbidden:
                skipped += 1
            except Exception:
                skipped += 1
                log.exception(f"Error scanning {target}")
        return latest, {"scanned": scanned, "skipped": skipped}

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerbackfill")
    async def lurker_backfill(self, ctx):
        """Dry run: scan recent message history and report who would be flagged."""
        cfg = self.config.guild(ctx.guild)
        role_id = await cfg.lurker_role_id()
        if not role_id:
            await ctx.send("Lurker role not configured. Use `.lurkerset role` first.")
            return
        lurker_role = ctx.guild.get_role(role_id)
        if not lurker_role:
            await ctx.send("Configured Lurker role no longer exists.")
            return
        if ctx.guild.id in self._bulk_running:
            await ctx.send("A bulk Lurker operation (backfill, undo or sweep) is already running here.")
            return

        threshold_days = await cfg.threshold_days()
        await ctx.send(
            f"Scanning the last {threshold_days} days across text channels, voice-text, threads and "
            f"forums — this may take a while..."
        )
        now = datetime.now(timezone.utc)
        cutoff_dt = now - timedelta(days=threshold_days)

        self._bulk_running.add(ctx.guild.id)
        try:
            latest, scan_stats = await self._scan_history(ctx.guild, cutoff_dt)
            await self._ensure_loaded(ctx.guild.id)
            # Seed the activity cache with what the scan found: the sweep and the
            # confirm step both read this, so scan results are never thrown away.
            self._cache[ctx.guild.id] = engine.merge_last_active(self._cache.get(ctx.guild.id, {}), latest)
            self._dirty.add(ctx.guild.id)
            await self._flush_all()

            exempt_ids = set(await cfg.exempt_role_ids())
            cutoff = engine.cutoff_ts(now.timestamp(), threshold_days)
            to_flag = self._sweep_candidates(ctx.guild, lurker_role, exempt_ids, cutoff)
            await cfg.backfill_scan.set({
                "remaining_ids": [m.id for m in to_flag],
                "exempt_ids": list(exempt_ids),
                "ts": now.timestamp(),
                "total": len(to_flag),
            })
        finally:
            self._bulk_running.discard(ctx.guild.id)

        warn = ""
        if scan_stats["skipped"]:
            warn = (
                f"\n⚠️ {scan_stats['skipped']} channel(s)/thread(s) couldn't be read "
                f"(bot permissions). Members active only there will look inactive."
            )
        if not exempt_ids:
            warn += "\n⚠️ No exempt roles are set — mods/boosters can be flagged. Use `.lurkerset exempt` first."
        await ctx.send(
            f"Dry run complete. Scanned {scan_stats['scanned']} channels/threads.\n"
            f"Active in last {threshold_days} days: {len(latest)}\n"
            f"Would be flagged as Lurkers: {len(to_flag)}{warn}\n"
            f"Run `.lurkerbackfillconfirm` within 24h to execute using this scan. "
            f"If interrupted (restart/reload), just run it again to resume — it's idempotent, "
            f"and anyone who becomes active before their turn is skipped."
        )

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerbackfillstatus")
    async def lurker_backfill_status(self, ctx):
        """Show remaining progress on the current backfill scan, if any."""
        scan = await self.config.guild(ctx.guild).backfill_scan()
        if not scan:
            await ctx.send("No backfill scan in progress.")
            return
        remaining = len(scan.get("remaining_ids", []))
        total = scan.get("total", remaining)
        age_min = (datetime.now(timezone.utc).timestamp() - scan.get("ts", 0)) / 60
        running = "running now" if ctx.guild.id in self._bulk_running else "not running"
        await ctx.send(
            f"Backfill scan: {total - remaining}/{total} done, {remaining} remaining ({running}). "
            f"Last checkpoint {age_min:.1f} min ago (expires after 24h of no progress)."
        )

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerbackfillconfirm")
    async def lurker_backfill_confirm(self, ctx):
        """Execute (or resume) the most recent .lurkerbackfill dry run."""
        cfg = self.config.guild(ctx.guild)
        scan = await cfg.backfill_scan()
        if not scan:
            await ctx.send("No recent scan found. Run `.lurkerbackfill` first.")
            return
        now = datetime.now(timezone.utc).timestamp()
        if (now - scan.get("ts", 0)) >= self.BACKFILL_TTL:
            await cfg.backfill_scan.set({})
            await ctx.send("Scan expired (older than 24h with no progress). Run `.lurkerbackfill` again.")
            return

        role_id = await cfg.lurker_role_id()
        lurker_role = ctx.guild.get_role(role_id) if role_id else None
        if not lurker_role:
            await ctx.send("Configured Lurker role no longer exists.")
            return
        if ctx.guild.id in self._bulk_running:
            # two overlapping loops re-flagging each other's restored members was a
            # real failure mode -- refuse to start a second one
            await ctx.send("A bulk Lurker operation (backfill, undo or sweep) is already running here.")
            return

        pending = list(scan["remaining_ids"])
        total = scan.get("total", len(pending))
        already_done = total - len(pending)
        # current config, not the stale scan copy: exempt roles may have been set since
        exempt_ids = set(await cfg.exempt_role_ids())
        cutoff = engine.cutoff_ts(now, await cfg.threshold_days())

        if already_done:
            await ctx.send(
                f"Resuming backfill: {already_done}/{total} already handled in a previous run, "
                f"{len(pending)} left. I'll report back when done."
            )
        else:
            await ctx.send(f"Flagging up to {len(pending)} members. I'll report back when done.")

        self._bulk_running.add(ctx.guild.id)
        tally = {"flagged": 0, "active": 0, "exempt": 0, "already": 0, "errors": 0}
        try:
            await self._ensure_loaded(ctx.guild.id)
            for i, member_id in enumerate(pending):
                member = ctx.guild.get_member(member_id)
                paced = False
                if member is not None:
                    try:
                        result = await self._flag_member(
                            member, lurker_role, exempt_ids, cutoff=cutoff, source="backfill"
                        )
                        tally[result] += 1
                        paced = result == "flagged"
                    except discord.Forbidden:
                        tally["errors"] += 1
                        paced = True
                        log.warning(f"Missing permissions to flag {member}")
                    except Exception:
                        tally["errors"] += 1
                        paced = True
                        log.exception(f"Failed to flag {member} during backfill")

                if (i + 1) % self.CHECKPOINT_EVERY == 0:
                    await cfg.backfill_scan.set({
                        "remaining_ids": pending[i + 1:],
                        "exempt_ids": list(exempt_ids),
                        "ts": datetime.now(timezone.utc).timestamp(),  # refresh TTL on progress
                        "total": total,
                    })
                if paced:
                    await asyncio.sleep(1.2)  # rate limit pacing
        finally:
            self._bulk_running.discard(ctx.guild.id)

        await self._flush_all()
        await cfg.backfill_scan.set({})
        await ctx.send(
            f"Backfill complete ({total} in list). Flagged {tally['flagged']} this run — "
            f"skipped: {tally['active']} became active since the scan, {tally['exempt']} exempt, "
            f"{tally['already']} already flagged. Errors: {tally['errors']}."
        )

    # ------------------------------------------------------- sweep tools

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkersweeppreview")
    async def lurker_sweep_preview(self, ctx):
        """Show who the next sweep would flag right now (flags nobody)."""
        cfg = self.config.guild(ctx.guild)
        role_id = await cfg.lurker_role_id()
        lurker_role = ctx.guild.get_role(role_id) if role_id else None
        if not lurker_role:
            await ctx.send("Lurker role not configured.")
            return
        await self._ensure_loaded(ctx.guild.id)
        now = datetime.now(timezone.utc).timestamp()
        cutoff = engine.cutoff_ts(now, await cfg.threshold_days())
        exempt_ids = set(await cfg.exempt_role_ids())
        sweep_max = await cfg.sweep_max()
        cands = self._sweep_candidates(ctx.guild, lurker_role, exempt_ids, cutoff)

        cache = self._cache.get(ctx.guild.id, {})
        rows = []
        for m in cands:
            tracked = cache.get(m.id)
            if tracked is not None:
                rows.append((m.id, str(m), tracked, "tracked activity"))
            else:
                joined = m.joined_at.timestamp() if m.joined_at else None
                rows.append((m.id, str(m), joined, "no activity seen — join date"))
        rows.sort(key=lambda r: r[2] or 0)

        verdict = (
            f"⚠️ Over the safety limit ({sweep_max}) — a real sweep would ABORT."
            if len(cands) > sweep_max else f"Within the safety limit ({sweep_max})."
        )
        no_data = sum(1 for r in rows if r[3].startswith("no activity"))
        text = (
            f"**{len(cands)}** members would be flagged right now. {verdict}\n"
            f"{no_data} of them have no recorded activity at all (judged by join date)."
        )
        if not exempt_ids:
            text += "\n⚠️ No exempt roles are set."
        file = None
        if rows:
            file = discord.File(
                io.BytesIO(engine.candidates_to_csv(rows).encode("utf-8")),
                filename="lurker_sweep_preview.csv",
            )
        await ctx.send(text, file=file)

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkersweeprun")
    async def lurker_sweep_run(self, ctx):
        """Run a sweep right now (still respects the safety limit)."""
        if ctx.guild.id in self._bulk_running:
            await ctx.send("A bulk Lurker operation is already running here.")
            return
        await ctx.send("Running sweep now…")
        stats = await self._sweep_guild(ctx.guild, datetime.now(timezone.utc).timestamp(), force=True)
        if stats is None:
            await ctx.send("Sweep didn't run — Lurker role isn't configured.")
        elif stats["aborted"]:
            await ctx.send("Sweep aborted by the safety limit (see the alert in the report channel / logs).")
        else:
            await ctx.send(
                f"Sweep done: {stats['flagged']} flagged of {stats['candidates']} candidates "
                f"({stats['skipped']} skipped, {stats['errors']} errors)."
            )

    # ------------------------------------------------------------- reports

    async def _send_alert(self, guild: discord.Guild, text: str):
        channel_id = await self.config.guild(guild).report_channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(text)
        except (discord.Forbidden, discord.HTTPException):
            log.warning(f"Could not post Lurker alert in {channel} ({guild})")

    async def _maybe_send_reports(self):
        now = datetime.now(timezone.utc).timestamp()
        for guild in self.bot.guilds:
            try:
                cfg = self.config.guild(guild)
                if not await cfg.report_channel_id():
                    continue
                if not engine.report_due(now, await cfg.last_report_ts(), await cfg.report_interval_days()):
                    continue
                await self._send_digest(guild, consume=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(f"Lurker report failed for {guild}")

    async def _build_digest(self, guild: discord.Guild, events: List[dict], counts: dict,
                            period_start: float, now: float) -> Tuple[discord.Embed, Optional[str]]:
        cfg = self.config.guild(guild)
        summary = engine.summarize(events, counts)
        role_id = await cfg.lurker_role_id()
        lurker_role = guild.get_role(role_id) if role_id else None
        in_lurkers = sum(1 for m in guild.members if lurker_role in m.roles) if lurker_role else 0
        chan_id = await cfg.lurker_channel_id()
        chan = guild.get_channel(chan_id) if chan_id else None
        last_sweep = await cfg.last_sweep()
        enabled = await cfg.enabled()

        quiet = not (events or any(counts.values()))
        embed = discord.Embed(
            title="Lurker report",
            description=(
                f"<t:{int(period_start)}:d> → <t:{int(now)}:d>"
                + ("\nNo lurker activity this period." if quiet else "")
            ),
            color=discord.Color.green(),
        )
        if summary["sweep"]:
            embed.add_field(
                name=f"Flagged by daily sweep ({len(summary['sweep'])})",
                value=engine.format_name_list(summary["sweep"]), inline=False,
            )
        if summary["manual"]:
            embed.add_field(
                name=f"Flagged manually ({len(summary['manual'])})",
                value=engine.format_name_list(summary["manual"]), inline=False,
            )
        if summary["bulk_flagged"]:
            embed.add_field(name="Backfill", value=f"{summary['bulk_flagged']} members flagged in bulk", inline=False)
        restored_bits = []
        if summary["restored_self"]:
            restored_bits.append(f"{summary['restored_self']} posted in {chan.mention if chan else 'the lurker channel'}")
        if summary["restored_mod"]:
            restored_bits.append(f"{summary['restored_mod']} restored by a mod")
        if summary["bulk_restored"]:
            restored_bits.append(f"{summary['bulk_restored']} restored in bulk")
        if restored_bits:
            embed.add_field(name="Restored", value="\n".join(restored_bits), inline=False)
        embed.add_field(name="Currently Lurkers", value=str(in_lurkers), inline=True)
        if last_sweep:
            note = " — **ABORTED by safety limit**" if last_sweep.get("aborted") else ""
            embed.add_field(
                name="Last sweep",
                value=(f"<t:{int(last_sweep['ts'])}:R>\n{last_sweep.get('flagged', 0)} flagged of "
                       f"{last_sweep.get('candidates', 0)} candidates{note}"),
                inline=True,
            )
        else:
            embed.add_field(name="Last sweep", value="Sweep enabled, hasn't run yet" if enabled else "Sweep is OFF", inline=True)
        if summary["dropped"]:
            embed.add_field(name="Note", value=f"{summary['dropped']} older events were dropped (list too long).", inline=False)

        csv_text = None
        listed = [e for e in events if e["src"] not in engine.BULK_SOURCES]
        if len(listed) > 15:
            csv_text = engine.events_to_csv(listed)
            embed.set_footer(text="Full list attached as CSV")
        return embed, csv_text

    async def _send_digest(self, guild: discord.Guild, *, consume: bool,
                           destination: "Optional[discord.abc.Messageable]" = None):
        """Build and post the digest. `consume=True` posts to the report channel and
        resets the counters; `consume=False` is a side-effect-free preview."""
        cfg = self.config.guild(guild)
        now = datetime.now(timezone.utc).timestamp()
        await self._flush_events(guild.id)

        async with self._events_lock:
            events = await cfg.report_events()
            counts = await cfg.report_counts()
            last_report_ts = await cfg.last_report_ts()
            period_start = last_report_ts or (now - engine.DAY * await cfg.report_interval_days())

            if destination is None:
                channel_id = await cfg.report_channel_id()
                destination = guild.get_channel(channel_id) if channel_id else None
            if destination is None:
                return False

            embed, csv_text = await self._build_digest(guild, events, counts, period_start, now)
            file = None
            if csv_text:
                file = discord.File(
                    io.BytesIO(csv_text.encode("utf-8")),
                    filename=f"lurker_report_{datetime.fromtimestamp(now, tz=timezone.utc):%Y-%m-%d}.csv",
                )
            try:
                await destination.send(embed=embed, file=file)
            except (discord.Forbidden, discord.HTTPException):
                log.warning(f"Could not post Lurker report in {destination} ({guild}); keeping events")
                if consume:
                    # retry in ~6h instead of hammering every hour or waiting a full week
                    await cfg.last_report_ts.set(
                        now - await cfg.report_interval_days() * engine.DAY + 6 * 3600
                    )
                return False

            if consume:
                await cfg.report_events.set([])
                await cfg.report_counts.set({})
                await cfg.last_report_ts.set(now)
            return True

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerreport")
    async def lurker_report(self, ctx):
        """Preview the mod report here. Changes nothing."""
        ok = await self._send_digest(ctx.guild, consume=False, destination=ctx.channel)
        if not ok:
            await ctx.send("Couldn't build the report.")

    @checks.admin_or_permissions(manage_roles=True)
    @commands.command(name="lurkerreportsend")
    async def lurker_report_send(self, ctx):
        """Post the mod report to the report channel now and reset the counters."""
        if not await self.config.guild(ctx.guild).report_channel_id():
            await ctx.send("No report channel set. Use `.lurkerset reportchannel #channel`.")
            return
        ok = await self._send_digest(ctx.guild, consume=True)
        await ctx.send("Report posted." if ok else "Couldn't post the report (check the channel and my permissions).")
