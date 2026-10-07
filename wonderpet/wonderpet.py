"""WonderPet: one pet for the whole server, raised together in #cuddle.

- a single card with Feed / Play / Clean / Treat buttons; each member gets one free
  free Feed, Play or Clean per day (their choice of one, Pacific time), treats cost wondercoins;
- the pet grows egg > baby > teen > adult over a few weeks and its adult form depends on how well
  it was looked after; neglect goes through four escalating warnings (about 3 days) before it is
  lost, and a new egg arrives a day later;
- weekly top carers, a Pet Hall of past pets, and little reactions to birthdays, anniversaries
  and event nights (other cogs dispatch `wonder_birthday`, `wonder_anniversary`, `wonder_event_start`).

All state is kept in Config; the loop only ages the pet and posts warnings.
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from redbot.core import Config, bank, commands
from redbot.core.data_manager import cog_data_path

from . import embeds, engine
from .constants import (
    ART_SLOTS, BTN_CLEAN, BTN_FEED, BTN_PLAY, BTN_TREAT, COG_VERSION, CONFIG_IDENTIFIER, DEFAULT_GUILD,
    DEFAULT_MEMBER, NEW_EGG_DELAY_HOURS, RENDER_DELAY_SECONDS, STAFF_ROLE_IDS, TICK_SECONDS, TREATS,
)

log = logging.getLogger("red.evac-cogs.wonderpet")

_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)
_VERBS = {"feed": "fed", "play": "played with", "clean": "cleaned"}
FLAVOR_GAP_SECONDS = 3 * 3600


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

class CareView(discord.ui.View):
    """One persistent view for the home card; there is only ever one pet per server."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Feed", emoji="\N{CUT OF MEAT}", style=discord.ButtonStyle.success, custom_id=BTN_FEED)
    async def feed(self, interaction, button):
        await self.cog.handle_action(interaction, "feed")

    @discord.ui.button(label="Play", emoji="\N{TENNIS RACQUET AND BALL}", style=discord.ButtonStyle.primary,
                       custom_id=BTN_PLAY)
    async def play(self, interaction, button):
        await self.cog.handle_action(interaction, "play")

    @discord.ui.button(label="Clean", emoji="\N{BUBBLES}", style=discord.ButtonStyle.primary, custom_id=BTN_CLEAN)
    async def clean(self, interaction, button):
        await self.cog.handle_action(interaction, "clean")

    @discord.ui.button(label="Treat", emoji="\N{WRAPPED PRESENT}", style=discord.ButtonStyle.secondary,
                       custom_id=BTN_TREAT)
    async def treat(self, interaction, button):
        await self.cog.open_treat_menu(interaction)


class TreatSelect(discord.ui.Select):
    def __init__(self, cog, options):
        super().__init__(placeholder="Pick a treat", options=options, min_values=1, max_values=1)
        self.cog = cog

    async def callback(self, interaction):
        await self.cog.handle_treat(interaction, self.values[0])


class TreatView(discord.ui.View):
    def __init__(self, cog, options):
        super().__init__(timeout=120)
        self.add_item(TreatSelect(cog, options))


# ---------------------------------------------------------------------------
# cog
# ---------------------------------------------------------------------------

