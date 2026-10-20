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
import time
from collections import deque
from typing import Union

import discord
from redbot.core import Config, commands

from . import embeds, engine, trail, trial, views
from . import panel as panel_specs
from .settings_panel import PanelError, open_panel
from .constants import (
    COG_VERSION, CONFIG_IDENTIFIER, DEFAULT_GUILD, DEFAULT_MEMBER, HINT_MAX, ICON_MAX_BYTES, NO_ROLE_TIERS, POOL_MAX,
    POOL_MIN, TICK_SECONDS, TIER_ROLE_NAMES, TRAIL_MAX_ROOMS, WATCH_EMOJI,
)
from .puzzles import PUZZLES

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
        self._task = self.bot.loop.create_task(self._loop())

    async def cog_load(self):
        self._view = views.ObjectView(self)  # persistent: the buttons keep working across restarts
        self.bot.add_view(self._view)
        self._witness_view = views.WitnessView(self)
        self.bot.add_view(self._witness_view)
        self._trail_view = views.TrailView(self)
        self.bot.add_view(self._trail_view)

    def cog_unload(self):
        self._task.cancel()
        for name in ("_view", "_witness_view", "_trail_view"):
            view = getattr(self, name, None)
            if view is not None:
                view.stop()

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
                    f"{message.author.mention} I could not reach your DMs. Open them and try once more.",
                    delete_after=15, allowed_mentions=discord.AllowedMentions(users=[message.author]),
                )
            except _DISCORD_ERRORS:
                pass

    async def _on_dm(self, message):
        text = (message.content or "").strip()
        if not text or len(text) > 60:
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
        hit = engine.find_object(await conf.objects(), payload.message_id)
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
        await self._dm(member, state["clue"])

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
                await self._dm(member, state.get("clue") or puzzle["idle"])
                return True
        return False

    async def handle_trail(self, interaction, key: str):
        """A button under a tea table was pressed. The reply is private, so a wrong table looks like any other."""
        try:
            text = await self._trail_text(interaction, key)
        except Exception:
            log.exception("secrethunt tea table click failed")
            text = None
        try:
            await interaction.response.send_message(text or "Nothing happens.", ephemeral=True)
        except _DISCORD_ERRORS:
            pass

    async def _trail_text(self, interaction, key: str):
        guild, member = interaction.guild, interaction.user
        message = getattr(interaction, "message", None)
        if guild is None or message is None or getattr(member, "bot", False):
            return None
        conf = self.config.guild(guild)
        if not await conf.enabled() or await self._blocked(guild, member):
            return None
        hit = engine.find_object(await conf.objects(), message.id)
        puzzle = PUZZLES.get(hit[0]) if hit else None
        rooms = await conf.trail()
        if puzzle is None or puzzle.get("kind") != "trail" or hit[1] not in rooms or not self._can_play(member, puzzle):
            return None
        pid, room = hit[0], rooms.index(hit[1])
        if key != "look":
            return puzzle["quiet"]  # Pour, Bow and Toast arrive with the later stages
        now, test_mode, salt = time.time(), await conf.test_mode(), await self._salt(guild)
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not state:
                    return puzzle["idle"]
                if len(rooms) < len(trail.SISTERS):
                    return None
                plan = trail.plan(trail.seed(salt, pid, member.id), len(rooms))
                action, detail = trail.look(state, room, plan, now, test_mode)
                if action == "sister":
                    nxt = self._pointer(guild, puzzle, rooms, plan, detail + 1) if detail + 1 < len(trail.SISTERS) else None
                    text = trail.sister_line(puzzle, detail, plan, nxt)
                    state["clue"] = text if detail + 1 < len(trail.SISTERS) else puzzle["sisters_done_dm"]
                    return text
                if action == "replay":
                    nxt = self._pointer(guild, puzzle, rooms, plan, detail + 1) if detail + 1 < len(trail.SISTERS) else None
                    return trail.sister_line(puzzle, detail, plan, nxt)
        if action == "rest":
            return puzzle["rest"].format(until=self._when(detail))
        if action == "rest_start":
            return puzzle["rest_start"].format(until=self._when(detail))
        if action == "wrong":
            return puzzle["wrong_look"][state["wrong_looks"] % len(puzzle["wrong_look"])]
        return puzzle["idle"]

    async def handle_click(self, interaction, key: str):
        """A button under a hunt object was pressed. Whatever happens, the answer is private to the clicker, so a decoy
        looks exactly like the real thing and nobody can watch someone else solve."""
        try:
            text = await self._click_text(interaction, key)
        except Exception:
            log.exception("secrethunt button click failed")
            text = None
        try:
            await interaction.response.send_message(text or "Nothing happens.", ephemeral=True)
        except _DISCORD_ERRORS:
            pass

    async def _click_text(self, interaction, key: str):
        guild, member = interaction.guild, interaction.user
        message = getattr(interaction, "message", None)
        if guild is None or message is None or getattr(member, "bot", False):
            return None
        conf = self.config.guild(guild)
        if not await conf.enabled() or await self._blocked(guild, member):
            return None
        hit = engine.find_object(await conf.objects(), message.id)
        puzzle = PUZZLES.get(hit[0]) if hit else None
        pool = await conf.pool()
        if puzzle is None or puzzle.get("kind") != "button" or len(pool) < POOL_MIN:
            return None
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
        try:
            text = await self._witness_text(interaction)
        except Exception:
            log.exception("secrethunt witness button failed")
            text = None
        try:
            await interaction.response.send_message(text or "Nothing happens.", ephemeral=True)
        except _DISCORD_ERRORS:
            pass

    async def _witness_text(self, interaction):
        guild, member = interaction.guild, interaction.user
        message = getattr(interaction, "message", None)
        if guild is None or message is None or getattr(member, "bot", False):
            return None
        conf = self.config.guild(guild)
        if not await conf.enabled() or await self._blocked(guild, member):
            return None
        hit = engine.find_object(await conf.objects(), message.id)
        puzzle = PUZZLES.get(hit[0]) if hit else None
        pool = await conf.pool()
        if puzzle is None or puzzle.get("kind") != "trial" or len(pool) != puzzle["needs_pool"] or hit[1] not in pool:
            return None
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

    async def _setup_objects(self, guild, pid: str, force: bool = False) -> list[str]:
        """Post and pin the hunt object in every pool channel (once per puzzle unless forced). Returns report lines."""
        puzzle, conf = PUZZLES[pid], self.config.guild(guild)
        report = []
        rooms = await self._rooms_of(guild, puzzle)
        async with conf.objects() as objects:
            by_channel = objects.setdefault(pid, {})
            for cid in rooms:
                channel = self._lookup(guild, cid)
                if channel is None:
                    report.append(f"- <#{cid}>: not found (was the post deleted?)")
                    continue
                if str(cid) in by_channel and not force:
                    report.append(f"- {channel.mention}: already set up")
                    continue
                if getattr(channel, "archived", False):  # a quiet forum post may have auto-archived
                    try:
                        await channel.edit(archived=False)
                    except _DISCORD_ERRORS:
                        pass
                try:
                    if puzzle.get("kind") == "button":
                        msg = await channel.send(embed=embeds.object_embed(puzzle), view=views.ObjectView(self))
                    elif puzzle.get("kind") == "trial":
                        msg = await channel.send(embed=embeds.witness_embed(puzzle), view=views.WitnessView(self))
                    elif puzzle.get("kind") == "trail":
                        msg = await channel.send(embed=embeds.object_embed(puzzle), view=views.TrailView(self))
                    else:
                        msg = await channel.send(embed=embeds.object_embed(puzzle))
                    await msg.pin()
                except _DISCORD_ERRORS as err:
                    report.append(f"- {channel.mention}: failed ({type(err).__name__}); needs Send Messages (in Threads), "
                                  "Embed Links and Manage Messages / Pin Messages there")
                    continue
                by_channel[str(cid)] = msg.id
                await self._delete_pin_notice(channel)
                report.append(f"- {channel.mention}: object pinned")
        return report

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
            "Test mode": "on (clues arrive in 30 seconds)" if await conf.test_mode() else "off",
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
        return ("Pool set: " + ", ".join(c.mention for c in channels)
                + ". Now set up each puzzle's objects under Puzzles. Changing the pool mid-puzzle moves "
                  "people's hiding places, so do it between puzzles.")

    async def _apply_trail_text(self, guild, member, values: dict) -> str:
        """The Treacle Well's trail rooms from pasted mentions or ids (forum posts are threads)."""
        found, bad = [], []
        for token in (values.get("rooms") or "").replace(",", " ").split():
            digits = token[2:-1] if token.startswith("<#") and token.endswith(">") else token
            channel = self._lookup(guild, int(digits)) if digits.isdigit() else None
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
        async with conf.objects() as objects:  # the old tea tables no longer match the rooms
            for pid, puzzle in PUZZLES.items():
                if puzzle.get("kind") == "trail":
                    objects.pop(pid, None)
        return (f"Trail rooms set ({len(found)}). Now press Set up objects on the Treacle Well. Changing them mid-puzzle "
                "moves people's sisters, so do it between puzzles.")

    async def _apply_blocked_role(self, guild, member, role) -> str:
        """Members with this role (Lurker) are ignored by every puzzle; their progress just waits."""
        await self.config.guild(guild).blocked_role_id.set(role.id if role else None)
        return (f"Members with {role.mention} cannot take part in any secret." if role
                else "Every member can take part again.")

    async def _apply_pool_text(self, guild, member, values: dict) -> str:
        """The pool from pasted mentions or ids (forum posts are threads, which a dropdown may not offer)."""
        found, bad = [], []
        for token in (values.get("channels") or "").replace(",", " ").split():
            digits = token[2:-1] if token.startswith("<#") and token.endswith(">") else token
            channel = self._lookup(guild, int(digits)) if digits.isdigit() else None
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
        return "\n".join(await self._setup_objects(guild, puzzle_id, force))

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
        await self.config.guild(guild).test_mode.set(bool(on))
        return f"Test mode {'on: the morning wait is 30 seconds' if on else 'off'}."

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
                lines.append(f"**{pid}**: stage {st.get('step', 2)}; sisters found {st.get('found', 0)}/3; "
                             f"wrong tables in a row {st.get('wrong_looks', 0)}")
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
                    if PUZZLES[pid].get("kind") == "trial" else "") for pid, st in found]
        embed = discord.Embed(title=f"Secrets found by {member.display_name}", description="\n".join(lines),
                              color=discord.Color.gold())
        embed.set_footer(text=f"{len(found)} secret{'' if len(found) == 1 else 's'} found")
        await ctx.send(embed=embed)
