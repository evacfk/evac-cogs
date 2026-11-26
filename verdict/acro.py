"""Acrophobia: the second Verdict round type, a quick acronym game.

Once a day, an hour after the daily question posts (10am Pacific), the bot posts 3-4 random letters in the same channel.
Members tap Submit and write one word per letter (each starting with its letter, in order), anonymously. After 4 hours
the entries go up numbered for a 4 hour vote (one vote each, never for your own). The winner gets wondercoin from a
capped daily faucet.

All state lives in Config (`acro_current`), so a restart resumes the round. Clicks and modals are handled in
`Verdict.on_interaction` by custom id (`verdict:acro:<round>:<action>`), like the daily question's buttons.
This file holds the Discord side as a mixin; the pure rules are in acro_engine.py and the rendering in acro_embeds.py.
"""
from __future__ import annotations

import logging
import random
import time

import discord
try:
    from redbot.core import bank
except ImportError:   # the repo's test stub has no bank; only the prize payout needs it
    bank = None

from . import acro_embeds as ui
from . import acro_engine as ae
from . import engine
from .constants import (
    ACRO_MAX_ENTRIES, ACRO_MAX_PRIZE, ACRO_MIN_ENTRIES, ACRO_REVIEW_DAYS, ACRO_REVIEW_MIN_AVG,
    ACRO_START_GRACE_SECONDS, ACRO_START_OFFSET_HOURS, ACRO_SUBMIT_SECONDS, ACRO_VOTE_SECONDS, ACRO_HISTORY, KEEP_MONTHS,
)
from .settings_panel import PanelError

log = logging.getLogger("red.evac-cogs.verdict")
_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)


def modal_value(data, key: str) -> str:
    """Text typed into the modal field `key`, wherever Discord nests it (action row or label wrapper)."""
    if isinstance(data, dict):
        if data.get("custom_id") == key and "value" in data:
            return str(data.get("value") or "")
        for v in data.values():
            found = modal_value(v, key)
            if found:
                return found
    elif isinstance(data, list):
        for v in data:
            found = modal_value(v, key)
            if found:
                return found
    return ""


