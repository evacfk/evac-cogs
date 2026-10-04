"""celebrations: birthdays and join anniversaries for Wonderland.

Birthdays (all dates are America/Los_Angeles):
- Members save theirs with `.birthday set March 14` (year optional; with a
  year, the post says how old they're turning).
- At midnight Pacific on the day: the hoisted birthday role is added and a
  wondercoin gift is paid (once per 300 days, never to brand-new joiners).
- At the announce hour: an embed in the chat channel (with a star reaction so
  it can be starred onto the starboard) and an embed in #announcements.
- When the day ends: the role comes off and the #announcements post is deleted.

Anniversaries: once a day at the announce hour, one embed in the chat channel
listing everyone whose join anniversary is today (current join date). The
"N year club!" roles themselves are handled by the lurker cog (`.yearclub`).

Lurkers (hidden by the lurker cog) are skipped for both.
"""
from __future__ import annotations

import asyncio
import logging
import time
import discord
from redbot.core import Config, bank, commands

from . import embeds, engine
from .constants import (
    COG_VERSION,
    COLOR_BIRTHDAY,
    CONFIG_IDENTIFIER,
    DEFAULT_GUILD,
    DEFAULT_MEMBER,
    TICK_MINUTES,
)

log = logging.getLogger("red.evac-cogs.celebrations")

_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)


