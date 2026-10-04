"""wonderevents: movie nights and game nights with RSVPs, reminders, votes and attendance.

Staff run `.night create movie` and fill in a short form (title, when, description,
vote options, image). The bot then:
- posts the event in the events channel with the kind's ping role, Going / Maybe /
  Can't make it buttons and a <t:...> time everyone sees in their own timezone;
- creates a native Discord Scheduled Event (in the voice channel, or an external event if the kind has none) (members who click
  Interested get Discord's own start notification);
- posts a native poll for the vote options (closes 2h before the event);
- reminds only the people who said Going/Maybe, an hour before (configurable);
- marks the event live at start time and samples who is in the voice channel every
  5 minutes, so `.night regulars` can show who actually shows up.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import Config, commands

from . import embeds, engine
from .constants import (
    BTN_GOING,
    BTN_MAYBE,
    BTN_NO,
    COG_VERSION,
    DEFAULT_LOCATION,
    CONFIG_IDENTIFIER,
    DEFAULT_DURATION_MIN,
    DEFAULT_GUILD,
    DEFAULT_MEMBER,
    HOME_TZ,
    POLL_MAX_OPTIONS,
    TICK_SECONDS,
)

log = logging.getLogger("red.evac-cogs.wonderevents")

_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)
_POLL_QUESTIONS = {"movie": "What should we watch?", "game": "What should we play?"}


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

class RSVPView(discord.ui.View):
    """One persistent view for every event post; the event is looked up by message id."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Going", emoji="\N{WHITE HEAVY CHECK MARK}", style=discord.ButtonStyle.success, custom_id=BTN_GOING)
    async def going(self, interaction, button):
        await self.cog.handle_rsvp(interaction, "going")

    @discord.ui.button(label="Maybe", emoji="\N{THINKING FACE}", style=discord.ButtonStyle.secondary, custom_id=BTN_MAYBE)
    async def maybe(self, interaction, button):
        await self.cog.handle_rsvp(interaction, "maybe")

    @discord.ui.button(label="Can't make it", style=discord.ButtonStyle.secondary, custom_id=BTN_NO)
    async def cant(self, interaction, button):
        await self.cog.handle_rsvp(interaction, "no")