class WonderPet(commands.Cog):
    """One shared server pet, raised together in #cuddle."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self._locks: dict[int, asyncio.Lock] = {}
        self._render_tasks: dict[int, asyncio.Task] = {}
        self.render_delay = RENDER_DELAY_SECONDS
        self._last_card_cmd: dict[int, float] = {}
        self.view = CareView(self)
        if hasattr(bot, "add_view"):
            bot.add_view(self.view)  # the buttons keep working across restarts
        self._task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        self._task.cancel()
        for task in self._render_tasks.values():
            task.cancel()
        try:
            self.view.stop()
        except Exception:
            pass

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()
            async with self._lock(guild.id):
                gconf = self.config.guild(guild)
                s = await gconf.all()
                engine.forget_user(s["pet"], s["week_carers"], s["history"], user_id)
                await gconf.pet.set(s["pet"])
                await gconf.week_carers.set(s["week_carers"])
                await gconf.history.set(s["history"])

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _now() -> float:
        return time.time()

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

    # -- art ----------------------------------------------------------------

    def _art_path(self, slot: str):
        path = cog_data_path(self) / "art" / f"{slot}.png"
        return path if path.exists() else None

    async def _card(self, guild, s: dict, now: float):
        pet = s["pet"]
        slot = embeds.art_slot(pet)
        path = self._art_path(slot)
        file = discord.File(str(path), filename=f"{slot}.png") if path else None
        embed = embeds.card_embed(
            pet, now=now,
            this_week=engine.top_carers(s["week_carers"].get(engine.week_key(now))),
            last_week=engine.top_carers(s["week_carers"].get(engine.prev_week_key(now)), 3),
            art_file=f"{slot}.png" if file else None,
        )
        return embed, file

    # -- card management ------------------------------------------------------

    def _channel(self, guild, s: dict):
        return guild.get_channel(s["channel_id"] or 0)

    async def _repost(self, guild) -> None:
        """Delete the old card and post a fresh one at the bottom of the channel."""
        gconf = self.config.guild(guild)
        s = await gconf.all()
        channel = self._channel(guild, s)
        if channel is None or not s["pet"]:
            return
        await self._delete_card(guild, s)
        embed, file = await self._card(guild, s, self._now())
        kwargs = {"embed": embed, "view": self.view}
        if file:
            kwargs["file"] = file
        try:
            msg = await channel.send(**kwargs)
        except _DISCORD_ERRORS:
            log.warning("wonderpet: could not post the card in %s", channel.id)
            return
        await gconf.card_message_id.set(msg.id)

    async def _delete_card(self, guild, s: dict) -> None:
        channel = self._channel(guild, s)
        if channel is not None and s["card_message_id"]:
            try:
                await channel.get_partial_message(s["card_message_id"]).delete()
            except _DISCORD_ERRORS:
                pass
        await self.config.guild(guild).card_message_id.set(None)

    async def _render(self, guild) -> None:
        """Redraw the existing card (or post one if it was deleted)."""
        gconf = self.config.guild(guild)
        s = await gconf.all()
        channel = self._channel(guild, s)
        if channel is None or not s["pet"]:
            return
        if not s["card_message_id"]:
            await self._repost(guild)
            return
        embed, file = await self._card(guild, s, self._now())
        try:
            await channel.get_partial_message(s["card_message_id"]).edit(
                embed=embed, view=self.view, attachments=[file] if file else [])
        except discord.NotFound:
            await self._repost(guild)
        except _DISCORD_ERRORS:
            log.warning("wonderpet: could not update the card")

    def _request_render(self, guild) -> None:
        """Button presses redraw the card at most once per `render_delay` seconds."""
        if guild.id in self._render_tasks:
            return
        self._render_tasks[guild.id] = asyncio.get_running_loop().create_task(self._delayed_render(guild))

    async def _delayed_render(self, guild) -> None:
        try:
            await asyncio.sleep(self.render_delay)
            self._render_tasks.pop(guild.id, None)
            await self._render(guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("wonderpet: card redraw failed for guild %s", guild.id)
        finally:
            self._render_tasks.pop(guild.id, None)

    async def _say(self, guild, s: dict, text: str, ping: bool = False) -> None:
        channel = self._channel(guild, s)
        if channel is None:
            return
        role = guild.get_role(s["ping_role_id"] or 0) if (ping and s["ping_role_id"]) else None
        if role is not None:
            text = f"{role.mention} {text}"
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(roles=[role] if role else False,
                                                                              users=False, everyone=False))
        except _DISCORD_ERRORS:
            log.warning("wonderpet: could not post in %s", channel.id)

    # -- turning events into announcements (call with the lock held) ----------------

    async def _process(self, guild, s: dict, pet: dict, events: list, now: float) -> list:
        """Apply events to saved state and return [(text, ping)] to send after the lock is released."""
        msgs: list = []
        gconf = self.config.guild(guild)
        for ev in events:
            if ev == "hatched":
                msgs.append((embeds.hatch_text(pet), False))
            elif ev.startswith("grew:"):
                msgs.append((embeds.grew_text(pet, ev.split(":")[1]), False))
            elif ev.startswith("warn:"):
                level = int(ev.split(":")[1])
                msgs.append((embeds.warn_text(pet, level), level >= 2))
            elif ev == "recovered":
                msgs.append((embeds.recovered_text(pet), False))
            elif ev.startswith("lost:") or ev == "retired":
                entry = engine.history_entry(pet, now)
                history = (s["history"] + [entry])[-50:]
                await gconf.history.set(history)
                msgs.append((embeds.ended_text(pet, entry), False))
                await self._delete_card(guild, await gconf.all())
                await gconf.pet.set({})
                await gconf.next_egg_ts.set(now + NEW_EGG_DELAY_HOURS * 3600)
                return msgs
        await gconf.pet.set(pet)
        return msgs

    async def _send_all(self, guild, s: dict, msgs: list) -> None:
        for text, ping in msgs:
            await self._say(guild, s, text, ping)

    # -- scheduler ------------------------------------------------------------

    async def _loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:
                    await self._tick(guild)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("wonderpet: tick failed for guild %s", guild.id)
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self, guild, now: float | None = None) -> None:
        now = now or self._now()
        gconf = self.config.guild(guild)
        msgs: list = []
        async with self._lock(guild.id):
            s = await gconf.all()
            if self._channel(guild, s) is None:
                return  # not set up in this server
            if not s["pet"]:
                if s["next_egg_ts"] and now >= s["next_egg_ts"]:
                    await self._new_egg(guild, s, now, announce=True)
                return
            pet = s["pet"]
            events = engine.advance(pet, now)
            msgs = await self._process(guild, s, pet, events, now)
            s = await gconf.all()
        await self._send_all(guild, s, msgs)
        if not s["pet"]:
            return
        today = engine.local_date(now)
        hour = engine.local_hour(now)
        if s["daily_post_date"] != today.isoformat() and hour >= s["daily_hour"]:
            await gconf.daily_post_date.set(today.isoformat())
            await self._repost(guild)
        else:
            await self._render(guild)

    async def _new_egg(self, guild, s: dict, now: float, *, announce: bool) -> None:
        gconf = self.config.guild(guild)
        pet = engine.new_pet(now, s["next_pet_id"])
        await gconf.next_pet_id.set(s["next_pet_id"] + 1)
        await gconf.pet.set(pet)
        await gconf.next_egg_ts.set(None)
        s = await gconf.all()
        await self._repost(guild)
        if announce:
            await self._say(guild, s, embeds.new_egg_text(pet))

    # -- buttons --------------------------------------------------------------

    async def handle_action(self, interaction, kind: str) -> None:
        guild, member, now = interaction.guild, interaction.user, self._now()
        gconf, mconf = self.config.guild(guild), self.config.member(member)
        msgs: list = []
        async with self._lock(guild.id):
            s = await gconf.all()
            pet = s["pet"]
            if not pet or not pet["alive"]:
                await interaction.response.send_message("There's no pet right now. A new egg is on its way.", ephemeral=True)
                return
            today = engine.local_date(now).isoformat()
            daily = engine.daily_for(await mconf.daily(), today)
            if daily["used"]:
                await interaction.response.send_message(
                    f"You already used your free care today ({_VERBS[daily['used']]} {pet['name']}). "
                    f"Come back tomorrow, or give a treat.", ephemeral=True)
                return
            events = engine.advance(pet, now)
            if any(e.startswith("lost:") or e == "retired" for e in events):
                msgs = await self._process(guild, s, pet, events, now)
                reply = f"It was too late. {pet['name']} is gone."
                changed = False
            else:
                before = round(pet[engine.ACTION_METER[kind]])
                engine.give_care(pet, kind, member.id)
                after = round(pet[engine.ACTION_METER[kind]])
                daily["used"] = kind
                events += engine.advance(pet, now)
                week = engine.record_week(s["week_carers"], now, member.id)
                await gconf.week_carers.set(week)
                await mconf.daily.set(daily)
                msgs = await self._process(guild, s, pet, events, now)
                reply = (f"{embeds.pet_emoji(pet)} You {_VERBS[kind]} {pet['name']} ({engine.ACTION_METER[kind]} "
                         f"{before} to {after}). That was your free care for today. Treats are still open.")
                changed = True
            s = await gconf.all()
        await interaction.response.send_message(reply, ephemeral=True)
        await self._send_all(guild, s, msgs)
        if changed or msgs:
            self._request_render(guild)

    async def open_treat_menu(self, interaction) -> None:
        guild = interaction.guild
        s = await self.config.guild(guild).all()
        pet = s["pet"]
        if not pet or not pet["alive"]:
            await interaction.response.send_message("There's no pet right now.", ephemeral=True)
            return
        if pet["stage"] == "egg":
            await interaction.response.send_message("The egg doesn't need treats yet. Wait for it to hatch.", ephemeral=True)
            return
        currency = await bank.get_currency_name(guild)
        options = [
            discord.SelectOption(label=f"{label} ({engine.treat_price(key, s['treat_price']):,} {currency})",
                                 value=key, emoji=emoji)
            for key, (label, emoji, _meters, _mult) in TREATS.items()
        ]
        await interaction.response.send_message(
            f"Treats fill meters faster than the free actions. Each member can give up to {s['treat_cap']} a day.",
            view=TreatView(self, options), ephemeral=True)

    async def handle_treat(self, interaction, key: str) -> None:
        guild, member, now = interaction.guild, interaction.user, self._now()
        gconf, mconf = self.config.guild(guild), self.config.member(member)
        msgs: list = []
        async with self._lock(guild.id):
            s = await gconf.all()
            pet = s["pet"]
            if key not in TREATS or not pet or not pet["alive"] or pet["stage"] == "egg":
                await interaction.response.send_message("Treats aren't available right now.", ephemeral=True)
                return
            today = engine.local_date(now).isoformat()
            daily = engine.daily_for(await mconf.daily(), today)
            if daily["treats"] >= s["treat_cap"]:
                await interaction.response.send_message(
                    f"You've given {s['treat_cap']} treats today already. Come back tomorrow.", ephemeral=True)
                return
            price = engine.treat_price(key, s["treat_price"])
            try:
                await bank.withdraw_credits(member, price)
            except ValueError:
                currency = await bank.get_currency_name(guild)
                await interaction.response.send_message(f"You need {price:,} {currency} for that.", ephemeral=True)
                return
            events = engine.advance(pet, now)
            engine.give_treat(pet, key, member.id)
            daily["treats"] += 1
            events += engine.advance(pet, now)
            await gconf.week_carers.set(engine.record_week(s["week_carers"], now, member.id))
            await mconf.daily.set(daily)
            msgs = await self._process(guild, s, pet, events, now)
            s = await gconf.all()
        label, emoji, _m, _mult = TREATS[key]
        await interaction.response.send_message(f"{emoji} {pet['name']} loved the {label.lower()}! (-{price:,})", ephemeral=True)
        await self._send_all(guild, s, msgs)
        self._request_render(guild)

    # -- reactions to server life (other cogs dispatch these) ---------------------

    async def _flavor(self, guild, kind: str, names: list) -> None:
        if not names:
            return
        now = self._now()
        gconf = self.config.guild(guild)
        async with self._lock(guild.id):
            s = await gconf.all()
            pet = s["pet"]
            if (not pet or not pet["alive"] or pet["stage"] == "egg" or self._channel(guild, s) is None
                    or now - s["last_flavor_ts"] < FLAVOR_GAP_SECONDS):
                return
            pet["happy"] = min(100.0, pet["happy"] + 5)
            await gconf.pet.set(pet)
            await gconf.last_flavor_ts.set(now)
        await self._say(guild, s, embeds.flavor_text(pet, kind, [str(n) for n in names]))

    @commands.Cog.listener()
    async def on_wonder_birthday(self, guild, members):
        await self._flavor(guild, "birthday", [m.mention for m in members])

    @commands.Cog.listener()
    async def on_wonder_anniversary(self, guild, members):
        await self._flavor(guild, "anniversary", [m.mention for m in members])

    @commands.Cog.listener()
    async def on_wonder_event_start(self, guild, title):
        await self._flavor(guild, "event", [f"**{title}**"])

    # -- commands -------------------------------------------------------------

    @commands.group(name="wonderpet", invoke_without_command=True)
    @commands.guild_only()
    async def pet(self, ctx: commands.Context):
        """The server pet. Posts its card so you can feed, play and clean."""
        await self.pet_card(ctx)

    @pet.command(name="card")
    async def pet_card(self, ctx: commands.Context):
        """Bring the pet's card to the bottom of the channel."""
        s = await self.config.guild(ctx.guild).all()
        if not s["channel_id"]:
            await ctx.send("The pet isn't set up here yet. Staff: `.wonderpet channel #cuddle`, then `.wonderpet newegg`.")
            return
        if not s["pet"]:
            await ctx.send("No pet right now. A new egg is on its way.")
            return
        now = self._now()
        if now - self._last_card_cmd.get(ctx.guild.id, 0) < 30:
            await ctx.send("The card was just posted. Check the channel.")
            return
        self._last_card_cmd[ctx.guild.id] = now
        await self._repost(ctx.guild)
        channel = self._channel(ctx.guild, s)
        if channel is not None and channel.id != ctx.channel.id:
            await ctx.send(f"Posted in {channel.mention}.")

    @pet.command(name="me")
    async def pet_me(self, ctx: commands.Context):
        """What you can still do for the pet today."""
        now = self._now()
        daily = engine.daily_for(await self.config.member(ctx.author).daily(), engine.local_date(now).isoformat())
        s = await self.config.guild(ctx.guild).all()
        treats = max(0, s["treat_cap"] - daily["treats"])
        free = "used (" + daily["used"] + ")" if daily["used"] else "still available, pick one of Feed, Play or Clean"
        await ctx.send(f"Your free care today: {free}. Treats left: {treats}.")

    @pet.command(name="carers")
    async def pet_carers(self, ctx: commands.Context):
        """Top carers this week and last week."""
        s = await self.config.guild(ctx.guild).all()
        now = self._now()
        this = engine.top_carers(s["week_carers"].get(engine.week_key(now)), 10)
        last = engine.top_carers(s["week_carers"].get(engine.prev_week_key(now)), 5)
        e = discord.Embed(title="\N{PAW PRINTS} Top carers", color=discord.Color(0x2ECC71))
        e.add_field(name="This week", value=embeds._people(this), inline=False)
        e.add_field(name="Last week", value=embeds._people(last), inline=False)
        await ctx.send(embed=e, allowed_mentions=discord.AllowedMentions.none())

    @pet.command(name="history", aliases=["hall"])
    async def pet_history(self, ctx: commands.Context):
        """The Pet Hall: pets from before."""
        s = await self.config.guild(ctx.guild).all()
        if not s["history"]:
            await ctx.send("The Pet Hall is empty so far.")
            return
        ends = {"died": "passed away", "ran_away": "ran away", "retired": "retired happily", "replaced": "replaced"}
        lines = []
        for h in reversed(s["history"][-10:]):
            lived = engine.age_text(h["end_ts"] - h["born_ts"])
            top = ", ".join(f"<@{t['user']}>" for t in h["top"][:2])
            lines.append(f"**{h['name']}**: {ends.get(h['end'], h['end'])} after {lived}" + (f" (carers: {top})" if top else ""))
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @pet.command(name="version")
    async def pet_version(self, ctx: commands.Context):
        """Show the running build (deploy probe)."""
        await ctx.send(f"WonderPet v{COG_VERSION}")

    # -- staff ----------------------------------------------------------------

    @pet.command(name="channel")
    async def pet_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Where the pet lives (#cuddle)."""
        if not await self._require_staff(ctx):
            return
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"The pet lives in {channel.mention}. Start with `.wonderpet newegg`.")

    @pet.command(name="pingrole")
    async def pet_pingrole(self, ctx: commands.Context, role: discord.Role = None):
        """Role pinged on the serious warnings (sick / critical / final). No role = no pings."""
        if not await self._require_staff(ctx):
            return
        await self.config.guild(ctx.guild).ping_role_id.set(role.id if role else None)
        await ctx.send(f"Serious warnings ping {role.name}." if role else "Warnings won't ping a role.",
                       allowed_mentions=discord.AllowedMentions.none())

    @pet.command(name="hour")
    async def pet_hour(self, ctx: commands.Context, hour: int):
        """Pacific hour (0-23) the card is posted fresh each day. Default 9."""
        if not await self._require_staff(ctx):
            return
        if not 0 <= hour <= 23:
            await ctx.send("Pick an hour from 0 to 23.")
            return
        await self.config.guild(ctx.guild).daily_hour.set(hour)
        await ctx.send(f"The card is re-posted daily at {hour}:00 Pacific.")

    @pet.command(name="treatprice")
    async def pet_treatprice(self, ctx: commands.Context, price: int):
        """Price of a basic treat (the feast costs 4x)."""
        if not await self._require_staff(ctx):
            return
        await self.config.guild(ctx.guild).treat_price.set(max(0, price))
        await ctx.send(f"Basic treats cost {max(0, price):,}; the royal feast {max(0, price) * 4:,}.")

    @pet.command(name="treatcap")
    async def pet_treatcap(self, ctx: commands.Context, per_day: int):
        """How many treats one member can give per day."""
        if not await self._require_staff(ctx):
            return
        await self.config.guild(ctx.guild).treat_cap.set(max(0, per_day))
        await ctx.send(f"Each member can give {max(0, per_day)} treats a day.")

    @pet.command(name="name")
    async def pet_name(self, ctx: commands.Context, *, name: str):
        """Rename the current pet."""
        if not await self._require_staff(ctx):
            return
        async with self._lock(ctx.guild.id):
            pet = await self.config.guild(ctx.guild).pet()
            if not pet:
                await ctx.send("There's no pet to rename.")
                return
            pet["name"] = name.strip()[:24]
            await self.config.guild(ctx.guild).pet.set(pet)
        await self._render(ctx.guild)
        await ctx.send(f"Now called {pet['name']}.")

    @pet.command(name="newegg")
    async def pet_newegg(self, ctx: commands.Context, confirm: str = ""):
        """Start a fresh egg. If a pet is alive, add `confirm` to replace it."""
        if not await self._require_staff(ctx):
            return
        async with self._lock(ctx.guild.id):
            gconf = self.config.guild(ctx.guild)
            s = await gconf.all()
            if self._channel(ctx.guild, s) is None:
                await ctx.send("Set the channel first: `.wonderpet channel #cuddle`.")
                return
            if s["pet"] and confirm.lower() != "confirm":
                await ctx.send(f"{s['pet']['name']} is still here. Use `.wonderpet newegg confirm` to replace it.")
                return
            if s["pet"]:
                pet = s["pet"]
                pet["ended"] = {"kind": "replaced", "ts": self._now()}
                await gconf.history.set((s["history"] + [engine.history_entry(pet, self._now())])[-50:])
            await self._new_egg(ctx.guild, s, self._now(), announce=False)
        await ctx.send("A new egg is in the card above.")

    @pet.command(name="art")
    async def pet_art(self, ctx: commands.Context, slot: str):
        """Upload a picture for a look: attach an image. Slots: egg, baby, teen, scruffy, happy, radiant, sick."""
        if not await self._require_staff(ctx):
            return
        slot = slot.lower()
        if slot not in ART_SLOTS:
            await ctx.send("Slots: " + ", ".join(ART_SLOTS))
            return
        if not ctx.message.attachments:
            await ctx.send("Attach an image to the same message.")
            return
        att = ctx.message.attachments[0]
        if att.size > 8 * 1024 * 1024:
            await ctx.send("That image is over 8 MB.")
            return
        data = await att.read()
        folder = cog_data_path(self) / "art"
        folder.mkdir(parents=True, exist_ok=True)
        try:  # normalise to a small PNG so the thumbnail is light and the extension is honest
            import io

            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            img.thumbnail((512, 512))
            img.save(folder / f"{slot}.png", "PNG")
        except Exception:
            await ctx.send("I couldn't read that as an image. Try a PNG or JPG.")
            return
        await self._render(ctx.guild)
        await ctx.send(f"Saved the picture for **{slot}**.")

    @pet.command(name="settings")
    async def pet_settings(self, ctx: commands.Context):
        """Show settings."""
        s = await self.config.guild(ctx.guild).all()
        channel = f"<#{s['channel_id']}>" if s["channel_id"] else "not set"
        role = f"<@&{s['ping_role_id']}>" if s["ping_role_id"] else "none"
        art = [slot for slot in ART_SLOTS if self._art_path(slot)]
        pet = s["pet"]
        state = f"{pet['name']} ({pet['stage']})" if pet else "no pet"
        await ctx.send(
            f"**WonderPet v{COG_VERSION}**\nChannel: {channel}\nPing role: {role}\nCard posted daily at {s['daily_hour']}:00 PT\n"
            f"Treats: {s['treat_price']:,} (feast {s['treat_price'] * 4:,}), {s['treat_cap']} per member per day\n"
            f"Art uploaded: {', '.join(art) or 'none yet'}\nCurrent: {state}",
            allowed_mentions=discord.AllowedMentions.none())