class Celebrations(commands.Cog):
    """Birthdays (role, gift, posts) and join-anniversary shout-outs."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self._locks: dict[int, asyncio.Lock] = {}
        self._task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        self._task.cancel()

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild in self.bot.guilds:
            await self.config.member_from_ids(guild.id, user_id).clear()

    # ------------------------------------------------------------------
    # helpers other cogs use
    # ------------------------------------------------------------------

    async def transient_role_ids(self, guild: discord.Guild) -> set[int]:
        """Roles the lurker cog must strip but never store/restore (the birthday role)."""
        role_id = await self.config.guild(guild).role_id()
        return {role_id} if role_id else set()

    async def _lurker_role_id(self, guild: discord.Guild):
        lurker = self.bot.get_cog("Lurker")
        if lurker is None:
            return None
        try:
            return await lurker.config.guild(guild).lurker_role_id()
        except Exception:
            log.exception("celebrations: could not read the Lurker role")
            return None

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _is_lurker(member, lurker_role_id) -> bool:
        return bool(lurker_role_id) and any(r.id == lurker_role_id for r in member.roles)

    async def _currency(self, guild) -> str:
        try:
            return await bank.get_currency_name(guild)
        except Exception:
            return "credits"

    async def _gift(self, member, amount: int, why: str) -> bool:
        if amount <= 0:
            return False
        try:
            await bank.deposit_credits(member, amount)
            return True
        except Exception:
            log.exception("celebrations: %s gift of %s failed for %s", why, amount, member.id)
            return False

    # ------------------------------------------------------------------
    # the daily cycle
    # ------------------------------------------------------------------

    async def _loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:
                    await self._tick(guild)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("celebrations: tick failed for guild %s", guild.id)
            await asyncio.sleep(TICK_MINUTES * 60)

    async def _tick(self, guild: discord.Guild, now_ts: float | None = None) -> None:
        async with self._lock(guild.id):
            await self._tick_locked(guild, now_ts or time.time())

    async def _tick_locked(self, guild: discord.Guild, now_ts: float) -> None:
        gconf = self.config.guild(guild)
        s = await gconf.all()
        if not (s["channel_id"] or s["role_id"] or s["announce_channel_id"]):
            return  # not configured in this server
        today = engine.local_today(now_ts)
        state = s["state"] or {}
        if state.get("date") != today.isoformat():
            if state.get("date"):
                await self._end_day(guild, s, state)
            state = {"date": today.isoformat(), "birthdays": {}, "anniv_posted": False}
            await gconf.state.set(state)

        role = guild.get_role(s["role_id"]) if s["role_id"] else None
        lurker_role_id = await self._lurker_role_id(guild)

        # anyone still wearing the role who isn't celebrating today (a cleanup missed by a restart)
        if role is not None:
            for m in list(role.members):
                if str(m.id) not in state["birthdays"]:
                    await self._remove_role(m, role, "Birthday is over")

        await self._start_birthdays(guild, s, state, today, now_ts, role, lurker_role_id)

        if engine.local_hour(now_ts) >= s["announce_hour"]:
            await self._post_birthdays(guild, s, state)
            if s["anniversaries"] and not state.get("anniv_posted"):
                state["anniv_posted"] = True  # mark first: at most one anniversary post per day
                await gconf.state.set(state)
                await self._post_anniversaries(guild, s, today, lurker_role_id)

    async def _start_birthdays(self, guild, s, state, today, now_ts, role, lurker_role_id):
        gconf = self.config.guild(guild)
        all_members = await self.config.all_members(guild)
        for uid, mc in all_members.items():
            if mc.get("month") is None or mc.get("day") is None or str(uid) in state["birthdays"]:
                continue
            if not engine.is_today(mc["month"], mc["day"], today):
                continue
            if engine.recently_celebrated(mc.get("last_celebrated"), today):
                continue
            member = guild.get_member(int(uid))
            if member is None or member.bot or self._is_lurker(member, lurker_role_id):
                continue
            gift = 0
            joined = member.joined_at.timestamp() if member.joined_at else now_ts
            if now_ts - joined >= s["gift_min_member_days"] * 86400:
                gift = engine.pick_gift(s["gift_min"], s["gift_max"])
            state["birthdays"][str(uid)] = {
                "gift": gift,
                "age": engine.turning_age(mc.get("year"), mc["month"], mc["day"], today),
                "posted": False,
                "ann": None,
            }
            # record before paying: a crash can at worst skip a gift, never pay it twice
            await gconf.state.set(state)
            await self.config.member(member).last_celebrated.set(today.isoformat())
            if role is not None:
                try:
                    await member.add_roles(role, reason="Birthday")
                except _DISCORD_ERRORS:
                    log.warning("celebrations: could not add the birthday role to %s", member.id)
            if gift and not await self._gift(member, gift, "birthday"):
                state["birthdays"][str(uid)]["gift"] = 0
                await gconf.state.set(state)

    async def _post_birthdays(self, guild, s, state):
        gconf = self.config.guild(guild)
        chat = guild.get_channel(s["channel_id"]) if s["channel_id"] else None
        ann_channel = guild.get_channel(s["announce_channel_id"]) if s["announce_channel_id"] else None
        if chat is None and ann_channel is None:
            return
        currency = None
        for uid, entry in state["birthdays"].items():
            if entry.get("posted"):
                continue
            member = guild.get_member(int(uid))
            if member is None:
                entry["posted"] = True
                await gconf.state.set(state)
                continue
            currency = currency or await self._currency(guild)
            if chat is not None:
                try:
                    msg = await chat.send(
                        content=f"\N{PARTY POPPER} Happy birthday {member.mention}! \N{BIRTHDAY CAKE}",
                        embed=embeds.birthday_embed(member, age=entry.get("age"), gift=entry.get("gift", 0),
                                                    currency=currency, star=s["star_emoji"]),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
                    if s["star_emoji"]:
                        try:
                            await msg.add_reaction(s["star_emoji"])
                        except _DISCORD_ERRORS:
                            log.warning("celebrations: could not add the star reaction")
                except _DISCORD_ERRORS:
                    log.warning("celebrations: could not post the birthday in %s", chat.id)
            if ann_channel is not None:
                try:
                    amsg = await ann_channel.send(
                        embed=embeds.announcement_embed(member, chat_channel_id=s["channel_id"]),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    entry["ann"] = [ann_channel.id, amsg.id]
                except _DISCORD_ERRORS:
                    log.warning("celebrations: could not post in announcements %s", ann_channel.id)
            entry["posted"] = True
            await gconf.state.set(state)

    async def _post_anniversaries(self, guild, s, today, lurker_role_id):
        chat = guild.get_channel(s["channel_id"]) if s["channel_id"] else None
        if chat is None:
            return
        rows = []
        for member in guild.members:
            if member.bot or member.joined_at is None or self._is_lurker(member, lurker_role_id):
                continue
            joined = engine.local_today(member.joined_at.timestamp())
            years = engine.anniversary_years(joined, today)
            if years >= 1:
                rows.append((member, years))
        if not rows:
            return
        rows.sort(key=lambda r: (-r[1], r[0].display_name.lower()))
        gift = int(s["anniversary_gift"] or 0)
        if gift:
            for member, _years in rows:
                await self._gift(member, gift, "anniversary")
        mentions = " ".join(m.mention for m, _ in rows[:40])
        try:
            msg = await chat.send(
                content=f"\N{PARTY POPPER} Happy Wonderland anniversary {mentions}!"[:2000],
                embed=embeds.anniversary_embed(rows, star=s["star_emoji"], gift=gift, currency=await self._currency(guild)),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            if s["star_emoji"]:
                try:
                    await msg.add_reaction(s["star_emoji"])
                except _DISCORD_ERRORS:
                    pass
        except _DISCORD_ERRORS:
            log.warning("celebrations: could not post anniversaries in %s", chat.id)

    async def _end_day(self, guild, s, state):
        role = guild.get_role(s["role_id"]) if s["role_id"] else None
        for uid, entry in (state.get("birthdays") or {}).items():
            member = guild.get_member(int(uid))
            if member is not None and role is not None:
                await self._remove_role(member, role, "Birthday is over")
            ann = entry.get("ann")
            if ann:
                channel = guild.get_channel(ann[0])
                if channel is not None:
                    try:
                        await channel.get_partial_message(ann[1]).delete()
                    except _DISCORD_ERRORS:
                        pass

    @staticmethod
    async def _remove_role(member, role, reason):
        if not any(r.id == role.id for r in member.roles):
            return
        try:
            await member.remove_roles(role, reason=reason)
        except _DISCORD_ERRORS:
            log.warning("celebrations: could not remove the birthday role from %s", member.id)

    # ------------------------------------------------------------------
    # member commands
    # ------------------------------------------------------------------

    @commands.group(name="birthday", aliases=["bday"], invoke_without_command=True)
    @commands.guild_only()
    async def birthday(self, ctx: commands.Context):
        """Your birthday. `.birthday set March 14` (year optional), `.birthday remove`, `.birthday upcoming`."""
        mc = await self.config.member(ctx.author).all()
        if mc["month"] is None:
            await ctx.send(
                "You haven't set a birthday. `.birthday set March 14`, or `.birthday set March 14 1998` "
                "to show the age you're turning in your birthday post."
            )
            return
        await ctx.send(
            f"\N{BIRTHDAY CAKE} Your birthday: **{engine.fmt_birthday(mc['month'], mc['day'], mc['year'])}**. "
            "Change it with `.birthday set`, remove it with `.birthday remove`."
        )

    @birthday.command(name="set")
    async def birthday_set(self, ctx: commands.Context, *, date: str):
        """Save your birthday: `March 14`, `14 March`, `March 14 1998`, `1998-03-14`. The year is optional."""
        await self._save_birthday(ctx, ctx.author, date)

    async def _save_birthday(self, ctx, member, text):
        today = engine.local_today(time.time())
        try:
            bd = engine.parse_birthday(text, today)
        except engine.BirthdayError as exc:
            await ctx.send(str(exc))
            return
        mconf = self.config.member(member)
        await mconf.month.set(bd.month)
        await mconf.day.set(bd.day)
        await mconf.year.set(bd.year)
        self_set = member == ctx.author
        who = "Your" if self_set else f"{member.display_name}'s"
        extra = ""
        if bd.year:
            extra = " Your age will show in your birthday post." if self_set else " Their age will show in the birthday post."
        if engine.is_today(bd.month, bd.day, today):
            extra += " That's today, happy birthday! \N{PARTY POPPER}"
        await ctx.send(f"\N{BIRTHDAY CAKE} Saved. {who} birthday: **{engine.fmt_birthday(bd.month, bd.day, bd.year)}**.{extra}")

    @birthday.command(name="remove")
    async def birthday_remove(self, ctx: commands.Context):
        """Forget your birthday."""
        mconf = self.config.member(ctx.author)
        await mconf.month.clear()
        await mconf.day.clear()
        await mconf.year.clear()
        await ctx.send("Done, your birthday is removed.")

    @birthday.command(name="show")
    async def birthday_show(self, ctx: commands.Context, member: discord.Member):
        """See someone's birthday (never their year)."""
        mc = await self.config.member(member).all()
        if mc["month"] is None:
            await ctx.send(f"{member.display_name} hasn't set a birthday.")
            return
        await ctx.send(f"\N{BIRTHDAY CAKE} {member.display_name}: **{engine.fmt_birthday(mc['month'], mc['day'])}**")

    @birthday.command(name="upcoming", aliases=["list"])
    async def birthday_upcoming(self, ctx: commands.Context, days: int = 30):
        """Birthdays in the next N days (default 30)."""
        days = max(1, min(days, 366))
        today = engine.local_today(time.time())
        lurker_role_id = await self._lurker_role_id(ctx.guild)
        rows = []
        for uid, mc in (await self.config.all_members(ctx.guild)).items():
            if mc.get("month") is None:
                continue
            member = ctx.guild.get_member(int(uid))
            if member is None or member.bot or self._is_lurker(member, lurker_role_id):
                continue
            nxt = engine.next_occurrence(mc["month"], mc["day"], today)
            if (nxt - today).days < days:
                rows.append((nxt, member.display_name))
        if not rows:
            await ctx.send(f"No birthdays in the next {days} days.")
            return
        rows.sort()
        lines = [
            f"`{d.strftime('%b %d')}` {name}" + (" \N{BIRTHDAY CAKE} today!" if d == today else "")
            for d, name in rows[:40]
        ]
        if len(rows) > 40:
            lines.append(f"…and {len(rows) - 40} more")
        e = discord.Embed(title=f"\N{BIRTHDAY CAKE} Birthdays in the next {days} days",
                          description="\n".join(lines), color=discord.Color(COLOR_BIRTHDAY))
        await ctx.send(embed=e)

    # ------------------------------------------------------------------
    # staff commands
    # ------------------------------------------------------------------

    @commands.group(name="celebrations", aliases=["celebrate"], invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    async def celebrations(self, ctx: commands.Context):
        """Birthday/anniversary settings. Subcommands: channel, announcements, role, makerole, gift,
        giftmindays, hour, star, anniversaries, anniversarygift, setfor, clearfor, preview, run, version."""
        s = await self.config.guild(ctx.guild).all()
        role = ctx.guild.get_role(s["role_id"]) if s["role_id"] else None
        role_ok = ""
        if role is not None:
            role_ok = "(hoisted)" if role.hoist else "(⚠️ not hoisted: `.celebrations role` again to fix)"
        await ctx.send(embeds.settings_text(s, role_ok=role_ok), allowed_mentions=discord.AllowedMentions.none())

    @celebrations.command(name="version")
    async def celebrations_version(self, ctx: commands.Context):
        """Show the running build (deploy probe)."""
        await ctx.send(f"Celebrations v{COG_VERSION}")

    @celebrations.command(name="channel")
    async def celebrations_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Where birthday + anniversary posts go (they stay up). Usually #cuddle."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"Birthday and anniversary posts go to {channel.mention}.")

    @celebrations.command(name="announcements")
    async def celebrations_announcements(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Where the temporary birthday post goes (deleted when the day ends). No channel = off."""
        await self.config.guild(ctx.guild).announce_channel_id.set(channel.id if channel else None)
        await ctx.send(f"Temporary birthday posts go to {channel.mention}." if channel else "Announcement posts off.")

    @celebrations.command(name="role")
    async def celebrations_role(self, ctx: commands.Context, role: discord.Role):
        """Use an existing role as the birthday role (made hoisted if it isn't)."""
        await self.config.guild(ctx.guild).role_id.set(role.id)
        note = ""
        if not role.hoist:
            try:
                await role.edit(hoist=True, reason="Birthday role must display separately")
            except _DISCORD_ERRORS:
                note = " ⚠️ I couldn't make it hoisted; tick *Display role members separately* on it."
        await ctx.send(f"Birthday role: {role.mention}.{note}" + self._position_note(ctx.guild, role),
                       allowed_mentions=discord.AllowedMentions.none())

    @celebrations.command(name="makerole")
    async def celebrations_makerole(self, ctx: commands.Context):
        """Create a hoisted 🎂 Birthday role and use it."""
        try:
            role = await ctx.guild.create_role(
                name="\N{BIRTHDAY CAKE} Birthday", colour=discord.Colour(COLOR_BIRTHDAY), hoist=True,
                mentionable=False, reason="Celebrations birthday role",
            )
        except _DISCORD_ERRORS:
            await ctx.send("I couldn't create the role (I need Manage Roles).")
            return
        try:  # as high as the bot can put it, so birthday members are listed in their own group
            await role.edit(position=max(1, ctx.guild.me.top_role.position - 1))
        except _DISCORD_ERRORS:
            pass
        await self.config.guild(ctx.guild).role_id.set(role.id)
        await ctx.send(f"Created {role.mention} and set it as the birthday role." + self._position_note(ctx.guild, role),
                       allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _position_note(guild, role) -> str:
        higher = [r for r in guild.roles if r.hoist and r > role and r != guild.me.top_role and not r.managed]
        if not higher:
            return ""
        names = ", ".join(r.name for r in sorted(higher, reverse=True)[:5])
        return (f"\nNote: members with a higher hoisted role ({names}) will still be listed under that role. "
                "Drag the birthday role above them in Server Settings → Roles if you want everyone shown separately.")

    @celebrations.command(name="gift")
    async def celebrations_gift(self, ctx: commands.Context, minimum: int, maximum: int = None):
        """Birthday gift range. `.celebrations gift 20000 30000`, `.celebrations gift 25000` (fixed), `.celebrations gift 0` (off)."""
        maximum = minimum if maximum is None else maximum
        if minimum < 0 or maximum < minimum:
            await ctx.send("Use `.celebrations gift <min> [max]` with min ≤ max.")
            return
        gconf = self.config.guild(ctx.guild)
        await gconf.gift_min.set(minimum)
        await gconf.gift_max.set(maximum)
        await ctx.send("Birthday gift off." if maximum == 0 else f"Birthday gift: {minimum:,}–{maximum:,}.")

    @celebrations.command(name="giftmindays")
    async def celebrations_giftmindays(self, ctx: commands.Context, days: int):
        """No birthday gift for members who joined less than N days ago (anti-farming, default 14)."""
        await self.config.guild(ctx.guild).gift_min_member_days.set(max(0, days))
        await ctx.send(f"Gift needs at least {max(0, days)} days in the server.")

    @celebrations.command(name="hour")
    async def celebrations_hour(self, ctx: commands.Context, hour: int):
        """Pacific hour (0–23) the posts go out. Default 9 (= 5pm UK / 6pm CET)."""
        if not 0 <= hour <= 23:
            await ctx.send("Pick an hour from 0 to 23.")
            return
        await self.config.guild(ctx.guild).announce_hour.set(hour)
        await ctx.send(f"Posts go out at {hour}:00 Pacific.")

    @celebrations.command(name="star")
    async def celebrations_star(self, ctx: commands.Context, emoji: str):
        """Reaction added to the chat post (match your starboard's emoji). `off` to disable."""
        value = "" if emoji.lower() == "off" else emoji
        await self.config.guild(ctx.guild).star_emoji.set(value)
        await ctx.send("Star reaction off." if not value else f"Posts get a {value} reaction.")

    @celebrations.command(name="anniversaries")
    async def celebrations_anniversaries(self, ctx: commands.Context, state: bool):
        """Turn the daily join-anniversary post on or off."""
        await self.config.guild(ctx.guild).anniversaries.set(state)
        await ctx.send(f"Anniversary posts {'on' if state else 'off'}.")

    @celebrations.command(name="anniversarygift")
    async def celebrations_anniversarygift(self, ctx: commands.Context, amount: int):
        """Wondercoins each member gets on their join anniversary (0 = off, the default)."""
        await self.config.guild(ctx.guild).anniversary_gift.set(max(0, amount))
        await ctx.send("Anniversary gift off." if amount <= 0 else f"Anniversary gift: {amount:,}.")

    @celebrations.command(name="setfor")
    async def celebrations_setfor(self, ctx: commands.Context, member: discord.Member, *, date: str):
        """Set or fix a member's birthday."""
        await self._save_birthday(ctx, member, date)

    @celebrations.command(name="clearfor")
    async def celebrations_clearfor(self, ctx: commands.Context, member: discord.Member):
        """Remove a member's birthday."""
        mconf = self.config.member(member)
        await mconf.month.clear()
        await mconf.day.clear()
        await mconf.year.clear()
        await ctx.send(f"Removed {member.display_name}'s birthday.")

    @celebrations.command(name="preview")
    async def celebrations_preview(self, ctx: commands.Context, member: discord.Member = None):
        """Show what a birthday post looks like, here (no role, no gift)."""
        member = member or ctx.author
        s = await self.config.guild(ctx.guild).all()
        mc = await self.config.member(member).all()
        today = engine.local_today(time.time())
        age = engine.turning_age(mc["year"], mc["month"] or 1, mc["day"] or 1, today) if mc["year"] else None
        await ctx.send(
            content="*(preview)*",
            embed=embeds.birthday_embed(member, age=age, gift=s["gift_max"], currency=await self._currency(ctx.guild),
                                        star=s["star_emoji"]),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @celebrations.command(name="run")
    async def celebrations_run(self, ctx: commands.Context):
        """Run the birthday/anniversary check right now instead of waiting up to 10 minutes."""
        await self._tick(ctx.guild)
        await ctx.send("Checked. \N{WHITE HEAVY CHECK MARK}")