class EventModal(discord.ui.Modal):
    def __init__(self, cog, *, guild_id: int, kind: str, host_id: int, event_id: int | None = None, values: dict | None = None):
        super().__init__(title="Edit event" if event_id else "New event", timeout=900)
        self.cog, self.guild_id, self.kind, self.host_id, self.event_id = cog, guild_id, kind, host_id, event_id
        v = values or {}
        self.f_title = discord.ui.TextInput(label="Title", max_length=100, default=v.get("title"))
        self.f_when = discord.ui.TextInput(
            label="When (your timezone unless you add one)", max_length=60, default=v.get("when"),
            placeholder="Sat 9pm ET · Oct 17 9:30pm · tomorrow 8pm",
        )
        self.f_desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False,
                                           max_length=1000, default=v.get("desc"))
        self.f_image = discord.ui.TextInput(label="Image URL (optional)", required=False, max_length=400,
                                            default=v.get("image"), placeholder="https://… poster or banner")
        self.add_item(self.f_title)
        self.add_item(self.f_when)
        self.add_item(self.f_desc)
        self.f_options = None
        if event_id is None:
            self.f_options = discord.ui.TextInput(
                label="Vote options (optional, one per line)", style=discord.TextStyle.paragraph, required=False,
                max_length=600, default=v.get("options"), placeholder="Project Hail Mary\nDune: Part Two\n…",
            )
            self.add_item(self.f_options)
        self.add_item(self.f_image)

    def values(self) -> dict:
        return {
            "title": (self.f_title.value or "").strip(),
            "when": (self.f_when.value or "").strip(),
            "desc": (self.f_desc.value or "").strip(),
            "options": (self.f_options.value or "").strip() if self.f_options else "",
            "image": (self.f_image.value or "").strip(),
        }

    async def on_submit(self, interaction):
        await self.cog.handle_form(interaction, self)

    async def on_error(self, interaction, error):
        log.exception("wonderevents: form error", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong saving that event.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong saving that event.", ephemeral=True)
        except _DISCORD_ERRORS:
            pass


class OpenFormView(discord.ui.View):
    """A button that opens the form (prefix commands can't open a modal directly)."""

    def __init__(self, cog, *, author_id: int, label: str = "Fill in the event", **modal_kw):
        super().__init__(timeout=900)
        self.cog, self.author_id, self.modal_kw = cog, author_id, modal_kw
        self.open_form.label = label

    @discord.ui.button(label="Fill in the event", emoji="\N{MEMO}", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction, button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran the command can use this.", ephemeral=True)
            return
        await interaction.response.send_modal(EventModal(self.cog, **self.modal_kw))


# ---------------------------------------------------------------------------
# cog
# ---------------------------------------------------------------------------

class WonderEvents(commands.Cog):
    """Movie/game nights: RSVP buttons, Discord scheduled events, votes, reminders, attendance."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self._locks: dict[int, asyncio.Lock] = {}
        self.rsvp_view = RSVPView(self)
        if hasattr(bot, "add_view"):
            bot.add_view(self.rsvp_view)  # buttons keep working across restarts
        self._task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        self._task.cancel()
        try:
            self.rsvp_view.stop()
        except Exception:
            pass

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()
            async with self._lock(guild.id):
                async with self.config.guild(guild).events() as events:
                    for ev in events.values():
                        (ev.get("rsvp") or {}).pop(str(user_id), None)
                        (ev.get("attend") or {}).pop(str(user_id), None)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    # -- permissions ------------------------------------------------------

    async def _can_host(self, member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_guild or getattr(perms, "manage_events", False)):
            return True
        try:
            if await self.bot.is_mod(member):
                return True
        except Exception:
            pass
        host_roles = set(await self.config.guild(member.guild).host_role_ids())
        return any(r.id in host_roles for r in member.roles)

    async def _require_host(self, ctx) -> bool:
        if await self._can_host(ctx.author):
            return True
        await ctx.send("Only staff and event hosts can do that.")
        return False

    # -- helpers ----------------------------------------------------------

    async def _member_tz(self, member):
        name = await self.config.member(member).tz()
        return engine.resolve_tz(name) or HOME_TZ

    async def _get_event(self, guild, event_id) -> dict | None:
        events = await self.config.guild(guild).events()
        return events.get(str(event_id))

    async def _save_event(self, guild, ev: dict) -> None:
        async with self.config.guild(guild).events() as events:
            events[str(ev["id"])] = ev

    async def _render(self, guild, ev: dict) -> None:
        """Re-draw the announcement (buttons removed once it's over)."""
        channel = guild.get_channel(ev.get("channel_id") or 0)
        if channel is None or not ev.get("message_id"):
            return
        over = ev.get("ended") or ev.get("cancelled")
        try:
            await channel.get_partial_message(ev["message_id"]).edit(
                embed=embeds.event_embed(ev, guild_id=guild.id), view=None if over else self.rsvp_view,
            )
        except _DISCORD_ERRORS:
            log.warning("wonderevents: could not update the post for event #%s", ev["id"])

    async def _scheduled(self, guild, ev):
        seid = ev.get("scheduled_event_id")
        if not seid:
            return None
        found = guild.get_scheduled_event(seid) if hasattr(guild, "get_scheduled_event") else None
        if found is None and hasattr(guild, "fetch_scheduled_event"):
            try:
                found = await guild.fetch_scheduled_event(seid)
            except _DISCORD_ERRORS:
                return None
        return found

    # -- form handling ----------------------------------------------------

    async def handle_form(self, interaction, modal: EventModal) -> None:
        guild = interaction.guild
        vals = modal.values()
        tz = await self._member_tz(interaction.user)
        try:
            start = engine.parse_when(vals["when"], datetime.now(timezone.utc), tz)
        except engine.WhenError as exc:
            retry = OpenFormView(self, author_id=interaction.user.id, label="Fix it", guild_id=modal.guild_id,
                                 kind=modal.kind, host_id=modal.host_id, event_id=modal.event_id, values=vals)
            await interaction.response.send_message(f"\N{WARNING SIGN} {exc}", view=retry, ephemeral=True)
            return
        if not vals["title"]:
            vals["title"] = "Event"
        image = vals["image"] if vals["image"].startswith(("http://", "https://")) else None
        if modal.event_id is None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            ev, note = await self.create_event(guild, kind=modal.kind, host_id=modal.host_id, title=vals["title"],
                                               start=start, desc=vals["desc"], image=image,
                                               options=engine.poll_options(vals["options"], POLL_MAX_OPTIONS),
                                               created_by=interaction.user.id)
            await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            note = await self.update_event(guild, modal.event_id, title=vals["title"], start=start,
                                           desc=vals["desc"], image=image)
            await interaction.followup.send(note, ephemeral=True)

    async def create_event(self, guild, *, kind, host_id, title, start, desc, image, options, created_by):
        gconf = self.config.guild(guild)
        settings = await gconf.all()
        k = settings["kinds"].get(kind) or {}
        channel = guild.get_channel(settings["channel_id"] or 0)
        if channel is None:
            return None, "Set the events channel first: `.night channel #channel`."
        async with self._lock(guild.id):
            event_id = await gconf.next_id()
            await gconf.next_id.set(event_id + 1)
        ev = {
            "id": event_id, "kind": kind, "title": title[:100], "desc": desc[:1000] if desc else "", "image": image,
            "emoji": k.get("emoji") or "\N{CALENDAR}", "host_id": host_id, "start_ts": start.timestamp(),
            "duration": int(k.get("duration") or DEFAULT_DURATION_MIN), "channel_id": channel.id,
            "vc_id": k.get("vc_id"), "ping_role_id": k.get("ping_role_id"), "message_id": None,
            "scheduled_event_id": None, "poll_message_id": None, "rsvp": {}, "attend": {},
            "reminded": False, "started": False, "ended": False, "cancelled": False, "last_sample": 0,
            "created_by": created_by, "created_ts": time.time(),
        }
        vc = guild.get_channel(ev["vc_id"] or 0)
        notes = []
        if hasattr(guild, "create_scheduled_event"):
            try:
                common = dict(name=ev["title"], start_time=start.astimezone(timezone.utc),
                              privacy_level=discord.PrivacyLevel.guild_only,
                              description=(ev["desc"] or ev["title"])[:1000])
                if vc is not None:
                    se = await guild.create_scheduled_event(entity_type=discord.EntityType.voice, channel=vc, **common)
                else:  # no fixed room (rooms are made on demand): an external event needs a place and an end time
                    se = await guild.create_scheduled_event(
                        entity_type=discord.EntityType.external, location=settings.get("location") or DEFAULT_LOCATION,
                        end_time=(start + timedelta(minutes=ev["duration"])).astimezone(timezone.utc), **common)
                ev["scheduled_event_id"] = se.id
            except _DISCORD_ERRORS:
                notes.append("couldn't create the Discord scheduled event (needs Manage Events)")
        role = guild.get_role(ev["ping_role_id"] or 0)
        host = f"<@{host_id}> is hosting" if host_id else "Join us for"
        content = f"{role.mention + ' ' if role else ''}{host} **{ev['title']}**! Hit **Going** to get pinged when it starts."
        try:
            msg = await channel.send(
                content=content, embed=embeds.event_embed(ev, guild_id=guild.id), view=self.rsvp_view,
                allowed_mentions=discord.AllowedMentions(roles=[role] if role else False, users=False, everyone=False),
            )
        except _DISCORD_ERRORS:
            return None, f"I couldn't post in {channel.mention}."
        ev["message_id"] = msg.id
        if len(options) >= 2:
            ev["poll_message_id"] = await self._post_poll(channel, ev, options)
            if not ev["poll_message_id"]:
                notes.append("couldn't post the vote")
        await self._save_event(guild, ev)
        tail = f" ({'; '.join(notes)})" if notes else ""
        return ev, f"\N{WHITE HEAVY CHECK MARK} Posted event #{event_id} in {channel.mention}{tail}."

    async def _post_poll(self, channel, ev, options):
        try:
            poll = discord.Poll(question=_POLL_QUESTIONS.get(ev["kind"], "Vote!"),
                                duration=timedelta(hours=engine.poll_hours(ev["start_ts"], time.time())))
            for opt in options[:POLL_MAX_OPTIONS]:
                poll.add_answer(text=opt)
            pmsg = await channel.send(poll=poll)
            return pmsg.id
        except (*_DISCORD_ERRORS, TypeError, ValueError):
            log.exception("wonderevents: could not post the poll")
            return None

    async def update_event(self, guild, event_id, *, title, start, desc, image):
        async with self._lock(guild.id):
            ev = await self._get_event(guild, event_id)
            if ev is None or ev.get("cancelled") or ev.get("ended"):
                return "That event doesn't exist or is already over."
            moved = abs(ev["start_ts"] - start.timestamp()) > 1
            ev.update(title=title[:100], desc=desc[:1000] if desc else "", image=image, start_ts=start.timestamp())
            if moved:
                ev.update(reminded=False, started=False, last_sample=0)
            await self._save_event(guild, ev)
        await self._render(guild, ev)
        se = await self._scheduled(guild, ev)
        if se is not None:
            try:
                await se.edit(name=ev["title"], description=(ev["desc"] or ev["title"])[:1000],
                              start_time=start.astimezone(timezone.utc))
            except _DISCORD_ERRORS:
                pass
        if moved and ev.get("message_id"):
            channel = guild.get_channel(ev["channel_id"])
            going = engine.rsvp_lists(ev["rsvp"])["going"] + engine.rsvp_lists(ev["rsvp"])["maybe"]
            if channel is not None and going:
                try:
                    await channel.send(
                        f"\N{SPIRAL CALENDAR PAD} **{ev['title']}** moved to <t:{int(ev['start_ts'])}:F> "
                        + " ".join(f"<@{u}>" for u in going[:40]),
                        reference=channel.get_partial_message(ev["message_id"]),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
                except _DISCORD_ERRORS:
                    pass
        return f"\N{WHITE HEAVY CHECK MARK} Updated event #{event_id}."

    # -- RSVP buttons -----------------------------------------------------

    async def handle_rsvp(self, interaction, choice: str) -> None:
        guild = interaction.guild
        message_id = interaction.message.id
        async with self._lock(guild.id):
            events = await self.config.guild(guild).events()
            ev = next((e for e in events.values() if e.get("message_id") == message_id), None)
            if ev is None or ev.get("ended") or ev.get("cancelled"):
                await interaction.response.send_message("This event is over.", ephemeral=True)
                return
            engine.toggle_rsvp(ev.setdefault("rsvp", {}), interaction.user.id, choice)
            await self._save_event(guild, ev)
        await interaction.response.edit_message(embed=embeds.event_embed(ev, guild_id=guild.id), view=self.rsvp_view)

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
                    log.exception("wonderevents: tick failed for guild %s", guild.id)
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self, guild, now_ts: float | None = None) -> None:
        now_ts = now_ts or time.time()
        gconf = self.config.guild(guild)
        remind = await gconf.remind_minutes()
        events = await gconf.events()
        for ev_id in list(events):
            ev = events[ev_id]
            actions = engine.due_actions(ev, now_ts, remind)
            if not actions:
                continue
            async with self._lock(guild.id):
                fresh = await self._get_event(guild, ev_id)  # a button/edit may have landed meanwhile
                if fresh is None:
                    continue
                for action in actions:
                    await getattr(self, f"_do_{action}")(guild, fresh, now_ts)
                await self._save_event(guild, fresh)
            if {"start", "end"} & set(actions):
                await self._render(guild, fresh)

    async def _ping_reply(self, guild, ev, text, ids):
        channel = guild.get_channel(ev.get("channel_id") or 0)
        if channel is None:
            return
        mentions = " ".join(f"<@{u}>" for u in ids[:40])
        try:
            await channel.send(
                f"{text} {mentions}".strip(),
                reference=channel.get_partial_message(ev["message_id"]) if ev.get("message_id") else None,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except _DISCORD_ERRORS:
            log.warning("wonderevents: could not post a reminder for event #%s", ev["id"])

    async def _do_remind(self, guild, ev, now_ts):
        ev["reminded"] = True
        lists = engine.rsvp_lists(ev.get("rsvp") or {})
        ids = lists["going"] + lists["maybe"]
        if not ids:
            return
        where = f" in <#{ev['vc_id']}>" if ev.get("vc_id") else ""
        await self._ping_reply(guild, ev, f"\N{ALARM CLOCK} **{ev['title']}** starts <t:{int(ev['start_ts'])}:R>{where}!", ids)

    async def _do_start(self, guild, ev, now_ts):
        ev["started"] = True
        se = await self._scheduled(guild, ev)
        if se is not None:
            try:
                await se.start()
            except _DISCORD_ERRORS:
                pass
        ids = engine.rsvp_lists(ev.get("rsvp") or {})["going"]
        where = f" in <#{ev['vc_id']}>" if ev.get("vc_id") else ""
        await self._ping_reply(guild, ev, f"{ev.get('emoji') or ''} **{ev['title']}** is starting now{where}!".strip(), ids)

    async def _do_sample(self, guild, ev, now_ts):
        ev["last_sample"] = now_ts
        vc = guild.get_channel(ev.get("vc_id") or 0)
        if vc is not None:
            present = [m.id for m in getattr(vc, "members", []) if not m.bot]
        else:
            # No fixed room: look in every voice channel, but only count people who said Going/Maybe
            # or hold the kind's ping role, so unrelated voice chat doesn't count as attending.
            lists = engine.rsvp_lists(ev.get("rsvp") or {})
            rsvped = set(lists["going"]) | set(lists["maybe"])
            role_id = ev.get("ping_role_id")
            present = []
            for ch in getattr(guild, "voice_channels", []):
                for m in getattr(ch, "members", []):
                    if m.bot:
                        continue
                    if m.id in rsvped or (role_id and any(r.id == role_id for r in getattr(m, "roles", []))):
                        present.append(m.id)
            present = list(dict.fromkeys(present))
        engine.add_sample(ev.setdefault("attend", {}), present)

    async def _do_end(self, guild, ev, now_ts):
        ev["ended"] = True
        se = await self._scheduled(guild, ev)
        if se is not None:
            try:
                if ev.get("started"):
                    await se.end()
                else:
                    await se.cancel()
            except _DISCORD_ERRORS:
                pass

    # -- commands ---------------------------------------------------------

    @commands.group(name="night", aliases=["nights"], invoke_without_command=True)
    @commands.guild_only()
    async def event(self, ctx: commands.Context):
        """Upcoming events. Staff: `.night create <kind>`, `edit`, `cancel`, `poll`. Everyone: `.night regulars`."""
        await self.event_list(ctx)

    @event.command(name="list")
    async def event_list(self, ctx: commands.Context):
        """Upcoming and live events."""
        events = await self.config.guild(ctx.guild).events()
        upcoming = sorted((e for e in events.values() if not e.get("ended") and not e.get("cancelled")),
                          key=lambda e: e["start_ts"])
        e = discord.Embed(title="\N{SPIRAL CALENDAR PAD} Upcoming events", description=embeds.list_text(upcoming[:15]),
                          color=discord.Color(0x9B59B6))
        await ctx.send(embed=e)

    @event.command(name="version")
    async def event_version(self, ctx: commands.Context):
        """Show the running build (deploy probe)."""
        await ctx.send(f"WonderEvents v{COG_VERSION}")

    @event.command(name="create")
    async def event_create(self, ctx: commands.Context, kind: str, host: discord.Member = None):
        """Create an event: `.night create movie` (or `game`, any kind from `.night kind list`). Optional host."""
        if not await self._require_host(ctx):
            return
        kinds = await self.config.guild(ctx.guild).kinds()
        kind = kind.lower()
        if kind not in kinds:
            known = ", ".join(f"`{k}`" for k in kinds) or "none yet: `.night kind add movie @MovieNight #Movie Night VC 🎬`"
            await ctx.send(f"Unknown kind `{kind}`. Kinds: {known}")
            return
        if not await self.config.guild(ctx.guild).channel_id():
            await ctx.send("Set the events channel first: `.night channel #channel`.")
            return
        label = kinds[kind].get("label") or kind.title()
        view = OpenFormView(self, author_id=ctx.author.id, guild_id=ctx.guild.id, kind=kind,
                            host_id=(host or ctx.author).id, values={"title": label})
        await ctx.send(f"{kinds[kind].get('emoji', '')} New **{label}**: fill in the form.", view=view)

    @event.command(name="edit")
    async def event_edit(self, ctx: commands.Context, event_id: int):
        """Change an event's title, time, description or image."""
        if not await self._require_host(ctx):
            return
        ev = await self._get_event(ctx.guild, event_id)
        if ev is None or ev.get("ended") or ev.get("cancelled"):
            await ctx.send("That event doesn't exist or is already over.")
            return
        tz = await self._member_tz(ctx.author)
        when = datetime.fromtimestamp(ev["start_ts"], tz).strftime("%Y-%m-%d %H:%M ") + str(tz.key if hasattr(tz, "key") else tz)
        view = OpenFormView(self, author_id=ctx.author.id, label="Edit the event", guild_id=ctx.guild.id,
                            kind=ev["kind"], host_id=ev.get("host_id") or ctx.author.id, event_id=event_id,
                            values={"title": ev["title"], "when": when, "desc": ev.get("desc"), "image": ev.get("image")})
        await ctx.send(f"Editing event #{event_id}.", view=view)

    @event.command(name="cancel")
    async def event_cancel(self, ctx: commands.Context, event_id: int):
        """Cancel an event (people who said Going/Maybe are told)."""
        if not await self._require_host(ctx):
            return
        async with self._lock(ctx.guild.id):
            ev = await self._get_event(ctx.guild, event_id)
            if ev is None or ev.get("ended") or ev.get("cancelled"):
                await ctx.send("That event doesn't exist or is already over.")
                return
            ev["cancelled"] = True
            await self._save_event(ctx.guild, ev)
        await self._render(ctx.guild, ev)
        se = await self._scheduled(ctx.guild, ev)
        if se is not None:
            try:
                await se.cancel()
            except _DISCORD_ERRORS:
                pass
        lists = engine.rsvp_lists(ev.get("rsvp") or {})
        await self._ping_reply(ctx.guild, ev, f"\N{CROSS MARK} **{ev['title']}** is cancelled.", lists["going"] + lists["maybe"])
        await ctx.send(f"Cancelled event #{event_id}.")

    @event.command(name="poll")
    async def event_poll(self, ctx: commands.Context, event_id: int, *, options: str):
        """Add a vote to an event: `.night poll 3 Project Hail Mary | Dune 2 | Arrival`."""
        if not await self._require_host(ctx):
            return
        ev = await self._get_event(ctx.guild, event_id)
        if ev is None or ev.get("ended") or ev.get("cancelled"):
            await ctx.send("That event doesn't exist or is already over.")
            return
        opts = engine.poll_options(options.replace("|", "\n"), POLL_MAX_OPTIONS)
        if len(opts) < 2:
            await ctx.send("Give at least two options separated by `|`.")
            return
        channel = ctx.guild.get_channel(ev["channel_id"])
        pid = await self._post_poll(channel, ev, opts) if channel else None
        if not pid:
            await ctx.send("I couldn't post the vote.")
            return
        async with self._lock(ctx.guild.id):
            fresh = await self._get_event(ctx.guild, event_id)
            fresh["poll_message_id"] = pid
            await self._save_event(ctx.guild, fresh)
        await ctx.send(f"Vote posted for event #{event_id}.")

    @event.command(name="attendance")
    async def event_attendance(self, ctx: commands.Context, event_id: int):
        """Who was in the voice channel (20+ minutes counts) and who said they'd come."""
        if not await self._require_host(ctx):
            return
        ev = await self._get_event(ctx.guild, event_id)
        if ev is None:
            await ctx.send("No such event.")
            return
        came = engine.attendees(ev.get("attend") or {})
        lists = engine.rsvp_lists(ev.get("rsvp") or {})
        said = set(lists["going"])
        lines = [
            f"**{ev['title']}** — <t:{int(ev['start_ts'])}:D>",
            f"Came ({len(came)}): " + (" ".join(f"<@{u}>" for u in came) or "—"),
            f"Said Going ({len(said)}), came: {len(said & set(came))}",
            f"Came without RSVPing: {len(set(came) - said - set(lists['maybe']))}",
        ]
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @event.command(name="regulars")
    async def event_regulars(self, ctx: commands.Context, days: int = 90):
        """Who comes to the most events (20+ minutes in the voice channel), last N days."""
        days = max(1, min(days, 365))
        events = list((await self.config.guild(ctx.guild).events()).values())
        rows = engine.regulars(events, time.time() - days * 86400)
        if not rows:
            await ctx.send(f"No attendance recorded in the last {days} days yet.")
            return
        medals = ["\N{FIRST PLACE MEDAL}", "\N{SECOND PLACE MEDAL}", "\N{THIRD PLACE MEDAL}"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — {n} event{'s' if n != 1 else ''}"
                 for i, (uid, n) in enumerate(rows[:15])]
        e = discord.Embed(title=f"\N{TROPHY} Event regulars — last {days} days", description="\n".join(lines),
                          color=discord.Color(0x9B59B6))
        await ctx.send(embed=e, allowed_mentions=discord.AllowedMentions.none())

    @event.command(name="mytz")
    async def event_mytz(self, ctx: commands.Context, tz: str):
        """The timezone you type event times in: `ET`, `PT`, `UK`, `CET` or e.g. `Europe/Berlin`."""
        zone = engine.resolve_tz(tz)
        if zone is None:
            await ctx.send("I don't know that timezone. Try `ET`, `PT`, `UK`, `CET` or `Europe/Berlin`.")
            return
        await self.config.member(ctx.author).tz.set(tz)
        await ctx.send(f"Event times you type are read as **{zone.key}** unless you add a timezone.")

    # -- settings ---------------------------------------------------------

    @event.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Where event announcements are posted."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"Events are posted in {channel.mention}.")

    @event.command(name="hostroles")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_hostroles(self, ctx: commands.Context, *roles: discord.Role):
        """Roles (besides admins/mods) that may create and manage events. Replaces the list."""
        await self.config.guild(ctx.guild).host_role_ids.set([r.id for r in roles])
        await ctx.send("Event hosts: " + (", ".join(r.name for r in roles) or "admins/mods only"))

    @event.command(name="remind")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_remind(self, ctx: commands.Context, minutes: int):
        """How long before start the Going/Maybe reminder goes out (0 = off). Default 60."""
        await self.config.guild(ctx.guild).remind_minutes.set(max(0, minutes))
        await ctx.send("Reminders off." if minutes <= 0 else f"Reminders go out {minutes} minutes before.")

    @event.command(name="location")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_location(self, ctx: commands.Context, *, text: str = None):
        """Where events without a fixed voice channel are held (shown on the Discord event). No text = show."""
        if text is None:
            await ctx.send(f"Location: {await self.config.guild(ctx.guild).location() or DEFAULT_LOCATION}")
            return
        await self.config.guild(ctx.guild).location.set(text[:100])
        await ctx.send(f"Location set to: {text[:100]}")

    @event.group(name="kind", invoke_without_command=True)
    async def event_kind(self, ctx: commands.Context):
        """Event kinds: `.night kind add <key> <@ping role> [voice channel] [emoji] [hours]`, `remove`, `list`."""
        await self.event_kind_list(ctx)

    @event_kind.command(name="list")
    async def event_kind_list(self, ctx: commands.Context):
        kinds = await self.config.guild(ctx.guild).kinds()
        if not kinds:
            await ctx.send("No kinds yet. `.night kind add movie @MovieNight #Movie Night VC 🎬 3`")
            return
        lines = [
            f"`{k}` {v.get('emoji', '')} **{v.get('label')}** — pings <@&{v.get('ping_role_id')}>, "
            f"{'in <#' + str(v['vc_id']) + '>' if v.get('vc_id') else 'no fixed voice channel'}, "
            f"{v.get('duration', DEFAULT_DURATION_MIN) // 60}h"
            for k, v in kinds.items()
        ]
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @event_kind.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_kind_add(self, ctx: commands.Context, key: str, ping_role: discord.Role,
                             voice_channel: Optional[discord.VoiceChannel] = None, emoji: str = None,
                             hours: float = 3.0):
        """Add or replace a kind. Voice channel is optional (skip it if rooms are made on demand).
        `.night kind add game @GameNight 🎮 3` or `.night kind add movie @MovieNight #Movie VC 🎬 3`"""
        key = key.lower()
        label = {"movie": "Movie Night", "game": "Game Night"}.get(key, key.title())
        emoji = emoji or {"movie": "\N{CLAPPER BOARD}", "game": "\N{VIDEO GAME}"}.get(key, "\N{CALENDAR}")
        async with self.config.guild(ctx.guild).kinds() as kinds:
            kinds[key] = {"label": label, "emoji": emoji, "ping_role_id": ping_role.id, "vc_id": voice_channel.id if voice_channel else None,
                          "duration": max(30, int(hours * 60))}
        await ctx.send(f"Kind `{key}` saved: {emoji} {label}, pings {ping_role.name}, "
                       f"{'in ' + voice_channel.name if voice_channel else 'no fixed voice channel'}.",
                       allowed_mentions=discord.AllowedMentions.none())

    @event_kind.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_kind_remove(self, ctx: commands.Context, key: str):
        async with self.config.guild(ctx.guild).kinds() as kinds:
            kinds.pop(key.lower(), None)
        await ctx.send(f"Removed kind `{key.lower()}`.")

    @event.command(name="settings")
    async def event_settings(self, ctx: commands.Context):
        """Show event settings."""
        s = await self.config.guild(ctx.guild).all()
        hosts = ", ".join(f"<@&{r}>" for r in s["host_role_ids"]) or "admins/mods only"
        channel = f"<#{s['channel_id']}>" if s["channel_id"] else "not set"
        await ctx.send(
            f"**WonderEvents v{COG_VERSION}**\nChannel: {channel}\n"
            f"Hosts: {hosts}\nReminder: {s['remind_minutes']} min before\nKinds: {', '.join(s['kinds']) or 'none'}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
