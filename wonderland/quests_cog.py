"""Discord side of daily quests, mixed into the Wonderland cog (no new cog, no new top-level command).

Members open it from the Daily Quests button on the #wondercasino hub (the gamble cog calls
`open_quest_panel`). Mods get `.wld quests`: the on/off switch, the reward multiplier, the quest pool
and the usage counts. Ships switched off: the hub button stays hidden and nothing is tracked or paid
until a mod turns it on.

Tracking. Each quest key has one source, so an action counts once:
  hook     `bot.dispatch("wonderland_quest", member, key, amount)` from the success path of our own cog.
           Used for every evac-cogs quest because a button press or reaction is not an outcome (an
           already-answered Verdict, a lost minigame and a "Can't make it" RSVP fire the same events).
  command  on_command_completion for third-party commands (`.fish`, LevelUp `.stars`), checked
           against the cog's own state where it keeps one (stars: LevelUp's cooldown stamp).
  chat     on_message in the chat channel (#cuddle), at most one counted message per 15 seconds.
Payday claims (for the "share of Payday users who open quests" number) come from ExtendedEconomy's
`red_economy_payday_claim` event, which only fires on a real claim.
"""
from __future__ import annotations

import asyncio
import logging
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

DEFAULT_GUILD_QUESTS = {
    "enabled": False,            # ships off: switched on in `.wld quests` once the coin ledger is live
    "multiplier": Q.DEFAULT_MULTIPLIER,
    "disabled": [],              # quest keys taken out of the pool
    "targets": {},               # key -> custom target (count quests)
    "chat_channel_id": None,     # None = the channel named "cuddle"
    "commands_channel_id": BOT_COMMANDS_CHANNEL_ID,
    "stats": {},                 # day -> usage counts (quests.stats_day)
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
    def __init__(self, cog, member_id: int):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.member_id = cog, member_id

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, _button):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message("Tap Daily Quests to open your own.", ephemeral=True)
        embed = await self.cog._q_panel_embed(interaction.user, count_open=False)
        await interaction.response.edit_message(embed=embed, view=self)


