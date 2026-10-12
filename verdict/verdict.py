"""Daily Verdict: one crowd question a day, "your answer" plus "guess the crowd".

- posts one question a day in the chat channel (default 10am Pacific) with 2-4 buttons;
- each member taps their own answer, then guesses what most people will pick;
- the next day's post carries yesterday's result, and the old post becomes the final tally listing who picked what;
- each question gets a private discussion thread; the bot adds a member to it only once both their taps are in,
  so reading it can't spoil a guess. The next post locks and archives it and copies the discussion into a public thread
  (Discord cannot turn a private thread public);
- streaks, a monthly Mind Reader role, member-submitted questions that wait for the owner's
  tick or cross in a private channel.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from redbot.core import Config, commands

from . import embeds, engine
from .constants import (
    COG_VERSION, CONFIG_IDENTIFIER, CROSS, DEFAULT_GUILD, DEFAULT_MEMBER, KEEP_HISTORY, KEEP_MONTHS, MAX_PENDING_PER_USER,
    STAFF_ROLE_IDS, STREAK_MILESTONES, SUGGEST_COOLDOWN_SECONDS, THREAD_ARCHIVE_MINUTES, TICK, TICK_SECONDS,
    EARLY_MANUAL_HOURS,
)
from .seeds import SEED_QUESTIONS

log = logging.getLogger("red.evac-cogs.verdict")
_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)


def build_view(kind: str, qid: str, options: list[str]):
    """Buttons whose clicks are handled by Verdict.on_interaction (so they survive restarts)."""
    view = discord.ui.View(timeout=None)
    for i, opt in enumerate(options):
        view.add_item(discord.ui.Button(
            label=f"{embeds.LETTERS[i]}. {opt}"[:80], custom_id=f"verdict:{kind}:{qid}:{i}",
            style=discord.ButtonStyle.primary if kind == "a" else discord.ButtonStyle.secondary,
        ))
    return view


class Verdict(commands.Cog):
    """A daily crowd question: pick your answer, then guess what everyone else picks."""

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
        uid = str(user_id)
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()
            async with self._lock(guild.id):
                conf = self.config.guild(guild)
                cur = await conf.current()
                if cur:
                    cur["answers"].pop(uid, None)
                    cur["predictions"].pop(uid, None)
                    await conf.current.set(cur)
                async with conf.months() as months:
                    for scores in months.values():
                        scores.pop(uid, None)
                for key in ("queue",):
                    async with getattr(conf, key)() as items:
                        for q in items:
                            if q.get("submitter_id") == user_id:
                                q["submitter_id"] = None
                async with conf.pending() as pending:
                    for q in pending.values():
                        if q.get("submitter_id") == user_id:
                            q["submitter_id"] = None

    # -- permissions ------------------------------------------------------

    async def _is_staff(self, member) -> bool:
        if any(r.id in STAFF_ROLE_IDS for r in getattr(member, "roles", [])):
            return True
        try:
            return bool(await self.bot.is_owner(member))
        except Exception:
            return False

    async def _require_staff(self, ctx) -> bool:
        if await self._is_staff(ctx.author):
            return True
        await ctx.send("Only Staff, Moderators and superpowers can do that.")
        return False

    async def _is_approver(self, guild, user_id: int) -> bool:
        if user_id in await self.config.guild(guild).approver_ids():
            return True
        try:
            return bool(await self.bot.is_owner(discord.Object(id=user_id)))
        except Exception:
            return False

    # -- scheduler --------------------------------------------------------

    async def _loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:
                    await self._tick(guild)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("verdict: tick failed for guild %s", guild.id)
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self, guild, now_ts: float | None = None) -> None:
        now_ts = now_ts or time.time()
        conf = self.config.guild(guild)
        if not await conf.enabled() or not await conf.channel_id():
            return
        if engine.pt_date(now_ts) == await conf.last_post_date():
            return
        if engine.pt_hour(now_ts) < await conf.post_hour():
            return
        await self.post_daily(guild, now_ts)

    def _next_question(self, queue: list, used: list) -> tuple[dict, list, list]:
        if queue:
            q = queue[0]
            return q, queue[1:], used
        idx, used = engine.pick_seed(used, len(SEED_QUESTIONS))
        s = SEED_QUESTIONS[idx]
        return {"question": s["question"], "options": list(s["options"]), "submitter_id": None}, queue, used

    async def post_daily(self, guild, now_ts: float, *, replace: bool = False) -> str:
        """Close the open question (if any) and post the next one. Returns a status line."""
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            channel = guild.get_channel(await conf.channel_id() or 0)
            if channel is None:
                return "Set the channel first: `.verdict channel #channel`."
            cur = await conf.current()
            yesterday = None
            if cur:
                yesterday = await self._close(guild, cur, now_ts)
            public_thread_id = await self._close_threads(guild, cur or {})
            award = await self._maybe_award(guild, now_ts)
            q, queue, used = self._next_question(await conf.queue(), await conf.used_seeds())
            seq = await conf.seq() + 1
            thread = None
            if await conf.threads_enabled():
                thread = await self._open_thread(channel, seq, q["question"])
            new = {"qid": f"{seq}x{int(now_ts)}", "seq": seq, "question": q["question"], "options": q["options"],
                   "submitter_id": q.get("submitter_id"), "message_id": None, "channel_id": channel.id,
                   "thread_id": thread.id if thread else None, "posted_ts": now_ts, "answers": {}, "predictions": {}}
            hour = await conf.post_hour()
            role = guild.get_role(await conf.ping_role_id() or 0)
            view = build_view("a", new["qid"], new["options"])
            try:
                msg = await channel.send(
                    content=role.mention if role else None,
                    embed=embeds.question_embed(new, yesterday=yesterday, award=award, post_hour=hour,
                                                yesterday_thread_id=public_thread_id), view=view,
                    allowed_mentions=discord.AllowedMentions(roles=[role] if role else False, users=False, everyone=False),
                )
            except _DISCORD_ERRORS:
                log.warning("verdict: could not post in channel %s", channel.id)
                if thread is not None:
                    try:
                        await thread.delete()
                    except _DISCORD_ERRORS:
                        pass
                return f"I couldn't post in {channel.mention}."
            try:
                view.stop()  # clicks are handled by on_interaction, so nothing needs to stay in memory
            except Exception:
                pass
            new["message_id"] = msg.id
            if thread is not None:
                try:
                    await thread.send(embeds.thread_intro(new), allowed_mentions=discord.AllowedMentions.none())
                except _DISCORD_ERRORS:
                    log.warning("verdict: could not write the opening message in thread %s", thread.id)
            await conf.current.set(new)
            await conf.queue.set(queue)
            await conf.used_seeds.set(used)
            await conf.seq.set(seq)
            if engine.uses_up_day(now_ts, hour, EARLY_MANUAL_HOURS):
                await conf.last_post_date.set(engine.pt_date(now_ts))
            return f"Posted Daily Verdict #{seq}."

    # -- discussion threads -----------------------------------------------

    async def _open_thread(self, channel, seq: int, question: str):
        """A private, voters-only thread. Never lets a thread problem stop the daily post."""
        try:
            return await channel.create_thread(
                name=engine.thread_name(seq, question), type=discord.ChannelType.private_thread, invitable=False,
                auto_archive_duration=THREAD_ARCHIVE_MINUTES, reason="Daily Verdict discussion")
        except _DISCORD_ERRORS:
            log.warning("verdict: could not create the discussion thread in %s (needs Create Private Threads)", channel.id)
        except Exception:
            log.exception("verdict: thread creation failed")
        return None

    async def _get_thread(self, guild, thread_id: int):
        thread = guild.get_thread(thread_id) if hasattr(guild, "get_thread") else None
        return thread if thread is not None else await guild.fetch_channel(thread_id)

    async def _lock_thread(self, guild, thread_id: int, results_url: str | None = None):
        """Say goodbye (when we have the results link), then lock and archive.
        Returns (done, thread): done is True when locked or already gone."""
        thread = None
        try:
            thread = await self._get_thread(guild, thread_id)
            if results_url:
                try:
                    await thread.send(f"\N{LOCK} Voting is closed. Final results: {results_url}\n"
                                      "This thread is now locked and its discussion is being copied to a public thread.",
                                      allowed_mentions=discord.AllowedMentions.none())
                except _DISCORD_ERRORS:
                    pass
            await thread.edit(archived=True, locked=True, reason="Daily Verdict closed")
            return True, thread
        except discord.NotFound:
            return True, None
        except _DISCORD_ERRORS:
            log.warning("verdict: could not lock thread %s (needs Manage Threads)", thread_id)
        except Exception:
            log.exception("verdict: locking thread %s failed", thread_id)
        return False, thread

    async def _publish_thread(self, guild, cur: dict, thread):
        """Threads cannot switch from private to public, so copy the finished discussion into a public, locked thread
        under the results post. Returns (public_thread_id or None, locked_ok); None when there was nothing to copy."""
        pub = None
        try:
            lines = []
            async for m in thread.history(limit=1000, oldest_first=True):
                if getattr(m.author, "bot", False) or m.is_system():
                    continue
                text = (m.clean_content or "").strip()
                files = " ".join(a.url for a in m.attachments)
                body = " ".join(p for p in (text, files) if p)
                if body:
                    lines.append(f"**{m.author.display_name}:** {body}")
            channel = guild.get_channel(cur.get("channel_id") or 0)
            if not lines or channel is None or not cur.get("message_id"):
                return None, True
            none = discord.AllowedMentions.none()
            pub = await channel.get_partial_message(cur["message_id"]).create_thread(
                name=engine.thread_name(cur["seq"], cur["question"]), auto_archive_duration=THREAD_ARCHIVE_MINUTES,
                reason="Daily Verdict discussion, now public")
            await pub.send("\N{SPEECH BALLOON} Discussion copied from the voters-only thread now that voting is closed.",
                           allowed_mentions=none)
            for page in engine.transcript_pages(lines):
                await pub.send(page, allowed_mentions=none)
        except _DISCORD_ERRORS:
            log.warning("verdict: could not copy the discussion to a public thread (needs Create Public Threads)")
        except Exception:
            log.exception("verdict: publishing the discussion failed")
        if pub is None:
            return None, True
        try:
            await pub.edit(archived=True, locked=True, reason="Daily Verdict closed")
            return pub.id, True
        except Exception:
            log.warning("verdict: could not lock public thread %s", pub.id)
            return pub.id, False

    async def _close_threads(self, guild, cur: dict):
        """Lock and archive the finished question's thread (retrying any earlier failures), then publish its discussion.
        Returns the public thread id, or None."""
        conf = self.config.guild(guild)
        leftovers = [t for t in await conf.thread_cleanup() if t != cur.get("thread_id")]
        left = [t for t in leftovers if not (await self._lock_thread(guild, t))[0]]
        public_id = None
        tid = cur.get("thread_id")
        if tid:
            url = (f"https://discord.com/channels/{guild.id}/{cur['channel_id']}/{cur['message_id']}"
                   if cur.get("channel_id") and cur.get("message_id") else None)
            done, thread = await self._lock_thread(guild, tid, url)
            if not done:
                left.append(tid)
            elif thread is not None:
                public_id, locked = await self._publish_thread(guild, cur, thread)
                if public_id and not locked:
                    left.append(public_id)
        await conf.thread_cleanup.set(left[-10:])
        return public_id

    async def _grant_thread(self, guild, cur: dict, user) -> bool | None:
        """Add a member who has locked in to the open thread. None: no thread today. False: it failed."""
        tid = cur.get("thread_id")
        if not tid:
            return None
        try:
            thread = await self._get_thread(guild, tid)
            await thread.add_user(user)
            return True
        except _DISCORD_ERRORS:
            log.warning("verdict: could not add %s to thread %s", getattr(user, "id", "?"), tid)
        except Exception:
            log.exception("verdict: adding a voter to the thread failed")
        return False

    async def _ensure_in_thread(self, interaction, guild, cur: dict) -> None:
        if await self._grant_thread(guild, cur, interaction.user) is False:
            try:
                await interaction.followup.send("I couldn't add you to the discussion thread. Tell a mod.", ephemeral=True)
            except Exception:
                pass

    @staticmethod
    def _thread_line(cur: dict) -> str:
        return f"\n\N{SPEECH BALLOON} Discussion thread: <#{cur['thread_id']}>" if cur.get("thread_id") else ""

    async def _close(self, guild, cur: dict, now_ts: float) -> str:
        """Settle the open question: scores, final embed on the old post. Returns yesterday's summary."""
        conf = self.config.guild(guild)
        answers, preds = cur.get("answers") or {}, cur.get("predictions") or {}
        counts = engine.tally(answers, len(cur["options"]))
        win = engine.winners(counts)
        players = engine.participants(answers, preds)
        readers = set(engine.mind_readers({u: preds[u] for u in players}, win))
        wolves = [u for u in engine.lone_wolves({u: answers[u] for u in players}, counts)]
        key = engine.month_key(cur.get("posted_ts") or now_ts)
        async with conf.months() as months:
            scores = months.setdefault(key, {})
            for uid in players:
                c, p = scores.get(uid, [0, 0])
                scores[uid] = [c + (1 if uid in readers else 0), p + 1]
            for old in sorted(months)[:-KEEP_MONTHS]:
                months.pop(old, None)
        async with conf.history() as hist:
            hist.append(engine.history_entry(cur, counts, win, players, readers, wolves))
            del hist[:-KEEP_HISTORY]
        milestones = []
        for uid in players:
            mconf = self.config.member_from_ids(guild.id, int(uid))
            played, correct = await mconf.played(), await mconf.correct()
            await mconf.played.set(played + 1)
            await mconf.correct.set(correct + (1 if uid in readers else 0))
            streak = await mconf.streak()
            if await mconf.last_seq() == cur["seq"] and streak in STREAK_MILESTONES:
                milestones.append(f"<@{uid}> ({streak})")
        extra = []
        if readers:
            extra.append(f"\N{BRAIN} {len(readers)} read the crowd")
        if wolves:
            extra.append("\N{WOLF FACE} Lone wolves: " + self._names(wolves))
        if milestones:
            extra.append("\N{FIRE} Streak milestones: " + ", ".join(milestones[:10]))
        channel = guild.get_channel(cur.get("channel_id") or 0)
        if channel is not None and cur.get("message_id"):
            try:
                await channel.get_partial_message(cur["message_id"]).edit(
                    embed=embeds.closed_embed(cur, counts, win, extra, votes=answers, readers=readers), view=None)
            except _DISCORD_ERRORS:
                log.warning("verdict: could not close the old post for #%s", cur["seq"])
        total = sum(counts)
        if not total:
            return f"**{cur['question']}**\nNobody answered. It happens."
        top = ", ".join(f"{cur['options'][i]} ({engine.percent(counts[i], total)}%)" for i in sorted(win))
        text = f"**{cur['question']}**\n\N{TROPHY} {top} · {total} voted"
        return text + ("\n" + " · ".join(extra) if extra else "")

    @staticmethod
    def _names(uids: list[str], limit: int = 5) -> str:
        shown = ", ".join(f"<@{u}>" for u in uids[:limit])
        return shown + (f" and {len(uids) - limit} more" if len(uids) > limit else "")

    async def _maybe_award(self, guild, now_ts: float) -> str | None:
        conf = self.config.guild(guild)
        this_month = engine.month_key(now_ts)
        last = await conf.last_award_month()
        if last == this_month:
            return None
        await conf.last_award_month.set(this_month)
        if not last:
            return None  # first ever run: nothing to award yet
        prev = engine.previous_month_key(this_month)
        scores = (await conf.months()).get(prev, {})
        winner = engine.pick_monthly_winner(scores)
        if winner is None:
            return None
        c, p = scores[winner]
        role = guild.get_role(await conf.mind_reader_role_id() or 0)
        if role is not None:
            try:
                for m in list(role.members):
                    if m.id != int(winner):
                        await m.remove_roles(role, reason="New Mind Reader of the month")
                member = guild.get_member(int(winner))
                if member is not None and role not in member.roles:
                    await member.add_roles(role, reason="Mind Reader of the month")
            except _DISCORD_ERRORS:
                log.warning("verdict: could not move the Mind Reader role")
        await conf.mind_reader_holder.set(int(winner))
        return f"<@{winner}> read the crowd best last month: {c} right out of {p}."

    # -- button clicks ----------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction):
        data = getattr(interaction, "data", None)
        cid = data.get("custom_id") if isinstance(data, dict) else None
        if not cid or not str(cid).startswith("verdict:") or interaction.guild is None:
            return
        try:
            await self.handle_click(interaction, str(cid))
        except Exception:
            log.exception("verdict: button handler failed")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Something went wrong. Try again in a moment.", ephemeral=True)
            except Exception:
                pass

    async def handle_click(self, interaction, cid: str) -> None:
        try:
            _, kind, qid, idx_s = cid.split(":")
            idx = int(idx_s)
        except ValueError:
            return
        guild, uid = interaction.guild, str(interaction.user.id)
        reply = interaction.response.send_message
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            cur = await conf.current()
            if not cur or cur["qid"] != qid:
                await reply("That question is closed. Today's is in the channel.", ephemeral=True)
                return
            options = cur["options"]
            if not 0 <= idx < len(options):
                return
            answers, preds = cur["answers"], cur["predictions"]
            if kind == "a":
                answers.setdefault(uid, idx)
                await conf.current.set(cur)
                mine = options[answers[uid]]
                if uid in preds:
                    await reply(f"\N{LOCK} Locked in. You: **{mine}** · your guess: **{options[preds[uid]]}**."
                                + self._thread_line(cur), ephemeral=True)
                    await self._ensure_in_thread(interaction, guild, cur)
                    return
                await reply(f"You picked **{mine}**.\n**Step 2 of 2.** Now guess: what will **most people** pick? "
                            "You score if your guess matches the winning answer.",
                            view=build_view("p", qid, options), ephemeral=True)
                return
            if kind != "p":
                return
            if uid not in answers:
                await reply("Pick your own answer first (tap one on the question).", ephemeral=True)
                return
            if uid in preds:
                await interaction.response.edit_message(
                    content=(f"\N{LOCK} Already locked in. You: **{options[answers[uid]]}** · your guess: **{options[preds[uid]]}**."
                             + self._thread_line(cur)), view=None)
                await self._ensure_in_thread(interaction, guild, cur)
                return
            preds[uid] = idx
            await conf.current.set(cur)
            mconf = self.config.member_from_ids(guild.id, interaction.user.id)
            streak = engine.update_streak(await mconf.last_seq(), await mconf.streak(), cur["seq"])
            await mconf.streak.set(streak)
            await mconf.last_seq.set(cur["seq"])
            if streak > await mconf.best_streak():
                await mconf.best_streak.set(streak)
        flame = f"\N{FIRE} Streak: {streak} day{'s' if streak != 1 else ''}"
        await interaction.response.edit_message(
            content=(f"\N{LOCK} Locked in. You: **{options[answers[uid]]}** · your guess: **{options[idx]}**.\n"
                     f"{flame}. Results, with everyone's picks, post tomorrow." + self._thread_line(cur)), view=None)
        await self._ensure_in_thread(interaction, guild, cur)

    # -- submissions ------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", None):
            return
        guild = self.bot.get_guild(payload.guild_id) if hasattr(self.bot, "get_guild") else None
        if guild is None:
            guild = next((g for g in self.bot.guilds if g.id == payload.guild_id), None)
        if guild is None:
            return
        emoji = str(payload.emoji)
        if emoji not in (TICK, CROSS):
            return
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            pending = await conf.pending()
            data = pending.get(str(payload.message_id))
            if data is None or not await self._is_approver(guild, payload.user_id):
                return
            pending.pop(str(payload.message_id))
            await conf.pending.set(pending)
            if emoji == TICK:
                async with conf.queue() as queue:
                    queue.append(data)
            status = (f"{TICK} Approved: queued" if emoji == TICK else f"{CROSS} Rejected")
        channel = guild.get_channel(await conf.review_channel_id() or 0)
        if channel is not None:
            part = channel.get_partial_message(payload.message_id)
            try:
                await part.edit(embed=embeds.review_embed(data, status=status))
                await part.clear_reactions()
            except _DISCORD_ERRORS:
                pass

    # -- commands ---------------------------------------------------------

    @commands.group(name="verdict", invoke_without_command=True)
    @commands.guild_only()
    async def verdict(self, ctx: commands.Context, member: discord.Member = None):
        """Your Daily Verdict record. Answer in the daily post; `.verdict suggest` to send a question."""
        member = member or ctx.author
        m = self.config.member(member)
        played, correct = await m.played(), await m.correct()
        acc = f"{engine.percent(correct, played)}%" if played else "n/a"
        await ctx.send(
            f"\N{BALLOT BOX WITH BALLOT} **{member.display_name}**\n"
            f"\N{FIRE} Streak: {await m.streak()} (best {await m.best_streak()})\n"
            f"\N{BRAIN} Crowd guesses right: {correct}/{played} ({acc})",
            allowed_mentions=discord.AllowedMentions.none())

    @verdict.command(name="version")
    async def verdict_version(self, ctx: commands.Context):
        """Show the running build (deploy probe)."""
        await ctx.send(f"Verdict v{COG_VERSION}")

    @verdict.command(name="top")
    async def verdict_top(self, ctx: commands.Context):
        """This month's best crowd-readers."""
        key = engine.month_key(time.time())
        rows = engine.ranking((await self.config.guild(ctx.guild).months()).get(key, {}))
        if not rows:
            await ctx.send("No scores yet this month.")
            return
        lines = [f"`{i}.` <@{uid}> — {c}/{p} right" for i, (uid, c, p) in enumerate(rows, 1)]
        await ctx.send("\N{BRAIN} **Mind Readers this month**\n" + "\n".join(lines),
                       allowed_mentions=discord.AllowedMentions.none())

    @verdict.command(name="results")
    async def verdict_results(self, ctx: commands.Context, number: int = 0):
        """Staff: everyone who played a closed question (default: the latest). Not who picked what."""
        if not await self._require_staff(ctx):
            return
        hist = await self.config.guild(ctx.guild).history()
        if not hist:
            await ctx.send("No closed questions kept yet. Participants are saved from the next close onward.")
            return
        entry = hist[-1] if not number else next((h for h in hist if h["seq"] == number), None)
        if entry is None:
            await ctx.send(f"I only keep the last {KEEP_HISTORY} questions, and #{number} isn't one of them.")
            return
        for page in engine.results_pages(entry):
            await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())

    @verdict.command(name="suggest")
    async def verdict_suggest(self, ctx: commands.Context, *, text: str):
        """Suggest a question: `.verdict suggest Best snack? | Chips | Candy | Fruit` (2-4 options)."""
        conf = self.config.guild(ctx.guild)
        review = ctx.guild.get_channel(await conf.review_channel_id() or 0)
        if review is None:
            await ctx.send("Suggestions aren't open yet.")
            return
        try:
            question, options = engine.parse_question(text)
        except engine.QuestionError as exc:
            await ctx.send(str(exc))
            return
        mconf = self.config.member(ctx.author)
        wait = SUGGEST_COOLDOWN_SECONDS - (time.time() - await mconf.last_suggest_ts())
        if wait > 0:
            await ctx.send(f"Slow down a little: try again in {int(wait // 60) + 1} min.")
            return
        async with self._lock(ctx.guild.id):
            pending = await conf.pending()
            mine = sum(1 for q in pending.values() if q.get("submitter_id") == ctx.author.id)
            if mine >= MAX_PENDING_PER_USER:
                await ctx.send(f"You already have {mine} waiting for approval. Let those clear first.")
                return
            data = {"question": question, "options": options, "submitter_id": ctx.author.id}
            try:
                msg = await review.send(embed=embeds.review_embed(data),
                                        allowed_mentions=discord.AllowedMentions.none())
                await msg.add_reaction(TICK)
                await msg.add_reaction(CROSS)
            except _DISCORD_ERRORS:
                await ctx.send("I couldn't send that for review. Tell a mod.")
                return
            pending[str(msg.id)] = data
            await conf.pending.set(pending)
        await mconf.last_suggest_ts.set(time.time())
        await ctx.send("\N{INBOX TRAY} Sent for approval. If it's picked, it goes out with your name on it.")

    # staff

    @verdict.command(name="add")
    async def verdict_add(self, ctx: commands.Context, *, text: str):
        """Queue a question straight away: `.verdict add Question | A | B`."""
        if not await self._require_staff(ctx):
            return
        try:
            question, options = engine.parse_question(text)
        except engine.QuestionError as exc:
            await ctx.send(str(exc))
            return
        async with self.config.guild(ctx.guild).queue() as queue:
            queue.append({"question": question, "options": options, "submitter_id": None})
            n = len(queue)
        await ctx.send(f"Queued (#{n} in line).")

    @verdict.command(name="queue")
    async def verdict_queue(self, ctx: commands.Context):
        """Questions waiting their turn."""
        if not await self._require_staff(ctx):
            return
        queue = await self.config.guild(ctx.guild).queue()
        if not queue:
            await ctx.send("Nothing queued. Built-in questions are used when the queue is empty.")
            return
        lines = [f"`{i}.` {q['question']} — {' / '.join(q['options'])}" for i, q in enumerate(queue[:15], 1)]
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @verdict.command(name="remove")
    async def verdict_remove(self, ctx: commands.Context, number: int):
        """Remove a queued question by its number in `.verdict queue`."""
        if not await self._require_staff(ctx):
            return
        async with self.config.guild(ctx.guild).queue() as queue:
            if not 1 <= number <= len(queue):
                await ctx.send("No such number.")
                return
            gone = queue.pop(number - 1)
        await ctx.send(f"Removed: {gone['question']}", allowed_mentions=discord.AllowedMentions.none())

    @verdict.command(name="post")
    async def verdict_post(self, ctx: commands.Context):
        """Close the current question and post the next one now."""
        if not await self._require_staff(ctx):
            return
        await ctx.send(await self.post_daily(ctx.guild, time.time()))

    # admin settings

    @verdict.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Where the daily question is posted (#cuddle)."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"The daily question goes in {channel.mention}.")

    @verdict.command(name="reviewchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_reviewchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Private channel where member suggestions wait for a ✅ or ❌ from you."""
        perms = channel.permissions_for(ctx.guild.default_role)
        if getattr(perms, "view_channel", False):
            await ctx.send(f"{channel.mention} can be seen by @everyone. Pick a private channel.")
            return
        await self.config.guild(ctx.guild).review_channel_id.set(channel.id)
        await ctx.send(f"Suggestions go to {channel.mention}. Only you (the bot owner) and `.verdict approvers` can approve.")

    @verdict.command(name="approvers")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_approvers(self, ctx: commands.Context, *members: discord.Member):
        """Extra people whose ✅/❌ counts (the bot owner always can). Replaces the list."""
        await self.config.guild(ctx.guild).approver_ids.set([m.id for m in members])
        await ctx.send("Approvers: owner" + "".join(f", {m.display_name}" for m in members))

    @verdict.command(name="hour")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_hour(self, ctx: commands.Context, hour: int):
        """Pacific hour (0-23) the daily question goes out. Default 10."""
        if not 0 <= hour <= 23:
            await ctx.send("Pick an hour from 0 to 23.")
            return
        await self.config.guild(ctx.guild).post_hour.set(hour)
        await ctx.send(f"Daily question at {hour}:00 Pacific.")

    @verdict.command(name="pingrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_pingrole(self, ctx: commands.Context, role: discord.Role = None):
        """Role pinged on the daily post (leave empty to stop pinging)."""
        await self.config.guild(ctx.guild).ping_role_id.set(role.id if role else None)
        await ctx.send(f"Pinging {role.name}." if role else "No ping on the daily post.",
                       allowed_mentions=discord.AllowedMentions.none())

    @verdict.command(name="mindreaderrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_mindreaderrole(self, ctx: commands.Context, role: discord.Role = None):
        """Role given to last month's best crowd-reader (leave empty to turn the title off)."""
        await self.config.guild(ctx.guild).mind_reader_role_id.set(role.id if role else None)
        await ctx.send(f"Mind Reader role: {role.name}." if role else "No Mind Reader role.",
                       allowed_mentions=discord.AllowedMentions.none())

    @verdict.command(name="threads")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_threads(self, ctx: commands.Context, setting: str = None):
        """Voters-only discussion thread per question: `.verdict threads on` or `off` (no argument shows the state)."""
        conf = self.config.guild(ctx.guild).threads_enabled
        if setting is None:
            await ctx.send(f"Discussion threads are {'on' if await conf() else 'off'}.")
            return
        if setting.lower() not in ("on", "off"):
            await ctx.send("Use `on` or `off`.")
            return
        await conf.set(setting.lower() == "on")
        await ctx.send(f"Discussion threads {setting.lower()} from the next question. An open thread still locks at the next post.")

    @verdict.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_enable(self, ctx: commands.Context):
        """Start posting a question every day."""
        if not await self.config.guild(ctx.guild).channel_id():
            await ctx.send("Set the channel first: `.verdict channel #channel`.")
            return
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("Daily Verdict is on. The first question goes out at the next posting hour, or use `.verdict post`.")

    @verdict.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def verdict_disable(self, ctx: commands.Context):
        """Stop posting."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("Daily Verdict is off.")

    @verdict.command(name="settings")
    async def verdict_settings(self, ctx: commands.Context):
        """Show settings."""
        if not await self._require_staff(ctx):
            return
        s = await self.config.guild(ctx.guild).all()
        ch = lambda i: f"<#{i}>" if i else "not set"  # noqa: E731
        await ctx.send(
            f"**Daily Verdict v{COG_VERSION}** {'(on)' if s['enabled'] else '(off)'}\n"
            f"Channel: {ch(s['channel_id'])} · Review: {ch(s['review_channel_id'])} · Hour: {s['post_hour']}:00 PT\n"
            f"Queued: {len(s['queue'])} · Waiting approval: {len(s['pending'])} · Posted so far: {s['seq']}\n"
            f"Discussion threads: {'on' if s['threads_enabled'] else 'off'}",
            allowed_mentions=discord.AllowedMentions.none())
