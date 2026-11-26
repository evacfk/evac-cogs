"""Discord side of daily quests, mixed into the Wonderland cog (no new cog, no new top-level command).

Members open it from the Daily Quests button on the #wondercasino hub (the gamble cog calls
`open_quest_panel`). Mods get `.wld quests`: the on/off switch, the reward multiplier, the quest pool
and the usage counts. Ships switched off: the hub button stays hidden and nothing is tracked or paid
until a mod turns it on.

Tracking. Each quest key has one source, so an action counts once:
  hook     `bot.dispatch("wonderland_quest", member, key, amount)` from the success path of our own cog.
           Used for every evac-cogs quest because a button press or reaction is not an outcome (an
           already-answered Verdict and a lost minigame fire the same events).
  command  on_command_completion for third-party commands (`.fish`, LevelUp `.stars`), checked
           against the cog's own state where it keeps one (stars: LevelUp's cooldown stamp).
  call     our own `.wld search` / `.wld page` call _q_record when a member really looked something up.
  chat     on_message in the chat channel (#cuddle), at most one counted message per 15 seconds.
Payday claims (for the "share of Payday users who open quests" number) come from ExtendedEconomy's
`red_economy_payday_claim` event, which only fires on a real claim.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from datetime import datetime
from typing import Optional

import discord
from redbot.core import commands

from . import quests as Q

log = logging.getLogger("red.evac-cogs.wonderland.quests")

BOT_COMMANDS_CHANNEL_ID = 1546909458582474772   # #bot-commands (stack-and-ids.md); editable in `.wld quests`
QUEST_COLOR = 0xF6C177
PANEL_TIMEOUT = 600
COUNT_CHECK_SECONDS = 5   # a counting bot deletes a wrong number within this long

DEFAULT_GUILD_QUESTS = {
    "enabled": False,            # ships off: switched on in `.wld quests` once the coin ledger is live
    "multiplier": Q.DEFAULT_MULTIPLIER,
    "disabled": [],              # quest keys taken out of the pool
    "targets": {},               # key -> custom target (count quests)
    "chat_channel_id": None,     # None = the channel named "cuddle"
    "commands_channel_id": BOT_COMMANDS_CHANNEL_ID,
    "stats": {},                 # day -> usage counts (quests.stats_day)
    "xp_per_quest": Q.DEFAULT_XP_PER_QUEST,      # LevelUp XP for each finished quest (never counts toward its weekly board)
    "xp_boost": dict(Q.DEFAULT_XP_BOOST),        # holiday XP window: {"start", "end", "multiplier"}
    "milestones": {},                            # streak days -> {"coins": paydays, "xp", "pack"}; blank = Q.DEFAULT_PRIZES
    "weekly": dict(Q.DEFAULT_WEEKLY),            # quests on N of 7 days
    "reroll_costs": list(Q.DEFAULT_REROLL_COSTS),  # coins for the 1st, 2nd, 3rd reroll of a day; then it doubles (a sink)
    "post_channels": {},                         # "selfie" / "counting" -> channel id; blank = found by name
    "heist_command": "",                         # the heist cog's command (e.g. "heist play"); the heist quest is off until set
    "set_pack": Q.DEFAULT_SET_PACK,              # a full set's card pack: off | half | free
}

OPEN, DONE, OFF = "open", "done", "off"
RECONCILE = ("verdict", "pet", "card")   # once-a-day actions whose own cog says whether they were done today


def _can(member, channel, *, send: bool = False, react: bool = False, threads: bool = False) -> bool:
    """Can this member see (and, if asked, post or react) there? Permission bits, never role names."""
    if channel is None or member is None:
        return False
    try:
        perms = channel.permissions_for(member)
    except Exception:
        return False
    if not perms.view_channel:
        return False
    if send and not perms.send_messages:
        return False
    if threads and not perms.send_messages_in_threads:
        return False
    if react and not perms.add_reactions:
        return False
    return True


def _public(guild, channel) -> bool:
    """Quests only ever point at channels @everyone can see: never Pillow Talk, the Rabbit Hole or anything private."""
    return _can(guild.default_role, channel)


class QuestPanelView(discord.ui.View):
    """Refresh, plus the buttons that only show when they can be used: reroll, free packs, the full-set pack."""

    def __init__(self, cog, member_id: int, extras: Optional[dict] = None):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.member_id = cog, member_id
        extras = extras or {}
        refresh = discord.ui.Button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
        refresh.callback = self._refresh
        self.add_item(refresh)
        if extras.get("reroll") is not None:
            cost = int(extras["reroll"])
            button = discord.ui.Button(label=f"Reroll · {cost:,}" if cost else "Reroll · free", emoji="🎲",
                                       style=discord.ButtonStyle.secondary)
            button.callback = self._reroll
            self.add_item(button)
        if extras.get("packs"):
            button = discord.ui.Button(label=f"Open a free pack ({int(extras['packs'])})", emoji="🎁",
                                       style=discord.ButtonStyle.success)
            button.callback = self._pack_voucher
            self.add_item(button)
        if extras.get("set_pack") is not None:
            price = int(extras["set_pack"])
            button = discord.ui.Button(label=f"Full-set pack · {price:,}" if price else "Full-set pack · free",
                                       emoji="🎁", style=discord.ButtonStyle.success)
            button.callback = self._pack_set
            self.add_item(button)

    async def _mine(self, interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("Tap Daily Quests to open your own.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        if not await self._mine(interaction):
            return
        embed, extras = await self.cog._q_panel_parts(interaction.user, count_open=False)
        await interaction.response.edit_message(embed=embed, view=QuestPanelView(self.cog, self.member_id, extras))

    async def _reroll(self, interaction: discord.Interaction):
        if not await self._mine(interaction):
            return
        keys, message = await self.cog._q_reroll_options(interaction.user)
        if not keys:
            return await interaction.response.send_message(message, ephemeral=True)
        await interaction.response.send_message(message, view=RerollView(self.cog, interaction.user, keys), ephemeral=True)

    async def _pack_voucher(self, interaction: discord.Interaction):
        await self._pack(interaction, "voucher")

    async def _pack_set(self, interaction: discord.Interaction):
        await self._pack(interaction, "set")

    async def _pack(self, interaction: discord.Interaction, kind: str):
        if not await self._mine(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text, file = await self.cog._q_open_pack(interaction.user, kind)
        kw = {"file": file} if file is not None else {}
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **kw)


class RerollView(discord.ui.View):
    """Pick the quest to swap. Private to the member."""

    def __init__(self, cog, member, keys: list):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.member = cog, member
        options = [discord.SelectOption(label=Q.BY_KEY[k].title.replace("{n}", "N").replace("{#", "#").replace("}", "")[:100],
                                        value=k) for k in keys]
        select = discord.ui.Select(placeholder="Which quest do you want to swap?", options=options)
        select.callback = self._pick
        self.add_item(select)

    async def _pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("This isn't yours.", ephemeral=True)
        result = await self.cog._q_reroll(interaction.user, interaction.data["values"][0])
        await interaction.response.edit_message(content=result, view=None)


class QuestsMixin:
    """Needs `self.bot`, `self.config` (guild `quests`, member `quests`), `self._exists` and `self._lurker_role_id`."""

    def _q_setup(self) -> None:
        self._q_locks: dict = {}
        self._q_chat_last: dict = {}     # (guild, member) -> timestamp of the last counted chat message
        self._q_chat_full: dict = {}     # (guild, member) -> day the chat quest filled up (stop counting)
        self._q_chat_cache: dict = {}    # guild -> (channel id, expires)
        self._q_live = False             # quests switched on? Gates the guide's Daily Quests line (`@quests`)
        self._q_post_cache: dict = {}    # guild -> (expires, {channel id: quest key}) for the selfie / counting quests
        self._q_heist = ""               # the heist command's qualified name (set in `.wld quests`)

    async def cog_load(self) -> None:
        await super().cog_load()
        try:
            saved = await self.config.all_guilds()
            self._q_live = any((data.get("quests") or {}).get("enabled") for data in saved.values())
            self._q_heist = next(((d.get("quests") or {}).get("heist_command", "") for d in saved.values()
                                  if (d.get("quests") or {}).get("heist_command")), "")
        except Exception:
            log.exception("quests: reading the on/off switch failed")

    def _q_lock(self, *key) -> asyncio.Lock:
        return self._q_locks.setdefault(key, asyncio.Lock())

    async def _q_conf(self, guild) -> dict:
        conf = dict(DEFAULT_GUILD_QUESTS)
        conf.update(await self.config.guild(guild).quests() or {})
        conf["weekly"] = {**Q.DEFAULT_WEEKLY, **(conf.get("weekly") or {})}
        conf["xp_boost"] = {**Q.DEFAULT_XP_BOOST, **(conf.get("xp_boost") or {})}
        return conf

    async def quests_open(self, guild) -> bool:
        """Public: the gamble hub asks this to decide whether to show the Daily Quests button."""
        try:
            return bool((await self._q_conf(guild))["enabled"])
        except Exception:
            return False

    async def _q_is_lurker(self, member) -> bool:
        role_id = await self._lurker_role_id(member.guild)
        return bool(role_id) and any(r.id == role_id for r in getattr(member, "roles", ()))

    # ------------------------------------------------------------------ payday

    async def _q_payday(self, member) -> int:
        """This member's payday, worked out the way ExtendedEconomy's payday override does it.
        Falls back to the guild base payday (and logs it) if any part of that fails."""
        from redbot.core import bank
        eco = self.bot.get_cog("Economy")
        if eco is None:
            log.warning("quests: Economy is not loaded, quest rewards are 0")
            return 0
        guild = member.guild
        global_bank = await bank.is_global()
        base = await (eco.config.PAYDAY_CREDITS() if global_bank else eco.config.guild(guild).PAYDAY_CREDITS())
        if global_bank:
            return int(base)
        try:
            roles = list(member.roles)
            role_credits = [await eco.config.role(role).PAYDAY_CREDITS() for role in roles]
            ext = self.bot.get_cog("ExtendedEconomy")
            stack, bonuses, statics = False, {}, {}
            if ext is not None:
                conf = ext.db.get_conf(guild)
                stack = bool(conf.stack_paydays)
                bonuses, statics = dict(conf.role_bonuses or {}), dict(conf.role_static_bonuses or {})
            return Q.payday_amount(base, role_credits, [r.id for r in roles], stack=stack, role_bonuses=bonuses,
                                   static_bonuses=statics)
        except Exception:
            log.exception("quests: working out %s's payday failed, using the guild base payday", member.id)
            return int(base)

    async def _q_pay(self, member, state: dict, new_keys: list, conf: dict) -> int:
        """Pay each newly finished quest a third of this member's payday x multiplier. Silent."""
        if not new_keys:
            return 0
        payday = await self._q_payday(member)
        total = sum(Q.quest_reward(payday, float(conf["multiplier"]), state["paid"].index(k)) for k in new_keys)
        if total > 0:
            from redbot.core import bank
            try:
                await bank.deposit_credits(member, total)
            except Exception:
                log.exception("quests: paying %s coins to %s failed", total, member.id)
                return 0
        state["earned"] = int(state.get("earned", 0)) + total
        return total

    async def _q_give_xp(self, member, xp: int) -> int:
        """Bonus XP through LevelUp's own profile and level-up check, so level roles and level gates follow.
        It is added to the profile only: never to the weekly board (the message listener does that, not us)."""
        cog = self.bot.get_cog("LevelUp")
        if cog is None or xp <= 0:
            return 0
        try:
            conf = cog.db.get_conf(member.guild)
            if member.id in getattr(conf, "ignoredusers", ()):
                return 0
            profile = conf.get_profile(member)
            profile.xp += int(xp)
            await cog.check_levelups(guild=member.guild, member=member, profile=profile, conf=conf)
            cog.save(False)
            return int(xp)
        except Exception:
            log.exception("quests: giving %s XP to %s failed", xp, member.id)
            return 0

    async def _q_extras(self, member, state: dict, conf: dict, new_keys: list, day: str) -> int:
        """Quest XP, the streak, its milestone prizes and the weekly prize for quests that just finished. Mutates
        `state` (the caller holds the lock and saves it); returns the extra coins paid."""
        if not new_keys:
            return 0
        mult = float(conf["multiplier"])
        xp = Q.quest_xp(conf["xp_per_quest"], conf["xp_boost"], day) * len(new_keys)
        coins = 0
        if Q.advance_streak(state, day) is not None:      # the first finished quest of the day
            streak, rewards = state["streak"], []
            for days in Q.milestones_due(streak, state["ms_given"]):
                state["ms_given"].append(days)
                emoji, title = Q.BADGES[days]
                rewards.append((f"{emoji} {title}: {days} days in a row", Q.prize_for(conf["milestones"], days)))
            weekly = conf["weekly"]
            if Q.track_week(state, day, weekly["days"]):
                rewards.append((f"📅 Weekly prize: quests on {int(weekly['days'])} days", weekly))
            payday = await self._q_payday(member) if rewards else 0
            for label, prize in rewards:
                note = {"label": label, "coins": Q.coins_for(payday, prize.get("coins", 0), mult),
                        "xp": max(0, int(prize.get("xp", 0))), "pack": bool(prize.get("pack"))}
                coins += note["coins"]
                xp += note["xp"]
                if note["pack"]:
                    state["packs"] = int(state.get("packs", 0)) + 1
                state["notes"].append(note)
            await self._q_bump(member.guild, day, milestones=len(rewards)) if rewards else None
        if coins > 0:
            from redbot.core import bank
            try:
                await bank.deposit_credits(member, coins)
                state["earned"] = int(state.get("earned", 0)) + coins
            except Exception:
                log.exception("quests: paying %s prize coins to %s failed", coins, member.id)
                coins = 0
        if xp > 0:
            state["xp"] = int(state.get("xp", 0)) + await self._q_give_xp(member, xp)
        return coins

    async def _q_pack_price(self, guild, conf: dict, kind: str) -> Optional[int]:
        """The price of a quest-reward pack: free for a voucher, per the setting for a full set. None = unavailable."""
        if kind == "voucher":
            return 0
        cog = self.bot.get_cog("CardCollect")
        if cog is None:
            return None
        return Q.set_pack_price(conf["set_pack"], int(await cog.config.guild(guild).pack_price()))

    async def _q_open_pack(self, member, kind: str) -> tuple:
        """Open a quest-reward pack through CardCollect. (text, file or None). A bonus on top of the daily pack."""
        guild, day = member.guild, Q.today()
        cog = self.bot.get_cog("CardCollect")
        if cog is None or not hasattr(cog, "open_bonus_pack"):
            return "Card packs aren't available right now.", None
        conf = await self._q_conf(guild)
        async with self._q_lock(guild.id, member.id):
            mconf = self.config.member(member)
            state = Q.roll_day(await mconf.quests(), day)
            full = bool(state["assigned"]) and len(state["paid"]) == len(state["assigned"])
            if kind == "voucher":
                if int(state.get("packs", 0)) <= 0:
                    return "You have no free packs waiting.", None
            elif not full or state.get("set_pack_day") == day or conf["set_pack"] == "off":
                return "There's no full-set pack for you right now.", None
            price = await self._q_pack_price(guild, conf, kind)
            if price is None:
                return "Card packs aren't available right now.", None
            result = await cog.open_bonus_pack(guild, member, price)
            if isinstance(result, str):
                return result, None
            if kind == "voucher":
                state["packs"] = int(state["packs"]) - 1
            else:
                state["set_pack_day"] = day
            await mconf.quests.set(state)
        await self._q_bump(guild, day, packs=1)
        try:
            text, file = await cog.pack_result_message(guild, member, result)
        except Exception:
            log.exception("quests: showing a quest pack to %s failed", member.id)
            return "Your pack was opened. Check `.card` to see the new cards.", None
        return text.replace("opened today's pack:", "opened a quest-reward pack:", 1), file

    async def _q_reroll_options(self, member) -> tuple:
        """([quest keys that can be swapped], message). A quest can be swapped before it has any progress."""
        conf = await self._q_conf(member.guild)
        state = Q.roll_day(await self.config.member(member).quests(), Q.today())
        keys = [k for k in state["assigned"] if k not in state["paid"] and not state["progress"].get(k)]
        if not keys:
            return [], "Nothing to swap: every quest is finished or already started."
        cost = Q.reroll_cost(conf["reroll_costs"], state["rerolls"])
        return keys, f"Swap one quest for another you can do today. This reroll costs **{cost:,}**; each one today costs double the last."

    async def _q_reroll(self, member, old_key: str) -> str:
        """Swap `old_key` for another quest this member can do now. As many times as they like, but each reroll
        today costs more (1k, 5k, 10k, 20k...). The fee is a coin sink."""
        guild, day = member.guild, Q.today()
        conf = await self._q_conf(guild)
        async with self._q_lock(guild.id, member.id):
            mconf = self.config.member(member)
            state = Q.roll_day(await mconf.quests(), day)
            if old_key not in state["assigned"] or old_key in state["paid"] or state["progress"].get(old_key):
                return "That quest can't be swapped any more."
            statuses = await self._q_statuses(member, conf)
            skip = set(state["assigned"]) | set(state["swapped"])
            candidates = [k for k, (s, _c, _r) in statuses.items()
                          if s == OPEN and k not in skip and not Q.is_done(state, k, conf["targets"])]
            pick = Q.deal(candidates, state["last_done"], day, Q.rng_for(guild.id, member.id, f"{day}r{state['rerolls']}"), n=1)
            if not pick:
                return "There's nothing else you can do today, so a swap would change nothing. You weren't charged."
            cost = Q.reroll_cost(conf["reroll_costs"], state["rerolls"])
            if cost > 0:
                from redbot.core import bank
                try:
                    await bank.withdraw_credits(member, cost)
                except ValueError:
                    return f"This reroll costs {cost:,} and you don't have enough."
            state["assigned"][state["assigned"].index(old_key)] = pick[0]
            state["swapped"].append(old_key)
            state["rerolls"] = int(state["rerolls"]) + 1
            await mconf.quests.set(state)
        await self._q_bump(guild, day, rerolls=1, rerolled_coins=cost, offered={pick[0]: 1})
        strip = lambda text: text.replace("{#", "#").replace("}", "")
        old, new = strip(Q.title_of(old_key, conf["targets"])), strip(Q.title_of(pick[0], conf["targets"]))
        nxt = Q.reroll_cost(conf["reroll_costs"], state["rerolls"])
        return f"Swapped **{old}** for **{new}** (-{cost:,}). The next reroll today costs {nxt:,}."

    # ------------------------------------------------------------------ availability

    def _q_hg_engine(self, cog):
        return sys.modules.get(type(cog).__module__.rsplit(".", 1)[0] + ".engine")

    async def _q_chat_channel(self, guild, conf: dict):
        if conf.get("chat_channel_id"):
            return guild.get_channel(conf["chat_channel_id"])
        cached = self._q_chat_cache.get(guild.id)
        if cached and cached[1] > time.monotonic():
            return guild.get_channel(cached[0])
        from .engine import match_channel
        channel_id = match_channel("cuddle", {c.id: c.name for c in getattr(guild, "text_channels", [])})
        self._q_chat_cache[guild.id] = (channel_id or 0, time.monotonic() + 600)
        return guild.get_channel(channel_id) if channel_id else None

    async def _q_post_channel(self, guild, key: str, conf: dict):
        """The channel a POST quest (selfie, counting) counts in: the one set in `.wld quests`, else found by name."""
        set_id = (conf.get("post_channels") or {}).get(key)
        if set_id:
            return guild.get_channel(int(set_id))
        from .engine import match_channel
        found = match_channel(Q.BY_KEY[key].channel, {c.id: c.name for c in getattr(guild, "text_channels", [])})
        return guild.get_channel(found) if found else None

    async def _q_post_targets(self, guild) -> dict:
        """{channel id: quest key} for the POST quests that are on, cached for ten minutes."""
        cached = self._q_post_cache.get(guild.id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        conf = await self._q_conf(guild)
        targets = {}
        if conf["enabled"]:
            for quest in Q.QUESTS:
                if quest.source == Q.POST and quest.key not in conf["disabled"]:
                    channel = await self._q_post_channel(guild, quest.key, conf)
                    if channel is not None:
                        targets[channel.id] = quest.key
        self._q_post_cache[guild.id] = (time.monotonic() + 600, targets)
        return targets

    async def _q_hub_channel(self, guild):
        gamble = self.bot.get_cog("GambleThreads")
        return None if gamble is None else await gamble._hub_channel(guild)

    async def _q_status(self, key: str, member, conf: dict) -> tuple:
        """(OPEN | DONE | OFF, channel or None, reason) for one quest and one member, right now.
        OPEN: can be done today. DONE: the cog's own state says it was already done today. OFF: can't
        be done today (cog unloaded, gate closed, channel hidden or not postable for them)."""
        guild, uid, day = member.guild, str(member.id), Q.today()
        get = self.bot.get_cog

        def ok(channel, **need):
            if channel is None:
                return OFF, None, "channel not found"
            if not _public(guild, channel):
                return OFF, channel, "channel is not public"
            if not _can(member, channel, **need):
                return OFF, channel, "no permission there"
            return OPEN, channel, ""

        if key == "verdict":
            cog = get("Verdict")
            if cog is None:
                return OFF, None, "Verdict not loaded"
            gc = cog.config.guild(guild)
            cur = await gc.current()
            if not await gc.enabled() or not cur:
                return OFF, None, "no open question"
            status = ok(guild.get_channel(await gc.channel_id() or 0))
            return (DONE, *status[1:]) if status[0] == OPEN and uid in (cur.get("predictions") or {}) else status
        if key == "pet":
            cog = get("WonderPet")
            if cog is None:
                return OFF, None, "WonderPet not loaded"
            pet = await cog.config.guild(guild).pet()
            if not pet or not pet.get("alive"):
                return OFF, None, "no pet right now"
            status = ok(guild.get_channel(await cog.config.guild(guild).channel_id() or 0))
            daily = await cog.config.member(member).daily() or {}
            used = daily.get("date") == day and (daily.get("used") or any(daily.get(k) for k in ("feed", "play", "clean")))
            return (DONE, *status[1:]) if status[0] == OPEN and used else status
        if key == "treat":
            cog = get("WonderPet")
            engine = None if cog is None else sys.modules.get(type(cog).__module__.rsplit(".", 1)[0] + ".engine")
            if engine is None or not hasattr(engine, "TREATS"):
                return OFF, None, "WonderPet not loaded"
            s = await cog.config.guild(guild).all()
            pet = s.get("pet")
            if not pet or not pet.get("alive") or pet.get("stage") == "egg":
                return OFF, None, "no pet to treat"
            if int(s.get("treat_cap", 0)) <= 0:
                return OFF, None, "treats are off"
            daily = await cog.config.member(member).daily() or {}
            given = int(daily.get("treats", 0)) if daily.get("date") == day else 0
            status = ok(guild.get_channel(s.get("channel_id") or 0))
            if status[0] == OPEN and given > 0:
                return DONE, *status[1:]
            from redbot.core import bank
            cheapest = min(engine.treat_price(k, s["treat_price"]) for k in engine.TREATS)
            if status[0] == OPEN and await bank.get_balance(member) < cheapest:
                return OFF, status[1], "can't afford a treat"
            return status
        if key == "card":
            cog = get("CardCollect")
            if cog is None or not self._exists("card daily"):
                return OFF, None, "CardCollect not loaded"
            status = ok(await self._q_hub_channel(guild))
            pulled = (await cog._member_state(member)).daily_pull_date == day
            return (DONE, *status[1:]) if status[0] == OPEN and pulled else status
        if key == "pick":
            cog = get("CardCollect")
            if cog is None:
                return OFF, None, "CardCollect not loaded"
            gc = cog.config.guild(guild)
            if await gc.test_mode():
                return OFF, None, "drops are in test mode"
            return ok(guild.get_channel(await gc.channel_id() or 0))   # drops land there; picking is a button
        if key == "puzzle":
            cog = get("Puzzle")
            if cog is None:
                return OFF, None, "Puzzle not loaded"
            if not await cog.config.guild(guild).active():
                return OFF, None, "no puzzle running"
            return ok(guild.get_channel(await cog.config.guild(guild).channel_id() or 0), react=True)
        if key == "minigame":
            cog = get("MinigameHub")
            if cog is None or not await cog.config.guild(guild).enabled():
                return OFF, None, "minigames off"
            return ok(guild.get_channel(await cog.config.guild(guild).spawn_channel() or 0), send=True)
        if key == "chat":
            return ok(await self._q_chat_channel(guild, conf), send=True)
        if key in ("fish", "star"):
            quest = Q.BY_KEY[key]
            if not self._exists(quest.command) or (key == "star" and get("LevelUp") is None):
                return OFF, None, f"`.{quest.command}` not available"
            return ok(guild.get_channel(conf.get("commands_channel_id") or 0), send=True)
        if key == "blackjack":
            if get("BlackjackTable") is None or not self._exists("wonderjack table"):
                return OFF, None, "Wonderjack not loaded"
            from redbot.core import bank
            if await bank.get_balance(member) <= 0:
                return OFF, None, "no coins to bet"
            return ok(await self._q_hub_channel(guild), threads=True)
        if key in ("selfie", "counting"):
            return ok(await self._q_post_channel(guild, key, conf), send=True)
        if key == "heist":
            cmd = (conf.get("heist_command") or "").strip()
            if not cmd:
                return OFF, None, "the heist command isn't set in .wld quests"
            if not self._exists(cmd):
                return OFF, None, f"`.{cmd}` not available"
            return ok(guild.get_channel(conf.get("commands_channel_id") or 0), send=True)
        if key == "duel":
            cog = get("Duel")
            if cog is None or not self._exists("duel"):
                return OFF, None, "Duel not loaded"
            from redbot.core import bank
            lowest = max(1, int(await cog.config.guild(guild).min_bet()))
            if await bank.get_balance(member) < lowest:
                return OFF, None, "can't cover the smallest wager"
            if await cog._check_cooldown(guild, member):
                return OFF, None, "on duel cooldown"
            return ok(guild.get_channel(conf.get("commands_channel_id") or 0), send=True)
        if key == "guide":
            return ok(guild.get_channel(conf.get("commands_channel_id") or 0), send=True)
        if key == "acro":
            cog = get("Verdict")
            if cog is None or not hasattr(cog, "_acro_status"):
                return OFF, None, "Acrophobia not loaded"
            gc = cog.config.guild(guild)
            if not await gc.acro_enabled():
                return OFF, None, "Acrophobia is off"
            now, cur = time.time(), await gc.acro_current()
            if cur:   # a round is running: entering (submit phase) or voting (vote phase) both count
                end = cur.get("submit_ends_ts") if cur.get("phase") == "submit" else cur.get("vote_ends_ts")
                if not end or now >= end:
                    return OFF, None, "round just closing"
            else:     # none running: only offer it while a round is still planned for later today
                plan = await gc.acro_plan() or {}
                left = [s for s in plan.get("slots") or [] if s not in set(plan.get("done") or []) and s > now - 60]
                if plan.get("date") != Q.today(datetime.fromtimestamp(now, Q.TZ)) or not left:
                    return OFF, None, "no round left today"
            return ok(guild.get_channel(await gc.channel_id() or 0))
        if key == "sponsor":
            cog = get("HungerGames")
            engine = None if cog is None else self._q_hg_engine(cog)
            if engine is None:
                return OFF, None, "Hunger Games not loaded"
            s = await cog.config.guild(guild).all()
            ed = s.get("current") or None
            if not s.get("enabled") or not ed or ed.get("test") or not engine.shop_open(ed):
                return OFF, None, "shop closed"
            hg_day = engine.day_key(time.time())
            per_day = getattr(engine, "PARACHUTES_PER_DAY", 3)
            backed = {r["tribute"] for r in ed.get("ledger", []) if r["sponsor"] == uid}
            eligible = [t for t, info in ed["roster"].items() if info.get("alive") and t != uid and t not in backed
                        and sum(1 for r in ed["ledger"] if r["tribute"] == t and r["day"] == hg_day) < per_day]
            if not eligible:
                return OFF, None, "no tribute you can back"
            from redbot.core import bank
            items = getattr(engine, "ITEMS", {})
            cheapest = min((engine.unit_price(s["prices"].get(i, v[2]), time.time()) for i, v in items.items()), default=0)
            if await bank.get_balance(member) < cheapest:
                return OFF, None, "can't afford a parachute"
            status = ok(cog._channel(guild, s, ed))
            sent_today = any(r["sponsor"] == uid and r["day"] == hg_day for r in ed.get("ledger", []))
            return (DONE, *status[1:]) if status[0] == OPEN and sent_today else status
        if key == "canvas":
            gamble = get("GambleThreads")
            if gamble is None or "canvas" not in (await gamble.config.guild(guild).games() or {}):
                return OFF, None, "canvas not on the hub"
            service = gamble._canvas(guild)
            if await service.blocked(member):
                return OFF, None, "can't paint (banned or lurker)"
            from redbot.core import bank
            base = getattr(sys.modules.get(type(service.store).__module__), "BASE_PRICE", 0)
            if await bank.get_balance(member) < base:
                return OFF, None, "can't afford a pixel"
            return ok(await self._q_hub_channel(guild))
        if key == "plant":
            cog = get("MinigameHub")
            module = None if cog is None else sys.modules.get(type(cog).__module__.rsplit(".", 1)[0] + ".plant")
            if module is None:
                return OFF, None, "coin drops not loaded"
            p = await cog.config.guild(guild).plant()
            gamble = get("GambleThreads")
            on_hub = gamble is not None and "plant" in (await gamble.config.guild(guild).games() or {})
            if not p or not p.get("enabled") or not on_hub:
                return OFF, None, "coin drops off"
            if cog._plant_last.get((guild.id, member.id), 0) + module.COOLDOWN_SECONDS > time.time():
                return OFF, None, "on cooldown"
            from redbot.core import bank
            if await bank.get_balance(member) < int(p.get("min", 0)):
                return OFF, None, "can't afford the smallest drop"
            status = ok(await self._q_hub_channel(guild))
            if status[0] != OPEN:
                return status
            return ok(module.find_channel(guild))   # the drop lands in #cuddle: it must be public and visible
        return OFF, None, "unknown quest"

    async def _q_statuses(self, member, conf: dict) -> dict:
        out = {}
        for quest in Q.QUESTS:
            if quest.key in conf["disabled"]:
                out[quest.key] = (OFF, None, "turned off in .wld quests")
                continue
            try:
                out[quest.key] = await self._q_status(quest.key, member, conf)
            except Exception:
                log.exception("quests: checking %r for %s failed", quest.key, member.id)
                out[quest.key] = (OFF, None, "check failed")
        return out

    # ------------------------------------------------------------------ state

    async def _q_bump(self, guild, day: str, **counts) -> None:
        """Add to today's guild usage counts. offered / done take {key: n}."""
        async with self._q_lock(guild.id, "stats"):
            async with self.config.guild(guild).quests() as saved:
                stats = saved.setdefault("stats", {})
                entry = Q.stats_day(stats, day)
                for name, value in counts.items():
                    if isinstance(value, dict):
                        for key, n in value.items():
                            entry[name][key] = entry[name].get(key, 0) + n
                    else:
                        entry[name] = entry.get(name, 0) + value
                Q.prune(stats, day)

    async def _q_record(self, member, key: str, amount: int = 1, *, source: str) -> Optional[int]:
        """Count an action. Returns the member's progress on `key` today, or None when it wasn't counted."""
        quest = Q.BY_KEY.get(key)
        guild = getattr(member, "guild", None)
        if quest is None or quest.source != source or guild is None or getattr(member, "bot", False):
            return None
        conf = await self._q_conf(guild)
        if not conf["enabled"] or key in conf["disabled"] or await self._q_is_lurker(member):
            return None
        day = Q.today()
        async with self._q_lock(guild.id, member.id):
            mconf = self.config.member(member)
            state = Q.roll_day(await mconf.quests(), day)
            Q.add_progress(state, key, amount, conf["targets"])
            new, set_done = Q.settle(state, conf["targets"])
            paid = await self._q_pay(member, state, new, conf)
            paid += await self._q_extras(member, state, conf, new, day)
            await mconf.quests.set(state)
            progress = int(state["progress"].get(key, 0))
        if new:
            await self._q_bump(guild, day, done={k: 1 for k in new}, sets=int(set_done), paid=paid)
        return progress

    async def _q_open(self, member, conf: dict, count_open: bool) -> dict:
        """Deal today's quests if needed, catch up on anything the listeners missed, count the open."""
        guild, day = member.guild, Q.today()
        offered: dict = {}
        async with self._q_lock(guild.id, member.id):
            mconf = self.config.member(member)
            state = Q.roll_day(await mconf.quests(), day)
            statuses = None
            if not state["assigned"]:
                statuses = await self._q_statuses(member, conf)
                candidates = [k for k, (s, _c, _r) in statuses.items()
                              if s == OPEN and not Q.is_done(state, k, conf["targets"])]
                state["assigned"] = Q.deal(candidates, state["last_done"], day, Q.rng_for(guild.id, member.id, day))
                offered = {k: 1 for k in state["assigned"]}
            for key in [k for k in state["assigned"] if k in RECONCILE and not Q.is_done(state, k, conf["targets"])]:
                status = await self._q_status(key, member, conf) if statuses is None else statuses[key]
                if status[0] == DONE:   # done since it was dealt, but the hook was missed (a reload, say)
                    Q.add_progress(state, key, Q.target_of(key, conf["targets"]), conf["targets"])
            new, set_done = Q.settle(state, conf["targets"])
            paid = await self._q_pay(member, state, new, conf)
            paid += await self._q_extras(member, state, conf, new, day)
            first_open = count_open and not state["opened"]
            if count_open:
                state["opened"] = True
            await mconf.quests.set(state)
        if count_open or new or offered:
            await self._q_bump(guild, day, opens=int(first_open), clicks=int(count_open),
                               both=int(first_open and state["payday"]), offered=offered,
                               done={k: 1 for k in new}, sets=int(set_done), paid=paid)
        return state

    # ------------------------------------------------------------------ member panel

    def _q_link(self, guild, text: str) -> str:
        from .engine import match_channel
        names = {c.id: c.name for c in getattr(guild, "channels", [])}
        out = text
        while "{#" in out:
            start = out.index("{#")
            end = out.index("}", start)
            slug = out[start + 2:end]
            channel_id = match_channel(slug, names)
            out = out[:start] + (f"<#{channel_id}>" if channel_id else f"**#{slug}**") + out[end + 1:]
        return out

    async def _q_panel_embed(self, member, count_open: bool = True) -> discord.Embed:
        return (await self._q_panel_parts(member, count_open))[0]

    async def _q_panel_parts(self, member, count_open: bool = True) -> tuple:
        """(the panel embed, which extra buttons to show: reroll price, free packs, the full-set pack price)."""
        guild = member.guild
        conf = await self._q_conf(guild)
        embed = discord.Embed(title="📜 Daily quests", color=discord.Color(QUEST_COLOR))
        if not conf["enabled"]:
            embed.description = "Daily quests aren't open yet."
            return embed, {}
        if await self._q_is_lurker(member):
            embed.description = (f"Daily quests unlock once you're active. Say hi in "
                                 f"{self._q_link(guild, '{#cuddle}')}, then tap the button again.")
            return embed, {}
        state = await self._q_open(member, conf, count_open)
        try:
            from redbot.core import bank
            currency = await bank.get_currency_name(guild)
        except Exception:
            currency = "coins"
        payday = await self._q_payday(member)
        mult = float(conf["multiplier"])
        day = state["day"]
        xp_each = Q.quest_xp(conf["xp_per_quest"], conf["xp_boost"], day)
        xp_text = f" · +{xp_each:,} XP" if xp_each else ""
        lines = []
        for index, key in enumerate(state["assigned"]):
            target = Q.target_of(key, conf["targets"])
            progress = int(state["progress"].get(key, 0))
            title = self._q_link(guild, Q.title_of(key, conf["targets"]))
            if key in state["paid"]:
                reward = Q.quest_reward(payday, mult, state["paid"].index(key))
                lines.append(f"✅ ~~{title}~~ · +{reward:,} {currency}{xp_text}")
                continue
            reward = Q.quest_reward(payday, mult, len(state["paid"]))
            head = f"⬜ **{title}** · +{reward:,} {currency}{xp_text}"
            if target > 1:
                head += f"\n`{Q.bar(progress, target)}` {progress}/{target}"
            how = self._q_link(guild, Q.BY_KEY[key].how.replace("{heist}", conf.get("heist_command") or "heist"))
            if key in ("verdict", "pet", "treat", "pick", "puzzle", "acro", "sponsor"):   # say where, when the cog knows
                channel = (await self._q_status(key, member, conf))[1]
                if channel is not None:
                    how += f" ({channel.mention})"
            lines.append(f"{head}\n-# {how}")
        if not lines:
            embed.description = "Nothing you can do today has a quest right now. Check back tomorrow."
        else:
            embed.description = "\n\n".join(lines)
        done, total = len(state["paid"]), len(state["assigned"])
        footer = []
        if Q.boost_multiplier(conf["xp_boost"], day) > 1:
            footer.append(f"🎉 **Holiday XP x{Q.boost_multiplier(conf['xp_boost'], day):g}** on quest XP until {conf['xp_boost']['end']}")
        for note in state["notes"]:
            got = ([f"+{note['coins']:,} {currency}"] if note["coins"] else []) + ([f"+{note['xp']:,} XP"] if note["xp"] else [])
            if note["pack"]:
                got.append("a free card pack")
            footer.append(f"🎁 **{note['label']}** · " + ", ".join(got))
        footer.append(f"**Today:** {done}/{total} done · +{int(state['earned']):,} {currency}" +
                      (f" · +{int(state['xp']):,} XP" if state["xp"] else "") + " earned")
        streak = Q.current_streak(state, day)
        best = int(state.get("best_streak", 0))
        if streak or best:
            line = f"🔥 **Streak:** {streak} day{'s' if streak != 1 else ''} (best {best})"
            if best and Q.badge_text(best):
                line += f" · {Q.badge_text(best)}"
            upcoming = Q.next_badge(streak)
            if upcoming:
                line += f"\nNext: {upcoming[1]} {upcoming[2]} in {upcoming[0] - streak} day{'s' if upcoming[0] - streak != 1 else ''}"
            footer.append(line)
        need = int(conf["weekly"]["days"])
        week = Q.week_days(state, day)
        paid_week = state.get("week_paid") == Q.week_key(day)
        footer.append(f"📅 **This week:** {min(week, need)}/{need} days" + (" ✅ prize earned" if paid_week else ""))
        if int(state.get("packs", 0)):
            footer.append(f"🎁 Free card packs waiting: **{int(state['packs'])}**")
        footer.append(f"⏳ New quests in **{Q.fmt_duration(Q.seconds_to_reset())}** (midnight Pacific)")
        embed.add_field(name="​", value="\n".join(footer), inline=False)
        embed.set_footer(text="Coins and XP land as you finish each quest. A full set pays one payday.")
        extras = {}
        if state["assigned"] and any(k not in state["paid"] and not state["progress"].get(k) for k in state["assigned"]):
            extras["reroll"] = Q.reroll_cost(conf["reroll_costs"], state["rerolls"])
        if int(state.get("packs", 0)):
            extras["packs"] = int(state["packs"])
        full = bool(state["assigned"]) and len(state["paid"]) == len(state["assigned"])
        if full and state.get("set_pack_day") != day:
            price = await self._q_pack_price(guild, conf, "set")
            if price is not None:
                extras["set_pack"] = price
        return embed, extras

    async def open_quest_panel(self, interaction: discord.Interaction) -> None:
        """Public: the gamble hub's Daily Quests button calls this. Ephemeral; nothing public, no DMs."""
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed, extras = await self._q_panel_parts(member)
        except Exception:
            log.exception("quests: building the panel for %s failed", member.id)
            return await interaction.followup.send("Something went wrong. Try again in a moment.", ephemeral=True)
        await interaction.followup.send(embed=embed, view=QuestPanelView(self, member.id, extras), ephemeral=True)

    async def quest_summary(self, member) -> Optional[dict]:
        """Public: `.mystats` reads the streak from here."""
        state = await self.config.member(member).quests()
        if not state or not (state.get("sets") or state.get("best_streak")):
            return None
        best = int(state.get("best_streak", 0))
        return {"streak": Q.current_streak(state, Q.today()), "best": best, "sets": int(state.get("sets", 0)),
                "badge": Q.badge_text(best)}

    # ------------------------------------------------------------------ listeners

    @commands.Cog.listener("on_wonderland_quest")
    async def _q_on_hook(self, member, key: str, amount: int = 1) -> None:
        try:
            await self._q_record(member, key, amount, source=Q.HOOK)
        except Exception:
            log.exception("quests: recording %r for %s failed", key, getattr(member, "id", "?"))

    @commands.Cog.listener("on_command_completion")
    async def _q_on_command(self, ctx) -> None:
        try:
            await self._q_command(ctx)
        except Exception:
            log.exception("quests: handling a command completion failed")

    async def _q_command(self, ctx) -> None:
        command = getattr(ctx, "command", None)
        guild, member = getattr(ctx, "guild", None), getattr(ctx, "author", None)
        if command is None or guild is None or not hasattr(member, "roles"):   # a server member, not a DM user
            return
        name = command.qualified_name
        if name == "payday" and self.bot.get_cog("ExtendedEconomy") is None:
            return await self._q_payday_claim(member, ctx.message)
        for quest in Q.QUESTS:
            wanted = self._q_heist if quest.command == "{heist}" else quest.command
            if quest.source == Q.COMMAND and wanted and wanted == name:
                if quest.key == "star" and not self._q_star_given(ctx):
                    return
                await self._q_record(member, quest.key, 1, source=Q.COMMAND)
                return

    async def _q_guide_lookup(self, member) -> None:
        """`.wld search` found something, or `.wld page` opened a topic: that is the guide quest."""
        try:
            if hasattr(member, "roles") and getattr(member, "guild", None) is not None:
                await self._q_record(member, "guide", 1, source=Q.CALL)
        except Exception:
            log.exception("quests: counting a guide lookup failed")

    def _q_star_given(self, ctx) -> bool:
        """LevelUp stamps the giver's cooldown only when a star actually went out (see its give_stars)."""
        cog = self.bot.get_cog("LevelUp")
        stamp = getattr(cog, "stars", {}).get(ctx.guild.id, {}).get(ctx.author.id) if cog else None
        if stamp is None:
            return False
        return abs((datetime.now() - stamp).total_seconds()) <= 60

    async def _q_payday_claim(self, member, message=None) -> None:
        """Without ExtendedEconomy: check Economy's own last-claim stamp to tell a claim from 'too soon'."""
        from redbot.core import bank
        eco = self.bot.get_cog("Economy")
        if eco is None:
            return
        group = eco.config.user(member) if await bank.is_global() else eco.config.member(member)
        stamp = await group.next_payday()   # Economy stores the time of the last successful claim here
        created = getattr(message, "created_at", None)
        now = created.timestamp() if created else time.time()
        if abs(now - stamp) <= 120:
            await self._q_mark_payday(member)

    async def _q_mark_payday(self, member) -> None:
        guild = member.guild
        conf = await self._q_conf(guild)
        if not conf["enabled"] or getattr(member, "bot", False):
            return
        day = Q.today()
        async with self._q_lock(guild.id, member.id):
            mconf = self.config.member(member)
            state = Q.roll_day(await mconf.quests(), day)
            if state["payday"]:
                return
            state["payday"] = True
            await mconf.quests.set(state)
        await self._q_bump(guild, day, payday=1, both=int(state["opened"]))

    @commands.Cog.listener("on_red_economy_payday_claim")
    async def _q_on_payday(self, payload) -> None:
        member = getattr(payload, "member", None)
        if hasattr(member, "roles") and getattr(member, "guild", None) is not None:
            try:
                await self._q_mark_payday(member)
                computed = await self._q_payday(member)
                if computed != getattr(payload, "amount", computed):
                    log.warning("quests: payday mirror says %s but the claim paid %s for %s",
                                computed, payload.amount, member.id)
            except Exception:
                log.exception("quests: recording a payday claim failed")

    @commands.Cog.listener("on_message")
    async def _q_on_message(self, message) -> None:
        try:
            await self._q_message(message)
        except Exception:
            log.exception("quests: counting a chat message failed")

    async def _q_message(self, message) -> None:
        guild, author = message.guild, message.author
        if guild is None or getattr(author, "bot", True) or not hasattr(author, "roles"):
            return
        await self._q_post_message(message)
        day = Q.today()
        key = (guild.id, author.id)
        if self._q_chat_full.get(key) == day:
            return
        conf = await self._q_conf(guild)
        if not conf["enabled"]:
            return
        channel = await self._q_chat_channel(guild, conf)
        if channel is None or message.channel.id != channel.id:
            return
        stamp = message.created_at.timestamp()
        if stamp - self._q_chat_last.get(key, 0) < Q.CHAT_GAP_SECONDS:
            return
        self._q_chat_last[key] = stamp
        progress = await self._q_record(author, "chat", 1, source=Q.CHAT)
        if progress is not None and progress >= Q.target_of("chat", conf["targets"]):
            self._q_chat_full[key] = day

    async def _q_post_message(self, message) -> None:
        """A selfie (a photo) or a count (a number) in its channel. A count that a counting bot deletes within a
        few seconds (a wrong number) doesn't count."""
        targets = await self._q_post_targets(message.guild)
        key = targets.get(message.channel.id)
        if key is None:
            return
        if key == "selfie":
            if not any(str(getattr(a, "content_type", "") or "").startswith("image/") for a in message.attachments):
                return
        elif key == "counting":
            if not re.match(r"^\s*\d[\d,._]*(\s|$)", message.content or ""):
                return
            await asyncio.sleep(COUNT_CHECK_SECONDS)
            try:
                await message.channel.fetch_message(message.id)
            except discord.NotFound:
                return
            except discord.HTTPException:
                pass   # can't tell: give the benefit of the doubt
        await self._q_record(message.author, key, 1, source=Q.POST)

    # ------------------------------------------------------------------ admin (used by quests_panel.py)

    async def _q_set(self, guild, **changes) -> None:
        async with self.config.guild(guild).quests() as saved:
            for name, value in changes.items():
                saved[name] = value
        if "enabled" in changes:
            self._q_live = bool(changes["enabled"])
        if "heist_command" in changes:
            self._q_heist = str(changes["heist_command"] or "")
        self._q_post_cache.clear()

    async def _q_refresh_hub(self, guild) -> str:
        gamble = self.bot.get_cog("GambleThreads")
        if gamble is None:
            return " The gamble cog isn't loaded, so the hub wasn't refreshed."
        error = await gamble._refresh_hub_message(guild)
        return f" Hub not refreshed: {error}" if error else " The #wondercasino hub was refreshed."

    async def _q_estimate(self, guild, completers: int = 100) -> str:
        """Faucet estimate from the real paydays of active (non-lurker, non-bot) members."""
        conf = await self._q_conf(guild)
        lurker = await self._lurker_role_id(guild)
        members = [m for m in guild.members if not m.bot and not (lurker and any(r.id == lurker for r in m.roles))]
        if not members:
            return "No active members to estimate from."
        paydays = [await self._q_payday(m) for m in members]
        average = sum(paydays) // len(paydays)
        mult = float(conf["multiplier"])
        daily = Q.faucet(completers, average, mult)
        weekly, prizes = conf["weekly"], [Q.prize_for(conf["milestones"], d) for d in Q.MILESTONE_DAYS]
        week_coins = Q.coins_for(average, weekly["coins"], mult)
        ms_coins = {d: Q.coins_for(average, p["coins"], mult) for d, p in zip(Q.MILESTONE_DAYS, prizes) if p["coins"]}
        reroll_text = ", ".join(format(Q.reroll_cost(conf["reroll_costs"], n), ",") for n in range(5))
        extras = (f"\nOn top: the weekly prize pays {week_coins:,} to each member who qualifies (about {week_coins * completers:,} "
                  f"if {completers} do); streak prizes pay " + ", ".join(f"{c:,} at {d} days" for d, c in ms_coins.items()) +
                  f". Quest XP is {conf['xp_per_quest']:,} per quest and adds no coins. Rerolls take coins out "
                  f"({reroll_text}...).")
        return (f"{len(members):,} active members, average payday {average:,} (range {min(paydays):,}–{max(paydays):,}). "
                f"{completers} full sets a day at x{mult:g} = **{daily:,} a day, {daily * 7:,} a week** created." + extras)

    async def _q_delete_member(self, user_id: int) -> None:
        everyone = await self.config.all_members()
        for guild_id, members in everyone.items():
            if user_id in members:
                await self.config.member_from_ids(guild_id, user_id).clear()