class QuestsMixin:
    """Needs `self.bot`, `self.config` (guild `quests`, member `quests`), `self._exists` and `self._lurker_role_id`."""

    def _q_setup(self) -> None:
        self._q_locks: dict = {}
        self._q_chat_last: dict = {}     # (guild, member) -> timestamp of the last counted chat message
        self._q_chat_full: dict = {}     # (guild, member) -> day the chat quest filled up (stop counting)
        self._q_chat_cache: dict = {}    # guild -> (channel id, expires)
        self._q_live = False             # quests switched on? Gates the guide's Daily Quests line (`@quests`)

    async def cog_load(self) -> None:
        await super().cog_load()
        try:
            saved = await self.config.all_guilds()
            self._q_live = any((data.get("quests") or {}).get("enabled") for data in saved.values())
        except Exception:
            log.exception("quests: reading the on/off switch failed")

    def _q_lock(self, *key) -> asyncio.Lock:
        return self._q_locks.setdefault(key, asyncio.Lock())

    async def _q_conf(self, guild) -> dict:
        conf = dict(DEFAULT_GUILD_QUESTS)
        conf.update(await self.config.guild(guild).quests() or {})
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
        if key == "card":
            cog = get("CardCollect")
            if cog is None or not self._exists("card daily"):
                return OFF, None, "CardCollect not loaded"
            status = ok(await self._q_hub_channel(guild))
            pulled = (await cog._member_state(member)).daily_pull_date == day
            return (DONE, *status[1:]) if status[0] == OPEN and pulled else status
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
        if key == "rsvp":
            cog = get("WonderEvents")
            if cog is None:
                return OFF, None, "WonderEvents not loaded"
            open_events = [e for e in (await cog.config.guild(guild).events() or {}).values()
                           if e.get("message_id") and not e.get("ended") and not e.get("cancelled")]
            if not open_events:
                return OFF, None, "no open event"
            todo = [e for e in open_events if str((e.get("rsvp") or {}).get(uid)) not in ("going", "maybe")]
            for ev in todo:
                status = ok(guild.get_channel(ev.get("channel_id") or 0))
                if status[0] == OPEN:
                    return status
            return (DONE, None, "already RSVP'd") if not todo else (OFF, None, "event channel hidden")
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
        guild = member.guild
        conf = await self._q_conf(guild)
        embed = discord.Embed(title="📜 Daily quests", color=discord.Color(QUEST_COLOR))
        if not conf["enabled"]:
            embed.description = "Daily quests aren't open yet."
            return embed
        if await self._q_is_lurker(member):
            embed.description = (f"Daily quests unlock once you're active. Say hi in "
                                 f"{self._q_link(guild, '{#cuddle}')}, then tap the button again.")
            return embed
        state = await self._q_open(member, conf, count_open)
        try:
            from redbot.core import bank
            currency = await bank.get_currency_name(guild)
        except Exception:
            currency = "coins"
        payday = await self._q_payday(member)
        mult = float(conf["multiplier"])
        lines = []
        for index, key in enumerate(state["assigned"]):
            target = Q.target_of(key, conf["targets"])
            progress = int(state["progress"].get(key, 0))
            title = self._q_link(guild, Q.title_of(key, conf["targets"]))
            if key in state["paid"]:
                reward = Q.quest_reward(payday, mult, state["paid"].index(key))
                lines.append(f"✅ ~~{title}~~ · +{reward:,} {currency}")
                continue
            reward = Q.quest_reward(payday, mult, len(state["paid"]))
            head = f"⬜ **{title}** · +{reward:,} {currency}"
            if target > 1:
                head += f"\n`{Q.bar(progress, target)}` {progress}/{target}"
            how = self._q_link(guild, Q.BY_KEY[key].how)
            if key in ("verdict", "pet", "puzzle", "rsvp", "sponsor"):   # say where, when the cog knows
                channel = (await self._q_status(key, member, conf))[1]
                if channel is not None:
                    how += f" ({channel.mention})"
            lines.append(f"{head}\n-# {how}")
        if not lines:
            embed.description = "Nothing you can do today has a quest right now. Check back tomorrow."
        else:
            embed.description = "\n\n".join(lines)
        done, total = len(state["paid"]), len(state["assigned"])
        footer = [f"**Today:** {done}/{total} done · +{int(state['earned']):,} {currency} earned"]
        streak = Q.current_streak(state, state["day"])
        if streak or state.get("best_streak"):
            footer.append(f"🔥 **Streak:** {streak} day{'s' if streak != 1 else ''} (best {state.get('best_streak', 0)})")
        footer.append(f"⏳ New quests in **{Q.fmt_duration(Q.seconds_to_reset())}** (midnight Pacific)")
        embed.add_field(name="​", value="\n".join(footer), inline=False)
        embed.set_footer(text="Coins land in your balance as you finish each quest. A full set pays one payday.")
        return embed

    async def open_quest_panel(self, interaction: discord.Interaction) -> None:
        """Public: the gamble hub's Daily Quests button calls this. Ephemeral; nothing public, no DMs."""
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed = await self._q_panel_embed(member)
        except Exception:
            log.exception("quests: building the panel for %s failed", member.id)
            return await interaction.followup.send("Something went wrong. Try again in a moment.", ephemeral=True)
        await interaction.followup.send(embed=embed, view=QuestPanelView(self, member.id), ephemeral=True)

    async def quest_summary(self, member) -> Optional[dict]:
        """Public: `.mystats` reads the streak from here."""
        state = await self.config.member(member).quests()
        if not state or not state.get("sets"):
            return None
        return {"streak": Q.current_streak(state, Q.today()), "best": int(state.get("best_streak", 0)),
                "sets": int(state.get("sets", 0))}

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
            if quest.source == Q.COMMAND and quest.command == name:
                if quest.key == "star" and not self._q_star_given(ctx):
                    return
                await self._q_record(member, quest.key, 1, source=Q.COMMAND)
                return

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

    # ------------------------------------------------------------------ admin (used by quests_panel.py)

    async def _q_set(self, guild, **changes) -> None:
        async with self.config.guild(guild).quests() as saved:
            for name, value in changes.items():
                saved[name] = value
        if "enabled" in changes:
            self._q_live = bool(changes["enabled"])

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
        return (f"{len(members):,} active members, average payday {average:,} (range {min(paydays):,}–{max(paydays):,}). "
                f"{completers} full sets a day at x{mult:g} = **{daily:,} a day, {daily * 7:,} a week** created.")

    async def _q_delete_member(self, user_id: int) -> None:
        everyone = await self.config.all_members()
        for guild_id, members in everyone.items():
            if user_id in members:
                await self.config.member_from_ids(guild_id, user_id).clear()
