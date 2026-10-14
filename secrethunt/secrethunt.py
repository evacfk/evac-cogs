"""Secrets of Wonderland: WoW-style hidden puzzles, one new easy one a week.

How a hunt runs (see puzzles.py for the content of each puzzle):
- a public poem's first letters spell a trigger word; saying it (`.tick` in chat, deleted at once, or just `tick` in
  a DM to the bot) starts a personal chain;
- each member's variant is a private salted hash of their user id, so nobody's answer helps anyone else;
- clue 1 arrives by DM; the object it describes is a pinned message in the member's own channel or forum post,
  "clicked" by reacting with the watch emoji (a reaction on any other object is a decoy);
- the next clue is delayed to the next 9:00 Pacific, then the jumbled answer is DM'd and the member DMs the answer back;
- solving lists them in the Hall of Secrets (one post per puzzle, edited in place) and grants the puzzle tier's role
  when that tier has one. Easy never does.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Union

import discord
from redbot.core import Config, commands

from . import embeds, engine
from .constants import (
    COG_VERSION, CONFIG_IDENTIFIER, DEFAULT_GUILD, DEFAULT_MEMBER, ICON_MAX_BYTES, POOL_MAX, POOL_MIN,
    TICK_SECONDS, TIER_ROLE_NAMES, WATCH_EMOJI,
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
        self._task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        self._task.cancel()

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
        if channel is not None and hint:
            return puzzle["riddle"].format(hint=hint[0], shape=hint[1])
        return puzzle["riddle_no_hint"].format(channel=channel.mention if channel else "a room I cannot name")

    def _final_text(self, puzzle, variant) -> str:
        answer = puzzle["answers"][variant % len(puzzle["answers"])]
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
        if not content or len(content) > 40 or " " in content:
            return
        guild = message.guild
        conf = self.config.guild(guild)
        if not await conf.enabled():
            return
        low = content.lower()
        word = next((low[len(p):] for p in await self._prefixes(guild) if p and low.startswith(p.lower())), None)
        pid = engine.match_trigger(PUZZLES, await conf.published(), word or "")
        if pid is None:
            return
        try:
            await message.delete()  # never advertise the word in chat
        except _DISCORD_ERRORS:
            pass
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
            if not await conf.enabled():
                continue
            pid = engine.match_trigger(PUZZLES, await conf.published(), low)
            if pid is not None:
                await self._run_trigger(guild, member, pid, now)
                return
            if await self._try_guess(guild, member, low, now):
                return

    async def _run_trigger(self, guild, member, pid: str, now: float) -> bool:
        """Advance/replay the member's chain for this puzzle and DM the right text. False if the DM failed."""
        puzzle = PUZZLES[pid]
        conf = self.config.guild(guild)
        pool = await conf.pool()
        if len(pool) < POOL_MIN:
            return True  # not configured yet: stay silent
        salt = await self._salt(guild)
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                state, action = engine.trigger(
                    states.get(pid), engine.variant_for(salt, pid, member.id, len(pool)), now)
                states[pid] = state
        variant = state["variant"]
        if action in ("start", "riddle_again"):
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

    async def _on_reaction(self, payload):
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", None):
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
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        await self._remove_reaction(guild, payload, member)  # the object always looks untouched
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
        for uid, data in (await self.config.all_members(guild)).items():
            for pid, state in data["puzzles"].items():
                if pid in PUZZLES and engine.is_due(state, now):
                    await self._send_final(guild, uid, pid, now)

    async def _send_final(self, guild, uid: int, pid: str, now: float):
        member = guild.get_member(uid)
        if member is None:
            return
        async with self._lock(guild.id):  # mark first, send after: a second pass can never double-send
            async with self.config.member(member).puzzles() as states:
                state = states.get(pid)
                if not engine.is_due(state, now):
                    return
                engine.mark_final_sent(state, now)
                variant = state["variant"]
        if await self._dm(member, self._final_text(PUZZLES[pid], variant)):
            return
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                if states.get(pid, {}).get("stage") == engine.STAGE_FINAL:
                    engine.dm_failed(states[pid], now)

    # ------------------------------------------------------------------ the answer (DM)

    async def _try_guess(self, guild, member, text: str, now: float) -> bool:
        """True when this DM was handled as a guess for a puzzle at the final stage."""
        conf = self.config.guild(guild)
        reply, solved = None, None
        async with self._lock(guild.id):
            async with self.config.member(member).puzzles() as states:
                for pid in await conf.published():
                    puzzle, state = PUZZLES.get(pid), states.get(pid)
                    if puzzle is None or not state or state["stage"] != engine.STAGE_FINAL:
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

    async def _on_solved(self, guild, member, pid: str, rank: int):
        puzzle = PUZZLES[pid]
        earned = await self._grant_roles(guild, member, pid)
        text = puzzle["solved"].format(rank=f"\n\nYou were finder **#{rank}**.")
        if earned:
            text += "\n\nYou earned the " + " and ".join(f"**{r.name}**" for r in earned) + (
                " roles." if len(earned) > 1 else " role.")
        await self._dm(member, text)
        await self._refresh_hall(guild, pid)

    async def _grant_roles(self, guild, member, pid: str) -> list:
        """The tier's role and this puzzle's own role, whichever are configured (Easy never has either)."""
        conf = self.config.guild(guild)
        tier = PUZZLES[pid]["tier"]
        if tier == "easy":
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
                    entries.append((rank, engine.escape_markdown(member.display_name)))
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
        async with conf.objects() as objects:
            by_channel = objects.setdefault(pid, {})
            for cid in await conf.pool():
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
        """Secret hunts: status and setup."""
        await ctx.send(embed=await self._status_embed(ctx.guild))

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
                    or "not set (`channels`)",
            "Roles": "easy: none (by design), " + ", ".join(tier_roles),
            "Puzzle roles": ", ".join(
                f"{PUZZLES[p]['title']}: {r.mention if r else 'MISSING'}"
                for p, rid in (await conf.puzzle_roles()).items() if p in PUZZLES
                for r in [guild.get_role(rid)]) or "none",
            "Hall of Secrets": hall.mention if hall else "not set (`hall`)",
        }
        for pid in await conf.published():
            started = sum(1 for d in members.values() if pid in d["puzzles"])
            lines[PUZZLES[pid]["title"] if pid in PUZZLES else pid] = (
                f"{started} started, {len(finishers.get(pid, []))} solved")
        return embeds.status_embed(COG_VERSION, lines)

    @secrethunt.command(name="channels")
    async def secrethunt_channels(self, ctx, *channels: Union[discord.TextChannel, discord.Thread]):
        """Set the 2-5 channels or forum posts hunt objects hide in (variant 1 = the first). Run `setup` after."""
        if not POOL_MIN <= len(channels) <= POOL_MAX or len({c.id for c in channels}) != len(channels):
            return await ctx.send(f"Give me {POOL_MIN} to {POOL_MAX} different channels or forum posts.")
        conf = self.config.guild(ctx.guild)
        await conf.pool.set([c.id for c in channels])
        await conf.objects.set({})  # the old objects no longer match the variants
        await ctx.send("Pool set: " + ", ".join(c.mention for c in channels)
                       + "\nRun `.secrethunt setup` to pin the objects. Changing the pool mid-puzzle moves "
                         "people's hiding places, so do it between puzzles.")

    @secrethunt.command(name="setup")
    async def secrethunt_setup(self, ctx, puzzle_id: str = "late_rabbit", force: bool = False):
        """Post and pin the puzzle's object in each pool channel (`setup <id> true` to redo them)."""
        if puzzle_id not in PUZZLES:
            return await ctx.send("Unknown puzzle. Known: " + ", ".join(PUZZLES))
        if len(await self.config.guild(ctx.guild).pool()) < POOL_MIN:
            return await ctx.send("Set the pool first: `.secrethunt channels #post1 #post2 #post3 #post4 #post5`")
        await ctx.send("\n".join(await self._setup_objects(ctx.guild, puzzle_id, force)))

    @secrethunt.command(name="start")
    async def secrethunt_start(self, ctx, puzzle_id: str):
        """Make a puzzle's trigger word live. Earlier puzzles stay live too."""
        if puzzle_id not in PUZZLES:
            return await ctx.send("Unknown puzzle. Known: " + ", ".join(PUZZLES))
        conf = self.config.guild(ctx.guild)
        async with conf.published() as published:
            if puzzle_id not in published:
                published.append(puzzle_id)
        await conf.active.set(puzzle_id)
        await self._refresh_intro(ctx.guild, create=True)
        await self._refresh_hall(ctx.guild, puzzle_id, create=True)  # its Hall post exists from day one, in release order
        await ctx.send(f"**{PUZZLES[puzzle_id]['title']}** is live (once the hunt is enabled).")

    @secrethunt.command(name="poem")
    async def secrethunt_poem(self, ctx, channel: Union[discord.TextChannel, discord.Thread] = None,
                              puzzle_id: str = ""):
        """Post the active puzzle's public poem (it hides the trigger word)."""
        conf = self.config.guild(ctx.guild)
        pid = puzzle_id or await conf.active()
        if pid not in PUZZLES:
            return await ctx.send("No puzzle is started. Use `.secrethunt start late_rabbit` first.")
        target = channel or ctx.channel
        try:
            posted = await target.send(embed=embeds.poem_embed(PUZZLES[pid]))
        except _DISCORD_ERRORS as err:
            return await ctx.send(f"I could not post there ({type(err).__name__}).")
        notes = []
        async with self.config.guild(ctx.guild).poems() as poems:
            old = poems.get(str(target.id))
            if old:  # the previous poem in this channel comes down as the new one goes up
                try:
                    await target.get_partial_message(old).unpin(reason="A new secret poem replaces it")
                except _DISCORD_ERRORS:
                    pass  # already unpinned or deleted
            try:
                await posted.pin(reason="Secret poem")
                poems[str(target.id)] = posted.id
            except _DISCORD_ERRORS as err:
                poems.pop(str(target.id), None)
                notes.append(f"Posted, but I could not pin it ({type(err).__name__}): I need Pin Messages here, "
                             "and a channel holds at most 50 pins.")
        await self._refresh_intro(ctx.guild)  # it names the channels the poem is pinned in
        if notes:
            return await ctx.send(" ".join(notes))
        if target.id != ctx.channel.id:
            await ctx.tick()

    @secrethunt.command(name="enable")
    async def secrethunt_enable(self, ctx):
        """Turn the hunt on."""
        conf = self.config.guild(ctx.guild)
        problems = []
        if len(await conf.pool()) < POOL_MIN:
            problems.append("no channel pool (`channels`)")
        if not await conf.published():
            problems.append("no puzzle started (`start`)")
        if problems:
            return await ctx.send("Not yet: " + "; ".join(problems))
        await conf.enabled.set(True)
        await ctx.send("The hunt is on.")

    @secrethunt.command(name="disable")
    async def secrethunt_disable(self, ctx):
        """Turn the hunt off (progress is kept)."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("The hunt is off. Nobody's progress was lost.")

    @secrethunt.command(name="testmode")
    async def secrethunt_testmode(self, ctx, on_off: bool):
        """In test mode the morning wait is 30 seconds, so you can run the whole chain yourself."""
        await self.config.guild(ctx.guild).test_mode.set(on_off)
        await ctx.send(f"Test mode {'on' if on_off else 'off'}.")

    @secrethunt.command(name="hall")
    async def secrethunt_hall(self, ctx, channel: Union[discord.TextChannel, discord.Thread] = None):
        """Set (or clear) the Hall of Secrets channel. Each puzzle gets one post there listing its solvers."""
        conf = self.config.guild(ctx.guild)
        await conf.hall_channel_id.set(channel.id if channel else None)
        await conf.hall_intro.set(0)
        await conf.hall_messages.set({})  # the old posts live in the old channel; new ones are written on demand
        await ctx.send(f"Hall of Secrets: {channel.mention}. Run `.secrethunt hallrefresh` to write the posts."
                       if channel else "Hall of Secrets cleared.")

    @secrethunt.command(name="hallrefresh")
    async def secrethunt_hallrefresh(self, ctx):
        """Rewrite every puzzle's Hall of Secrets post from the saved solver lists (after a deleted post, say)."""
        await self._refresh_intro(ctx.guild, create=True)
        done = 0
        for pid in (await self.config.guild(ctx.guild).published()):  # release order
            if pid in PUZZLES:
                await self._refresh_hall(ctx.guild, pid, create=True)
                done += 1
        await ctx.send(f"Refreshed {done} Hall post{'s' if done != 1 else ''}.")

    async def _tier_role(self, ctx, tier: str):
        """Validate a tier name for the role commands; returns (tier, role or None) or None after replying."""
        tier = (tier or "").lower()
        if tier == "easy":
            await ctx.send("Easy has no role or badge by design: its solvers are only listed in the Hall of Secrets.")
            return None
        if tier not in TIER_ROLE_NAMES:
            await ctx.send("Tier must be one of: " + ", ".join(TIER_ROLE_NAMES))
            return None
        roles = await self.config.guild(ctx.guild).roles()
        return tier, ctx.guild.get_role(roles.get(tier) or 0)

    @secrethunt.command(name="role")
    async def secrethunt_role(self, ctx, tier: str, role: discord.Role = None):
        """Set a tier's reward role (medium, hard or expert): pass an existing role, or omit it to create one."""
        checked = await self._tier_role(ctx, tier)
        if checked is None:
            return
        tier = checked[0]
        if role is None:
            try:
                role = await ctx.guild.create_role(
                    name=TIER_ROLE_NAMES[tier], hoist=True,
                    mentionable=False, reason="Secret hunt reward role")
            except _DISCORD_ERRORS as err:
                return await ctx.send(f"I could not create the role ({type(err).__name__}); I need Manage Roles.")
        async with self.config.guild(ctx.guild).roles() as roles:
            roles[tier] = role.id
        await ctx.send(f"{tier.title()} reward role: {role.mention}. Drag it where you want it in the role list, and "
                       "keep it below my own top role so I can hand it out.")

    @secrethunt.command(name="puzzlerole")
    async def secrethunt_puzzlerole(self, ctx, puzzle_id: str, role: discord.Role = None):
        """Give one puzzle its own badge role (Medium and up): pass a role, or omit it to create one named for the puzzle."""
        puzzle = PUZZLES.get(puzzle_id)
        if puzzle is None:
            return await ctx.send("Unknown puzzle. Known: " + ", ".join(PUZZLES))
        if puzzle["tier"] == "easy":
            return await ctx.send("Easy puzzles have no role or badge by design.")
        if role is None:
            try:
                role = await ctx.guild.create_role(name=puzzle["title"], hoist=False, mentionable=False,
                                                   reason="Secret hunt puzzle role")
            except _DISCORD_ERRORS as err:
                return await ctx.send(f"I could not create the role ({type(err).__name__}); I need Manage Roles.")
        async with self.config.guild(ctx.guild).puzzle_roles() as roles:
            roles[puzzle_id] = role.id
        await ctx.send(f"**{puzzle['title']}** now awards {role.mention} to everyone who solves it from here on. "
                       "Keep it below my top role. Earlier solvers do not get it retroactively.")

    @secrethunt.command(name="roleicon")
    async def secrethunt_roleicon(self, ctx, target: str):
        """Attach an image (under 256 KB) as a role's icon. `target` is a tier or a puzzle id (needs boost level 2+)."""
        key = (target or "").lower()
        if key in PUZZLES:
            if PUZZLES[key]["tier"] == "easy":
                return await ctx.send("Easy puzzles have no role or badge by design.")
            role = ctx.guild.get_role((await self.config.guild(ctx.guild).puzzle_roles()).get(key) or 0)
            hint = f".secrethunt puzzlerole {key}"
        else:
            checked = await self._tier_role(ctx, key)
            if checked is None:
                return
            role, hint = checked[1], f".secrethunt role {checked[0]}"
        if role is None:
            return await ctx.send(f"Set the role first with `{hint}`.")
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
        pool = await self.config.guild(ctx.guild).pool()
        lines = []
        for pid, st in states.items():
            where = f"<#{pool[st['variant'] % len(pool)]}>" if pool else "?"
            lines.append(f"**{pid}**: {names.get(st['stage'], st['stage'])}; variant {st['variant'] + 1} ({where}); "
                         f"wrong guesses today {st['wrong_count']}")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @secrethunt.command(name="preview")
    async def secrethunt_preview(self, ctx, puzzle_id: str = "late_rabbit"):
        """DM you every variant's clues and answer (spoilers, so it never goes in a channel)."""
        puzzle = PUZZLES.get(puzzle_id)
        pool = await self.config.guild(ctx.guild).pool()
        if puzzle is None or len(pool) < POOL_MIN:
            return await ctx.send("Unknown puzzle, or the pool is not set yet.")
        lines = [f"**{puzzle['title']}** trigger word: `{puzzle['trigger']}`"]
        for v in range(len(pool)):
            _, hint = self._hint_for(ctx.guild, puzzle, pool, v)
            hint = hint[0] if hint else None
            answer = puzzle["answers"][v % len(puzzle["answers"])]
            jumbled = engine.scramble(answer, f"{puzzle['id']}:{v}:final")
            lines.append(f"Variant {v + 1}: <#{pool[v]}>, scrambled title `{hint}`, answer `{answer}`, jumbled `{jumbled}`")
        if await self._dm(ctx.author, "\n".join(lines)):
            await ctx.tick()
        else:
            await ctx.send("I could not DM you.")

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
                 + (f" \N{MIDDLE DOT} finder #{st['rank']}" if st.get("rank") else "") for pid, st in found]
        embed = discord.Embed(title=f"Secrets found by {member.display_name}", description="\n".join(lines),
                              color=discord.Color.gold())
        embed.set_footer(text=f"{len(found)} secret{'' if len(found) == 1 else 's'} found")
        await ctx.send(embed=embed)
