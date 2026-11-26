"""The Hunger Games: a weekly, numbered event run by Hana.

Sunday: the Reaping (4 drawn, 8 volunteer seats, swap requests, a 9 PM top-up draw). Monday: training
challenges in public threads plus a crowd-judged interview. Tuesday noon: Training Scores and the
sponsorship shop. Tuesday-Friday 7 PM: live arena broadcasts. Saturday 7 PM: the Finale and the Crowning.

Everything lives in one Config dict per guild (`current`), advanced by a single loop, so a restart picks up
where it left off: a broadcast resumes at the next unposted line. A training challenge that was mid-run
during a restart is lost (it scores 0); that is the accepted risk.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta

import discord
from redbot.core import Config, bank, commands

from . import content, embeds, engine
from . import panel as panel_specs
from . import views as ui
from .constants import (
    CHALLENGES, COG_VERSION, CONFIG_IDENTIFIER, DEFAULT_GUILD, DEFAULT_MEMBER, DRAW_AT_REAPING, ITEMS, MAX_TRIBUTES,
    MIN_MESSAGES_7D, MIN_TRIBUTES, NEXT_FLAVOR, POST_GAP, REPORTS_KEPT, SCENE_GAP, STAFF_ROLE_IDS, STRATEGIES,
    TEST_POST_GAP, TICK_SECONDS, TOPUP_TARGET,
)
from .settings_panel import PanelError, open_panel

log = logging.getLogger("red.evac-cogs.hungergames")
_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)
NONE = discord.AllowedMentions.none()
TEST_NAMES = ("Glimmer-bot", "Cato-tron", "Foxface 3000", "Thresh.exe", "Rue-mba", "Marvel-ous", "Clove Bot",
              "Peeta Bread", "Finnick-y", "Johanna Byte", "Beetee Beep")


class HungerGames(commands.Cog):
    """The Hunger Games: a weekly Reaping, training challenges, live arena broadcasts and sponsorship."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(last_sunday=None, **DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self._locks: dict = {}
        self.rng = random.Random()
        self.pool = content.load_events()
        self.ciphers = content.load_ciphers()
        self._sessions: dict = {}
        self.views = ui.persistent_views(self)
        if hasattr(bot, "add_view"):
            for view in self.views:
                bot.add_view(view)   # buttons keep working across restarts
        self._task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        self._task.cancel()
        for view in self.views:
            try:
                view.stop()
            except Exception:
                pass

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()
            async with self._lock(guild.id):
                hall = await self.config.guild(guild).hall()
                for h in hall:
                    if h.get("uid") == str(user_id):
                        h["uid"], h["name"] = None, "a former member"
                await self.config.guild(guild).hall.set(hall)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _now() -> float:
        return time.time()

    # =================================================================================================
    # small helpers
    # =================================================================================================

    async def _is_staff(self, member) -> bool:
        if any(r.id in STAFF_ROLE_IDS for r in getattr(member, "roles", [])):
            return True
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_guild):
            return True
        try:
            return bool(await self.bot.is_owner(member))
        except Exception:
            return False

    async def _lurker_role_id(self, guild):
        cog = self.bot.get_cog("Lurker")
        if cog is None:
            return None
        try:
            return await cog.config.guild(guild).lurker_role_id()
        except Exception:
            return None

    async def _is_lurker(self, member) -> bool:
        rid = await self._lurker_role_id(member.guild)
        return bool(rid) and any(r.id == rid for r in member.roles)

    async def _messages_7d(self, guild, now: float):
        """{uid: messages in the last 7 Pacific days} from ServerPulse, or None if it can't be read."""
        pulse = self.bot.get_cog("ServerPulse")
        if pulse is None or not hasattr(pulse, "_days"):
            return None
        today = engine.local_date(now)
        try:
            days = await pulse._days(guild.id, today - timedelta(days=6), today)
        except Exception:
            log.warning("hungergames: could not read ServerPulse counts", exc_info=True)
            return None
        counts: dict = {}
        for doc in (days or {}).values():
            for src in ((doc or {}).get("u") or {}, (doc or {}).get("ub") or {}):
                for uid, n in src.items():
                    counts[str(uid)] = counts.get(str(uid), 0) + int(n)
        return counts

    def _channel(self, guild, s: dict, ed: dict | None = None):
        cid = (ed or {}).get("channel_id") or s["channel_id"]
        return guild.get_channel(cid) if cid else None

    @staticmethod
    def _mention(ed: dict, uid: str) -> str:
        t = ed["roster"].get(uid)
        if t is None:
            return f"<@{uid}>" if uid.isdigit() else uid
        return t["name"] if t.get("fake") else f"<@{uid}>"

    @staticmethod
    def _touch(ed: dict, uid) -> None:
        uid = str(uid)
        if uid not in ed.setdefault("pressers", []):
            ed["pressers"].append(uid)

    @staticmethod
    async def _ack(interaction):
        """Acknowledge a press inside 3 seconds; the real reply comes later via _done."""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except _DISCORD_ERRORS:
            pass

    @staticmethod
    async def _done(interaction, **kwargs):
        try:
            await interaction.edit_original_response(**kwargs)
        except _DISCORD_ERRORS:
            pass

    async def _send(self, channel, *args, **kwargs):
        if channel is None:
            return None
        kwargs.setdefault("allowed_mentions", NONE)
        try:
            return await channel.send(*args, **kwargs)
        except _DISCORD_ERRORS:
            log.warning("hungergames: could not post in %s", getattr(channel, "id", "?"), exc_info=True)
            return None

    async def _edit(self, channel, message_id, **kwargs):
        if channel is None or not message_id:
            return
        try:
            await channel.get_partial_message(message_id).edit(**kwargs)
        except _DISCORD_ERRORS:
            log.debug("hungergames: could not edit message %s", message_id, exc_info=True)

    async def _pin(self, channel, message, ed: dict):
        if channel is None or message is None:
            return
        old = ed["messages"].get("pinned")
        if old:
            try:
                await channel.get_partial_message(old).unpin()
            except _DISCORD_ERRORS:
                pass
        try:
            await message.pin()
            ed["messages"]["pinned"] = message.id
        except _DISCORD_ERRORS:
            log.warning("hungergames: could not pin (needs Manage Messages)")

    def _pings(self, s: dict, ed: dict, users=()) -> tuple:
        """(prefix text, allowed_mentions) for a ping of the HG role plus some tributes. Test runs never ping."""
        if ed.get("test"):
            return "", NONE
        role_id = s["ping_role_id"]
        prefix = f"<@&{role_id}> " if role_id else ""
        objs = [discord.Object(id=int(u)) for u in users if str(u).isdigit()]
        roles = [discord.Object(id=role_id)] if role_id else []
        return prefix, discord.AllowedMentions(roles=roles, users=objs, everyone=False)

    async def _teaser(self, guild, s: dict, ed: dict, text: str):
        if ed.get("test") or not s["teaser_channel_id"]:
            return
        await self._send(guild.get_channel(s["teaser_channel_id"]), text)

    async def _render_reaping(self, guild, s: dict, ed: dict):
        channel = self._channel(guild, s, ed)
        view = ui.ReapingView(self) if engine.current_phase(ed) in ("reaping", "second_reaping") else None
        await self._edit(channel, ed["messages"].get("reaping"), embed=embeds.reaping_embed(ed), view=view)

    # =================================================================================================
    # the draft
    # =================================================================================================

    async def _draft(self, guild, s: dict, ed: dict, k: int, now: float) -> list:
        """Draw k tributes (fakes in a test run). Adds them to the roster and returns their ids."""
        if k <= 0:
            return []
        if ed.get("test"):
            out = []
            for name in TEST_NAMES:
                uid = "fake" + name.split()[0].lower().replace("-", "").replace(".", "")
                if uid in ed["roster"] or uid in ed["withdrawn"]:
                    continue
                ed["roster"][uid] = engine.new_tribute(name, "drafted", fake=True)
                out.append(uid)
                if len(out) >= k:
                    break
            return out
        counts = await self._messages_7d(guild, now)
        if counts is None:
            log.warning("hungergames: ServerPulse not available, drafting with equal odds")
        lurker_id = await self._lurker_role_id(guild)
        members = await self.config.all_members(guild)
        people = []
        for m in guild.members:
            people.append({
                "uid": m.id, "bot": m.bot,
                "messages": counts.get(str(m.id), 0) if counts is not None else MIN_MESSAGES_7D,
                "lurker": bool(lurker_id) and any(r.id == lurker_id for r in m.roles),
                "opt_out": members.get(m.id, {}).get("opt_out", False),
            })
        pool = engine.draft_pool(people, ed["roster"], min_messages=MIN_MESSAGES_7D,
                                 last_tributes=s["last_tributes"], withdrawn=ed["withdrawn"])
        drawn = engine.weighted_draw(pool, k, self.rng)
        for uid in drawn:
            member = guild.get_member(int(uid))
            ed["roster"][uid] = engine.new_tribute(member.display_name if member else uid, "drafted")
        return drawn

    # =================================================================================================
    # the loop
    # =================================================================================================

    async def _loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:
                    await self._tick(guild)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("hungergames: tick failed for guild %s", guild.id)
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self, guild, now: float | None = None) -> None:
        now = now or self._now()
        gconf = self.config.guild(guild)
        async with self._lock(guild.id):
            s = await gconf.all()
            ed = (s["current"] or None)
            if s["enabled"]:
                sunday, sched = engine.next_reaping(now)
                due = now >= sched[0][1] and s["last_sunday"] != sunday.isoformat()
                if due and ed is not None and ed.get("test"):
                    await self._send(self._channel(guild, s, ed), "🛑 Test run stopped: the real Reaping is starting.")
                    await self._cleanup_threads(guild, ed)
                    ed = None
                if due and ed is None:
                    await gconf.last_sunday.set(sunday.isoformat())
                    ed = await self._start_edition(guild, s, sched, now, test=False)
                    await gconf.current.set(ed)
                    return
            if ed is None:
                return
            b = ed.get("broadcast")
            if b and b["pos"] < len(b["queue"]):
                if now >= b["next_ts"]:
                    item = b["queue"][b["pos"]]
                    b["pos"] += 1
                    nxt = b["queue"][b["pos"]] if b["pos"] < len(b["queue"]) else None
                    slow = item["t"] == "line" and nxt is not None and nxt["t"] == "line"
                    gap = TEST_POST_GAP if ed.get("test") else (POST_GAP if slow else SCENE_GAP)
                    b["next_ts"] = now + self.rng.uniform(*gap)
                    await gconf.current.set(ed)   # mark it posted first: never double-post after a crash
                    await self._post_item(guild, s, ed, item)
                    if item["t"] == "victor":
                        await self._finish(guild, s, ed)
                        return
                    await gconf.current.set(ed)
                return
            if b is not None:
                ed["broadcast"] = None
            step = engine.due_step(ed, now)
            if step is None:
                if b is not None:
                    await gconf.current.set(ed)
                return
            handler = getattr(self, f"_step_{step}")
            result = await handler(guild, s, ed, now)
            if result == "cancelled":
                await gconf.current.set({})
                return
            ed["step"] += 1
            await gconf.current.set(ed)

    # =================================================================================================
    # steps
    # =================================================================================================

    async def _start_edition(self, guild, s: dict, sched: list, now: float, *, test: bool, admin=None) -> dict:
        number = 0 if test else s["edition_number"] + 1
        channel_id = s["test_channel_id"] if test else s["channel_id"]
        ed = engine.new_edition(number, sched, test=test, channel_id=channel_id, rng=self.rng)
        drawn = await self._draft(guild, s, ed, DRAW_AT_REAPING, now)
        if admin is not None:
            ed["roster"][str(admin.id)] = engine.new_tribute(admin.display_name, "volunteer")
        ed["step"] = 1   # the Reaping itself
        channel = self._channel(guild, s, ed)
        prefix, mentions = self._pings(s, ed, drawn)
        names = ", ".join(self._mention(ed, u) for u in drawn) or "nobody (no one was eligible)"
        text = (f"{prefix}🎯 **{engine.edition_title(ed)}** begins! The Reaping is open.\n"
                f"Hana reaches into the bowl and draws: {names}.\n"
                "May the odds be ever in your favor. 🙋 Volunteers, the seats are open!")
        msg = await self._send(channel, text, embed=embeds.reaping_embed(ed), view=ui.ReapingView(self),
                               allowed_mentions=mentions)
        if msg is not None:
            ed["messages"]["reaping"] = msg.id
            await self._pin(channel, msg, ed)
        await self._teaser(guild, s, ed, f"🎯 **{engine.edition_title(ed)}**: the Reaping is open in {channel.mention if channel else 'the Hunger Games channel'}. Volunteer as tribute!")
        return ed

    async def _step_second_reaping(self, guild, s, ed, now):
        need = max(0, TOPUP_TARGET - len(ed["roster"]))
        drawn = await self._draft(guild, s, ed, need, now)
        channel = self._channel(guild, s, ed)
        if drawn:
            prefix, mentions = self._pings(s, ed, drawn)
            await self._send(channel, f"{prefix}🎲 **Second Reaping!** Hana draws {', '.join(self._mention(ed, u) for u in drawn)}. "
                             "Volunteers have until midnight to take a drafted tribute's place.",
                             allowed_mentions=mentions)
        else:
            await self._send(channel, "🎲 **Second Reaping:** no extra draw needed. Volunteers have until midnight to swap in.")
        await self._render_reaping(guild, s, ed)

    async def _step_lock(self, guild, s, ed, now):
        channel = self._channel(guild, s, ed)
        for target, req in ed["swap_requests"].items():
            await self._edit(channel, req.get("message_id"), content=f"⌛ The Reaping closed. {self._mention(ed, target)} keeps their seat.",
                             view=None, allowed_mentions=NONE)
        ed["swap_requests"] = {}
        await self._edit(channel, ed["messages"].get("reaping"), embed=embeds.reaping_embed(ed, closed=True), view=None)
        if len(ed["roster"]) < MIN_TRIBUTES:
            await self._send(channel, f"😶 Only {len(ed['roster'])} tributes this week, and the Games need {MIN_TRIBUTES}. "
                             "The Gamemakers cancel this one. See you next Sunday!")
            return "cancelled"
        if not ed.get("test"):
            await self.config.guild(guild).edition_number.set(ed["edition"])
            for uid, t in ed["roster"].items():
                if not uid.isdigit():
                    continue
                mconf = self.config.member_from_ids(guild.id, int(uid))
                await mconf.editions.set(await mconf.editions() + 1)
                key = "volunteered" if t["origin"] == "volunteer" else "drafted"
                await getattr(mconf, key).set(await getattr(mconf, key)() + 1)
        await self._send(channel, embed=embeds.roster_embed(
            ed, f"🔒 The tributes of {engine.edition_title(ed)}",
            f"Training opens {embeds.ts(engine.step_ts(ed['schedule'], 'training'), 'F')}."))

    async def _step_training(self, guild, s, ed, now):
        channel = self._channel(guild, s, ed)
        prefix, mentions = self._pings(s, ed, [])
        await self._send(channel, f"🏋️ **Training is open!** Each tribute has their own thread below. Everyone can watch. "
                         f"Cheer for your favorite interview answers. {embeds.challenge_menu_line()}")
        for uid, t in ed["roster"].items():
            if t.get("fake"):
                self._fake_training(ed, uid)
                continue
            starter = await self._send(channel, f"🏋️ Training Center — **{t['name']}**")
            if starter is None:
                continue
            try:
                thread = await starter.create_thread(name=f"Training — {t['name']}"[:100], auto_archive_duration=1440)
            except _DISCORD_ERRORS:
                log.warning("hungergames: could not open a training thread (needs Create Public Threads)")
                continue
            ed["threads"][str(thread.id)] = uid
            ed.setdefault("starters", {})[str(thread.id)] = starter.id
            await self._send(thread, embeds.training_intro(ed, uid), view=ui.TrainingView(self),
                             allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(uid))]))

    def _fake_training(self, ed: dict, uid: str) -> None:
        t = ed["roster"][uid]
        for kind in CHALLENGES:
            t["challenges"][kind] = {"acc": self.rng.uniform(20, 70), "time": self.rng.uniform(8, 40), "finished": True}
        t["interview"] = {"q": "test", "a": "beep boop", "message_id": None}
        ed["cheers"][uid] = [f"v{i}" for i in range(self.rng.randint(0, 5))]

    async def _step_training_close(self, guild, s, ed, now):
        engine.score_challenges(ed["roster"])
        charisma = engine.charisma_scores(ed["cheers"], list(ed["roster"]))
        for uid, t in ed["roster"].items():
            t["stats"]["charisma"] = charisma[uid]
        await self._lock_threads(guild, ed)
        await self._send(self._channel(guild, s, ed),
                         f"🔒 **Training is over.** The Gamemakers are scoring. Scores and sponsorship open "
                         f"{embeds.ts(engine.step_ts(ed['schedule'], 'scores'), 'F')}.")

    async def _thread(self, guild, tid):
        """The thread object, or None if it is gone or unreachable."""
        try:
            return guild.get_channel_or_thread(int(tid)) or await guild.fetch_channel(int(tid))
        except _DISCORD_ERRORS:
            return None

    async def _lock_threads(self, guild, ed):
        """Training is over: lock the training threads so they stay readable (everyone can see how the
        others did) but nobody can post in them. Needs Manage Threads."""
        for tid in ed.get("threads", {}):
            thread = await self._thread(guild, tid)
            if thread is None:
                continue
            try:
                await thread.edit(locked=True)
            except _DISCORD_ERRORS:
                log.warning("hungergames: could not lock thread %s (needs Manage Threads)", tid, exc_info=True)

    async def _cleanup_threads(self, guild, ed):
        """The edition is over: delete its training threads and the starter posts they hang from. The
        starter post is only deleted once its thread is gone, so a failed delete never orphans a thread."""
        for tid in list(ed.get("threads", {})):
            thread = await self._thread(guild, tid)
            if thread is not None:
                try:
                    await thread.delete()
                except _DISCORD_ERRORS:
                    log.warning("hungergames: could not delete thread %s (needs Manage Threads)", tid, exc_info=True)
                    continue
            starter = ed.get("starters", {}).get(tid)
            channel = guild.get_channel(ed.get("channel_id") or 0)
            if starter and channel is not None:
                try:
                    await channel.get_partial_message(starter).delete()
                except _DISCORD_ERRORS:
                    log.debug("hungergames: could not delete starter post %s", starter, exc_info=True)

    async def _step_scores(self, guild, s, ed, now):
        channel = self._channel(guild, s, ed)
        prefix, mentions = self._pings(s, ed, [])
        msg = await self._send(channel, f"{prefix}📊 The Training Scores are in. **Sponsorship is open!**",
                               embed=embeds.scores_embed(ed), view=ui.ArenaView(self), allowed_mentions=mentions)
        if msg is not None:
            ed["messages"]["scores"] = msg.id
            await self._pin(channel, msg, ed)

    async def _step_broadcast(self, guild, s, ed, now):
        gone = [u for u, t in ed["roster"].items()
                if t["alive"] and not t.get("fake") and guild.get_member(int(u)) is None]
        number = ed["broadcasts_done"] + 1
        queue = engine.run_broadcast(ed, self.pool, self.rng, gone=gone)
        ed["broadcast"] = {"queue": queue, "pos": 0, "next_ts": now + 5}
        prefix, mentions = self._pings(s, ed, [])
        if number > 4:
            text = f"{prefix}🏟️ **The Finale of {engine.edition_title(ed)} is starting now!** Only one walks out."
        else:
            text = f"{prefix}📺 **{engine.edition_title(ed)}, broadcast {number}** is starting now! Sponsorship pauses until it ends."
        await self._send(self._channel(guild, s, ed), text, allowed_mentions=mentions)

    _step_finale = _step_broadcast

    def _avatar_for(self, guild, ed):
        def url(uid):
            t = ed["roster"].get(str(uid))
            if not t or t.get("fake") or not str(uid).isdigit():
                return None
            member = guild.get_member(int(uid))
            avatar = getattr(member, "display_avatar", None)
            return avatar.url if avatar is not None else None
        return url

    @staticmethod
    def _next_update(b: dict) -> tuple:
        """(when the next post is due, a flavour line) for the card's countdown; none after the last post."""
        if b["pos"] >= len(b["queue"]):
            return None, ""
        return b["next_ts"], NEXT_FLAVOR[b["pos"] % len(NEXT_FLAVOR)]

    async def _edit_card(self, channel, message_id, embed) -> bool:
        if channel is None or not message_id:
            return False
        try:
            await channel.get_partial_message(message_id).edit(embed=embed)
            return True
        except _DISCORD_ERRORS:
            log.debug("hungergames: could not edit scene card %s", message_id, exc_info=True)
            return False

    async def _close_card(self, guild, channel, ed):
        """The scene is over: redraw its card without the "next update" countdown, which would go stale."""
        b = ed["broadcast"]
        card = b.get("card")
        if not card or not card.get("id"):
            return
        queue = b["queue"]
        head = queue[card["head"]] if card["head"] is not None else {"title": "📺 Live from the arena", "scene": ""}
        lines = [i for i in queue[card["start"]:b["pos"] - 1] if i["t"] == "line"]
        await self._edit_card(channel, card["id"], embeds.scene_embed(head, lines, self._avatar_for(guild, ed)))

    async def _post_scene_line(self, guild, channel, ed, item):
        """A broadcast line goes into the current scene card (one message per phase, edited as lines arrive).
        The card is rebuilt from the saved queue each time, so a restart or a deleted card never loses a line."""
        b = ed["broadcast"]
        queue, idx = b["queue"], b["pos"] - 1
        avatar = self._avatar_for(guild, ed)
        card = b.get("card")
        if card is None:
            card = {"id": None, "start": idx, "head": None}
        head = queue[card["head"]] if card["head"] is not None else {"title": "📺 Live from the arena", "scene": ""}
        lines = [i for i in queue[card["start"]:idx + 1] if i["t"] == "line"]
        if card["id"] and embeds.scene_text_len(head, lines) > embeds.CARD_LIMIT:
            card = {"id": None, "start": idx, "head": card["head"]}   # full: carry on in a fresh card, same header
            head = queue[card["head"]] if card["head"] is not None else head
            lines = [item]
        embed = embeds.scene_embed(head, lines, avatar, *self._next_update(b))
        if card["id"] and await self._edit_card(channel, card["id"], embed):
            b["card"] = card
            return
        msg = await self._send(channel, embed=embed)
        if msg is not None:
            card["id"] = msg.id
            b["card"] = card

    async def _post_item(self, guild, s, ed, item):
        channel = self._channel(guild, s, ed)
        kind = item["t"]
        b = ed["broadcast"]
        if kind != "line":
            await self._close_card(guild, channel, ed)
        if kind == "header":
            avatar = self._avatar_for(guild, ed)
            msg = await self._send(channel, embed=embeds.scene_embed(item, [], avatar, *self._next_update(b)))
            b["card"] = {"id": msg.id if msg is not None else None, "start": b["pos"], "head": b["pos"] - 1}
        elif kind == "line":
            await self._post_scene_line(guild, channel, ed, item)
        elif kind == "fallen":
            b["card"] = None
            final = item.get("night") is None
            await self._send(channel, embed=embeds.fallen_embed(ed, item, self._avatar_for(guild, ed)),
                             view=None if final else ui.ArenaView(self))
        elif kind == "victor":
            b["card"] = None
            await self._crown(guild, s, ed)

    async def _crown(self, guild, s, ed):
        channel = self._channel(guild, s, ed)
        uid = ed["victor"]
        currency = "coins"
        prize = minted = 0
        member = guild.get_member(int(uid)) if uid.isdigit() and not ed["roster"][uid].get("fake") else None
        if not ed.get("test"):
            try:
                currency = await bank.get_currency_name(guild)
            except Exception:
                pass
            prize, minted = engine.prize(engine.total_spend(ed), s["prize_share"], s["prize_floor"])
            if member is not None:
                try:
                    await bank.deposit_credits(member, prize)
                except Exception:
                    log.exception("hungergames: could not pay the victor")
                    prize = minted = 0
                role = guild.get_role(s["victor_role_id"]) if s["victor_role_id"] else None
                if role is not None:
                    try:
                        await member.add_roles(role, reason=f"Victor of {engine.edition_title(ed)}")
                    except _DISCORD_ERRORS:
                        log.warning("hungergames: could not give the Victor role")
            else:
                prize = minted = 0
        ed["prize"], ed["minted"] = prize, minted
        wins = None
        if not ed.get("test") and uid.isdigit():
            wins = (await self.config.member_from_ids(guild.id, int(uid)).wins()) + 1   # counting this one
        awards = engine.awards(ed)
        prefix, mentions = self._pings(s, ed, [uid])
        msg = await self._send(channel, f"{prefix}🏆 {self._mention(ed, uid)} is the Victor of **{engine.edition_title(ed)}**!",
                               embed=embeds.crowning_embed(ed, guild, prize, currency, awards, wins), allowed_mentions=mentions)
        await self._pin(channel, msg, ed)
        await self._teaser(guild, s, ed, f"🏆 {self._mention(ed, uid)} won **{engine.edition_title(ed)}**! "
                                         f"See the Crowning in {channel.mention if channel else 'the Hunger Games channel'}.")
        if ed.get("test"):
            return
        for tid, t in ed["roster"].items():
            if not tid.isdigit():
                continue
            mconf = self.config.member_from_ids(guild.id, int(tid))
            m = await mconf.all()
            await mconf.kills.set(m["kills"] + t["kills"])
            await mconf.deaths.set(m["deaths"] + (0 if t["alive"] else 1))
            await mconf.best_score.set(max(m["best_score"], t["score"]))
            if tid == uid:
                await mconf.wins.set(m["wins"] + 1)
        hall = s["hall"]
        t = ed["roster"][uid]
        hall.append({"edition": ed["edition"], "title": engine.edition_title(ed), "uid": uid, "name": t["name"],
                     "score": t["score"], "kills": t["kills"], "prize": prize, "ts": self._now()})
        await self.config.guild(guild).hall.set(hall)

    async def _finish(self, guild, s, ed):
        gconf = self.config.guild(guild)
        reports = (await gconf.reports())[-(REPORTS_KEPT - 1):]
        reports.append(engine.edition_report(ed))
        await gconf.reports.set(reports)
        await self._cleanup_threads(guild, ed)
        if not ed.get("test"):
            await gconf.last_tributes.set([u for u in ed["roster"] if u.isdigit()])
            await gconf.last_puzzles.set(ed["puzzles_used"])
            await gconf.last_templates.set(ed["used"])
        await gconf.current.set({})

    # =================================================================================================
    # Reaping buttons
    # =================================================================================================

    async def _reaping_ed(self, interaction):
        """(settings, edition) if the Reaping is open, else (None, None) after telling the member."""
        s = await self.config.guild(interaction.guild).all()
        ed = (s["current"] or None)
        if not ed or engine.current_phase(ed) not in ("reaping", "second_reaping"):
            await ui.say(interaction, "The Reaping isn't open right now.")
            return None, None
        return s, ed

    async def on_volunteer(self, interaction):
        guild, member = interaction.guild, interaction.user
        uid = str(member.id)
        async with self._lock(guild.id):
            s, ed = await self._reaping_ed(interaction)
            if ed is None:
                return
            if member.bot or await self._is_lurker(member):
                await ui.say(interaction, "Say hi in chat first, then come back to volunteer!")
                return
            if uid in ed["roster"]:
                await ui.say(interaction, "You're already a tribute in these Games.")
                return
            self._touch(ed, uid)
            if len(ed["roster"]) < MAX_TRIBUTES:
                ed["roster"][uid] = engine.new_tribute(member.display_name, "volunteer")
                ed["counts"]["volunteers"] += 1
                if uid in ed["withdrawn"]:
                    ed["withdrawn"].remove(uid)
                await self.config.guild(guild).current.set(ed)
                await ui.say(interaction, "🙋 You volunteered as tribute! Training opens Monday morning in your own thread.")
                await self._send(self._channel(guild, s, ed), f"🙋 {member.mention} volunteers as tribute!")
                await self._render_reaping(guild, s, ed)
                return
            await self.config.guild(guild).current.set(ed)
            if uid in ed["swap_used"]:
                await ui.say(interaction, "Every seat is taken, and you've already used your one swap request this Games.")
                return
            targets = [u for u, t in ed["roster"].items() if t["origin"] == "drafted" and t.get("tag")]
        if not targets:
            await ui.say(interaction, "Every seat is taken by volunteers or drafted tributes who chose to keep their spot.")
            return
        options = [discord.SelectOption(label=ed["roster"][u]["name"][:100], value=u) for u in targets]

        async def picked(inter, target):
            await self._swap_request(inter, target)

        await ui.say(interaction, "All 12 seats are full. Pick **one** drafted tribute to ask for their seat. "
                     "You only get one request this Games, so choose wisely:",
                     view=ui.PickView(member.id, options, picked, "Ask a drafted tribute"))

    async def _swap_request(self, interaction, target: str):
        guild, member = interaction.guild, interaction.user
        uid = str(member.id)
        await self._ack(interaction)
        async with self._lock(guild.id):
            s, ed = await self._reaping_ed(interaction)
            if ed is None:
                return
            t = ed["roster"].get(target)
            if uid in ed["roster"] or uid in ed["swap_used"]:
                await ui.say(interaction, "You can't send a swap request now.")
                return
            if t is None or t["origin"] != "drafted" or not t.get("tag"):
                await ui.say(interaction, "That tribute isn't open to swaps any more.")
                return
            ed["swap_used"].append(uid)
            req = ed["swap_requests"].setdefault(target, {"volunteers": [], "message_id": None})
            req["volunteers"].append(uid)
            ed["counts"]["swap_requests"] += 1
            channel = self._channel(guild, s, ed)
            asks = ", ".join(f"<@{v}>" for v in req["volunteers"])
            text = (f"🤝 {self._mention(ed, target)}, {asks} would take your seat in {engine.edition_title(ed)}. "
                    "Accept one of them, or keep your spot. No answer means you stay in.")
            if req["message_id"] is None:   # the drafted tribute is pinged once, on the first request only
                users = [discord.Object(id=int(target))] if target.isdigit() else []
                msg = await self._send(channel, text, view=ui.SwapView(self),
                                       allowed_mentions=discord.AllowedMentions(users=users, roles=False, everyone=False))
                req["message_id"] = msg.id if msg else None
            else:
                await self._edit(channel, req["message_id"], content=text, allowed_mentions=NONE)
            await self.config.guild(guild).current.set(ed)
        await self._done(interaction, content="✅ Swap request sent. If they accept, you're in.", view=None)

    def _target_for_message(self, ed: dict, message_id: int):
        for target, req in ed["swap_requests"].items():
            if req.get("message_id") == message_id:
                return target
        return None

    async def on_swap_accept(self, interaction):
        guild = interaction.guild
        async with self._lock(guild.id):
            s, ed = await self._reaping_ed(interaction)
            if ed is None:
                return
            target = self._target_for_message(ed, interaction.message.id)
            if target != str(interaction.user.id):
                await ui.say(interaction, "Only the drafted tribute can answer this.")
                return
            vols = [v for v in ed["swap_requests"][target]["volunteers"]
                    if v not in ed["roster"] and guild.get_member(int(v)) is not None]
        if not vols:
            await ui.say(interaction, "None of those volunteers can take your seat any more.")
            return
        options = [discord.SelectOption(label=guild.get_member(int(v)).display_name[:100], value=v) for v in vols]

        async def picked(inter, vol):
            await self._do_swap(inter, target, vol)

        await ui.say(interaction, "Who gets your seat?", view=ui.PickView(interaction.user.id, options, picked, "Pick a volunteer"))

    async def _do_swap(self, interaction, target: str, vol: str):
        guild = interaction.guild
        await self._ack(interaction)
        async with self._lock(guild.id):
            s, ed = await self._reaping_ed(interaction)
            if ed is None:
                return
            member = guild.get_member(int(vol))
            if target not in ed["roster"] or vol in ed["roster"] or member is None:
                await ui.say(interaction, "That swap can't happen any more.")
                return
            req = ed["swap_requests"].pop(target, {})
            del ed["roster"][target]
            ed["withdrawn"].append(target)
            ed["roster"][vol] = engine.new_tribute(member.display_name, "volunteer")
            ed["counts"]["swaps"] += 1
            self._touch(ed, target)
            await self.config.guild(guild).current.set(ed)
            channel = self._channel(guild, s, ed)
            await self._edit(channel, req.get("message_id"), content=f"🤝 <@{target}> handed their seat to <@{vol}>.",
                             view=None, allowed_mentions=NONE)
            await self._send(channel, f"🙋 <@{vol}> takes <@{target}>'s place! *I volunteer as tribute!*")
            await self._render_reaping(guild, s, ed)
        await self._done(interaction, content="Done. Thanks for being a good sport!", view=None)

    async def on_withdraw(self, interaction):
        uid = str(interaction.user.id)
        s, ed = await self._reaping_ed(interaction)
        if ed is None:
            return
        t = ed["roster"].get(uid)
        if t is None or t["origin"] != "drafted":
            await ui.say(interaction, "Only drafted tributes can withdraw. (Volunteers chose this!)")
            return

        async def chosen(inter, value):
            if value == "stay":
                await inter.response.edit_message(content="You're staying in. Good luck! ✊", view=None)
                return
            await self._do_withdraw(inter, value == "never")

        await ui.say(interaction, "Withdraw from these Games? Your seat goes to a volunteer who asked for it, "
                     "or to a fresh draw.", view=ui.ConfirmView(interaction.user.id, [
                         ("Withdraw", "danger", "once"), ("Withdraw and never draft me", "danger", "never"),
                         ("Stay in", "secondary", "stay")], chosen))

    async def _do_withdraw(self, interaction, never: bool):
        guild, member = interaction.guild, interaction.user
        uid = str(member.id)
        now = self._now()
        await self._ack(interaction)
        async with self._lock(guild.id):
            s, ed = await self._reaping_ed(interaction)
            if ed is None:
                return
            if uid not in ed["roster"] or ed["roster"][uid]["origin"] != "drafted":
                await ui.say(interaction, "You're not a drafted tribute any more.")
                return
            seat = engine.seat_for_withdrawal(ed, uid)
            req = ed["swap_requests"].pop(uid, {})
            del ed["roster"][uid]
            ed["withdrawn"].append(uid)
            ed["counts"]["withdrawals"] += 1
            self._touch(ed, uid)
            channel = self._channel(guild, s, ed)
            note = f"🏳️ {member.mention} withdraws from {engine.edition_title(ed)}."
            mentions = NONE
            if seat is not None and guild.get_member(int(seat)) is not None:
                ed["roster"][seat] = engine.new_tribute(guild.get_member(int(seat)).display_name, "volunteer")
                note += f" <@{seat}>, who asked for the seat, takes it! 🙋"
                mentions = discord.AllowedMentions(users=[discord.Object(id=int(seat))])
            elif engine.current_phase(ed) == "reaping":
                drawn = await self._draft(guild, s, ed, 1, now)
                if drawn:
                    note += f" Hana draws a replacement: {self._mention(ed, drawn[0])}."
                    mentions = discord.AllowedMentions(users=[discord.Object(id=int(d)) for d in drawn if d.isdigit()])
            else:
                note += " The seat is open for a volunteer."
            if never:
                await self.config.member(member).opt_out.set(True)
            await self.config.guild(guild).current.set(ed)
            await self._edit(channel, req.get("message_id"), content=f"🏳️ {member.mention} withdrew.", view=None,
                             allowed_mentions=NONE)
            await self._send(channel, note, allowed_mentions=mentions)
            await self._render_reaping(guild, s, ed)
        extra = " You won't be drafted again until you press **Never draft me / Draft me again**." if never else ""
        await self._done(interaction, content="You've withdrawn." + extra, view=None)

    async def on_keep(self, interaction):
        guild = interaction.guild
        uid = str(interaction.user.id)
        async with self._lock(guild.id):
            s, ed = await self._reaping_ed(interaction)
            if ed is None:
                return
            t = ed["roster"].get(uid)
            if t is None or t["origin"] != "drafted":
                await ui.say(interaction, "That's only for drafted tributes.")
                return
            t["tag"] = False
            req = ed["swap_requests"].pop(uid, {})
            self._touch(ed, uid)
            await self.config.guild(guild).current.set(ed)
            channel = self._channel(guild, s, ed)
            await self._edit(channel, req.get("message_id"), content=f"✊ <@{uid}> is keeping their spot.", view=None,
                             allowed_mentions=NONE)
            await self._render_reaping(guild, s, ed)
        await ui.say(interaction, "✊ You're keeping your spot. The drafted tag is gone and nobody can ask for your seat now.")

    async def on_optout(self, interaction):
        mconf = self.config.member(interaction.user)
        now_out = not await mconf.opt_out()
        await mconf.opt_out.set(now_out)
        await ui.say(interaction, "🚫 You won't be drafted any more. You can still volunteer any time."
                     if now_out else "✅ You can be drafted again. May the odds be ever in your favor!")

    async def on_pings(self, interaction):
        s = await self.config.guild(interaction.guild).all()
        role = interaction.guild.get_role(s["ping_role_id"]) if s["ping_role_id"] else None
        if role is None:
            await ui.say(interaction, "The ping role isn't set up yet.")
            return
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Hunger Games pings off")
                await ui.say(interaction, "🔕 You won't get Hunger Games pings any more.")
            else:
                await member.add_roles(role, reason="Hunger Games pings on")
                await ui.say(interaction, "🔔 You'll be pinged for the Reaping, broadcasts and the Crowning.")
        except _DISCORD_ERRORS:
            await ui.say(interaction, "I couldn't change your roles. Ask a mod.")

    # =================================================================================================
    # training
    # =================================================================================================

    async def on_train(self, interaction, kind: str):
        guild = interaction.guild
        uid = str(interaction.user.id)
        async with self._lock(guild.id):
            s = await self.config.guild(guild).all()
            ed = (s["current"] or None)
            if not ed or engine.current_phase(ed) != "training":
                await ui.say(interaction, "Training isn't open right now.")
                return
            if ed["threads"].get(str(interaction.channel.id)) != uid:
                await ui.say(interaction, "Use the buttons in your own training thread.")
                return
            t = ed["roster"][uid]
            self._touch(ed, uid)
            if kind == "interview":
                if t.get("interview"):
                    await ui.say(interaction, "You've already given your interview.")
                    return
                question = self.rng.choice(content.INTERVIEW_QUESTIONS)
                await self.config.guild(guild).current.set(ed)
                await ui.say(interaction, f"🎤 {question}\n(Up to 300 characters. It's posted publicly for cheers.)",
                             view=ui.InterviewView(self, interaction.user.id, question))
                return
            if kind in t["challenges"]:
                await ui.say(interaction, "One attempt per challenge, and you've used this one.")
                return
            t["challenges"][kind] = {"started": self._now()}
            puzzles = None
            if kind == "intellect":
                puzzles = engine.pick_puzzles(self.ciphers, self.rng, ed["puzzles_used"] + s["last_puzzles"])
                ed["puzzles_used"] += [p["id"] for p in puzzles]
            await self.config.guild(guild).current.set(ed)
        thread, member = interaction.channel, interaction.user
        if kind == "agility":
            session = ui.SprintChallenge(self, guild, member.id, thread, self.rng)
        elif kind == "strength":
            session = ui.CombatChallenge(self, guild, member.id, thread, self.rng)
        elif kind == "endurance":
            session = ui.MarchChallenge(self, guild, member.id, thread, self.rng)
        else:
            session = ui.CipherChallenge(self, guild, member.id, thread, self.rng, puzzles)
        self._sessions[(guild.id, member.id, kind)] = session
        if kind == "endurance":
            await session.start(interaction)
        else:
            await interaction.response.send_message(session.prompt(), view=session, ephemeral=True)

    async def finish_challenge(self, guild, user_id: int, kind: str, result: dict):
        self._sessions.pop((guild.id, user_id, kind), None)
        async with self._lock(guild.id):
            ed = (await self.config.guild(guild).current() or None)
            uid = str(user_id)
            if not ed or uid not in ed["roster"] or engine.current_phase(ed) != "training":
                return   # finished after training closed: the attempt doesn't count
            ed["roster"][uid]["challenges"][kind] = {"acc": round(result["acc"], 2), "time": round(result["time"], 2),
                                                     "finished": bool(result["finished"])}
            await self.config.guild(guild).current.set(ed)

    async def submit_interview(self, interaction, question: str, answer: str):
        guild = interaction.guild
        uid = str(interaction.user.id)
        async with self._lock(guild.id):
            s = await self.config.guild(guild).all()
            ed = (s["current"] or None)
            if not ed or engine.current_phase(ed) != "training" or uid not in ed["roster"]:
                await ui.say(interaction, "Training is over, so the interview is closed.")
                return
            t = ed["roster"][uid]
            if t.get("interview"):
                await ui.say(interaction, "You've already given your interview.")
                return
            if not answer:
                await ui.say(interaction, "Caesar waits... but you didn't say anything. Try again.")
                return
            e = discord.Embed(title=f"🎤 {t['name']} talks to Caesar Flickerman", color=discord.Color(0xD4A017),
                              description=f"*{question}*\n\n> {answer}")
            e.set_footer(text="Cheer once for each tribute you like. Cheers become their Charisma score.")
            msg = await self._send(self._channel(guild, s, ed), embed=e, view=ui.CheerView(self))
            t["interview"] = {"q": question, "a": answer, "message_id": msg.id if msg else None}
            if msg is not None:
                ed["interview_msgs"][str(msg.id)] = uid
            await self.config.guild(guild).current.set(ed)
        await ui.say(interaction, "🎤 Your interview is live in the channel. Go collect some cheers!")

    async def on_cheer(self, interaction):
        guild = interaction.guild
        voter = str(interaction.user.id)
        async with self._lock(guild.id):
            ed = (await self.config.guild(guild).current() or None)
            uid = (ed or {}).get("interview_msgs", {}).get(str(interaction.message.id))
            if not ed or uid is None or engine.current_phase(ed) != "training":
                await ui.say(interaction, "Cheering for this interview has closed.")
                return
            if voter == uid:
                await ui.say(interaction, "Cheering for yourself? The Capitol saw that. 👀")
                return
            cheers = ed["cheers"].setdefault(uid, [])
            if voter in cheers:
                await ui.say(interaction, "You've already cheered for this tribute.")
                return
            cheers.append(voter)
            ed["counts"]["cheers"] += 1
            self._touch(ed, voter)
            await self.config.guild(guild).current.set(ed)
            count = len(cheers)
        try:
            await interaction.response.edit_message(view=ui.CheerView(self, count))
        except _DISCORD_ERRORS:
            pass
        await ui.say(interaction, f"👏 Cheered for {ed['roster'][uid]['name']}!")

    # =================================================================================================
    # the arena: sponsorship and plans
    # =================================================================================================

    async def on_sponsor(self, interaction):
        s = await self.config.guild(interaction.guild).all()
        if not engine.shop_open((s["current"] or None)):
            await ui.say(interaction, "The sponsorship shop is closed right now. It opens with the Training Scores "
                         "on Tuesday and pauses during each broadcast.")
            return
        if await self._is_lurker(interaction.user):
            await ui.say(interaction, "Say hi in chat first, then come back to sponsor!")
            return
        await self.render_shop(interaction, None, None)

    def _shop_state(self, s: dict, ed: dict, sponsor: str, tribute: str | None, now: float):
        today = engine.day_key(now)
        prices = {k: engine.unit_price(s["prices"].get(k, ITEMS[k][2]), now, test=ed.get("test")) for k in ITEMS}
        tributes = []
        for uid in engine.ranking(ed["roster"]):
            t = ed["roster"][uid]
            if t["alive"] and uid != sponsor:
                held = len(t["inventory"]["gear"]) + len(t["inventory"]["consumables"])
                tributes.append((uid, t["name"], f"score {t['score']} · holding {held} item(s)"))
        blocked, items = {}, []
        if tribute:
            for key in ITEMS:
                err = engine.purchase_error(ed, sponsor, tribute, key, today)
                if err:
                    blocked[key] = err
                else:
                    items.append((key, f"{ITEMS[key][0]} — {prices[key]:,}", ITEMS[key][4]))
        return prices, tributes, blocked, items

    async def render_shop(self, interaction, tribute, item):
        s = await self.config.guild(interaction.guild).all()
        ed = (s["current"] or None)
        if not engine.shop_open(ed):
            await ui.say(interaction, "The shop just closed. Try again after the broadcast.")
            return
        sponsor = str(interaction.user.id)
        prices, tributes, blocked, items = self._shop_state(s, ed, sponsor, tribute, self._now())
        if item is not None and item not in {k for k, _l, _d in items}:
            item = None
        embed = embeds.shop_embed(ed, tribute, prices, blocked)
        view = ui.ShopView(self, interaction.user.id, tributes, items, tribute, item)
        if tribute is None:   # opened from a Sponsor button: a fresh private shop
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:                 # a choice inside the shop: redraw it
            await interaction.response.edit_message(embed=embed, view=view)

    async def buy(self, interaction, tribute: str, item: str):
        guild, member = interaction.guild, interaction.user
        sponsor = str(member.id)
        now = self._now()
        await self._ack(interaction)
        async with self._lock(guild.id):
            s = await self.config.guild(guild).all()
            ed = (s["current"] or None)
            if not engine.shop_open(ed):
                await ui.say(interaction, "The shop just closed. Nothing was charged.")
                return
            err = engine.purchase_error(ed, sponsor, tribute, item, engine.day_key(now))
            if err:
                await ui.say(interaction, err + " Nothing was charged.")
                return
            price = engine.unit_price(s["prices"].get(item, ITEMS[item][2]), now, test=ed.get("test"))
            if not ed.get("test"):
                try:
                    await bank.withdraw_credits(member, price)
                except ValueError:
                    currency = await bank.get_currency_name(guild)
                    await ui.say(interaction, f"You need {price:,} {currency} for that parachute.")
                    return
                mconf = self.config.member(member)
                await mconf.items_sent.set(await mconf.items_sent() + 1)
                await mconf.coins_spent.set(await mconf.coins_spent() + price)
            engine.apply_purchase(ed, sponsor, tribute, item, price, engine.day_key(now), now)
            self._touch(ed, sponsor)
            await self.config.guild(guild).current.set(ed)
            name, emoji = ITEMS[item][0], ITEMS[item][1]
            await self._send(self._channel(guild, s, ed),
                             f"🪂 A silver parachute! {member.mention} sends {emoji} **{name}** to **{ed['roster'][tribute]['name']}**.")
        await self._done(interaction, content=f"🪂 Sent! {emoji} {name} is on its way to {ed['roster'][tribute]['name']} (-{price:,}).",
                         embed=None, view=None)

    async def on_plan(self, interaction):
        guild = interaction.guild
        uid = str(interaction.user.id)
        s = await self.config.guild(guild).all()
        ed = (s["current"] or None)
        phase = engine.current_phase(ed)
        if not ed or uid not in ed["roster"] or phase not in ("scores", "broadcast"):
            await ui.say(interaction, "Plans are for living tributes, between the Training Scores and the Finale.")
            return
        t = ed["roster"][uid]
        if not t["alive"] and uid not in engine.pending_deaths(ed):
            await ui.say(interaction, "You've fallen. Watch the rest from the stands. 🍿")
            return
        arena = engine.Arena(ed, {"templates": [], "lines": {}}, self.rng)
        possible = len(arena.allies_of(uid)) + 1 < 3 and len(engine.alive_ids(ed)) > 2

        async def picked(inter, value):
            if value == "ally":
                await self._pick_ally(inter)
                return
            await self._set_strategy(inter, value)

        await ui.say(interaction, embeds.plan_text(t) + " Choose your plan for the next broadcast:",
                     view=ui.PickView(interaction.user.id, ui.strategy_options(t["strategy"], possible), picked, "Your plan"))

    async def _set_strategy(self, interaction, value: str):
        guild = interaction.guild
        uid = str(interaction.user.id)
        async with self._lock(guild.id):
            ed = (await self.config.guild(guild).current() or None)
            if not ed or uid not in ed["roster"] or not ed["roster"][uid]["alive"] or value not in STRATEGIES:
                await ui.say(interaction, "That plan can't be set now.")
                return
            ed["roster"][uid]["strategy"] = value
            self._touch(ed, uid)
            await self.config.guild(guild).current.set(ed)
            t = ed["roster"][uid]
        await interaction.response.edit_message(content=embeds.plan_text(t) + " Saved.", view=None)

    async def _pick_ally(self, interaction):
        guild = interaction.guild
        uid = str(interaction.user.id)
        ed = (await self.config.guild(guild).current() or None)
        arena = engine.Arena(ed, {"templates": [], "lines": {}}, self.rng)
        mine = arena.allies_of(uid)
        cands = [u for u in engine.alive_ids(ed) if u != uid and u not in mine and not ed["roster"][u].get("fake")]
        if not cands:
            await interaction.response.edit_message(content="There's nobody left to ally with.", view=None)
            return
        options = [discord.SelectOption(label=ed["roster"][u]["name"][:100], value=u) for u in cands]

        async def picked(inter, other):
            await self._ally_request(inter, other)

        await interaction.response.edit_message(content="Who do you want to team up with? They have to accept.",
                                                view=ui.PickView(interaction.user.id, options, picked, "Pick a tribute"))

    async def _ally_request(self, interaction, other: str):
        guild = interaction.guild
        uid = str(interaction.user.id)
        async with self._lock(guild.id):
            s = await self.config.guild(guild).all()
            ed = (s["current"] or None)
            if not ed or not ed["roster"].get(uid, {}).get("alive") or not ed["roster"].get(other, {}).get("alive"):
                await ui.say(interaction, "That alliance can't happen now.")
                return
            if any(req[0] == uid for req in ed["ally_requests"].values()):
                await ui.say(interaction, "You already have an alliance request waiting for an answer.")
                return
            msg = await self._send(self._channel(guild, s, ed),
                                   f"🤝 <@{other}>, **{ed['roster'][uid]['name']}** wants to form an alliance with you. "
                                   "Allies spend time together and won't attack each other until the final four.",
                                   view=ui.AllyView(self),
                                   allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(other))]))
            if msg is not None:
                ed["ally_requests"][str(msg.id)] = [uid, other]
            self._touch(ed, uid)
            await self.config.guild(guild).current.set(ed)
        await interaction.response.edit_message(content="🤝 Alliance request sent.", view=None)

    async def on_ally_answer(self, interaction, accept: bool):
        guild = interaction.guild
        async with self._lock(guild.id):
            ed = (await self.config.guild(guild).current() or None)
            req = (ed or {}).get("ally_requests", {}).get(str(interaction.message.id))
            if not ed or req is None:
                await ui.say(interaction, "This alliance request has expired.")
                return
            a, b = req
            if str(interaction.user.id) != b:
                await ui.say(interaction, "Only the invited tribute can answer.")
                return
            del ed["ally_requests"][str(interaction.message.id)]
            err = engine.form_alliance(ed, a, b) if accept else None
            await self.config.guild(guild).current.set(ed)
            names = (ed["roster"][a]["name"], ed["roster"][b]["name"])
        if not accept:
            text = f"🙅 {names[1]} turns down {names[0]}'s alliance."
        elif err:
            text = f"🤝 {names[0]} and {names[1]} can't ally: {err}"
        else:
            text = f"🤝 **{names[0]}** and **{names[1]}** are now allies!"
        try:
            await interaction.response.edit_message(content=text, view=None, allowed_mentions=NONE)
        except _DISCORD_ERRORS:
            pass

    # =================================================================================================
    # member commands
    # =================================================================================================

    @commands.group(name="hg", aliases=["hungergames"], invoke_without_command=True)
    @commands.guild_only()
    async def hg(self, ctx: commands.Context):
        """The Hunger Games: status, sponsorship, your stats and the Hall of Victors."""
        s = await self.config.guild(ctx.guild).all()
        now = self._now()
        nxt = engine.next_reaping(now)[1][0][1] if s["enabled"] else None
        embed = embeds.status_embed((s["current"] or None), s["hall"], now, nxt, engine.shop_open((s["current"] or None)))
        await ctx.send(embed=embed, view=ui.PlayerView(self, ctx.author.id, engine.shop_open((s["current"] or None))),
                       allowed_mentions=NONE)

    @hg.command(name="version")
    async def hg_version(self, ctx: commands.Context):
        """Version probe."""
        await ctx.send(f"hungergames v{COG_VERSION}")

    @hg.command(name="admin")
    async def hg_admin(self, ctx: commands.Context):
        """Mods: the Hunger Games control panel."""
        if not await self._is_staff(ctx.author):
            await ctx.send("Only Staff and Moderators can open the Hunger Games panel.")
            return
        await open_panel(ctx, self, panel_specs.build_specs(), title="Hunger Games", intro=panel_specs.INTRO)

    async def on_my_stats(self, interaction):
        m = await self.config.member(interaction.user).all()
        await ui.say(interaction, None, embed=embeds.stats_embed(interaction.user.display_name, m))

    async def on_leaderboard(self, interaction):
        members = await self.config.all_members(interaction.guild)
        rows = []
        for mid, m in members.items():
            if m.get("wins") or m.get("kills"):
                member = interaction.guild.get_member(mid)
                rows.append((member.display_name if member else f"User {mid}", m.get("wins", 0), m.get("kills", 0)))
        rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
        await ui.say(interaction, None, embed=embeds.leaderboard_embed(rows[:15]))

    async def on_hall(self, interaction):
        hall = await self.config.guild(interaction.guild).hall()
        await ui.say(interaction, None, embed=embeds.hall_embed(hall))

    # =================================================================================================
    # the admin panel calls these
    # =================================================================================================

    async def _apply_enabled(self, guild, member, value) -> str:
        s = await self.config.guild(guild).all()
        if value and not self._channel(guild, s):
            raise PanelError("Pick the Hunger Games channel first.")
        await self.config.guild(guild).enabled.set(bool(value))
        if not value:
            return "Turned off. A running edition finishes; no new Reaping starts."
        sunday, sched = engine.next_reaping(self._now())
        return f"Turned on. The next Reaping opens {embeds.ts(sched[0][1], 'F')}."

    async def _set_id(self, guild, key: str, value) -> None:
        await getattr(self.config.guild(guild), key).set(value.id if value is not None else None)

    async def _apply_channel(self, guild, member, value) -> str:
        if value is None:
            raise PanelError("The Games need a channel.")
        await self._set_id(guild, "channel_id", value)
        return f"Channel set to {value.mention}."

    async def _apply_ping_role(self, guild, member, value) -> str:
        await self._set_id(guild, "ping_role_id", value)
        return "Ping role saved." if value else "No ping role: announcements won't ping."

    async def _apply_victor_role(self, guild, member, value) -> str:
        await self._set_id(guild, "victor_role_id", value)
        return "Victor role saved." if value else "No Victor role."

    async def _apply_teaser(self, guild, member, value) -> str:
        await self._set_id(guild, "teaser_channel_id", value)
        return f"Teasers go to {value.mention}." if value else "Teasers off."

    async def _apply_test_channel(self, guild, member, value) -> str:
        await self._set_id(guild, "test_channel_id", value)
        return f"Test runs post in {value.mention}." if value else "Test channel cleared."

    async def _apply_prize(self, guild, member, values) -> str:
        try:
            share = float(values["share"]) / 100
            floor = int(values["floor"].replace(",", ""))
        except (KeyError, ValueError):
            raise PanelError("Use numbers: a percent like 15 and a floor like 10000.")
        if not 0 <= share <= 1 or floor < 0:
            raise PanelError("The share must be 0-100% and the floor 0 or more.")
        conf = self.config.guild(guild)
        await conf.prize_share.set(share)
        await conf.prize_floor.set(floor)
        return f"Victor gets {share:.0%} of sponsorship spend, at least {floor:,}."

    async def _apply_prices(self, guild, member, values) -> str:
        prices = await self.config.guild(guild).prices()
        for key, raw in values.items():
            try:
                n = int(str(raw).replace(",", ""))
            except ValueError:
                raise PanelError(f"{ITEMS[key][0]}: use a whole number.")
            if n < 100:
                raise PanelError(f"{ITEMS[key][0]}: at least 100.")
            prices[key] = n
        await self.config.guild(guild).prices.set(prices)
        return "Prices saved (Tuesday prices; they rise each day up to x1.5 on Saturday)."

    def _describe_next(self, ed: dict | None, s: dict) -> str:
        if not ed:
            if not s["enabled"]:
                return "Nothing running and the Games are off."
            sunday, sched = engine.next_reaping(self._now())
            return f"Nothing running. The next Reaping opens {embeds.ts(sched[0][1], 'F')}."
        if ed.get("broadcast") and ed["broadcast"]["pos"] < len(ed["broadcast"]["queue"]):
            left = len(ed["broadcast"]["queue"]) - ed["broadcast"]["pos"]
            return f"{engine.edition_title(ed)}: a broadcast is posting ({left} lines left). Advancing waits for it to finish."
        if ed["step"] >= len(ed["schedule"]):
            return f"{engine.edition_title(ed)}: wrapping up."
        name, at = ed["schedule"][ed["step"]]
        return (f"{engine.edition_title(ed)}, phase: {engine.current_phase(ed)}. Next step: **{name}** "
                f"at {embeds.ts(at, 'F')}. Advancing runs it within a few seconds.")

    async def _act_preview(self, guild, member, _value) -> str:
        s = await self.config.guild(guild).all()
        return self._describe_next((s["current"] or None), s)

    async def _act_advance(self, guild, member, _value) -> str:
        async with self._lock(guild.id):
            ed = (await self.config.guild(guild).current() or None)
            if not ed:
                raise PanelError("Nothing is running.")
            if ed["step"] >= len(ed["schedule"]):
                raise PanelError("All steps have run.")
            name = ed["schedule"][ed["step"]][0]
            ed["schedule"][ed["step"]][1] = self._now()
            await self.config.guild(guild).current.set(ed)
        return f"Running **{name}** now."

    async def _act_cancel(self, guild, member, _value) -> str:
        async with self._lock(guild.id):
            s = await self.config.guild(guild).all()
            ed = (s["current"] or None)
            if not ed:
                raise PanelError("Nothing is running.")
            refunded = 0
            if not ed.get("test"):
                for row in ed["ledger"]:
                    sponsor = guild.get_member(int(row["sponsor"]))
                    if sponsor is not None:
                        try:
                            await bank.deposit_credits(sponsor, row["price"])
                            refunded += row["price"]
                        except Exception:
                            log.exception("hungergames: refund failed")
            await self.config.guild(guild).current.set({})
            await self._cleanup_threads(guild, ed)
            await self._send(self._channel(guild, s, ed), f"🛑 The Gamemakers have cancelled {engine.edition_title(ed)}."
                             + (f" Sponsors were refunded ({refunded:,} in total)." if refunded else ""))
        return f"Cancelled {engine.edition_title(ed)}." + (f" Refunded {refunded:,} to sponsors." if refunded else "")

    async def _act_signup(self, guild, member, _value) -> str:
        s = await self.config.guild(guild).all()
        channel = guild.get_channel(s["channel_id"]) if s["channel_id"] else None
        if channel is None:
            raise PanelError("Set the Hunger Games channel first (Setup tab).")
        _, sched = engine.next_reaping(self._now())
        embed, view = embeds.signup_embed(engine.step_ts(sched, "reaping")), ui.OptInView(self)
        if s["signup_message_id"]:
            try:
                await channel.get_partial_message(s["signup_message_id"]).edit(embed=embed, view=view)
                return f"Refreshed the sign-up message in {channel.mention}."
            except discord.NotFound:
                pass                                            # deleted by hand: post a fresh one
            except _DISCORD_ERRORS as exc:
                raise PanelError(f"I couldn't edit the sign-up message ({exc.__class__.__name__}).")
        msg = await self._send(channel, embed=embed, view=view)
        if msg is None:
            raise PanelError(f"I couldn't post in {channel.mention}. Check my Send Messages and Embed Links permissions.")
        await self.config.guild(guild).signup_message_id.set(msg.id)
        try:
            await msg.pin()
        except _DISCORD_ERRORS:
            return f"Posted in {channel.mention}, but I couldn't pin it (needs Manage Messages). Pin it by hand."
        return f"Posted and pinned in {channel.mention}."

    async def _act_test(self, guild, member, _value) -> str:
        async with self._lock(guild.id):
            s = await self.config.guild(guild).all()
            if (s["current"] or None):
                raise PanelError("An edition is already running. Cancel it first (if it's a test run).")
            if not s["test_channel_id"] or guild.get_channel(s["test_channel_id"]) is None:
                raise PanelError("Pick a test channel first (Setup tab). Use an owner-only channel.")
            ed = await self._start_edition(guild, s, engine.test_schedule(self._now()), self._now(), test=True, admin=member)
            await self.config.guild(guild).current.set(ed)
        return ("Test run started in the test channel with you as a tribute: Reaping now, training in about 80 seconds "
                "(5 minutes to play), then the shop and 4 broadcasts about 2 minutes apart (the shop is open between them) "
                "and the Finale, about 16 minutes in all. No coins move and nobody is pinged.")

    async def _act_report(self, guild, member, _value) -> str:
        s = await self.config.guild(guild).all()
        try:
            currency = await bank.get_currency_name(guild)
        except Exception:
            currency = "coins"
        parts = []
        if (s["current"] or None):
            parts.append("**Running now**\n" + embeds.report_text(engine.edition_report((s["current"] or None)), currency))
        for r in reversed(s["reports"][-3:]):
            parts.append(embeds.report_text(r, currency))
        pulse = "ServerPulse counts available" if self.bot.get_cog("ServerPulse") else "ServerPulse NOT loaded: drafts use equal odds"
        return ("\n\n".join(parts) or "No editions yet.") + f"\n\n*{pulse}.*"

    async def _status_get(self, guild):
        s = await self.config.guild(guild).all()
        return self._describe_next((s["current"] or None), s)