class AcroMixin:
    """Mixed into Verdict. Needs self.bot, self.config, self._is_staff and the `_acro_locks` / `_acro_active` fields."""

    def _acro_init(self):
        self._acro_locks: dict = {}
        self._acro_active: set = set()

    def _alock(self, guild_id: int):
        import asyncio
        return self._acro_locks.setdefault(guild_id, asyncio.Lock())

    # -- who may do what ----------------------------------------------------

    async def _acro_is_mod(self, member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_guild):
            return True
        return await self._is_staff(member)

    async def _acro_is_lurker(self, member) -> bool:
        cog = self.bot.get_cog("Lurker")
        if cog is None:
            return False
        try:
            rid = await cog.config.guild(member.guild).lurker_role_id()
        except Exception:
            return False
        return bool(rid) and any(r.id == rid for r in getattr(member, "roles", []))

    # -- scheduler ------------------------------------------------------------

    async def _acro_tick(self, guild, now_ts: float | None = None) -> None:
        now_ts = now_ts or time.time()
        conf = self.config.guild(guild)
        if await conf.acro_current():
            self._acro_active.add(guild.id)
            await self._acro_advance(guild, now_ts)
            return
        self._acro_active.discard(guild.id)
        if not await conf.acro_enabled() or not await conf.channel_id():
            return
        await self._acro_schedule(guild, now_ts)

    async def _acro_schedule(self, guild, now_ts: float) -> None:
        """One round a day, opening an hour after the daily question (the Daily Verdict post hour)."""
        conf = self.config.guild(guild)
        if not ae.start_due(now_ts, min(23, await conf.post_hour() + ACRO_START_OFFSET_HOURS), await conf.acro_auto_date(), ACRO_START_GRACE_SECONDS):
            return
        await conf.acro_auto_date.set(engine.pt_date(now_ts))   # marked before posting, so a failure can't start it twice
        status = await self.acro_start(guild, now_ts, manual=False)
        if not status.startswith("Started"):
            log.warning("verdict: daily Acrophobia round not started: %s", status)

    # -- round lifecycle --------------------------------------------------------

    async def acro_start(self, guild, now_ts: float, *, manual: bool) -> str:
        async with self._alock(guild.id):
            conf = self.config.guild(guild)
            if await conf.acro_current():
                return "A round is already running."
            channel = guild.get_channel(await conf.channel_id() or 0)
            if channel is None:
                return "Open `.verdict settings` and pick the channel first."
            seq = await conf.acro_seq() + 1
            rid = f"{seq}x{int(now_ts)}"
            cur = {"rid": rid, "seq": seq, "letters": ae.pick_letters(), "theme": ae.pick_theme(await conf.acro_themes()),
                   "phase": "submit", "started_ts": now_ts, "submit_ends_ts": now_ts + ACRO_SUBMIT_SECONDS,
                   "vote_ends_ts": None, "channel_id": channel.id, "message_id": None, "vote_message_id": None,
                   "entries": {}, "order": [], "removed": [], "votes": {}, "manual": manual}
            view = ui.submit_view(rid)
            try:
                msg = await channel.send(embed=ui.submit_embed(cur), view=view)
            except _DISCORD_ERRORS:
                log.warning("verdict: could not post Acrophobia in channel %s", channel.id)
                return f"I couldn't post in {channel.mention}."
            self._stop_view(view)
            cur["message_id"] = msg.id
            await conf.acro_current.set(cur)
            await conf.acro_seq.set(seq)
            self._acro_active.add(guild.id)
            return f"Started Acrophobia #{seq}."

    @staticmethod
    def _stop_view(view) -> None:
        try:
            view.stop()   # clicks are handled by on_interaction, so nothing needs to stay in memory
        except Exception:
            pass

    async def _acro_advance(self, guild, now_ts: float) -> None:
        """Move the open round to its next phase when its time is up. Safe to call as often as you like."""
        async with self._alock(guild.id):
            cur = await self.config.guild(guild).acro_current()
            if not cur:
                return
            if cur["phase"] == "submit" and now_ts >= cur["submit_ends_ts"]:
                await self._acro_close_submit(guild, cur, now_ts)
            elif cur["phase"] == "vote" and now_ts >= cur["vote_ends_ts"]:
                await self._acro_finish(guild, cur, now_ts)

    async def _edit_round_message(self, guild, cur: dict, message_id, **kw) -> bool:
        channel = guild.get_channel(cur.get("channel_id") or 0)
        if channel is None or not message_id:
            return False
        try:
            await channel.get_partial_message(message_id).edit(**kw)
            return True
        except _DISCORD_ERRORS:
            log.warning("verdict: could not edit Acrophobia message %s", message_id)
            return False

    async def _acro_record(self, guild, cur: dict, now_ts: float, **kw) -> None:
        async with self.config.guild(guild).acro_history() as hist:
            hist.append(ae.history_entry(cur, ts=now_ts, **kw))
            del hist[:-ACRO_HISTORY]

    async def _acro_close_submit(self, guild, cur: dict, now_ts: float) -> None:
        conf = self.config.guild(guild)
        entries = cur["entries"]
        if len(entries) < ACRO_MIN_ENTRIES:
            await self._edit_round_message(guild, cur, cur["message_id"], embed=ui.cancelled_embed(cur, len(entries)), view=None)
            await self._acro_record(guild, cur, now_ts, cancelled=True, entries=len(entries), votes=0, win=[], paid=0)
            await conf.acro_current.set({})
            self._acro_active.discard(guild.id)
            return
        order = list(entries)
        random.shuffle(order)
        cur.update(order=order, phase="vote", vote_ends_ts=now_ts + ACRO_VOTE_SECONDS)
        channel = guild.get_channel(cur["channel_id"])
        view = ui.vote_view(cur)
        try:
            msg = await channel.send(embed=ui.vote_embed(cur), view=view)
        except _DISCORD_ERRORS + (AttributeError,):
            log.warning("verdict: could not post the Acrophobia vote; dropping round #%s", cur["seq"])
            await self._edit_round_message(guild, cur, cur["message_id"], embed=ui.cancelled_embed(cur, len(entries)), view=None)
            await conf.acro_current.set({})
            self._acro_active.discard(guild.id)
            return
        self._stop_view(view)
        cur["vote_message_id"] = msg.id
        await conf.acro_current.set(cur)
        await self._edit_round_message(guild, cur, cur["message_id"], embed=ui.submit_closed_embed(cur, len(entries)), view=None)

    async def _acro_finish(self, guild, cur: dict, now_ts: float) -> None:
        conf = self.config.guild(guild)
        counts = ae.tally(cur["votes"], cur["entries"], cur["removed"])
        win = ae.winners(counts)
        votes_n = sum(counts.values())
        paid_state = await conf.acro_paid()
        today = engine.pt_date(now_ts)
        if paid_state.get("date") != today:
            paid_state = {"date": today, "amount": 0}
        share, _total = ae.payout(await conf.acro_prize(), paid_state["amount"], await conf.acro_daily_cap(), len(win))
        try:
            currency = await bank.get_currency_name(guild)
        except Exception:
            currency = "coins"
        paid = 0
        for uid in win:
            member = guild.get_member(int(uid))
            if share > 0 and member is not None:
                try:
                    await bank.deposit_credits(member, share)
                    paid += share
                except Exception:
                    log.exception("verdict: could not pay the Acrophobia winner %s", uid)
        paid_state["amount"] += paid
        await conf.acro_paid.set(paid_state)
        await conf.acro_total_paid.set(await conf.acro_total_paid() + paid)
        await self._acro_score(guild, cur, win, now_ts)
        await self._acro_record(guild, cur, now_ts, cancelled=False, entries=len(counts) + len(cur["removed"]),
                                votes=votes_n, win=win, paid=paid)
        await conf.acro_current.set({})
        self._acro_active.discard(guild.id)
        prize_line = self._acro_prize_line(win, share, currency)
        channel = guild.get_channel(cur["channel_id"])
        await self._edit_round_message(guild, cur, cur["vote_message_id"], embed=ui.closed_vote_embed(cur), view=None)
        if channel is None:
            return
        mentions = ", ".join(f"<@{u}>" for u in win)
        try:
            await channel.send(content=f"\N{TROPHY} {mentions}" if win else None,
                               embed=ui.results_embed(cur, counts, win, prize_line=prize_line,
                                                      removed_n=len(cur["removed"]), votes_n=votes_n),
                               allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(u)) for u in win],
                                                                        roles=False, everyone=False))
        except _DISCORD_ERRORS:
            log.warning("verdict: could not post the Acrophobia results for #%s", cur["seq"])

    @staticmethod
    def _acro_prize_line(win: list, share: int, currency: str) -> str:
        if not win:
            return "Nobody voted, so there's no winner this time."
        who = " and ".join(f"<@{u}>" for u in win) if len(win) <= 3 else f"{len(win)} entries"
        if share <= 0:
            return f"{who} won! Today's prize limit is used up, so no {currency} this round."
        if len(win) == 1:
            return f"{who} wins **{share:,} {currency}**!"
        return f"{who} tie and get **{share:,} {currency}** each!"

    async def _acro_score(self, guild, cur: dict, win: list, now_ts: float) -> None:
        for uid in cur["entries"]:
            mconf = self.config.member_from_ids(guild.id, int(uid))
            await mconf.acro_rounds.set(await mconf.acro_rounds() + 1)
        if not win:
            return
        for uid in win:
            mconf = self.config.member_from_ids(guild.id, int(uid))
            await mconf.acro_wins.set(await mconf.acro_wins() + 1)
        key = engine.month_key(now_ts)
        async with self.config.guild(guild).acro_months() as months:
            scores = months.setdefault(key, {})
            for uid in win:
                scores[uid] = scores.get(uid, 0) + 1
            for old in sorted(months)[:-KEEP_MONTHS]:
                months.pop(old, None)

    async def acro_cancel(self, guild, now_ts: float) -> str:
        async with self._alock(guild.id):
            conf = self.config.guild(guild)
            cur = await conf.acro_current()
            if not cur:
                return "There's no round to cancel."
            n = len(cur["entries"])
            embed = ui.cancelled_embed(cur, n)
            embed.description = "A mod stopped this round. Nothing was paid. The next one is coming."
            for mid in (cur.get("message_id"), cur.get("vote_message_id")):
                await self._edit_round_message(guild, cur, mid, embed=embed, view=None)
            await self._acro_record(guild, cur, now_ts, cancelled=True, entries=n, votes=len(cur["votes"]), win=[], paid=0,
                                    aborted=True)
            await conf.acro_current.set({})
            self._acro_active.discard(guild.id)
            return f"Cancelled Acrophobia #{cur['seq']}."

    async def acro_remove_entry(self, guild, number: int) -> str:
        async with self._alock(guild.id):
            conf = self.config.guild(guild)
            cur = await conf.acro_current()
            if not cur or cur["phase"] != "vote":
                return "Entries can only be removed while the vote is open."
            if not 1 <= number <= len(cur["order"]):
                return "No such entry."
            uid = cur["order"][number - 1]
            if uid in cur["removed"]:
                return f"Entry #{number} is already removed."
            cur["removed"].append(uid)
            cur["votes"] = {v: t for v, t in cur["votes"].items() if t != uid}
            await conf.acro_current.set(cur)
            view = ui.vote_view(cur)
            await self._edit_round_message(guild, cur, cur["vote_message_id"], embed=ui.vote_embed(cur), view=view)
            self._stop_view(view)
            return f"Removed entry #{number} from the vote. Votes for it were cleared."

    async def _acro_forget_user(self, guild, uid: str) -> None:
        async with self._alock(guild.id):
            conf = self.config.guild(guild)
            cur = await conf.acro_current()
            if cur:
                cur["entries"].pop(uid, None)
                cur["votes"] = {v: t for v, t in cur["votes"].items() if v != uid and t != uid}
                if uid in cur["order"]:
                    cur["removed"] = sorted(set(cur["removed"]) | {uid})
                await conf.acro_current.set(cur)
            async with conf.acro_months() as months:
                for scores in months.values():
                    scores.pop(uid, None)

    # -- clicks -------------------------------------------------------------------

    async def handle_acro(self, interaction, cid: str) -> None:
        try:
            _, _, rid, action = cid.split(":", 3)
        except ValueError:
            return
        if action == "submit":
            await self._acro_on_submit_click(interaction, rid)
        elif action == "entry":
            await self._acro_on_entry(interaction, rid)
        elif action.startswith("v") and action[1:].isdigit():
            await self._acro_on_vote(interaction, rid, int(action[1:]))
        elif action == "mod":
            await self._acro_on_mod_open(interaction)
        elif action.startswith("m_") or action == "themes":
            await self._acro_on_mod_action(interaction, rid, action)

    @staticmethod
    async def _say(interaction, text: str) -> None:
        await interaction.response.send_message(text, ephemeral=True)

    async def _acro_round(self, guild, rid: str):
        cur = await self.config.guild(guild).acro_current()
        return cur if cur and cur.get("rid") == rid else None

    async def _acro_on_submit_click(self, interaction, rid: str) -> None:
        guild, user = interaction.guild, interaction.user
        if getattr(user, "bot", False) or await self._acro_is_lurker(user):
            await self._say(interaction, "Say a few words in chat first, then come play!")
            return
        async with self._alock(guild.id):
            cur = await self._acro_round(guild, rid)
            if not cur:
                await self._say(interaction, "That round is over. A new one is coming soon.")
                return
            if cur["phase"] != "submit" or time.time() >= cur["submit_ends_ts"]:
                await self._say(interaction, "Submissions just closed. You can still vote in the next message.")
                return
            mine = cur["entries"].get(str(user.id), "")
            if not mine and len(cur["entries"]) >= ACRO_MAX_ENTRIES:
                await self._say(interaction, f"This round is full ({ACRO_MAX_ENTRIES} entries). Vote on the next message!")
                return
        await interaction.response.send_modal(ui.entry_modal(rid, cur, mine))

    async def _acro_on_entry(self, interaction, rid: str) -> None:
        guild, user = interaction.guild, interaction.user
        text = modal_value(interaction.data, "text")
        if getattr(user, "bot", False) or await self._acro_is_lurker(user):
            await self._say(interaction, "Say a few words in chat first, then come play!")
            return
        async with self._alock(guild.id):
            conf = self.config.guild(guild)
            cur = await self._acro_round(guild, rid)
            if not cur:
                await self._say(interaction, "That round is over. A new one is coming soon.")
                return
            if cur["phase"] != "submit" or time.time() >= cur["submit_ends_ts"]:
                await self._say(interaction, "Too late, submissions just closed. Vote in the next message!")
                return
            clean, err = ae.validate_entry(text, cur["letters"])
            if err:
                await self._say(interaction, f"\N{CROSS MARK} {err}\nYou wrote: {ae.escape(' '.join(text.split()))[:300]}\n"
                                             "Tap **Submit** again to retry.")
                return
            uid = str(user.id)
            if uid not in cur["entries"] and len(cur["entries"]) >= ACRO_MAX_ENTRIES:
                await self._say(interaction, f"This round is full ({ACRO_MAX_ENTRIES} entries).")
                return
            first_entry = uid not in cur["entries"]
            cur["entries"][uid] = clean
            await conf.acro_current.set(cur)
            ends = int(cur["submit_ends_ts"])
        if first_entry:
            self.bot.dispatch("wonderland_quest", user, "acro", 1)   # daily quests (wonderland)
        await self._say(interaction, f"\N{WHITE HEAVY CHECK MARK} Entered: **{ae.escape(clean)}**\n"
                                     f"Tap **Submit** again to change it before <t:{ends}:R>.")

    async def _acro_on_vote(self, interaction, rid: str, idx: int) -> None:
        guild, user = interaction.guild, interaction.user
        if getattr(user, "bot", False) or await self._acro_is_lurker(user):
            await self._say(interaction, "Say a few words in chat first, then come play!")
            return
        async with self._alock(guild.id):
            conf = self.config.guild(guild)
            cur = await self._acro_round(guild, rid)
            if not cur or cur["phase"] != "vote" or time.time() >= cur["vote_ends_ts"]:
                await self._say(interaction, "Voting is closed.")
                return
            if not 0 <= idx < len(cur["order"]):
                return
            target, uid = cur["order"][idx], str(user.id)
            if target in cur["removed"]:
                await self._say(interaction, "A mod removed that entry. Pick another.")
                return
            if target == uid:
                await self._say(interaction, "You can't vote for your own entry. Pick another!")
                return
            before = cur["votes"].get(uid)
            cur["votes"][uid] = target
            await conf.acro_current.set(cur)
        if before is None:
            self.bot.dispatch("wonderland_quest", user, "acro", 1)   # daily quests (wonderland)
        if before == target:
            await self._say(interaction, f"Your vote is already on **#{idx + 1}**.")
        elif before is None:
            await self._say(interaction, f"\N{WHITE HEAVY CHECK MARK} Voted for **#{idx + 1}**. Tap another number to change it.")
        else:
            await self._say(interaction, f"\N{WHITE HEAVY CHECK MARK} Changed your vote to **#{idx + 1}**.")

    # -- mod panel ---------------------------------------------------------------------

    async def _acro_status(self, guild, now_ts: float) -> tuple[str, str]:
        """(what's happening, the numbers) for the mod panel and the staff settings readout."""
        conf = self.config.guild(guild)
        s = await conf.all()
        cur = s["acro_current"]
        if cur:
            ends = cur["submit_ends_ts"] if cur["phase"] == "submit" else cur["vote_ends_ts"]
            state = (f"Round **#{cur['seq']}** is open ({cur['phase']} phase, ends <t:{int(ends)}:R>): "
                     f"{len(cur['entries'])} entries, {len(cur['votes'])} votes.")
        else:
            state = "No round is open."
        lines = [f"Acrophobia is **{'on' if s['acro_enabled'] else 'off'}** · one round a day · "
                 f"prize {s['acro_prize']:,} · daily cap {s['acro_daily_cap']:,}", state]
        if s["acro_enabled"] and not cur and s["acro_auto_date"] != engine.pt_date(now_ts):
            hour = s["post_hour"]
            lines.append(f"Next automatic round: today at {hour % 12 or 12}{'am' if hour < 12 else 'pm'} PT if it's still ahead, "
                         "otherwise tomorrow (an hour after the daily question).")
        paid = s["acro_paid"]
        today_paid = paid["amount"] if paid.get("date") == engine.pt_date(now_ts) else 0
        lines.append(f"Paid today: {today_paid:,} of {s['acro_daily_cap']:,}")
        avg = ae.averages(s["acro_history"])
        stats = [f"Rounds run: {avg['rounds']} ({avg['cancelled']} cancelled for too few entries in the last {avg['window']})"]
        if avg["window"]:
            stats.append(f"Average per round (last {avg['window']}): {avg['avg_entries']:.1f} entries, {avg['avg_votes']:.1f} votes")
        stats.append(f"Total paid out: {s['acro_total_paid']:,}")
        wins = ae.month_wins(s["acro_months"], engine.month_key(now_ts))
        if wins:
            top = sorted(wins.items(), key=lambda kv: (-kv[1], int(kv[0])))[:5]
            stats.append("Wins this month: " + ", ".join(f"<@{u}> {n}" for u, n in top))
        rv = ae.review_state(s["acro_launch_ts"], now_ts, s["acro_history"], ACRO_REVIEW_DAYS, ACRO_REVIEW_MIN_AVG)
        if rv["days_left"] is None:
            stats.append("Review: starts the first time it's switched on (3 weeks later).")
        elif not rv["due"]:
            stats.append(f"Review in {rv['days_left']} days: keep it if rounds average {ACRO_REVIEW_MIN_AVG:g}+ entries.")
        elif rv["retire"]:
            stats.append(f"\N{WARNING SIGN} Review due: rounds average {rv['avg']:.1f} entries (under {ACRO_REVIEW_MIN_AVG:g}). Retire it?")
        else:
            stats.append(f"Review due: rounds average {rv['avg']:.1f} entries, so it stays.")
        return "\n".join(lines), "\n".join(stats)

    async def _acro_panel(self, guild, now_ts: float, note: str = "") -> dict:
        conf = self.config.guild(guild)
        cur = await conf.acro_current()
        status, stats = await self._acro_status(guild, now_ts)
        removable = []
        if cur and cur["phase"] == "vote":
            gone = set(cur["removed"])
            removable = [(i, cur["entries"][u]) for i, u in enumerate(cur["order"], 1) if u not in gone]
        view = ui.mod_view(cur["rid"] if cur else "0", enabled=await conf.acro_enabled(), active=bool(cur),
                           removable=removable)
        return {"embed": ui.mod_embed(status=status, stats=stats, note=note), "view": view}

    async def _acro_on_mod_open(self, interaction) -> None:
        if not await self._acro_is_mod(interaction.user):
            await self._say(interaction, "Only Staff and Moderators can open this.")
            return
        await interaction.response.send_message(**await self._acro_panel(interaction.guild, time.time()), ephemeral=True)

    async def _acro_on_mod_action(self, interaction, rid: str, action: str) -> None:
        guild = interaction.guild
        if not await self._acro_is_mod(interaction.user):
            await self._say(interaction, "Only Staff and Moderators can do that.")
            return
        if action == "m_themes":
            await interaction.response.send_modal(ui.themes_modal(rid, await self.config.guild(guild).acro_themes()))
            return
        note = ""
        if action == "themes":
            try:
                themes = ae.parse_themes(modal_value(interaction.data, "themes"))
            except ValueError as exc:
                await self._say(interaction, f"\N{CROSS MARK} {exc}")
                return
            await self.config.guild(guild).acro_themes.set(themes)
            await self._say(interaction, f"\N{WHITE HEAVY CHECK MARK} {len(themes)} themes saved."
                            + ("" if themes else " Rounds will have no theme."))
            return
        now = time.time()
        if action == "m_start":
            note = await self._acro_start_checked(guild, now)
        elif action == "m_cancel":
            cur = await self.config.guild(guild).acro_current()
            note = await self.acro_cancel(guild, now) if cur and cur["rid"] == rid else "That round is already over."
        elif action == "m_rm":
            values = (interaction.data or {}).get("values") or []
            note = await self.acro_remove_entry(guild, int(values[0])) if values and str(values[0]).isdigit() else "Pick an entry."
        await interaction.response.edit_message(**await self._acro_panel(guild, time.time(), note))

    async def _acro_start_checked(self, guild, now_ts: float) -> str:
        if not await self.config.guild(guild).acro_enabled():
            return "Acrophobia is off. An admin can switch it on in `.verdict settings`."
        return await self.acro_start(guild, now_ts, manual=True)

    # -- settings (the panel calls these) -------------------------------------------------

    async def _apply_acro_enabled(self, guild, member, on) -> str:
        conf = self.config.guild(guild)
        if on and not await conf.channel_id():
            raise PanelError("Pick the daily question channel first (Daily post tab). Acrophobia posts there.")
        await conf.acro_enabled.set(bool(on))
        if on and not await conf.acro_launch_ts():
            await conf.acro_launch_ts.set(time.time())
        return "Acrophobia is on. A round opens every day an hour after the daily question." if on else "Acrophobia is off. An open round still finishes."

    async def _apply_acro_prize(self, guild, member, values) -> str:
        try:
            prize = int(str(values["prize"]).replace(",", "").strip())
            cap = int(str(values["cap"]).replace(",", "").strip())
        except (KeyError, TypeError, ValueError):
            raise PanelError("Enter whole numbers for the prize and the daily cap.")
        if not 0 <= prize <= ACRO_MAX_PRIZE:
            raise PanelError(f"The prize must be between 0 and {ACRO_MAX_PRIZE:,}.")
        if cap < prize:
            raise PanelError("The daily cap must be at least the prize, or the first win would be cut short.")
        await self.config.guild(guild).acro_prize.set(prize)
        await self.config.guild(guild).acro_daily_cap.set(cap)
        return f"Prize {prize:,}, daily cap {cap:,}. Check `.wld economy` after a week."

    async def _apply_acro_themes(self, guild, member, values) -> str:
        try:
            themes = ae.parse_themes((values or {}).get("themes") or "")
        except ValueError as exc:
            raise PanelError(str(exc))
        await self.config.guild(guild).acro_themes.set(themes)
        return f"{len(themes)} themes saved." + ("" if themes else " Rounds will have no theme.")

    async def _apply_acro_start(self, guild, member, value) -> str:
        status = await self._acro_start_checked(guild, time.time())
        if not status.startswith("Started"):
            raise PanelError(status)
        return status

    async def _apply_acro_cancel(self, guild, member, value) -> str:
        status = await self.acro_cancel(guild, time.time())
        if status.startswith("There's no"):
            raise PanelError(status)
        return status
