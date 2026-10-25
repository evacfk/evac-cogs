"""Secrets of Wonderland: WoW-style hidden puzzles, one new easy one a week.

How a hunt runs (see puzzles.py for the content of each puzzle):
- a public poem hides a trigger word (its first letters spell it, or it is the answer to the poem's riddle); saying it
  (`.tick` in chat, deleted at once, or just `tick` in a DM to the bot) starts a personal chain;
- each member's variant is a private salted hash of their user id, so nobody's answer helps anyone else;
- clue 1 arrives by DM; the object it describes is a pinned message in the member's own channel or forum post,
  "clicked" by reacting with the watch emoji, or (`kind: button` puzzles) by pressing the right one of four buttons; a
  wrong object or button is a decoy, and button replies are private to the clicker;
- the next clue is delayed to the next 9:00 Pacific, then a jumbled word or emoji rebus is DM'd and the member DMs the answer back;
- solving lists them in the Hall of Secrets (one post per puzzle, edited in place) and grants the puzzle tier's role
  when that tier has one. Easy and Medium never do.
- `kind: trial` puzzles (The Knave's Trial, trial.py) are logic puzzles instead of a chain of finds: every pool room
  holds a witness whose private, generated statement is read with a button; wrong answers re-roll the testimony.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from collections import deque
from datetime import datetime
from typing import Union

import discord
from redbot.core import Config, commands

from . import embeds, engine, trail, trial, views
from . import panel as panel_specs
from .settings_panel import PanelError, open_panel
from .constants import (
    COG_VERSION, CONFIG_IDENTIFIER, DEFAULT_GUILD, DEFAULT_MEMBER, HINT_MAX, HOME_TZ, ICON_MAX_BYTES, LAUNCH_WARN_SECONDS,
    NO_ROLE_TIERS, POOL_MAX, POOL_MIN, TICK_SECONDS, TIER_ROLE_NAMES, TRAIL_HINTS_PER_STAGE, TRAIL_MAX_ROOMS,
    WATCH_EMOJI,
)
from .puzzles import DM_CLOSED_NOTE, DM_CLOSED_PUBLIC, PUZZLES

log = logging.getLogger("red.evac-cogs.secrethunt")
_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)


class SecretHunt(commands.Cog):
    """Hidden puzzles for the server: find the trigger, follow the chain, earn your place in the Hall of Secrets."""

    __version__ = COG_VERSION

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self._locks: dict[int, asyncio.Lock] = {}
        self._poem_ids: dict[int, dict[int, str]] = {}   # guild id -> {poem message id: puzzle id}, loaded on first use
        self._poem_hist: dict[tuple, deque] = {}         # (guild, member, puzzle) -> recent poem reactions (memory only)
        self._sent_views = weakref.WeakSet()  # the views posted with a hunt object: discord.py keeps each one per message
        self._purged = 0                      # old copies' message views stopped when this cog loaded (shown in status)
        self._task = self.bot.loop.create_task(self._loop())

    async def cog_load(self):
        self._view = views.ObjectView(self)  # persistent: the buttons keep working across restarts
        self.bot.add_view(self._view)
        self._witness_view = views.WitnessView(self)
        self.bot.add_view(self._witness_view)
        self._trail_view = views.TrailView(self)
        self.bot.add_view(self._trail_view)
        self._purged = self._drop_stale_views()

    def cog_unload(self):
        self._task.cancel()
        for name in ("_view", "_witness_view", "_trail_view"):
            view = getattr(self, name, None)
            if view is not None:
                view.stop()
        for view in list(self._sent_views):  # a view posted with a message answers that message's buttons for as long as it
            view.stop()                      # is registered, ahead of the persistent ones, so it must not outlive this copy

    def _view_store(self):
        return getattr(getattr(self.bot, "_connection", None), "_view_store", None)

    def _message_views(self) -> list:
        """[(message id, button)] for every hunt button discord.py holds a view for on one specific message. It asks those
        before the persistent views, so a view an older copy of this cog left behind answers with that copy's code.
        Reads discord.py's private view table, so it never raises: nothing found means nothing is reported."""
        store = self._view_store()
        found = []
        try:
            for mid, table in list(getattr(store, "_views", {}).items()):
                if mid is None:
                    continue
                found += [(mid, item) for (_kind, cid), item in list(table.items()) if str(cid).startswith("secrethunt:")]
        except Exception:
            log.exception("secrethunt could not read the button table")
        return found

    def _drop_stale_views(self) -> int:
        """Stop every hunt view that an earlier copy of this cog left registered for a message (an unload only stopped the
        copy's own three persistent views). The message's buttons then fall back to this copy's. How many were stopped."""
        stopped = {}
        try:
            for _mid, item in self._message_views():
                view = getattr(item, "view", None)
                if view is not None and getattr(view, "cog", None) is not self:
                    stopped[id(view)] = view
            for view in stopped.values():
                view.stop()
        except Exception:
            log.exception("secrethunt could not clear the old button handlers")
        if stopped:
            log.warning("secrethunt: stopped %s button views left behind by an earlier copy of this cog", len(stopped))
        return len(stopped)

    def _sent_view(self, cls):
        """A fresh view for a message about to be posted, remembered so this copy can stop it when it unloads."""
        view = cls(self)
        self._sent_views.add(view)
        return view

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()
            touched = []
            async with self.config.guild(guild).finishers() as fin:
                for pid, ids in fin.items():
                    if user_id in ids:
                        ids.remove(user_id)
                        touched.append(pid)
            for pid in touched:
                await self._refresh_hall(guild, pid)

    # ------------------------------------------------------------------ helpers

    async def _salt(self, guild) -> str:
        conf = self.config.guild(guild)
        salt = await conf.salt()
        if not salt:
            import secrets  # stdlib; imported here only because this package must not be called "secrets"
            salt = secrets.token_hex(16)
            await conf.salt.set(salt)
        return salt

    async def _dm(self, member, content=None, embed=None) -> bool:
        try:
            if embed is not None:
                await member.send(content, embed=embed)
            else:
                await member.send(content)
            return True
        except _DISCORD_ERRORS:
            return False

    @staticmethod
    def _lookup(guild, cid):
        """A channel OR thread (forum post) by id; plain get_channel never returns threads."""
        getter = getattr(guild, "get_channel_or_thread", None) or guild.get_channel
        return getter(cid)

    async def _blocked(self, guild, member) -> bool:
        """True for a member holding the "blocked" role (Lurker). Every puzzle ignores them: their progress just waits."""
        rid = await self.config.guild(guild).blocked_role_id()
        return bool(rid) and any(getattr(r, "id", None) == rid for r in getattr(member, "roles", []))

    async def _find_object(self, conf, message_id: int):
        """(puzzle id, room id) of the object on this message: a real one, or a rehearsal copy in the test channel."""
        return (engine.find_object(await conf.objects(), message_id)
                or engine.find_object(await conf.test_objects(), message_id))

    async def _rehearsal_box(self, guild):
        """The test channel while Test mode is on and one is set, else None. While it is set, every puzzle's poem and
        objects are posted there (nothing public changes); turning Test mode off posts them for real."""
        conf = self.config.guild(guild)
        if not await conf.test_mode():
            return None
        return self._lookup(guild, await conf.test_channel_id() or 0)

    @staticmethod
    def _can_play(member, puzzle: dict) -> bool:
        """A work-in-progress puzzle (`wip`) is playable by admins only, so it can be tested live before it is finished."""
        if not puzzle.get("wip"):
            return True
        perms = getattr(member, "guild_permissions", None)
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

    async def _rooms_of(self, guild, puzzle: dict) -> list:
        """The rooms a puzzle's objects live in: the trail rooms for a trail puzzle, else the pool."""
        conf = self.config.guild(guild)
        return list(await (conf.trail() if puzzle.get("kind") == "trail" else conf.pool()))

    @staticmethod
    def _when(ts: float) -> str:
        """A Discord relative timestamp ("in 3 hours"), which shows in the reader's own time."""
        return f"<t:{int(ts)}:R>"

    @staticmethod
    def _rooms(pool) -> list:
        """How each witness is named in testimony: a channel mention, so a renamed forum post never breaks it."""
        return [f"<#{c}>" for c in pool]

    async def _prefixes(self, guild):
        try:
            return sorted(await self.bot.get_valid_prefixes(guild), key=len, reverse=True)
        except Exception:  # prefix lookup must never break message handling
            return []

    def _hint_for(self, guild, puzzle, pool, variant):
        """(channel or post, (scrambled title, letter shape) or None) for this variant's hiding place."""
        channel = self._lookup(guild, pool[variant % len(pool)])
        if channel is None:
            return None, None
        return channel, engine.title_hint(channel.name, f"{puzzle['id']}:{channel.id}")

    def _riddle_text(self, guild, puzzle, pool, variant) -> str:
        channel, hint = self._hint_for(guild, puzzle, pool, variant)
        if "pointers" in puzzle:  # this puzzle names the room in words, not as a jumble
            pointer = engine.pointer_for(puzzle, channel.name, variant) if channel is not None else None
            if pointer:
                return puzzle["riddle"].format(pointer=pointer, object_hint=engine.object_hint(puzzle, variant))
        elif channel is not None and hint:
            return puzzle["riddle"].format(hint=hint[0], shape=hint[1])
        return puzzle["riddle_no_hint"].format(
            channel=channel.mention if channel else "a room I cannot name",
            object_hint=engine.object_hint(puzzle, variant) if "buttons" in puzzle else "")

    def _final_text(self, puzzle, variant) -> str:
        answer = puzzle["answers"][variant % len(puzzle["answers"])]
        if "rebuses" in puzzle:  # a picture to read instead of a word to unjumble
            return puzzle["final"].format(rebus=puzzle["rebuses"][variant % len(puzzle["rebuses"])],
                                          shape=len(engine.normalize(answer)))
        return puzzle["final"].format(anagram=engine.scramble(answer, f"{puzzle['id']}:{variant}:final"))

    # ------------------------------------------------------------------ trigger (guild message or DM)

    @commands.Cog.listener()
    async def on_message(self, message):
        if getattr(message.author, "bot", False):
            return
        try:
            if message.guild is None:
                await self._on_dm(message)
            else:
                await self._on_guild_message(message)
        except Exception:
            log.exception("secrethunt on_message failed")

    async def _on_guild_message(self, message):
        content = (message.content or "").strip()
        if not content or len(content) > 40 or content.count(" ") > 3:
            return
        guild = message.guild
        conf = self.config.guild(guild)
        if not await conf.enabled():
            return
        published = await conf.published()
        if engine.match_trigger(PUZZLES, published, content) is None:  # cheap first look (ignores spaces and the prefix)
            return
        low = content.lower()
        word = next((low[len(p):] for p in await self._prefixes(guild) if p and low.startswith(p.lower())), None)
        pid = engine.match_trigger(PUZZLES, published, word or "")
        if pid is None:
            return
        try:
            await message.delete()  # never advertise the word in chat
        except _DISCORD_ERRORS:
            pass
        if await self._blocked(guild, message.author) or not self._can_play(message.author, PUZZLES[pid]):
            return
        if not await self._run_trigger(guild, message.author, pid, time.time()):
            try:
                await message.channel.send(
                    DM_CLOSED_PUBLIC.format(mention=message.author.mention),
                    delete_after=15, allowed_mentions=discord.AllowedMentions(users=[message.author]),
                )
            except _DISCORD_ERRORS:
                pass

    async def _on_dm(self, message):
        text = (message.content or "").strip()
        if not text or len(text) > 120:
            return
        user = message.author
        now = time.time()
        prefixes = await self._prefixes(None)
        low = text.lower()
        for p in prefixes:
            if p and low.startswith(p.lower()):
                low = low[len(p):]
                break
        for guild in list(self.bot.guilds):
            member = guild.get_member(user.id)
            if member is None:
                continue
            conf = self.config.guild(guild)
            if not await conf.enabled() or await self._blocked(guild, member):
                continue
            if engine.normalize(low) == "recap" and await self._trail_recap(guild, member):
                return
            pid = engine.match_trigger(PUZZLES, await conf.published(), low)
            if pid is not None:
                if self._can_play(member, PUZZLES[pid]):
                    await self._run_trigger(guild, member, pid, now)
                return
            if await self._trail_dm(guild, member, low, now):
                return
            if await self._try_guess(guild, member, low, now):
                return

    async def _run_trigger(self, guild, member, pid: str, now: float) -> bool:
        """Advance/replay the member's chain for this puzzle and DM the right text. False if the DM failed."""
        puzzle = PUZZLES[pid]
        conf = self.config.guild(guild)
        pool = await conf.pool()
        if len(pool) < POOL_MIN or len(pool) < puzzle.get("needs_pool", 0):
            return True  # not configured yet: stay silent
        salt = await self._salt(guild)
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state, action = engine.trigger(
                    states.get(pid), engine.variant_for(salt, pid, member.id, len(pool)), now)
                if puzzle.get("kind") == "trial":
                    trial.ensure(state)
                states[pid] = state
        variant = state["variant"]
        if puzzle.get("kind") == "trial":
            rooms = " ".join(self._rooms(pool))
            text = {"start": puzzle["summons"].format(rooms=rooms), "riddle_again": puzzle["summons"].format(rooms=rooms),
                    "waiting": puzzle["waiting"], "final_again": puzzle["summons2"].format(rooms=rooms),
                    }.get(action, puzzle["already"])
        elif action in ("start", "riddle_again"):
            text = self._riddle_text(guild, puzzle, pool, variant)
        elif action == "waiting":
            text = puzzle["waiting"]
        elif action == "final_again":
            text = self._final_text(puzzle, variant)
        else:
            text = puzzle["already"]
        return await self._dm(member, text)

    # ------------------------------------------------------------------ the object (reaction)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        try:
            await self._on_reaction(payload)
        except Exception:
            log.exception("secrethunt on_raw_reaction_add failed")

    async def _poem_map(self, guild_id: int) -> dict:
        """{poem message id: puzzle id} for a guild, loaded once and kept in step by _apply_poem."""
        if guild_id not in self._poem_ids:
            guild = self.bot.get_guild(guild_id)
            raw = await self.config.guild(guild).poem_ids() if guild is not None else {}
            self._poem_ids[guild_id] = {int(k): v for k, v in raw.items()}
        return self._poem_ids[guild_id]

    async def _on_reaction(self, payload):
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", None):
            return
        pid = (await self._poem_map(payload.guild_id)).get(payload.message_id)
        if pid is not None:
            await self._on_poem_reaction(payload, pid)
            return
        if engine.normalize_emoji(str(payload.emoji)) != WATCH_EMOJI:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        conf = self.config.guild(guild)
        if not await conf.enabled():
            return
        hit = await self._find_object(conf, payload.message_id)
        if hit is None:
            return
        pid, channel_id = hit
        if PUZZLES.get(pid, {}).get("kind", "reaction") != "reaction":
            return  # a button object: reactions on it mean nothing
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        await self._remove_reaction(guild, payload, member)  # the object always looks untouched
        if await self._blocked(guild, member):
            return
        puzzle, pool = PUZZLES.get(pid), await conf.pool()
        if puzzle is None or len(pool) < POOL_MIN:
            return
        now, test_mode = time.time(), await conf.test_mode()
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not state:
                    return
                own = pool[state["variant"] % len(pool)] == channel_id
                action = engine.react(state, own, now, test_mode)
                if action == "ignore":
                    return
        if action == "found":
            await self._dm(member, puzzle["found"])
        elif action in ("decoy", "lockout"):  # while locked out ("locked") the reaction just vanishes
            await self._dm(member, puzzle[action])

    async def _on_poem_reaction(self, payload, pid: str):
        """A reaction on a trail puzzle's pinned poem (stage 1). Every reaction is removed at once, right or wrong, so the
        poem always looks untouched and nobody can copy anyone by looking. The last three, in order, wake the Dormouse."""
        puzzle = PUZZLES.get(pid)
        guild = self.bot.get_guild(payload.guild_id)
        if puzzle is None or puzzle.get("kind") != "trail" or guild is None:
            return
        conf = self.config.guild(guild)
        if not await conf.enabled() or pid not in await conf.published():
            return
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        await self._remove_reaction(guild, payload, member)
        if await self._blocked(guild, member) or not self._can_play(member, puzzle):
            return
        now = time.time()
        history = self._poem_hist.setdefault((guild.id, member.id, pid), deque())
        if not trail.record_reaction(history, str(payload.emoji), now) or not trail.poem_solved(history):
            return
        rooms = await conf.trail()
        if len(rooms) < max(puzzle["trail_min"], len(trail.SISTERS)):
            return  # not set up yet: stay silent
        plan = trail.plan(trail.seed(await self._salt(guild), pid, member.id), len(rooms))
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                if states.get(pid):
                    return  # already woke him
                state = trail.start(0, now)
                state["clue"] = trail.first_clue(puzzle, self._pointer(guild, puzzle, rooms, plan, 0))
                states[pid] = state
        history.clear()
        if not await self._dm(member, state["clue"]):
            async with self._lock(guild.id):  # nothing was delivered: undo the start so reacting again works once DMs are open
                async with self.config.member(member).puzzles() as states:
                    states.pop(pid, None)
            channel = self._lookup(guild, payload.channel_id)
            if channel is not None:
                try:
                    await channel.send(DM_CLOSED_PUBLIC.format(mention=member.mention), delete_after=15,
                                       allowed_mentions=discord.AllowedMentions(users=[member]))
                except _DISCORD_ERRORS:
                    pass

    def _pointer(self, guild, puzzle: dict, rooms: list, plan: dict, step: int):
        """How trail room `plan['chain'][step]` is named in a clue; None when the room is gone."""
        channel = self._lookup(guild, rooms[plan["chain"][step]])
        if channel is None:
            return None
        return trail.room_pointer(puzzle, channel.name, step)

    async def _trail_recap(self, guild, member) -> bool:
        """`recap` by DM: send the current stage's clue again. True when this member is on a trail."""
        conf = self.config.guild(guild)
        states = await self.config.member(member).puzzles()
        for pid in await conf.published():
            puzzle, state = PUZZLES.get(pid), states.get(pid)
            if puzzle and puzzle.get("kind") == "trail" and state and state.get("stage") != engine.STAGE_SOLVED \
                    and self._can_play(member, puzzle):
                await self._dm(member, await self._stage_clue(guild, pid, puzzle, state, member) or state.get("clue") or puzzle["idle"])
                return True
        return False

    async def _stage_clue(self, guild, pid: str, puzzle: dict, state: dict, member):
        """The Dormouse's current message for stages 3 to 6, rebuilt from the member's plan rather than read from what was
        saved when the stage opened, so a message saved by an older build can never go stale. None before stage 3 (the
        sisters' lines carry the route and are kept as saved) or when it cannot be built."""
        step = state.get("step")
        if step not in (3, 4, 5, 6):
            return None
        try:
            rooms = await self.config.guild(guild).trail()
            plan = trail.plan(trail.seed(await self._salt(guild), pid, member.id), len(rooms))
            return {3: lambda: puzzle["stage3_intro"],
                    4: lambda: self._stage3_done_text(guild, puzzle, rooms, plan),
                    5: lambda: self._stage4_done_text(guild, puzzle, rooms, plan),
                    6: lambda: self._stage5_done_text(puzzle, plan)}[step]()
        except Exception:
            log.exception("secrethunt could not rebuild a recap")
            return None

    async def handle_trail(self, interaction, key: str):
        """A button under a tea table was pressed. The reply is private, so a wrong table looks like any other."""
        outcome, trace = "answered", {}
        try:
            text = await self._trail_text(interaction, key, trace)
        except Exception as err:
            log.exception("secrethunt tea table click failed")
            text, outcome = None, "error"
            trace["result"] = "error"
            msg, who = getattr(interaction, "message", None), getattr(interaction, "user", None)
            if getattr(interaction, "guild", None) is not None and msg is not None and who is not None:
                await self._note_click(interaction.guild, f"tea table ({key}): error {type(err).__name__}: {err}", msg, who)
        if not text:
            outcome = "error" if outcome == "error" else "no answer"
        try:
            await interaction.response.send_message(text or "Nothing happens.", ephemeral=True)
        except _DISCORD_ERRORS as err:
            outcome += f", reply failed ({type(err).__name__})"
            await self._note_click(getattr(interaction, "guild", None), f"tea table ({key}): reply failed, {type(err).__name__}",
                                   getattr(interaction, "message", None), getattr(interaction, "user", None))
        await self._note_press("tea table", key, interaction, outcome)
        await self._note_member_press(interaction, key, trace)

    async def _note_member_press(self, interaction, key: str, trace: dict) -> None:
        """Keep a member's last few tea table presses (room, button, what it did) for `.secrethunt who`, so a stuck member's
        route can be checked without asking them. It records what they were told, never where the right room is."""
        guild, member = getattr(interaction, "guild", None), getattr(interaction, "user", None)
        if guild is None or member is None or getattr(member, "bot", False):
            return
        try:
            async with self.config.member(member).presses() as rows:
                rows.append([time.time(), trace.get("room"), key, str(trace.get("result") or "answered")[:60]])
                del rows[:-6]
        except Exception:
            log.exception("secrethunt could not record a member's press")

    async def _note_click(self, guild, why: str, message, member) -> None:
        """Keep the last few unanswered button clicks (shown in status), so a "Nothing happens" can be explained
        without reading container logs. `why` starts with which handler gave the reply: tea table, hunt object or witness."""
        if guild is None or member is None:
            return
        try:
            async with self.config.guild(guild).click_log() as entries:
                entries.append([time.time(), str(why)[:140], member.id, getattr(getattr(message, "channel", None), "id", None)])
                del entries[:-8]
        except Exception:
            log.exception("secrethunt could not record an ignored click")

    async def _note_press(self, handler: str, key: str, interaction, outcome: str) -> None:
        """TEMPORARY diagnostics (1.13.4): the last dozen button presses that reached this process, answered or not, with
        the handler, the button, the message id and the button's custom id, so a "Nothing happens" can be traced to the
        handler that gave it, or shown never to have arrived at all. Holds ids and counts only, never an answer."""
        guild, message, who = (getattr(interaction, "guild", None), getattr(interaction, "message", None),
                               getattr(interaction, "user", None))
        if guild is None or who is None:
            log.warning("secrethunt: %s press (%s) arrived without a guild or user: %s", handler, key, outcome)
            return
        data = getattr(interaction, "data", None)
        cid = data.get("custom_id") if isinstance(data, dict) else None
        try:
            async with self.config.guild(guild).press_log() as entries:
                entries.append([time.time(), f"{handler} {key}: {outcome}"[:100], who.id,
                                getattr(getattr(message, "channel", None), "id", None), getattr(message, "id", None), cid])
                del entries[:-12]
        except Exception:
            log.exception("secrethunt could not record a button press")

    async def _ignore_press(self, guild, member, message, kind: str, why: str):
        """A hunt object or witness press that gets no answer: note and log why. An admin is told privately (a member
        sees only "Nothing happens.")."""
        await self._note_click(guild, f"{kind}: {why}", message, member)
        log.warning("secrethunt: %s click ignored (%s): message %s in channel %s, user %s", kind, why,
                    getattr(message, "id", None), getattr(getattr(message, "channel", None), "id", None), member.id)
        if getattr(getattr(member, "guild_permissions", None), "administrator", False):
            return f"(Admin only: this click was ignored because {why}.)"
        return None

    async def _room_pointer(self, guild, puzzle: dict, rooms: list, room_index: int, step: int):
        """How trail room number `room_index` is named in a clue; None when the room is gone."""
        channel = self._lookup(guild, rooms[room_index])
        return trail.room_pointer(puzzle, channel.name, step) if channel is not None else None

    def _stage3_done_text(self, guild, puzzle: dict, rooms: list, plan: dict) -> str:
        """The Dormouse's words after stage 3: his token, and where to pour and toast (also what `recap` repeats)."""
        pour = trail.room_pointer(puzzle, self._lookup(guild, rooms[plan["pour_room"]]).name, 3) \
            if self._lookup(guild, rooms[plan["pour_room"]]) is not None else None
        toast = trail.room_pointer(puzzle, self._lookup(guild, rooms[plan["toast_room"]]).name, 4) \
            if self._lookup(guild, rooms[plan["toast_room"]]) is not None else None
        return puzzle["stage3_done"].format(token=plan["extra_tokens"][0], pour_pointer=pour or "a room I cannot name",
                                            toast_pointer=toast or "a room I cannot name")

    async def _stale_table(self, guild, message, rooms: list):
        """(puzzle id, room id) for a tea table the bot posted earlier and no longer tracks (the rooms were re-pasted and the
        tables set up again, leaving the old ones pinned). It still belongs to the room it is in, so it keeps working."""
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        author = getattr(message, "author", None)
        if channel_id not in rooms or (author is not None and author.id != getattr(self.bot.user, "id", None)):
            return None
        titles = [e.title for e in getattr(message, "embeds", None) or []] or (
            [message.embed.title] if getattr(message, "embed", None) is not None else [])
        published = await self.config.guild(guild).published()
        for pid, puzzle in PUZZLES.items():
            if puzzle.get("kind") == "trail" and puzzle["object_title"] in titles and pid in published:
                return pid, channel_id
        return None

    def _stage4_done_text(self, guild, puzzle: dict, rooms: list, plan: dict) -> str:
        """The Dormouse's words after stage 4: his token and the four courtesies as verses (also what `recap` repeats)."""
        verses = []
        for k in range(trail.COURTESIES):
            channel = self._lookup(guild, rooms[plan["courtesy_rooms"][k]])
            pointer = trail.room_pointer(puzzle, channel.name, 5 + k) if channel is not None else None
            verses.append(f"**{puzzle['verse_label'][k]}**\n{puzzle['verses'][plan['courtesy_buttons'][k]]}\n"
                          + puzzle["verse_place"].format(pointer=pointer or "a room I cannot name"))
        return puzzle["stage4_done"].format(token=plan["extra_tokens"][1], verses="\n\n".join(verses))

    @staticmethod
    def _stage5_done_text(puzzle: dict, plan: dict) -> str:
        """The Dormouse's words after stage 5: his token and the order of the tale (also what `recap` repeats)."""
        return puzzle["stage5_done"].format(token=plan["extra_tokens"][2], tale=trail.tale_text(plan))

    def _room_names(self, guild, rooms: list) -> list:
        """(name, mention) per trail room for the hint texts."""
        out = []
        for cid in rooms:
            channel = self._lookup(guild, cid)
            out.append((channel.name, channel.mention) if channel is not None else ("a room I cannot name",) * 2)
        return out

    @staticmethod
    def _trial_open(states: dict, published) -> bool:
        """True while one of the member's Knave's Trial rounds is open: that puzzle keeps the DM word `hint` meanwhile."""
        return any(PUZZLES.get(p, {}).get("kind") == "trial" and states.get(p) and trial.round_of(states[p])
                   for p in published)

    async def _trail_text(self, interaction, key: str, trace: dict = None):
        trace = {} if trace is None else trace
        guild, member = interaction.guild, interaction.user
        message = getattr(interaction, "message", None)
        trace["room"] = getattr(getattr(message, "channel", None), "id", None)
        if guild is None or message is None or getattr(member, "bot", False):
            log.warning("secrethunt: tea table click dropped (guild %s, message %s, bot %s)", getattr(guild, "id", None),
                        getattr(message, "id", None), getattr(member, "bot", False))
            if guild is not None and member is not None:
                await self._note_click(guild, "tea table (%s): %s" % (
                    key, "the press carried no message" if message is None else "a bot pressed it"), message, member)
            return None
        conf = self.config.guild(guild)

        async def ignored(why: str):
            trace["result"] = f"ignored: {why}"
            await self._note_click(guild, f"tea table ({key}): {why}", message, member)
            log.warning("secrethunt: tea table click ignored (%s): message %s in channel %s, user %s", why, message.id,
                        getattr(getattr(message, "channel", None), "id", None), member.id)
            perms = getattr(member, "guild_permissions", None)
            if getattr(perms, "administrator", False):  # an admin is testing: say why, privately (members still see nothing)
                return f"(Admin only: this click was ignored because {why}.)"
            return None
        if not await conf.enabled():
            return await ignored("the hunt is switched off")
        if await self._blocked(guild, member):
            return await ignored("the member holds the blocked role")
        rooms = await conf.trail()
        hit = await self._find_object(conf, message.id) or await self._stale_table(guild, message, rooms)
        puzzle = PUZZLES.get(hit[0]) if hit else None
        if puzzle is None:
            return await ignored("this message is not a tea table the bot knows")
        if puzzle.get("kind") != "trail" or hit[1] not in rooms:
            return await ignored("this room is not one of the trail rooms")
        if not self._can_play(member, puzzle):
            return await ignored("the puzzle is admins-only")
        pid, room = hit[0], rooms.index(hit[1])
        now, test_mode, salt = time.time(), await conf.test_mode(), await self._salt(guild)
        dm_after = resend = None
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not state:
                    trace["result"] = "has not started"
                    return puzzle["idle"] if key == "look" else puzzle["quiet"]
                if state.get("stage") == engine.STAGE_SOLVED:
                    trace["result"] = "already solved"
                    return puzzle["already"]
                if state.get("dm_pending") and state.get("clue"):
                    resend = state["clue"]  # an earlier DM bounced: try again now that they pressed something
                if len(rooms) < len(trail.SISTERS):
                    return await ignored(f"only {len(rooms)} trail rooms are set")
                plan = trail.plan(trail.seed(salt, pid, member.id), len(rooms))
                if state.get("step") == 5:  # every button belongs to the Courtesies now
                    action, detail = trail.courtesy(state, room, key, plan, now, test_mode)
                    trace["result"] = f"courtesy {action}" + (f" {detail + 1}" if action == "sister" else "")
                    if action == "sister":  # Look in a sister's room that is not the next courtesy: she is heard again, free
                        nxt = (await self._room_pointer(guild, puzzle, rooms, plan["chain"][detail + 1], detail + 1)
                               if detail + 1 < len(trail.SISTERS) else None)
                        text_out = trail.sister_line(puzzle, detail, plan, nxt)
                    elif action == "ok":
                        text_out = puzzle["courtesy_ok"][detail[1]].format(done=detail[0] + 1)
                    elif action == "passed":
                        state["clue"] = dm_after = text_out = self._stage5_done_text(puzzle, plan)
                    elif action == "wrong":
                        text_out = (puzzle["courtesy_lost"] if detail
                                    else puzzle["wrong_look"][state["slips"] % len(puzzle["wrong_look"])])
                    elif action in ("rest", "rest_start"):
                        text_out = puzzle["courtesy_rest_start" if action == "rest_start" else "courtesy_rest"].format(
                            until=self._when(detail))
                    else:
                        text_out = puzzle["idle"]
                elif state.get("step") == 3 and not (key == "look" and room in plan["chain"]):
                    trace["result"] = "stage 3: told the Dormouse waits in DMs"  # a sister's room still replays her below
                    text_out = puzzle["stage3_nudge"]
                    if not state.get("s3_sent"):  # said once: whoever never got his message (or lost it) gets it now
                        state["s3_sent"] = True
                        state["clue"] = dm_after = puzzle["stage3_intro"]
                        trace["result"] += ", message sent again"
                elif key == "bow":
                    trace["result"] = "bow"
                    text_out = puzzle["quiet"]
                elif key == "look":
                    action, detail = trail.look(state, room, plan, now, test_mode)
                    if action in ("sister", "replay"):
                        trace["result"] = f"heard sister {detail + 1}" + (" again" if action == "replay" else "")
                    else:
                        trace["result"] = f"look: {action}"
                    if action in ("sister", "replay"):
                        nxt = (await self._room_pointer(guild, puzzle, rooms, plan["chain"][detail + 1], detail + 1)
                               if detail + 1 < len(trail.SISTERS) else None)
                        text = trail.sister_line(puzzle, detail, plan, nxt)
                        if action == "sister":
                            if detail + 1 < len(trail.SISTERS):
                                state["clue"] = text
                            else:  # the third sister has spoken: the Dormouse wakes and stage 3 opens
                                state["clue"] = dm_after = puzzle["stage3_intro"]
                        text_out = text
                    else:
                        text_out = {"rest": lambda: puzzle["rest"].format(until=self._when(detail)),
                                    "rest_start": lambda: puzzle["rest_start"].format(until=self._when(detail)),
                                    "wrong": lambda: puzzle["wrong_look"][state["wrong_looks"] % len(puzzle["wrong_look"])],
                                    }.get(action, lambda: puzzle["idle"])()
                elif key == "pour":
                    action, detail = trail.pour(state, room, plan, now, test_mode)
                    trace["result"] = f"pour: {action}"
                    text_out = {"poured": lambda: puzzle["pour_ok"].format(n=detail), "wrong": lambda: puzzle["pour_wrong"],
                                "full": lambda: puzzle["pour_full"],
                                "rest": lambda: puzzle["pour_rest"].format(until=self._when(detail)),
                                }.get(action, lambda: puzzle["quiet"])()
                else:  # toast
                    action, detail = trail.toast(state, room, plan, now, test_mode)
                    trace["result"] = f"toast: {action}"
                    if action == "passed":
                        state["clue"] = dm_after = self._stage4_done_text(guild, puzzle, rooms, plan)
                    text_out = {"wrong": lambda: puzzle["toast_wrong"], "empty": lambda: puzzle["toast_empty"],
                                "miscount": lambda: puzzle["toast_miscount"],
                                "miscount_rest": lambda: puzzle["toast_miscount_rest"].format(until=self._when(detail)),
                                "rest": lambda: puzzle["pour_rest"].format(until=self._when(detail)),
                                "passed": lambda: dm_after,
                                }.get(action, lambda: puzzle["quiet"])()
        send = dm_after or resend
        if send:
            delivered = await self._dm(member, send)
            async with self._lock(guild.id):
                async with self.config.member(member).puzzles() as states:
                    if states.get(pid) is not None:
                        if delivered:
                            states[pid].pop("dm_pending", None)
                        else:
                            states[pid]["dm_pending"] = True
            if not delivered:
                text_out = f"{text_out}\n\n{DM_CLOSED_NOTE}"
        return text_out

    async def _trail_dm(self, guild, member, text: str, now: float) -> bool:
        """A DM from a member on the Treacle Well: `hint` (any stage), a word or the test answer (stage 3, the Dormouse's
        words), or the six letters (stage 6). Goes before the other puzzles' guesses (stage 3's words are ordinary words),
        except an exact answer of the member's own Easy puzzle and the DM word `hint` while a Knave's Trial round is open.
        True when it was handled."""
        conf = self.config.guild(guild)
        published = await conf.published()
        pid = next((p for p in published if PUZZLES.get(p, {}).get("kind") == "trail"), None)
        if pid is None:
            return False
        puzzle, rooms = PUZZLES[pid], await conf.trail()
        if not self._can_play(member, puzzle) or len(rooms) < max(len(trail.SISTERS), trail.COURTESIES):
            return False
        test_mode, salt = await conf.test_mode(), await self._salt(guild)
        plan = trail.plan(trail.seed(salt, pid, member.id), len(rooms))
        asking_hint = engine.normalize(text) == "hint"
        reply, solved = None, None
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not state or state.get("stage") != engine.STAGE_RIDDLE:
                    return False
                if asking_hint:
                    if self._trial_open(states, published):
                        return False
                    action, detail = trail.hint(state, now, test_mode)
                    if action == "ok":
                        reply = puzzle["hint"].format(
                            hint=trail.hint_text(state["step"], detail, plan, state, self._room_names(guild, rooms)),
                            n=detail, max=TRAIL_HINTS_PER_STAGE)
                    else:
                        reply = {"early": lambda: puzzle["hint_early"].format(when=self._when(detail)),
                                 "max": lambda: puzzle["hint_max"]}.get(action, lambda: None)()
                elif state.get("step") not in (3, 6):
                    return False
                elif any((other := PUZZLES.get(p)) and (st := states.get(p)) and other.get("kind") not in ("trial", "trail")
                         and st["stage"] == engine.STAGE_FINAL
                         and engine.normalize(text) == engine.normalize(other["answers"][st["variant"] % len(other["answers"])])
                         for p in published):
                    return False
                elif state["step"] == 6:
                    typed = engine.normalize(text)
                    if typed != plan["answer"] and typed in {engine.normalize(n) for n in trial.SUSPECTS}:
                        return False  # a suspect's name ("Hatter" has six letters) is the Knave's Trial's, not a wrong answer
                    action, detail = trail.answer(state, text, plan, now, test_mode)
                    if action == "solved":
                        async with conf.finishers() as fin:  # claimed before any further await
                            ids = fin.setdefault(pid, [])
                            if member.id not in ids:
                                ids.append(member.id)
                            state["rank"] = ids.index(member.id) + 1
                        solved = (pid, state["rank"])
                    else:
                        reply = {"wrong": lambda: puzzle["answer_wrong"].format(until=self._when(detail)),
                                 "rest": lambda: puzzle["answer_rest"].format(until=self._when(detail)),
                                 }.get(action, lambda: None)()
                else:
                    action, detail = trail.muchness(state, plan, text, now, test_mode)
                    words = ", ".join(detail) if action in ("ready", "test_open") else ""
                    if action == "passed":
                        state["clue"] = reply = self._stage3_done_text(guild, puzzle, rooms, plan)
                    elif action == "probe":
                        reply = puzzle["probe_yes" if detail[1] else "probe_no"].format(word=detail[0])
                    elif action == "probe_pause":
                        reply = puzzle["probe_yes" if detail[1] else "probe_no"].format(word=detail[0]) + "\n\n" + \
                            puzzle["probe_pause"].format(until=self._when(detail[2]))
                    else:
                        reply = {"saving": lambda: puzzle["probe_saving"], "tired": lambda: puzzle["probe_tired"],
                                 "pausing": lambda: puzzle["probe_resting"].format(until=self._when(detail)),
                                 "ready": lambda: puzzle["test_intro"].format(words=words),
                                 "test_open": lambda: puzzle["test_again"].format(words=words),
                                 "resting": lambda: puzzle["test_rest"].format(until=self._when(detail)),
                                 "failed": lambda: puzzle["test_failed"].format(until=self._when(detail)),
                                 "malformed": lambda: puzzle["test_malformed"]}.get(action, lambda: None)()
        if solved is not None:
            await self._on_solved(guild, member, *solved)
            return True
        if reply is None:
            return False
        await self._dm(member, reply)
        return True

    async def handle_click(self, interaction, key: str):
        """A button under a hunt object was pressed. Whatever happens, the answer is private to the clicker, so a decoy
        looks exactly like the real thing and nobody can watch someone else solve."""
        outcome = "answered"
        try:
            text = await self._click_text(interaction, key)
        except Exception as err:
            log.exception("secrethunt button click failed")
            text, outcome = None, "error"
            msg, who = getattr(interaction, "message", None), getattr(interaction, "user", None)
            await self._note_click(getattr(interaction, "guild", None), f"hunt object ({key}): error {type(err).__name__}: {err}",
                                   msg, who)
        if not text:
            outcome = "error" if outcome == "error" else "no answer"
        try:
            await interaction.response.send_message(text or "Nothing happens.", ephemeral=True)
        except _DISCORD_ERRORS as err:
            outcome += f", reply failed ({type(err).__name__})"
            await self._note_click(getattr(interaction, "guild", None), f"hunt object ({key}): reply failed, {type(err).__name__}",
                                   getattr(interaction, "message", None), getattr(interaction, "user", None))
        await self._note_press("hunt object", key, interaction, outcome)

    async def _click_text(self, interaction, key: str):
        guild, member = interaction.guild, interaction.user
        message = getattr(interaction, "message", None)
        if guild is None or message is None or getattr(member, "bot", False):
            log.warning("secrethunt: hunt object click dropped (guild %s, message %s, bot %s)", getattr(guild, "id", None),
                        getattr(message, "id", None), getattr(member, "bot", False))
            return None
        conf = self.config.guild(guild)
        kind = f"hunt object ({key})"
        if not await conf.enabled():
            return await self._ignore_press(guild, member, message, kind, "the hunt is switched off")
        if await self._blocked(guild, member):
            return await self._ignore_press(guild, member, message, kind, "the member holds the blocked role")
        hit = await self._find_object(conf, message.id)
        puzzle = PUZZLES.get(hit[0]) if hit else None
        pool = await conf.pool()
        if puzzle is None:
            return await self._ignore_press(guild, member, message, kind, "this message is not a hunt object the bot knows")
        if puzzle.get("kind") != "button":
            return await self._ignore_press(guild, member, message, kind,
                                            f"this message belongs to {hit[0]}, which has no {key} button")
        if len(pool) < POOL_MIN:
            return await self._ignore_press(guild, member, message, kind, f"only {len(pool)} hiding places are set")
        pid, channel_id = hit
        now, test_mode = time.time(), await conf.test_mode()
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not state:
                    return puzzle["idle"]
                variant = state["variant"]
                buttons = puzzle["buttons"]
                own = pool[variant % len(pool)] == channel_id and buttons[variant % len(buttons)] == key
                action = engine.react(state, own, now, test_mode)
        return puzzle["idle"] if action == "ignore" else puzzle[{"found": "found", "decoy": "decoy",
                                                                    "lockout": "lockout", "locked": "locked"}[action]]

    async def handle_witness(self, interaction):
        """The button under a trial witness was pressed. The reply is private: it is this member's own testimony."""
        outcome = "answered"
        try:
            text = await self._witness_text(interaction)
        except Exception as err:
            log.exception("secrethunt witness button failed")
            text, outcome = None, "error"
            await self._note_click(getattr(interaction, "guild", None), f"witness: error {type(err).__name__}: {err}",
                                   getattr(interaction, "message", None), getattr(interaction, "user", None))
        if not text:
            outcome = "error" if outcome == "error" else "no answer"
        try:
            await interaction.response.send_message(text or "Nothing happens.", ephemeral=True)
        except _DISCORD_ERRORS as err:
            outcome += f", reply failed ({type(err).__name__})"
            await self._note_click(getattr(interaction, "guild", None), f"witness: reply failed, {type(err).__name__}",
                                   getattr(interaction, "message", None), getattr(interaction, "user", None))
        await self._note_press("witness", "hear", interaction, outcome)

    async def _witness_text(self, interaction):
        guild, member = interaction.guild, interaction.user
        message = getattr(interaction, "message", None)
        if guild is None or message is None or getattr(member, "bot", False):
            log.warning("secrethunt: witness click dropped (guild %s, message %s, bot %s)", getattr(guild, "id", None),
                        getattr(message, "id", None), getattr(member, "bot", False))
            return None
        conf = self.config.guild(guild)
        if not await conf.enabled():
            return await self._ignore_press(guild, member, message, "witness", "the hunt is switched off")
        if await self._blocked(guild, member):
            return await self._ignore_press(guild, member, message, "witness", "the member holds the blocked role")
        hit = await self._find_object(conf, message.id)
        puzzle = PUZZLES.get(hit[0]) if hit else None
        pool = await conf.pool()
        if puzzle is None:
            return await self._ignore_press(guild, member, message, "witness", "this message is not a hunt object the bot knows")
        if puzzle.get("kind") != "trial":
            return await self._ignore_press(guild, member, message, "witness", f"this message belongs to {hit[0]}, not a trial")
        if len(pool) != puzzle["needs_pool"] or hit[1] not in pool:
            return await self._ignore_press(guild, member, message, "witness",
                                            f"the pool holds {len(pool)} places, the trial needs {puzzle['needs_pool']}, "
                                            "or this room is not one of them")
        pid, witness = hit[0], pool.index(hit[1])
        salt, now = await self._salt(guild), time.time()
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not state:
                    return puzzle["idle"]
                round_no = trial.round_of(state)
                if not round_no:
                    return puzzle["already"] if state["stage"] == engine.STAGE_SOLVED else puzzle["closed"]
                trial.ensure(state)
                until = trial.recess_until(state, now)
                if until:
                    return puzzle["recess"].format(until=self._when(until))
                roll = state["roll"]
        case = await asyncio.to_thread(trial.generate, trial.case_seed(salt, pid, member.id, round_no, roll), round_no)
        rooms = self._rooms(pool)
        return puzzle["testimony"].format(room=rooms[witness], text=trial.testimony(case[0][witness], rooms))

    async def _remove_reaction(self, guild, payload, member):
        channel = self._lookup(guild, payload.channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(payload.message_id).remove_reaction(payload.emoji, member)
        except _DISCORD_ERRORS:
            pass

    # ------------------------------------------------------------------ the delayed clue (loop)

    async def _loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:  # one broken guild must never kill the loop for the others
                    await self._deliver_due(guild, time.time())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("secrethunt delivery failed for guild %s", guild.id)
            await asyncio.sleep(TICK_SECONDS)

    async def _deliver_due(self, guild, now: float):
        await self._run_launches(guild, now)  # a scheduled go-live must not wait on anything else
        if not await self.config.guild(guild).enabled():
            return
        await self._reopen_trail(guild)
        for uid, data in (await self.config.all_members(guild)).items():
            for pid, state in data["puzzles"].items():
                if pid in PUZZLES and engine.is_due(state, now):
                    await self._send_final(guild, uid, pid, now)

    async def _reopen_trail(self, guild) -> None:
        """A quiet forum post auto-archives, which would strand the tea table in it: reopen any that has."""
        conf = self.config.guild(guild)
        if not any(PUZZLES.get(p, {}).get("kind") == "trail" for p in await conf.published()):
            return
        live = await conf.objects()
        if not any(live.get(p) for p, puzzle in PUZZLES.items() if puzzle.get("kind") == "trail"):
            return  # only a rehearsal so far: the real rooms hold nothing to keep open
        for cid in await conf.trail():
            channel = self._lookup(guild, cid)
            if channel is None:  # archived threads are not always cached
                try:
                    channel = await guild.fetch_channel(cid)
                except (*_DISCORD_ERRORS, AttributeError):
                    continue
            if getattr(channel, "archived", False):
                try:
                    await channel.edit(archived=False)
                except _DISCORD_ERRORS:
                    log.warning("secrethunt: could not reopen trail room %s in guild %s", cid, guild.id)

    async def _send_final(self, guild, uid: int, pid: str, now: float):
        member = guild.get_member(uid)
        if member is None or await self._blocked(guild, member):
            return
        puzzle = PUZZLES[pid]
        async with self._lock(guild.id):  # mark first, send after: a second pass can never double-send
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not engine.is_due(state, now):
                    return
                engine.mark_final_sent(state, now)
                if puzzle.get("kind") == "trial":
                    trial.start_round2(state)  # the court reconvenes: fresh testimony, fresh hint ladder
                variant = state["variant"]
        if puzzle.get("kind") == "trial":
            text = puzzle["summons2"].format(rooms=" ".join(self._rooms(await self.config.guild(guild).pool())))
        else:
            text = self._final_text(puzzle, variant)
        if await self._dm(member, text):
            return
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                if states.get(pid, {}).get("stage") == engine.STAGE_FINAL:
                    engine.dm_failed(states[pid], now)

    # ------------------------------------------------------------------ the answer (DM)

    async def _try_guess(self, guild, member, text: str, now: float) -> bool:
        """True when this DM was handled as a guess for a puzzle at the final stage."""
        conf = self.config.guild(guild)
        reply, solved, handled = None, None, False
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                # An exact Easy answer always belongs to that Easy puzzle (two Late Rabbit answers are also suspects'
                # names, and a wrong trial guess costs a lockout), so such a DM never reaches the trial.
                easy_exact = any(
                    (puzzle := PUZZLES.get(pid)) and (state := states.get(pid)) and puzzle.get("kind") != "trial"
                    and state["stage"] == engine.STAGE_FINAL
                    and engine.normalize(text) == engine.normalize(puzzle["answers"][state["variant"] % len(puzzle["answers"])])
                    for pid in await conf.published())
                for pid in ([] if easy_exact else await conf.published()):  # an open trial round goes first: its answers are names
                    puzzle, state = PUZZLES.get(pid), states.get(pid)
                    if puzzle is None or not state or puzzle.get("kind") != "trial":
                        continue
                    outcome = await self._trial_dm(guild, member, pid, puzzle, state, text, now)
                    if outcome is not None:
                        (reply, solved), handled = outcome, True
                        break
                for pid in ([] if handled else await conf.published()):
                    puzzle, state = PUZZLES.get(pid), states.get(pid)
                    if (puzzle is None or not state or puzzle.get("kind") == "trial"
                            or state["stage"] != engine.STAGE_FINAL):
                        continue
                    answer = puzzle["answers"][state["variant"] % len(puzzle["answers"])]
                    result = engine.guess(state, answer, text, now)
                    if result == "correct":
                        async with conf.finishers() as fin:  # claimed before any further await
                            ids = fin.setdefault(pid, [])
                            if member.id not in ids:
                                ids.append(member.id)
                            state["rank"] = ids.index(member.id) + 1
                        solved = (pid, state["rank"])
                        break
                    if reply is None:
                        reply = (puzzle["locked"] if result == "locked"
                                 else puzzle["wrong"][(state["wrong_count"] - 1) % len(puzzle["wrong"])])
        if solved is not None:
            await self._on_solved(guild, member, *solved)
            return True
        if reply is not None:
            await self._dm(member, reply)
            return True
        return False

    async def _trial_dm(self, guild, member, pid: str, puzzle: dict, state: dict, text: str, now: float):
        """A DM from a member whose trial round is open: a hint request or an answer. Returns (reply, solved) where
        `solved` is (pid, rank) for a finished trial, or None when this DM is not for the trial at all. Called with the
        guild lock and the member's states held, so it must not take either again."""
        round_no = trial.round_of(state)
        pool = await self.config.guild(guild).pool()
        if not round_no or len(pool) != puzzle["needs_pool"]:
            return None
        trial.ensure(state)
        conf = self.config.guild(guild)
        test_mode, salt = await conf.test_mode(), await self._salt(guild)
        seed = trial.case_seed(salt, pid, member.id, round_no, state["roll"])
        if engine.normalize(text) == "hint":
            outcome, extra = trial.request_hint(state, now, test_mode)
            if outcome == "ok":
                case = await asyncio.to_thread(trial.generate, seed, round_no)
                hint = trial.hint_text(case, round_no, extra, self._rooms(pool), seed)
                return puzzle["hint"].format(hint=hint, n=extra, max=HINT_MAX), None
            return {"recess": lambda: puzzle["recess"].format(until=self._when(extra or 0)),
                    "max": lambda: puzzle["hint_max"],
                    "early": lambda: puzzle["hint_early"].format(when=self._when(extra or 0)),
                    }.get(outcome, lambda: None)(), None
        if trial.parse_answer(text, round_no)[0] == "none":
            return None  # not an answer to the trial: another puzzle may want it
        case = await asyncio.to_thread(trial.generate, seed, round_no)
        result = trial.check_answer(state, case, text, now, test_mode)
        if result == "correct" and round_no == 1:
            return puzzle["round1_done"], None
        if result == "correct":
            async with conf.finishers() as fin:  # claimed before any further await
                ids = fin.setdefault(pid, [])
                if member.id not in ids:
                    ids.append(member.id)
                state["rank"] = ids.index(member.id) + 1
            return None, (pid, state["rank"])
        until = self._when(state.get("locked_until", 0.0))
        reply = {"wrong": puzzle["wrong1" if round_no == 1 else "wrong2"].format(until=until),
                 "recess": puzzle["recess"].format(until=until),
                 "malformed_suspect": puzzle["malformed_suspect"], "malformed_hour": puzzle["malformed_hour"]}.get(result)
        return (reply, None) if reply else None

    async def _on_solved(self, guild, member, pid: str, rank: int):
        puzzle = PUZZLES[pid]
        earned = await self._grant_roles(guild, member, pid)
        text = puzzle["solved"].format(rank=f"\n\nYou were finder **#{rank}**.")
        if earned:
            text += "\n\nYou earned the " + " and ".join(f"**{r.name}**" for r in earned) + (
                " roles." if len(earned) > 1 else " role.")
        await self._dm(member, text)
        await self._refresh_hall(guild, pid)
        await self._announce(guild, member, pid, rank)

    async def _announce(self, guild, member, pid: str, rank: int) -> None:
        """One line in the announcement channel (if set) when someone finishes a secret. Never pings."""
        channel = self._lookup(guild, await self.config.guild(guild).announce_channel_id() or 0)
        if channel is None:
            return
        puzzle = PUZZLES[pid]
        text = (f"\N{OLD KEY} {member.mention} has solved **{puzzle['title']}** "
                f"({puzzle['tier'].title()}) \N{MIDDLE DOT} finder #{rank}")
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except _DISCORD_ERRORS:
            log.warning("secrethunt: could not announce a solve in guild %s", guild.id)

    async def _grant_roles(self, guild, member, pid: str) -> list:
        """The tier's role and this puzzle's own role, whichever are configured (Easy and Medium never have either)."""
        conf = self.config.guild(guild)
        tier = PUZZLES[pid]["tier"]
        if tier in NO_ROLE_TIERS:
            return []
        ids = [(await conf.roles()).get(tier), (await conf.puzzle_roles()).get(pid)]
        earned = []
        for rid in ids:
            role = guild.get_role(rid or 0)
            if role is None or role in earned:
                continue
            try:
                await member.add_roles(role, reason="Solved a secret hunt")
                earned.append(role)
            except _DISCORD_ERRORS:
                log.warning("secrethunt: could not give %s to %s", role.id, member.id)
        return earned

    async def _refresh_intro(self, guild, create: bool = False) -> None:
        """Keep the "what is this?" post at the very top of the Hall. Edited in place when it exists. When it has to be
        created, the puzzle posts are cleared first (the caller rewrites them) so that it really is the first post."""
        conf = self.config.guild(guild)
        hall = self._lookup(guild, await conf.hall_channel_id() or 0)
        if hall is None:
            return
        async with self._lock(guild.id):
            mid = await conf.hall_intro()
            if not mid and not create:
                return
            embed = embeds.intro_embed(list((await conf.poems()).keys())[:3])
            try:
                if mid:
                    try:
                        await hall.get_partial_message(mid).edit(embed=embed)
                        return
                    except discord.NotFound:
                        pass  # deleted: rebuild the channel top-down
                async with conf.hall_messages() as posts:
                    for old in list(posts.values()):
                        try:
                            await hall.get_partial_message(old).delete()
                        except _DISCORD_ERRORS:
                            pass
                    posts.clear()
                msg = await hall.send(embed=embed)
                await conf.hall_intro.set(msg.id)
            except _DISCORD_ERRORS:
                log.warning("secrethunt: could not write the Hall of Secrets intro in guild %s", guild.id)

    @staticmethod
    def _trail_note(state: dict) -> str:
        """'no hints · solved in 3 days' for a finished trail."""
        return trial.hint_label(state.get("hints", 0)) + " \N{MIDDLE DOT} " + trail.took_label(
            state.get("started_ts", 0.0), state.get("solved_ts", 0.0))

    async def _refresh_hall(self, guild, pid: str, create: bool = False):
        """Post or edit this puzzle's Hall of Secrets entry: its tier and every solver's name, in finishing order."""
        puzzle, conf = PUZZLES[pid], self.config.guild(guild)
        hall = self._lookup(guild, await conf.hall_channel_id() or 0)
        if hall is None:
            return
        async with self._lock(guild.id):
            ids = (await conf.finishers()).get(pid, [])
            entries = []
            for rank, uid in enumerate(ids, 1):
                member = guild.get_member(uid)
                if member is not None:  # someone who left the server drops off the list
                    name = engine.escape_markdown(member.display_name)
                    if puzzle.get("kind") == "trial":  # the list shows how many hints each solver needed
                        theirs = (await self.config.member_from_ids(guild.id, uid).puzzles()).get(pid) or {}
                        entries.append((rank, name, trial.hint_label(theirs.get("hints", 0))))
                    elif puzzle.get("kind") == "trail":  # hints used, and how many days the trail took
                        theirs = (await self.config.member_from_ids(guild.id, uid).puzzles()).get(pid) or {}
                        entries.append((rank, name, self._trail_note(theirs)))
                    else:
                        entries.append((rank, name))
            mid = (await conf.hall_messages()).get(pid)
            if not entries and not mid and not create:
                return
            text, hidden = engine.hall_text(entries)
            embed = embeds.hall_embed(puzzle["tier"], puzzle["title"], text, hidden, len(entries))
            try:
                if mid:
                    try:
                        await hall.get_partial_message(mid).edit(embed=embed)
                        return
                    except discord.NotFound:
                        pass  # the post was deleted: write a fresh one
                msg = await hall.send(embed=embed)
                async with conf.hall_messages() as posts:
                    posts[pid] = msg.id
            except _DISCORD_ERRORS:
                log.warning("secrethunt: could not update the Hall of Secrets in guild %s", guild.id)

    # ------------------------------------------------------------------ admin: objects and poem

    def _object_message(self, puzzle: dict, label: str = "") -> dict:
        """The embed (and buttons) of a puzzle's hunt object. `label` names the room a rehearsal copy stands in for."""
        kind = puzzle.get("kind")
        if kind == "button":
            return {"embed": embeds.object_embed(puzzle, label), "view": self._sent_view(views.ObjectView)}
        if kind == "trial":
            return {"embed": embeds.witness_embed(puzzle, label), "view": self._sent_view(views.WitnessView)}
        if kind == "trail":
            return {"embed": embeds.object_embed(puzzle, label), "view": self._sent_view(views.TrailView)}
        return {"embed": embeds.object_embed(puzzle, label)}

    async def _setup_objects(self, guild, pid: str, force: bool = False) -> list[str]:
        """Post and pin the hunt object in every pool channel (once per puzzle unless forced). Returns report lines.
        While Test mode is on with a test channel set, the objects are posted there instead, one per room and labelled
        with it, and nothing real is touched."""
        puzzle, conf = PUZZLES[pid], self.config.guild(guild)
        report = []
        rooms = await self._rooms_of(guild, puzzle)
        box = await self._rehearsal_box(guild)
        async with (conf.test_objects() if box is not None else conf.objects()) as objects:
            by_channel = objects.setdefault(pid, {})
            for cid in rooms:
                channel = self._lookup(guild, cid)
                if channel is None:
                    report.append(f"- <#{cid}>: not found (was the post deleted?)")
                    continue
                if str(cid) in by_channel and not force:
                    if puzzle.get("kind") != "trail" or await self._table_alive(channel, by_channel[str(cid)]):
                        report.append(f"- {channel.mention}: already set up")
                        continue
                    report.append(f"- {channel.mention}: the recorded tea table was gone, posting a new one")
                if box is not None:  # rehearsal: this room's object stands in the test channel, labelled
                    try:
                        msg = await box.send(**self._object_message(puzzle, channel.name))
                    except _DISCORD_ERRORS as err:
                        report.append(f"- {channel.mention}: failed in the test channel ({type(err).__name__})")
                        continue
                    by_channel[str(cid)] = msg.id
                    report.append(f"- {channel.mention}: object posted in {box.mention} (rehearsal)")
                    continue
                if getattr(channel, "archived", False):  # a quiet forum post may have auto-archived
                    try:
                        await channel.edit(archived=False)
                    except _DISCORD_ERRORS:
                        pass
                if puzzle.get("kind") == "trail":
                    gone = await self._remove_old_tables(channel, puzzle)
                    if gone:
                        report.append(f"- {channel.mention}: removed {gone} old tea table{'' if gone == 1 else 's'}")
                try:
                    msg = await channel.send(**self._object_message(puzzle))
                    await msg.pin()
                except _DISCORD_ERRORS as err:
                    report.append(f"- {channel.mention}: failed ({type(err).__name__}); needs Send Messages (in Threads), "
                                  "Embed Links and Manage Messages / Pin Messages there")
                    continue
                by_channel[str(cid)] = msg.id
                await self._delete_pin_notice(channel)
                report.append(f"- {channel.mention}: object pinned")
        return report

    @staticmethod
    async def _table_alive(channel, message_id: int) -> bool:
        """Is the recorded tea table still in the room? (An unreadable room counts as alive: nothing is reposted blindly.)"""
        fetch = getattr(channel, "fetch_message", None)
        if fetch is None:
            return True
        try:
            await fetch(message_id)
        except discord.NotFound:
            return False
        except _DISCORD_ERRORS:
            return True
        return True

    async def _remove_old_tables(self, channel, puzzle: dict) -> int:
        """Unpin and delete this puzzle's earlier pinned tables (posted by the bot) in a room before a fresh one goes up, so
        a room never holds two. How many were removed."""
        pins = getattr(channel, "pins", None)
        if pins is None:
            return 0
        try:
            result = pins()
            items = [m async for m in result] if hasattr(result, "__aiter__") else await result
        except _DISCORD_ERRORS:
            return 0
        removed = 0
        for m in items:
            author = getattr(m, "author", None)
            if author is not None and author.id != getattr(self.bot.user, "id", None):
                continue
            titles = [e.title for e in getattr(m, "embeds", None) or []] or (
                [m.embed.title] if getattr(m, "embed", None) is not None else [])
            if puzzle["object_title"] not in titles:
                continue
            try:
                await m.unpin(reason="Replaced by a fresh tea table")
                await m.delete()
                removed += 1
            except _DISCORD_ERRORS:
                pass
        return removed

    async def _delete_pin_notice(self, channel):
        """Pinning posts a 'pinned a message' line that would advertise the object; remove it."""
        pins_add = getattr(getattr(discord, "MessageType", None), "pins_add", None)
        history = getattr(channel, "history", None)
        if pins_add is None or history is None:
            return
        try:
            async for m in history(limit=5):
                if m.type == pins_add and getattr(m.author, "id", None) == getattr(self.bot.user, "id", None):
                    await m.delete()
                    break
        except _DISCORD_ERRORS:
            pass

    # ------------------------------------------------------------------ admin commands

    @commands.group(name="secrethunt", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def secrethunt(self, ctx):
        """Secret hunt control panel: pool, Hall, announcements, puzzles and reward roles."""
        await open_panel(ctx, self, panel_specs.build_specs(), title="Secret hunt", intro=panel_specs.INTRO)

    @secrethunt.command(name="version")
    async def secrethunt_version(self, ctx):
        """Show the running version (use it to confirm a deploy)."""
        await ctx.send(f"SecretHunt v{COG_VERSION}")

    @secrethunt.command(name="status")
    async def secrethunt_status(self, ctx):
        """Show the current configuration and progress counts."""
        await ctx.send(embed=await self._status_embed(ctx.guild))

    async def _status_embed(self, guild):
        conf = self.config.guild(guild)
        pool, roles = await conf.pool(), await conf.roles()
        members = await self.config.all_members(guild)
        finishers = await conf.finishers()
        hall = self._lookup(guild, await conf.hall_channel_id() or 0)
        tier_roles = []
        for tier in TIER_ROLE_NAMES:
            role = guild.get_role(roles.get(tier) or 0)
            tier_roles.append(f"{tier}: {role.mention if role else 'not set'}")
        lines = {
            "Enabled": "yes" if await conf.enabled() else "no",
            "Test mode": ("on (clues arrive in 30 seconds"
                          + ((lambda ch: f"; rehearsing in {ch.mention}" if ch else "")(
                              self._lookup(guild, await conf.test_channel_id() or 0))) + ")") if await conf.test_mode() else "off",
            "Pool": ", ".join(f"<#{c}>" if self._lookup(guild, c) else f"**MISSING** `{c}`" for c in pool)
                    or "not set (open `.secrethunt`)",
            "Roles": "easy and medium: none (by design), " + ", ".join(tier_roles),
            "Puzzle roles": ", ".join(
                f"{PUZZLES[p]['title']}: {r.mention if r else 'MISSING'}"
                for p, rid in (await conf.puzzle_roles()).items() if p in PUZZLES
                for r in [guild.get_role(rid)]) or "none",
            "Hall of Secrets": hall.mention if hall else "not set (open `.secrethunt`)",
            "Solve announcements": (lambda ch: ch.mention if ch else "off (open `.secrethunt`)")(
                self._lookup(guild, await conf.announce_channel_id() or 0)),
        }
        trail_ids = await conf.trail()
        if trail_ids:
            names = [(ch.name[:30] if ch is not None else f"**MISSING** `{cid}`")
                     for cid in trail_ids for ch in [self._lookup(guild, cid)]]
            lines["Trail rooms"] = f"{len(trail_ids)} set: " + ", ".join(names)
        for pid in await conf.published():
            if PUZZLES.get(pid, {}).get("kind") == "trail":
                f = trail.funnel([d["puzzles"][pid] for d in members.values() if pid in d["puzzles"]])
                at = " · ".join(f"stage {k}: {v}" for k, v in f["at"].items())
                med = ", ".join(f"{k}: {v} min" for k, v in sorted(f["median_minutes"].items())) or "nobody has finished a stage"
                lines[f"{PUZZLES[pid]['title']} funnel"] = f"{at} · solved {f['solved']} · hints {f['hints']} · median per stage ({med})"
        recent = await conf.click_log()
        if recent:
            lines["Unanswered button clicks"] = "\n".join(
                f"<t:{int(t)}:t> <@{u}> in <#{c}>: {why}" for t, why, u, c in recent[-5:])[:1000]
        held = self._message_views()
        if self._view_store() is not None:  # TEMPORARY (1.13.5): who answers the buttons on each posted object
            mine = {m for m, item in held if getattr(getattr(item, "view", None), "cog", None) is self}
            older = {m for m, item in held} - mine
            lines["Button handlers"] = (f"{len(mine)} posted objects answered by this copy, {len(older)} by an older copy"
                                        f"; {self._purged} older views stopped at load")
        presses = await conf.press_log()
        if presses:  # TEMPORARY (1.13.4): which handler received each press, and what it did
            lines["Latest button presses"] = "\n".join(
                f"<t:{int(t)}:t> <@{u}> in <#{c}> msg `{m}` id `{cid}`: {what}"
                for t, what, u, c, m, cid in presses[-6:])[:900]
        pending = [(pid, item) for pid, item in (await conf.launches()).items() if pid in PUZZLES]
        if pending:
            lines["Scheduled launches"] = ", ".join(
                f"{PUZZLES[pid]['title']} {self._when(item['at'])} ({item['state']})" for pid, item in pending)
        for pid in await conf.published():
            started = sum(1 for d in members.values() if pid in d["puzzles"])
            lines[PUZZLES[pid]["title"] if pid in PUZZLES else pid] = (
                f"{started} started, {len(finishers.get(pid, []))} solved")
        return embeds.status_embed(COG_VERSION, lines)

    # ------------------------------------------------------------------ setup actions (used by the panel)

    async def _apply_pool(self, guild, member, channels) -> str:
        """Set the 2-5 channels or forum posts the hunt objects hide in (variant 1 = the first)."""
        channels = list(channels or [])
        if not POOL_MIN <= len(channels) <= POOL_MAX or len({c.id for c in channels}) != len(channels):
            raise PanelError(f"Give me {POOL_MIN} to {POOL_MAX} different channels or forum posts.")
        conf = self.config.guild(guild)
        await conf.pool.set([c.id for c in channels])
        await conf.objects.set({})  # the old objects no longer match the variants
        await conf.test_objects.set({})
        return ("Pool set: " + ", ".join(c.mention for c in channels)
                + ". Now set up each puzzle's objects under Puzzles. Changing the pool mid-puzzle moves "
                  "people's hiding places, so do it between puzzles.")

    async def _apply_trail_text(self, guild, member, values: dict) -> str:
        """The Treacle Well's trail rooms from pasted mentions or ids (forum posts are threads)."""
        found, bad = [], []
        for token in (values.get("rooms") or "").replace(",", " ").split():
            cid = self._channel_id_of(token)
            channel = self._lookup(guild, cid) if cid else None
            if channel is None:
                bad.append(token)
            elif channel.id not in [c.id for c in found]:
                found.append(channel)
        if bad:
            raise PanelError("I couldn't find: " + ", ".join(bad) + ". Nothing was changed.")
        low = min((p.get("trail_min", 0) for p in PUZZLES.values() if p.get("kind") == "trail"), default=0)
        if not max(low, len(trail.SISTERS)) <= len(found) <= TRAIL_MAX_ROOMS:
            raise PanelError(f"Give me {max(low, len(trail.SISTERS))} to {TRAIL_MAX_ROOMS} different rooms (12 planned).")
        conf = self.config.guild(guild)
        await conf.trail.set([c.id for c in found])
        for store in (conf.objects, conf.test_objects):  # the old tea tables no longer match the rooms
            async with store() as objects:
                for pid, puzzle in PUZZLES.items():
                    if puzzle.get("kind") == "trail":
                        objects.pop(pid, None)
        return (f"Trail rooms set ({len(found)}). Now press Set up objects on the Treacle Well. Changing them mid-puzzle "
                "moves people's sisters, so do it between puzzles.")

    async def _forget_rehearsal(self, guild) -> None:
        """Drop every rehearsal copy from the books: its objects stop answering and its poems stop counting reactions."""
        conf = self.config.guild(guild)
        old = await conf.test_poems()
        async with conf.poem_ids() as ids:
            for mid in old.values():
                ids.pop(str(mid), None)
            self._poem_ids[guild.id] = {int(k): v for k, v in ids.items()}
        await conf.test_poems.set({})
        await conf.test_objects.set({})

    async def _post_test_poem(self, guild, pid: str, box) -> str:
        """A puzzle's rehearsal poem, in the test channel. Kept out of `poems`: that list is printed in the public Hall intro."""
        conf = self.config.guild(guild)
        puzzle = PUZZLES[pid]
        try:
            posted = await box.send(embed=embeds.poem_embed(puzzle))
        except _DISCORD_ERRORS as err:
            raise PanelError(f"I could not post in the test channel ({type(err).__name__}).")
        notes = ""
        try:
            await posted.pin(reason="Rehearsal poem")
        except _DISCORD_ERRORS:
            notes = " I could not pin it, which is fine for a rehearsal."
        async with conf.test_poems() as poems:
            old = poems.get(pid)
            poems[pid] = posted.id
        async with conf.poem_ids() as ids:
            if old:
                ids.pop(str(old), None)
                try:
                    await box.get_partial_message(old).unpin(reason="A new rehearsal poem replaces it")
                except _DISCORD_ERRORS:
                    pass
            if puzzle.get("kind") == "trail":  # only a trail reads reactions on its poem
                ids[str(posted.id)] = pid
            self._poem_ids[guild.id] = {int(k): v for k, v in ids.items()}
        return f"{puzzle['title']}: poem posted in the test channel {box.mention} (rehearsal).{notes}"

    async def _apply_test_channel(self, guild, member, channel) -> str:
        """The private channel that rehearsals run in. Anything already rehearsed is forgotten."""
        await self._forget_rehearsal(guild)
        await self.config.guild(guild).test_channel_id.set(channel.id if channel else None)
        if channel:
            return (f"Test channel: {channel.mention}. While Test mode is on, Set up objects and the poem post there, one "
                    "object per room, labelled with the room, and nothing real is touched. Turning Test mode off posts "
                    "the poems and objects for real.")
        return "Test channel cleared. Test mode now only shortens the waits."

    async def _launch_rehearsed(self, guild, member) -> str:
        """Test mode was turned off: every puzzle that was rehearsed gets its real objects and its poem in its regular channel."""
        conf = self.config.guild(guild)
        rehearsed = set(await conf.test_objects()) | set(await conf.test_poems())
        had_poem = set(await conf.test_poems())
        targets = await conf.poem_targets()
        scheduled = {p: l for p, l in (await conf.launches()).items() if l.get("state") == "pending"}
        await self._forget_rehearsal(guild)
        lines = []
        for pid, puzzle in PUZZLES.items():
            if pid not in rehearsed:
                continue
            try:
                lines.append(f"- {puzzle['title']}: objects ready in the real rooms\n"
                             + await self._apply_setup(guild, member, pid))
            except PanelError as err:
                lines.append(f"- {puzzle['title']}: objects NOT set up ({err})")
            if pid in scheduled:  # its go-live and poem wait for the scheduled time
                lines.append(f"- {puzzle['title']}: go-live and poem are scheduled for {self._when(scheduled[pid]['at'])}")
                continue
            if pid in had_poem:
                target = self._lookup(guild, targets.get(pid) or 0)
                if target is None:
                    lines.append(f"- {puzzle['title']}: no regular poem channel known. Pick one under Puzzles, "
                                 "\"Post the newest poem in...\".")
                    continue
                try:
                    lines.append(f"- {puzzle['title']}: " + await self._apply_poem(guild, member, target, pid))
                except PanelError as err:
                    lines.append(f"- {puzzle['title']}: poem NOT posted ({err})")
        return "Test mode off." + ("\n" + "\n".join(lines) if lines else "")

    async def _apply_blocked_role(self, guild, member, role) -> str:
        """Members with this role (Lurker) are ignored by every puzzle; their progress just waits."""
        await self.config.guild(guild).blocked_role_id.set(role.id if role else None)
        return (f"Members with {role.mention} cannot take part in any secret." if role
                else "Every member can take part again.")

    @staticmethod
    def _channel_id_of(token: str):
        """A channel or forum-post id from a mention (<#id>), a bare id or a pasted Discord link; None when it is none."""
        token = token.strip()
        if token.startswith("<#") and token.endswith(">"):
            token = token[2:-1]
        if token.startswith("http"):  # .../channels/<guild>/<channel>[/<message>]
            numbers = re.findall(r"\d+", token.split("/channels/", 1)[-1])
            return int(numbers[1]) if len(numbers) >= 2 else None
        return int(token) if token.isdigit() else None

    async def _apply_pool_text(self, guild, member, values: dict) -> str:
        """The pool from pasted mentions, ids or links (forum posts are threads, which a dropdown may not offer)."""
        found, bad = [], []
        for token in (values.get("channels") or "").replace(",", " ").split():
            cid = self._channel_id_of(token)
            channel = self._lookup(guild, cid) if cid else None
            if channel is None:
                bad.append(token)
            elif channel.id not in [c.id for c in found]:
                found.append(channel)
        if bad:
            raise PanelError("I couldn't find: " + ", ".join(bad) + ". Nothing was changed.")
        return await self._apply_pool(guild, member, found)

    async def _apply_setup(self, guild, member, puzzle_id: str, force: bool = False) -> str:
        """Post and pin the puzzle's object in each pool channel."""
        if puzzle_id not in PUZZLES:
            raise PanelError("Unknown puzzle. Known: " + ", ".join(PUZZLES))
        conf = self.config.guild(guild)
        if PUZZLES[puzzle_id].get("kind") == "trail":
            self._check_trail_size(puzzle_id, await conf.trail())
        else:
            pool = await conf.pool()
            if len(pool) < POOL_MIN:
                raise PanelError("Set the pool first (Hunt tab).")
            self._check_pool_size(puzzle_id, pool)
        lines = await self._setup_objects(guild, puzzle_id, force)
        box = await self._rehearsal_box(guild)
        if box is not None and (force or puzzle_id not in await conf.test_poems()):
            lines.append("- " + await self._post_test_poem(guild, puzzle_id, box))  # a rehearsal runs end to end from one channel
        return "\n".join(lines)

    @staticmethod
    def _check_trail_size(puzzle_id: str, rooms) -> None:
        need = PUZZLES[puzzle_id].get("trail_min", 0)
        if len(rooms) < need:
            raise PanelError(f"{PUZZLES[puzzle_id]['title']} needs at least {need} trail rooms "
                             f"({PUZZLES[puzzle_id].get('needs_trail', need)} planned); {len(rooms)} are set. "
                             "Set them under Hunt first.")

    @staticmethod
    def _check_pool_size(puzzle_id: str, pool) -> None:
        need = PUZZLES[puzzle_id].get("needs_pool", 0)
        if need and len(pool) != need:
            raise PanelError(f"{PUZZLES[puzzle_id]['title']} needs exactly {need} hiding places (one witness in each); "
                             f"the pool has {len(pool)}. Change it under Hunt first.")

    async def _apply_start(self, guild, member, puzzle_id: str) -> str:
        """Make a puzzle's trigger word live. Earlier puzzles stay live too."""
        if puzzle_id not in PUZZLES:
            raise PanelError("Unknown puzzle. Known: " + ", ".join(PUZZLES))
        conf = self.config.guild(guild)
        if PUZZLES[puzzle_id].get("kind") == "trail":
            self._check_trail_size(puzzle_id, await conf.trail())
        else:
            self._check_pool_size(puzzle_id, await conf.pool())
        async with conf.published() as published:
            if puzzle_id not in published:
                published.append(puzzle_id)
        await conf.active.set(puzzle_id)
        await self._refresh_intro(guild, create=True)
        await self._refresh_hall(guild, puzzle_id, create=True)  # its Hall post exists from day one, in release order
        return f"{PUZZLES[puzzle_id]['title']} is live (once the hunt is enabled)."

    async def _apply_poem(self, guild, member, target, puzzle_id: str = "") -> str:
        """Post a puzzle's public poem (it hides the trigger word) in `target` and pin it."""
        conf = self.config.guild(guild)
        pid = puzzle_id or await conf.active()
        if pid not in PUZZLES:
            raise PanelError("No puzzle is started. Start one under Puzzles first.")
        async with conf.poem_targets() as targets:  # remembered, so turning Test mode off knows where it belongs
            targets[pid] = target.id
        box = await self._rehearsal_box(guild)
        if box is not None:  # a rehearsal: the poem goes in the test channel, wherever you asked
            return await self._post_test_poem(guild, pid, box)
        try:
            posted = await target.send(embed=embeds.poem_embed(PUZZLES[pid]))
        except _DISCORD_ERRORS as err:
            raise PanelError(f"I could not post there ({type(err).__name__}).")
        notes = []
        async with self.config.guild(guild).poems() as poems:
            old = poems.get(str(target.id))
            if old:  # the previous poem in this channel comes down as the new one goes up
                try:
                    await target.get_partial_message(old).unpin(reason="A new secret poem replaces it")
                except _DISCORD_ERRORS:
                    pass  # already unpinned or deleted
            try:
                await posted.pin(reason="Secret poem")
                poems[str(target.id)] = posted.id
                async with self.config.guild(guild).poem_ids() as ids:  # a reaction on a trail poem finds its puzzle here
                    ids.pop(str(old), None)
                    ids[str(posted.id)] = pid
                    self._poem_ids[guild.id] = {int(k): v for k, v in ids.items()}
            except _DISCORD_ERRORS as err:
                poems.pop(str(target.id), None)
                notes.append(f"Posted, but I could not pin it ({type(err).__name__}): I need Pin Messages here, "
                             "and a channel holds at most 50 pins.")
        await self._refresh_intro(guild)  # it names the channels the poem is pinned in
        if notes:
            raise PanelError(" ".join(notes))
        return f"Poem posted and pinned in {target.mention}."

    # ------------------------------------------------------------------ scheduled launches

    _WHEN_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I%p")

    @classmethod
    def _parse_when(cls, text):
        """Pacific wall-clock text ("2026-10-24 09:00" or "2026-10-24 9:00am") as a timestamp; None if unreadable."""
        text = " ".join((text or "").split()).upper()
        for fmt in cls._WHEN_FORMATS:
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=HOME_TZ).timestamp()
            except ValueError:
                continue
        return None

    def _find_channel(self, guild, token: str):
        """A channel from what was typed: a #mention, an ID, a link, or its NAME (`announcements`, `#announcements`; emoji
        and dashes in the real name are ignored, and a part of a name works when it fits exactly one channel)."""
        cid = self._channel_id_of(token.strip())
        channel = self._lookup(guild, cid) if cid else None
        if channel is not None:
            return channel
        def squash(text):
            return re.sub(r"[^a-z0-9]", "", text.lower())
        wanted = squash(token.lstrip("#"))
        text = getattr(guild, "text_channels", None)
        pool = list(text) if text is not None else list(guild.channels.values())
        exact = [c for c in pool if squash(c.name) == wanted]
        part = [c for c in pool if wanted and wanted in squash(c.name)]
        hits = exact if len(exact) == 1 else (part if not exact else exact)
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise PanelError("That fits several channels: " + ", ".join(c.mention for c in hits[:6])
                             + ". Paste the one you mean as a #mention or an ID.")
        raise PanelError(f"I couldn't find a channel called `{token}`. Type its name, a #mention or an ID.")

    @staticmethod
    def _find_puzzle(text) -> str:
        """A puzzle from what was typed: its id, its title, or any part of either that fits exactly one puzzle."""
        text = " ".join((text or "").lower().split())
        for pid, puzzle in PUZZLES.items():
            if text in (pid, puzzle["title"].lower()):
                return pid
        hits = [pid for pid, puzzle in PUZZLES.items() if text and (text in pid.replace("_", " ") or text in puzzle["title"].lower())]
        if len(hits) == 1:
            return hits[0]
        raise PanelError(("That fits several puzzles. " if hits else "I don't know that puzzle. ")
                         + "Type its name, e.g. " + ", ".join(f"`{PUZZLES[p]['title']}`" for p in list(PUZZLES)[-3:]) + ".")

    async def _launch_problems(self, guild, pid: str) -> list:
        """What still stands between a scheduled puzzle and a clean launch (empty list = ready)."""
        conf, out = self.config.guild(guild), []
        if not await conf.enabled():
            out.append("the hunt is switched off (Hunt tab, Turn on)")
        if await conf.test_mode():
            out.append("Test mode is still on (turning it off sets up the real objects)")
        rooms = await self._rooms_of(guild, PUZZLES[pid])
        placed = (await conf.objects()).get(pid, {})
        if not rooms or not all(str(r) in placed for r in rooms):
            out.append("the objects are not set up in the real rooms (Puzzles tab, Set up objects)")
        return out

    async def _apply_launch(self, guild, member, values: dict) -> str:
        """Schedule (or, with an empty time, cancel) a puzzle's go-live: it is made live and its poem posted and pinned."""
        conf = self.config.guild(guild)
        pid = self._find_puzzle(values.get("puzzle"))
        title, puzzle = PUZZLES[pid]["title"], PUZZLES[pid]
        when_text = (values.get("when") or "").strip()
        if not when_text:
            async with conf.launches() as launches:
                if launches.pop(pid, None) is None:
                    raise PanelError(f"No launch is scheduled for {title}.")
            return f"{title}: the scheduled launch is cancelled."
        if puzzle.get("wip"):
            raise PanelError(f"{title} is still being built and only admins can play it, so it can't be launched yet.")
        at = self._parse_when(when_text)
        if at is None:
            raise PanelError("I couldn't read that time. Use Pacific time like `2026-10-24 09:00` or `2026-10-24 9:00am`.")
        if at < time.time() + 60:
            raise PanelError("That time has already passed (times are Pacific).")
        token = (values.get("channel") or "").strip()
        if token:
            channel = self._find_channel(guild, token)
        else:
            cid = (await conf.poem_targets()).get(pid)
            channel = self._lookup(guild, cid) if cid else None
            if channel is None:
                raise PanelError("Tell me which channel gets the poem (its name, a #mention or an ID).")
        if puzzle.get("kind") == "trail":
            self._check_trail_size(pid, await conf.trail())
        else:
            if len(await conf.pool()) < POOL_MIN:
                raise PanelError("Set the pool first (Hunt tab).")
            self._check_pool_size(pid, await conf.pool())
        async with conf.launches() as launches:
            launches[pid] = {"at": at, "channel": channel.id, "by": member.id, "state": "pending", "note": "", "warned": False}
        async with conf.poem_targets() as targets:
            targets[pid] = channel.id
        text = (f"{title} will launch <t:{int(at)}:F> (<t:{int(at)}:R>): it goes live and its poem is posted and pinned in "
                f"{channel.mention}.")
        problems = await self._launch_problems(guild, pid)
        if problems:
            text += "\nNot ready yet: " + "; ".join(problems) + ". I will DM you an hour before if it still isn't."
        return text

    async def _tell_scheduler(self, guild, item: dict, text: str) -> None:
        member = guild.get_member(item.get("by") or 0)
        if member is not None:
            await self._dm(member, text)

    async def _run_launches(self, guild, now: float) -> None:
        """Called every tick: warn an hour ahead if a scheduled launch is not ready, and run the ones that are due."""
        conf = self.config.guild(guild)
        for pid, item in (await conf.launches()).items():
            if item.get("state") != "pending" or pid not in PUZZLES:
                continue
            title = PUZZLES[pid]["title"]
            if item["at"] > now:
                if item["at"] - now <= LAUNCH_WARN_SECONDS and not item.get("warned"):
                    problems = await self._launch_problems(guild, pid)
                    async with conf.launches() as launches:
                        if pid in launches:
                            launches[pid]["warned"] = True
                    if problems:
                        await self._tell_scheduler(guild, item, f"{title} launches {self._when(item['at'])} but is not ready: "
                                                   + "; ".join(problems) + ".")
                continue
            async with self._lock(guild.id):  # mark first, run after: a second pass can never launch twice
                async with conf.launches() as launches:
                    if launches.get(pid, {}).get("state") != "pending":
                        continue
                    launches[pid]["state"] = "running"
            try:
                ok, note = await self._execute_launch(guild, pid, item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("secrethunt scheduled launch of %s failed", pid)
                ok, note = False, "something went wrong (it is in the bot log)"
            async with conf.launches() as launches:
                if pid in launches:
                    launches[pid]["state"], launches[pid]["note"] = ("done" if ok else "failed"), note
            await self._tell_scheduler(guild, item, f"{title}: " + note)

    async def _execute_launch(self, guild, pid: str, item: dict):
        problems = await self._launch_problems(guild, pid)
        channel = self._lookup(guild, item["channel"])
        if channel is None:
            problems.append("the poem channel is gone")
        if problems:
            return False, "NOT launched: " + "; ".join(problems) + ". Fix it and schedule again (or go live by hand)."
        try:
            await self._apply_start(guild, None, pid)
        except PanelError as err:
            return False, f"NOT launched: {err}"
        try:
            poem = await self._apply_poem(guild, None, channel, pid)
        except PanelError as err:
            return False, f"it is live, but the poem has a problem: {err}"
        return True, f"launched on schedule. {poem}"

    async def _apply_enabled(self, guild, member, on: bool) -> str:
        conf = self.config.guild(guild)
        if not on:
            await conf.enabled.set(False)
            return "The hunt is off. Nobody's progress was lost."
        problems = []
        if len(await conf.pool()) < POOL_MIN:
            problems.append("no channel pool")
        if not await conf.published():
            problems.append("no puzzle started")
        if problems:
            raise PanelError("Not yet: " + "; ".join(problems) + ".")
        await conf.enabled.set(True)
        return "The hunt is on."

    async def _apply_test_mode(self, guild, member, on: bool) -> str:
        conf = self.config.guild(guild)
        was = await conf.test_mode()
        await conf.test_mode.set(bool(on))
        if on:
            box = self._lookup(guild, await conf.test_channel_id() or 0)
            return ("Test mode on: waits are 30 seconds. " + (
                f"Set up objects and the poem now post in {box.mention}, labelled, and touch no real room."
                if box else "Set a Test channel (Hunt tab) to rehearse privately; until then setup still posts for real."))
        if not was:
            return "Test mode off."
        return await self._launch_rehearsed(guild, member)

    async def _apply_hall(self, guild, member, channel) -> str:
        """Set (or clear) the Hall of Secrets channel. Each puzzle gets one post there listing its solvers."""
        conf = self.config.guild(guild)
        await conf.hall_channel_id.set(channel.id if channel else None)
        await conf.hall_intro.set(0)
        await conf.hall_messages.set({})  # the old posts live in the old channel; new ones are written on demand
        return (f"Hall of Secrets: {channel.mention}. Use Refresh Hall posts to write them." if channel
                else "Hall of Secrets cleared.")

    async def _apply_announce(self, guild, member, channel) -> str:
        """Set (or clear) the channel that gets a one-line "X has solved <puzzle>". It never pings."""
        await self.config.guild(guild).announce_channel_id.set(channel.id if channel else None)
        return f"Solves will be announced in {channel.mention}." if channel else "Solve announcements are off."

    async def _apply_hallrefresh(self, guild, member, _value=None) -> str:
        """Rewrite every puzzle's Hall of Secrets post from the saved solver lists (after a deleted post, say)."""
        await self._refresh_intro(guild, create=True)
        done = 0
        for pid in (await self.config.guild(guild).published()):  # release order
            if pid in PUZZLES:
                await self._refresh_hall(guild, pid, create=True)
                done += 1
        return f"Refreshed {done} Hall post{'s' if done != 1 else ''}."

    async def _apply_preview(self, guild, member, puzzle_id: str) -> str:
        """DM the admin every variant's clues and answer (spoilers, so it never goes in a channel)."""
        puzzle = PUZZLES.get(puzzle_id)
        if puzzle is not None and puzzle.get("kind") == "trail":
            return await self._preview_trail(guild, member, puzzle)
        pool = await self.config.guild(guild).pool()
        if puzzle is None or len(pool) < POOL_MIN:
            raise PanelError("Unknown puzzle, or the pool is not set yet.")
        if puzzle.get("kind") == "trial":
            return await self._preview_trial(guild, member, puzzle, pool)
        lines = [f"**{puzzle['title']}** trigger word: `{puzzle['trigger']}`"]
        for v in range(len(pool)):
            channel, hint = self._hint_for(guild, puzzle, pool, v)
            answer = puzzle["answers"][v % len(puzzle["answers"])]
            if "pointers" in puzzle:
                pointer = engine.pointer_for(puzzle, channel.name, v) if channel is not None else None
                button = puzzle["buttons"][v % len(puzzle["buttons"])]
                rebus = puzzle["rebuses"][v % len(puzzle["rebuses"])]
                lines.append(f"Variant {v + 1}: <#{pool[v]}>, pointer `{pointer}`, button `{button}`, "
                             f"rebus {rebus}, answer `{answer}`")
                continue
            hint = hint[0] if hint else None
            jumbled = engine.scramble(answer, f"{puzzle['id']}:{v}:final")
            lines.append(f"Variant {v + 1}: <#{pool[v]}>, scrambled title `{hint}`, answer `{answer}`, jumbled `{jumbled}`")
        if not await self._dm(member, "\n".join(lines)):
            raise PanelError("I could not DM you.")
        return "Sent you a DM with every variant and its answer."

    async def _preview_trail(self, guild, member, puzzle: dict) -> str:
        """DM the admin their OWN trail: the poem's emoji and the three sisters, with the clue that names each room.
        Check every pointer here: a room whose title matches no keyword gets the letter clue instead."""
        rooms = await self.config.guild(guild).trail()
        self._check_trail_size(puzzle["id"], rooms)
        plan = trail.plan(trail.seed(await self._salt(guild), puzzle["id"], member.id), len(rooms))
        lines = [f"**{puzzle['title']}** (YOUR trail; every member's differs). Stage 1: react to the poem with "
                 + " then ".join(" or ".join(group) for group in trail.EMOJI_SLOTS) + "."]
        for k, name in enumerate(trail.SISTERS):
            channel = self._lookup(guild, rooms[plan["chain"][k]])
            where = channel.mention if channel is not None else f"MISSING `{rooms[plan['chain'][k]]}`"
            pointer = self._pointer(guild, puzzle, rooms, plan, k)
            lines.append(f"{k + 1}. {name} is in {where}; the clue to reach her: {pointer or 'NONE (room missing)'}; "
                         f"her number {plan['numbers'][k]}, her word `{plan['tokens'][k]}`")
        pour = await self._room_pointer(guild, puzzle, rooms, plan["pour_room"], 3)
        toast = await self._room_pointer(guild, puzzle, rooms, plan["toast_room"], 4)
        words, yes = trail.make_test(plan["seed"], 0, tuple(plan["rule"]))
        lines.append(f"Stage 3: the Dormouse can draw words that {trail.rule_text(tuple(plan['rule']))}. Your first test is "
                     f"{', '.join(words)} (he can draw: {', '.join(yes)}); his reward word is `{plan['extra_tokens'][0]}`.")
        lines.append(f"Stage 4: pour {plan['pour_total']} times in <#{rooms[plan['pour_room']]}> (clue: {pour}), then Toast "
                     f"in <#{rooms[plan['toast_room']]}> (clue: {toast}); his reward word is `{plan['extra_tokens'][1]}`.")
        courtesies = "; ".join(
            f"{k + 1}. {plan['courtesy_buttons'][k].title()} in <#{rooms[plan['courtesy_rooms'][k]]}>" for k in range(trail.COURTESIES))
        lines.append(f"Stage 5: {courtesies}; his reward word is `{plan['extra_tokens'][2]}`.")
        words = plan["tokens"] + plan["extra_tokens"]
        lines.append(f"Stage 6: the tale goes {trail.tale_text(plan)}; the six words are {', '.join(f'`{w}`' for w in words)}; "
                     f"the answer is `{plan['answer']}`.")
        if not await self._dm(member, "\n".join(lines)):
            raise PanelError("I could not DM you.")
        return "Sent you a DM with your own trail."

    async def _preview_trial(self, guild, member, puzzle: dict, pool) -> str:
        """DM the admin their OWN cases for both rounds, with the answers (spoilers: every member's cases differ)."""
        self._check_pool_size(puzzle["id"], pool)
        salt, rooms = await self._salt(guild), self._rooms(pool)
        intro = (f"**{puzzle['title']}** trigger: `{puzzle['trigger']}` (the rhyme-breakers, in order). "
                 "These are YOUR cases; every member's are different. Re-rolls after a wrong answer are different again.")
        if not await self._dm(member, intro):
            raise PanelError("I could not DM you.")
        for round_no in (1, 2):
            seed = trial.case_seed(salt, puzzle["id"], member.id, round_no, 0)
            statements, thief, hour, liars = await asyncio.to_thread(trial.generate, seed, round_no)
            lines = [f"**Round {round_no}** (exactly {trial.LIARS[round_no]} lie)"]
            lines += [f"{rooms[w]}: {trial.testimony(statements[w], rooms)}" for w in range(trial.WITNESSES)]
            answer = trial.SUSPECTS[thief] + (f" {hour}" if round_no == 2 else "")
            lines.append(f"Answer: `{answer}`. Liars: " + ", ".join(rooms[i] for i in liars))
            if not await self._dm(member, "\n".join(lines)):
                raise PanelError("I could not DM you.")
        return "Sent you a DM with your own round 1 and round 2 cases and their answers."

    async def _tier_role(self, ctx, tier: str):
        """Validate a tier name for the role commands; returns (tier, role or None) or None after replying."""
        tier = (tier or "").lower()
        if tier in NO_ROLE_TIERS:
            await ctx.send(f"{tier.title()} has no role or badge by design: its solvers are only listed in the Hall of Secrets.")
            return None
        if tier not in TIER_ROLE_NAMES:
            await ctx.send("Tier must be one of: " + ", ".join(TIER_ROLE_NAMES))
            return None
        roles = await self.config.guild(ctx.guild).roles()
        return tier, ctx.guild.get_role(roles.get(tier) or 0)

    async def _apply_tier_role(self, guild, member, tier: str, role) -> str:
        """Set (or clear, with None) a tier's reward role: medium, hard or expert. Easy never has one."""
        tier = (tier or "").lower()
        if tier in NO_ROLE_TIERS:
            raise PanelError(f"{tier.title()} has no role or badge by design: its solvers are only listed in the Hall of Secrets.")
        if tier not in TIER_ROLE_NAMES:
            raise PanelError("Tier must be one of: " + ", ".join(TIER_ROLE_NAMES))
        async with self.config.guild(guild).roles() as roles:
            if role is None:
                roles.pop(tier, None)
                return f"{tier.title()} no longer has a reward role."
            roles[tier] = role.id
        return (f"{tier.title()} reward role: {role.mention}. Keep it below my own top role so I can hand it out.")

    async def _apply_create_tier_roles(self, guild, member, _value=None) -> str:
        """Create the default hoisted role for every tier that has none yet."""
        made = []
        for tier, name in TIER_ROLE_NAMES.items():
            if guild.get_role((await self.config.guild(guild).roles()).get(tier) or 0) is not None:
                continue
            try:
                role = await guild.create_role(name=name, hoist=True, mentionable=False, reason="Secret hunt reward role")
            except _DISCORD_ERRORS as err:
                raise PanelError(f"I could not create the role ({type(err).__name__}); I need Manage Roles.")
            async with self.config.guild(guild).roles() as roles:
                roles[tier] = role.id
            made.append(role.mention)
        return ("Created " + ", ".join(made) + ". Drag them where you want them and keep them below my top role."
                if made else "Every tier already has a role.")

    async def _apply_puzzle_role(self, guild, member, puzzle_id: str, role) -> str:
        """Give one puzzle its own badge role (Hard and up); role None removes it."""
        puzzle = PUZZLES.get(puzzle_id)
        if puzzle is None:
            raise PanelError("Unknown puzzle. Known: " + ", ".join(PUZZLES))
        if puzzle["tier"] in NO_ROLE_TIERS:
            raise PanelError(f"{puzzle['tier'].title()} puzzles have no role or badge by design.")
        async with self.config.guild(guild).puzzle_roles() as roles:
            if role is None:
                roles.pop(puzzle_id, None)
                return f"{puzzle['title']} no longer awards a badge role."
            roles[puzzle_id] = role.id
        return (f"{puzzle['title']} now awards {role.mention} to everyone who solves it from here on. "
                "Keep it below my top role. Earlier solvers do not get it retroactively.")

    async def _apply_create_puzzle_role(self, guild, member, puzzle_id: str) -> str:
        puzzle = PUZZLES.get(puzzle_id)
        if puzzle is None or puzzle["tier"] in NO_ROLE_TIERS:
            raise PanelError("Only Hard and Expert puzzles have a badge role.")
        try:
            role = await guild.create_role(name=puzzle["title"], hoist=False, mentionable=False,
                                           reason="Secret hunt puzzle role")
        except _DISCORD_ERRORS as err:
            raise PanelError(f"I could not create the role ({type(err).__name__}); I need Manage Roles.")
        return await self._apply_puzzle_role(guild, member, puzzle_id, role)

    @secrethunt.command(name="roleicon")
    async def secrethunt_roleicon(self, ctx, target: str):
        """Attach an image (under 256 KB) as a role's icon. `target` is a tier or a puzzle id (needs boost level 2+)."""
        key = (target or "").lower()
        if key in PUZZLES:
            if PUZZLES[key]["tier"] in NO_ROLE_TIERS:
                return await ctx.send(f"{PUZZLES[key]['tier'].title()} puzzles have no role or badge by design.")
            role = ctx.guild.get_role((await self.config.guild(ctx.guild).puzzle_roles()).get(key) or 0)
            hint = "`.secrethunt` (Roles tab)"
        else:
            checked = await self._tier_role(ctx, key)
            if checked is None:
                return
            role, hint = checked[1], "`.secrethunt` (Roles tab)"
        if role is None:
            return await ctx.send(f"Set the role first in {hint}.")
        if not ctx.message.attachments:
            return await ctx.send("Attach a PNG or JPG with the command.")
        att = ctx.message.attachments[0]
        if att.size > ICON_MAX_BYTES:
            return await ctx.send("That image is over 256 KB; Discord rejects bigger role icons.")
        try:
            await role.edit(display_icon=await att.read(), reason="Secret hunt role icon")
        except _DISCORD_ERRORS as err:
            return await ctx.send(f"Discord refused it ({type(err).__name__}). Needs boost level 2 or more, "
                                  "Manage Roles, and a PNG/JPG.")
        await ctx.send("Icon set.")

    @secrethunt.command(name="who")
    async def secrethunt_who(self, ctx, member: discord.Member):
        """Where a member is in each puzzle (so you can answer 'I'm stuck' without guessing)."""
        states = await self.config.member(member).puzzles()
        if not states:
            return await ctx.send(f"{member.display_name} has not started anything.")
        names = {engine.STAGE_RIDDLE: "riddle sent", engine.STAGE_FOUND: "found the object, waiting",
                 engine.STAGE_FINAL: "has the jumbled word", engine.STAGE_SOLVED: "solved"}
        trial_names = {engine.STAGE_RIDDLE: "round 1 open", engine.STAGE_FOUND: "round 1 solved, court adjourned",
                       engine.STAGE_FINAL: "round 2 open", engine.STAGE_SOLVED: "solved"}
        pool = await self.config.guild(ctx.guild).pool()
        lines = []
        for pid, st in states.items():
            if PUZZLES.get(pid, {}).get("kind") == "trail":
                lines.append(f"**{pid}**: " + ("solved" if st.get("stage") == engine.STAGE_SOLVED else f"stage {st.get('step', 2)}")
                             + f"; sisters found {st.get('found', 0)}/3; wrong tables in a row {st.get('wrong_looks', 0)}; "
                             f"failed word tests {st.get('test_roll', 0)}; pours {st.get('pours', 0)}; "
                             f"courtesies paid {st.get('courtesy', 0)}/4; wrong answers {st.get('answer_wrongs', 0)}; "
                             f"hints {st.get('hints', 0)}" + ("; a DM to them bounced (closed DMs?)" if st.get("dm_pending") else ""))
                rows = await self.config.member(member).presses()
                if rows:
                    lines.append("recent tea table presses: " + "; ".join(
                        f"<t:{int(t)}:t> <#{room}> {key} \N{RIGHTWARDS ARROW} {what}" for t, room, key, what in rows[-6:]))
                continue
            if PUZZLES.get(pid, {}).get("kind") == "trial":
                lines.append(f"**{pid}**: {trial_names.get(st['stage'], st['stage'])}; re-rolls {st.get('roll', 0)}, "
                             f"wrong answers {st.get('wrongs', 0)}, hints {st.get('hints', 0)}")
                continue
            where = f"<#{pool[st['variant'] % len(pool)]}>" if pool else "?"
            lines.append(f"**{pid}**: {names.get(st['stage'], st['stage'])}; variant {st['variant'] + 1} ({where}); "
                         f"wrong guesses today {st['wrong_count']}")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @secrethunt.command(name="preview")
    async def secrethunt_preview(self, ctx, puzzle_id: str = "late_rabbit"):
        """DM you every variant's clues and answer (spoilers, so it never goes in a channel)."""
        try:
            await self._apply_preview(ctx.guild, ctx.author, puzzle_id)
        except PanelError as err:
            return await ctx.send(str(err))
        await ctx.tick()

    @secrethunt.command(name="reset")
    async def secrethunt_reset(self, ctx, member: discord.Member, puzzle_id: str = "late_rabbit"):
        """Wipe one member's progress on a puzzle (they can start it again)."""
        async with self._lock(ctx.guild.id):
            async with self.config.member(member).puzzles() as states:
                existed = states.pop(puzzle_id, None) is not None
            async with self.config.guild(ctx.guild).finishers() as fin:
                if member.id in fin.get(puzzle_id, []):
                    fin[puzzle_id].remove(member.id)
        if puzzle_id in PUZZLES:
            role = ctx.guild.get_role((await self.config.guild(ctx.guild).puzzle_roles()).get(puzzle_id) or 0)
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Secret hunt progress reset")
                except _DISCORD_ERRORS:
                    pass
            await self._refresh_hall(ctx.guild, puzzle_id)
        await ctx.send("Reset." if existed else "They had no progress on that puzzle.")

    # ------------------------------------------------------------------ members

    @commands.command(name="secrets")
    @commands.guild_only()
    async def secrets_cmd(self, ctx, member: discord.Member = None):
        """The secrets a member has found. Only solved ones are shown, nothing about hunts in progress."""
        member = member or ctx.author
        states = await self.config.member(member).puzzles()
        found = [(pid, st) for pid, st in states.items()
                 if pid in PUZZLES and st.get("stage") == engine.STAGE_SOLVED]
        order = {pid: i for i, pid in enumerate(await self.config.guild(ctx.guild).published())}
        found.sort(key=lambda item: order.get(item[0], len(order)))
        if not found:
            return await ctx.send(f"{engine.escape_markdown(member.display_name)} has not found any secrets yet.")
        lines = [f"**{PUZZLES[pid]['tier'].title()}** \N{MIDDLE DOT} {PUZZLES[pid]['title']}"
                 + (f" \N{MIDDLE DOT} finder #{st['rank']}" if st.get("rank") else "")
                 + (f" \N{MIDDLE DOT} {trial.hint_label(st.get('hints', 0))}"
                    if PUZZLES[pid].get("kind") == "trial" else "")
                 + (f" \N{MIDDLE DOT} {self._trail_note(st)}" if PUZZLES[pid].get("kind") == "trail" else "")
                 for pid, st in found]
        embed = discord.Embed(title=f"Secrets found by {member.display_name}", description="\n".join(lines),
                              color=discord.Color.gold())
        embed.set_footer(text=f"{len(found)} secret{'' if len(found) == 1 else 's'} found")
        await ctx.send(embed=embed)
