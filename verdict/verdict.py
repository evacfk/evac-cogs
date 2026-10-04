"""Daily Verdict: one crowd question a day, "your answer" plus "guess the crowd".

- posts one question a day in the chat channel (default 10am Pacific) with 2-4 buttons;
- each member taps their own answer, then guesses what most people will pick;
- the next day's post carries yesterday's result, who read the crowd and the lone wolves;
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
    COG_VERSION, CONFIG_IDENTIFIER, CROSS, DEFAULT_GUILD, DEFAULT_MEMBER, KEEP_MONTHS, MAX_PENDING_PER_USER,
    STAFF_ROLE_IDS, STREAK_MILESTONES, SUGGEST_COOLDOWN_SECONDS, TICK, TICK_SECONDS,
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
            award = await self._maybe_award(guild, now_ts)
            q, queue, used = self._next_question(await conf.queue(), await conf.used_seeds())
            seq = await conf.seq() + 1
            new = {"qid": f"{seq}x{int(now_ts)}", "seq": seq, "question": q["question"], "options": q["options"],
                   "submitter_id": q.get("submitter_id"), "message_id": None, "channel_id": channel.id,
                   "posted_ts": now_ts, "answers": {}, "predictions": {}}
            hour = await conf.post_hour()
            role = guild.get_role(await conf.ping_role_id() or 0)
            view = build_view("a", new["qid"], new["options"])
            try:
                msg = await channel.send(
                    content=role.mention if role else None,
                    embed=embeds.question_embed(new, yesterday=yesterday, award=award, post_hour=hour), view=view,
                    allowed_mentions=discord.AllowedMentions(roles=[role] if role else False, users=False, everyone=False),
                )
            except _DISCORD_ERRORS:
                log.warning("verdict: could not post in channel %s", channel.id)
                return f"I couldn't post in {channel.mention}."
            try:
                view.stop()  # clicks are handled by on_interaction, so nothing needs to stay in memory
            except Exception:
                pass
            new["message_id"] = msg.id
            await conf.current.set(new)
            await conf.queue.set(queue)
            await conf.used_seeds.set(used)
            await conf.seq.set(seq)
            await conf.last_post_date.set(engine.pt_date(now_ts))
            return f"Posted Daily Verdict #{seq}."

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
                    embed=embeds.closed_embed(cur, counts, win, extra), view=None)
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
                    await reply(f"\N{LOCK} Locked in. You: **{mine}** · your guess: **{options[preds[uid]]}**.", ephemeral=True)
                    return
                await reply(f"You picked **{mine}**.\nNow guess: what will **most people** pick?",
                            view=build_view("p", qid, options), ephemeral=True)
                return
            if kind != "p":
                return
            if uid not in answers:
                await reply("Pick your own answer first (tap one on the question).", ephemeral=True)
                return
            if uid in preds:
                await interaction.response.edit_message(
                    content=f"\N{LOCK} Already locked in. You: **{options[answers[uid]]}** · your guess: **{options[preds[uid]]}**.", view=None)
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
                     f"{flame}. The result comes with tomorrow's question."), view=None)

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
            f"Queued: {len(s['queue'])} · Waiting approval: {len(s['pending'])} · Posted so far: {s['seq']}",
            allowed_mentions=discord.AllowedMentions.none())
