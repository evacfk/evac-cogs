"""Wonderween 2026: the Halloween costume contest.

- Members press Enter on the announcement; the bot DMs them. They send 1 or 2 photos and press Submit.
- Each entry lands in a private review channel with Approve / Reject buttons. Approved entries are posted as
  "Costume #N" in a temporary channel that only the mods can see until the open date.
- On the open date the channel becomes visible to everyone except the excluded roles (Lurker, 13-17, Muted).
  Members get one vote a day, never for their own costume and never for the same costume twice.
- People's Choice is decided by votes, Mods' Choice by mod picks (1st/2nd/3rd each). The reveal pays coins and gives
  the pumpkin roles; the purge deletes the channel, the review cards and every stored photo.
Entry sessions (before Submit) live in memory: a restart just means pressing Enter again. Everything after Submit is
stored in Config, and the scheduler catches up after a restart.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

import discord
from redbot.core import Config, bank, commands

from . import embeds, engine, images
from . import panel as panel_specs
from .constants import (
    BRONZE_COLOR, BRONZE_ROLE_NAME, CHANNEL_NAME, COG_VERSION, CONFIG_IDENTIFIER, CONTEST_NAME, DEFAULT_GUILD,
    GOLD_COLOR, GOLD_ROLE_NAME, IMAGE_EXTENSIONS, MAX_PHOTOS, MAX_TITLE_LEN, MIN_VOTE_LEVEL, REJECT_REASONS,
    SESSION_SECONDS, STAFF_ROLE_IDS, TICK_SECONDS,
)
from .settings_panel import PanelError, open_panel

log = logging.getLogger("red.evac-cogs.contest")
_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)
_NO_MENTIONS = discord.AllowedMentions.none()
TICK, CROSS = "\N{WHITE HEAVY CHECK MARK}", "\N{CROSS MARK}"


# ------------------------------------------------------------------------------------ views
# Every button is handled by Contest.on_interaction (custom ids), so they survive restarts.

def _button(label, custom_id, style, emoji=None):
    return discord.ui.Button(label=label, custom_id=custom_id, style=style, emoji=emoji)


def enter_view():
    view = discord.ui.View(timeout=None)
    view.add_item(_button("Enter the contest", "contest:enter", discord.ButtonStyle.success, "\N{JACK-O-LANTERN}"))
    return view


def vote_view(number: int):
    view = discord.ui.View(timeout=None)
    view.add_item(_button("Vote", f"contest:vote:{number}", discord.ButtonStyle.primary, "\N{JACK-O-LANTERN}"))
    return view


def queue_view(uid: int):
    view = discord.ui.View(timeout=None)
    view.add_item(_button("Approve", f"contest:ok:{uid}", discord.ButtonStyle.success))
    view.add_item(_button("Reject", f"contest:no:{uid}", discord.ButtonStyle.danger))
    return view


def reject_view(uid: int):
    view = discord.ui.View(timeout=None)
    options = [discord.SelectOption(label=label[:100], value=key) for key, label in REJECT_REASONS]
    view.add_item(discord.ui.Select(custom_id=f"contest:why:{uid}", placeholder="Why is it rejected?", options=options))
    return view


def dm_view(guild_id: int, can_submit: bool):
    view = discord.ui.View(timeout=None)
    if can_submit:
        view.add_item(_button("Submit my entry", f"contest:dm:submit:{guild_id}", discord.ButtonStyle.success))
    view.add_item(_button("Start over", f"contest:dm:reset:{guild_id}", discord.ButtonStyle.secondary))
    view.add_item(_button("Cancel", f"contest:dm:cancel:{guild_id}", discord.ButtonStyle.danger))
    return view


DM_INTRO = (
    "\N{JACK-O-LANTERN} **{name}: your entry**\n"
    "Send me **1 or 2 photos** of your costume in this chat (jpg, png or webp, "
    f"no more than {MAX_PHOTOS}). Your Discord **username and the word \"Wonderland\"** must be written on paper or on "
    "yourself and clearly visible in **at least one** photo, or the mods will reject it.\n"
    f"You can also type a short title for your costume (up to {MAX_TITLE_LEN} characters).\n"
    "Then press **Submit**. Only the mods see your photos until voting opens, and they are deleted after the contest."
)


class Contest(commands.Cog):
    """Wonderween 2026: the Halloween costume contest."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self._locks: dict[int, asyncio.Lock] = {}
        self._sessions: dict[int, dict] = {}     # user id -> DM entry in progress (memory only, see module docstring)
        self._task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        self._task.cancel()

    # ------------------------------------------------------------------------ small helpers

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def _now(self) -> float:
        return time.time()

    def _guild(self, guild_id: int):
        return next((g for g in self.bot.guilds if g.id == guild_id), None)

    @staticmethod
    def _sched(g: dict) -> dict:
        return {k: g[k] for k in engine.SCHEDULE_KEYS}

    def _data_dir(self, guild) -> Path:
        from redbot.core.data_manager import cog_data_path
        return cog_data_path(self) / "photos" / str(guild.id)

    @staticmethod
    def _role_ids(member) -> list[int]:
        return [r.id for r in getattr(member, "roles", [])]

    async def _is_staff(self, member) -> bool:
        if engine.has_any(self._role_ids(member), STAFF_ROLE_IDS):
            return True
        try:
            return bool(await self.bot.is_owner(member))
        except Exception:
            return False

    async def _is_owner(self, member) -> bool:
        try:
            return bool(await self.bot.is_owner(member))
        except Exception:
            return False

    async def _get_level(self, member):
        """Level via the LevelUp cog's own database. None = unreadable (voting then fails closed)."""
        cog = self.bot.get_cog("LevelUp")
        if cog is None:
            return None
        try:
            conf = cog.db.get_conf(member.guild)
            profile = conf.users.get(member.id)
            return int(profile.level) if profile is not None else 0
        except Exception:
            log.warning("contest: could not read LevelUp level for %s", member.id, exc_info=True)
            return None

    async def _lurker_role_id(self, guild):
        cog = self.bot.get_cog("Lurker")
        if cog is None:
            return None
        try:
            return await cog.config.guild(guild).lurker_role_id()
        except Exception:
            return None

    async def _alert(self, guild, text: str) -> None:
        """Tell the mods (review channel) and the bot owner. Best effort."""
        g = await self.config.guild(guild).all()
        channel = guild.get_channel(g["approval_channel_id"] or 0)
        if channel is not None:
            try:
                await channel.send(text, allowed_mentions=_NO_MENTIONS)
            except _DISCORD_ERRORS:
                log.warning("contest: could not post an alert in the review channel")
        send = getattr(self.bot, "send_to_owners", None)
        if send is not None:
            try:
                await send(text)
            except Exception:
                log.warning("contest: could not DM the owner", exc_info=True)

    async def _once(self, guild, key: str) -> bool:
        """True the first time `key` is seen for this guild (and remembers it)."""
        conf = self.config.guild(guild)
        async with conf.notified() as done:
            if key in done:
                return False
            done.append(key)
            return True

    @staticmethod
    def _approved(g: dict) -> dict[int, int]:
        return {e["number"]: e["user_id"] for e in g["entries"].values() if e["status"] == "approved"}

    @staticmethod
    def _mod_numbers(g: dict) -> list[int]:
        return [e["number"] for e in g["entries"].values() if e["status"] == "approved" and e.get("is_mod")]

    def _podium(self, g: dict) -> dict:
        approved = self._approved(g)
        counts = engine.tally(g["votes"], approved)
        pc = engine.places_from_counts(counts)
        eligible = engine.eligible_for_picks(approved, self._mod_numbers(g), pc)
        return {"approved": approved, "counts": counts, "pc": pc, "eligible": eligible}

    def _photo_files(self, guild, entry: dict, prefix: str = "entry"):
        files = []
        for i, name in enumerate(entry.get("photos") or []):
            path = self._data_dir(guild) / name
            if path.exists():
                files.append(discord.File(str(path), filename=f"{prefix}_{i + 1}.jpg"))
        return files

    def _drop_files(self, guild, entry: dict) -> None:
        for name in entry.get("photos") or []:
            try:
                (self._data_dir(guild) / name).unlink(missing_ok=True)
            except OSError:
                log.warning("contest: could not delete %s", name)

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        for guild in self.bot.guilds:
            async with self._lock(guild.id):
                conf = self.config.guild(guild)
                entries = await conf.entries()
                entry = entries.pop(str(user_id), None)
                if entry is not None:
                    self._drop_files(guild, entry)
                    await conf.entries.set(entries)
                votes = await conf.votes()
                votes.pop(str(user_id), None)
                if entry is not None and entry.get("number") is not None:
                    votes = engine.drop_entry_votes(votes, entry["number"])
                await conf.votes.set(votes)

    # ------------------------------------------------------------------------ scheduler

    async def _loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:
                    await self._tick(guild)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("contest: tick failed for guild %s", guild.id)
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self, guild, now: float | None = None) -> None:
        now = now if now is not None else self._now()
        conf = self.config.guild(guild)
        g = await conf.all()
        if not g["launched"] or g["purged"]:
            return
        s = self._sched(g)
        if not g["opened"] and now >= s["open_ts"]:
            try:
                await self.open_channel(guild, now)
            except PanelError as exc:
                if await self._once(guild, "open_failed"):
                    await self._alert(guild, f"{CROSS} The contest channel could not be opened automatically: {exc}")
            g = await conf.all()
        if g["status_date"] != engine.pt_date(now):
            await self._refresh_status(guild, now)
        if now >= s["voting_close_ts"] and not g["revealed"] and await self._once(guild, "voting_closed"):
            await self._alert(guild, f"\N{JACK-O-LANTERN} Voting is closed. Open `.contest` > Run > **Mods' Choice picks** "
                                     f"to choose the Mods' Choice podium. Results post {engine.fmt_pt(s['reveal_ts'])}.")
        if g["revealed"]:
            if now >= s["purge_ts"] and not g["hold_purge"]:
                await self.purge(guild)
            return
        pod = self._podium(g)
        complete = engine.picks_complete([p if p in pod["eligible"] else None for p in g["mods_picks"]], pod["eligible"])
        if now >= s["reveal_ts"] - 3600 and not complete and await self._once(guild, "picks_reminder"):
            await self._alert(guild, f"\N{WARNING SIGN} The Mods' Choice picks are not complete and results post "
                                     f"{engine.fmt_pt(s['reveal_ts'])}. Open `.contest` > Run > Mods' Choice picks.")
        if now >= s["reveal_ts"]:
            if complete:
                await self.reveal(guild)
            elif await self._once(guild, f"held:{int(now // 3600)}"):
                await self._alert(guild, "\N{WARNING SIGN} Results are being held until the Mods' Choice picks are complete. "
                                         "Finish them in `.contest` > Run, or press **Reveal now** to post with blank slots.")

    # ------------------------------------------------------------------------ interactions

    @commands.Cog.listener()
    async def on_interaction(self, interaction):
        data = getattr(interaction, "data", None)
        cid = data.get("custom_id") if isinstance(data, dict) else None
        if not cid or not str(cid).startswith("contest:"):
            return
        try:
            await self._route(interaction, str(cid))
        except Exception:
            log.exception("contest: button handler failed")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Something went wrong. Try again in a moment.", ephemeral=True)
            except Exception:
                pass

    async def _route(self, interaction, cid: str) -> None:
        parts = cid.split(":")
        kind = parts[1] if len(parts) > 1 else ""
        if kind == "enter":
            await self._on_enter(interaction)
            return
        try:
            if kind == "dm":
                action, ident = parts[2], int(parts[3])
            else:
                action, ident = "", int(parts[2])
        except (IndexError, ValueError):
            return   # not one of our ids
        if kind == "dm":
            await self._on_dm(interaction, action, ident)
        elif kind == "vote":
            await self._on_vote(interaction, ident)
        elif kind == "ok":
            await self._on_approve(interaction, ident)
        elif kind == "no":
            await self._on_reject_button(interaction, ident)
        elif kind == "why":
            await self._on_reject_reason(interaction, ident)

    # ---- entering

    async def _on_enter(self, interaction) -> None:
        guild, user = interaction.guild, interaction.user
        reply = interaction.response.send_message
        if guild is None:
            return
        g = await self.config.guild(guild).all()
        s, now = self._sched(g), self._now()
        if not g["launched"] or g["purged"]:
            await reply("The contest is not running.", ephemeral=True)
            return
        if not engine.entries_open(now, s, launched=True):
            await reply(f"Entries closed {engine.fmt_pt(s['entries_close_ts'])}. Thanks for the interest!", ephemeral=True)
            return
        if engine.is_excluded(self._role_ids(user), g["excluded_role_ids"]):
            await reply("Sorry, your account can't take part in this contest.", ephemeral=True)
            return
        entry = g["entries"].get(str(user.id))
        if entry and entry["status"] == "approved":
            await reply(f"You're already in as **Costume #{entry['number']}**. Ask the mods if something needs changing.",
                        ephemeral=True)
            return
        self._sessions[user.id] = {"guild_id": guild.id, "photos": [], "title": "", "expires": now + SESSION_SECONDS,
                                   "replace": bool(entry and entry["status"] == "pending")}
        try:
            await user.send(DM_INTRO.format(name=CONTEST_NAME) + (
                "\n\n*You already have an entry waiting for review. Submitting a new one replaces it.*"
                if self._sessions[user.id]["replace"] else ""), view=dm_view(guild.id, False))
        except _DISCORD_ERRORS:
            self._sessions.pop(user.id, None)
            await reply("I can't DM you. Open Server Privacy Settings, allow direct messages from server members, "
                        "then press **Enter the contest** again.", ephemeral=True)
            return
        await reply("\N{INCOMING ENVELOPE} Check your DMs from me to send your photos. Nobody else can see them.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is not None or message.author.bot:
            return
        sess = self._sessions.get(message.author.id)
        if sess is None:
            return
        if self._now() > sess["expires"]:
            self._sessions.pop(message.author.id, None)
            await message.channel.send("That entry timed out. Press **Enter the contest** in the server to start again.")
            return
        notes = []
        for att in message.attachments:
            name = (getattr(att, "filename", "") or "").lower()
            if not name.endswith(IMAGE_EXTENSIONS):
                notes.append(f"`{name or 'that file'}` isn't a jpg, png or webp photo.")
                continue
            if len(sess["photos"]) >= MAX_PHOTOS:
                notes.append(f"You already have {MAX_PHOTOS} photos. Press **Start over** to swap them.")
                break
            if getattr(att, "size", 0) and att.size > 25 * 1024 * 1024:
                notes.append(f"`{name}` is too large.")
                continue
            try:
                sess["photos"].append(images.process_photo(await att.read()))
            except images.PhotoError as exc:
                notes.append(str(exc))
            except _DISCORD_ERRORS:
                notes.append(f"I couldn't download `{name}`. Send it again.")
        text = (message.content or "").strip()
        if text:
            sess["title"] = engine.clip(text, MAX_TITLE_LEN)
        n = len(sess["photos"])
        lines = list(notes)
        if n:
            lines.append(f"Got **{n}** photo{'s' if n != 1 else ''}" + (f" and the title **{sess['title']}**" if sess["title"] else "")
                         + ". Send another (max 2), or press **Submit** when you're ready.")
        elif sess["title"]:
            lines.append(f"Title saved: **{sess['title']}**. Now send your photos.")
        elif not notes:
            lines.append("Send your photos here (1 or 2).")
        await message.channel.send("\n".join(lines), view=dm_view(sess["guild_id"], n > 0))

    async def _on_dm(self, interaction, action: str, guild_id: int) -> None:
        user = interaction.user
        sess = self._sessions.get(user.id)
        if sess is None or sess["guild_id"] != guild_id or self._now() > sess["expires"]:
            self._sessions.pop(user.id, None)
            await interaction.response.send_message("This entry expired. Press **Enter the contest** in the server again.",
                                                    ephemeral=True)
            return
        if action == "cancel":
            self._sessions.pop(user.id, None)
            await interaction.response.edit_message(content="Cancelled. Nothing was submitted.", view=None)
        elif action == "reset":
            sess["photos"], sess["title"] = [], ""
            await interaction.response.edit_message(content="Starting over. Send your photos (1 or 2).", view=dm_view(guild_id, False))
        elif action == "submit":
            await self._submit(interaction, sess)

    async def _submit(self, interaction, sess: dict) -> None:
        user, guild = interaction.user, self._guild(sess["guild_id"])
        reply = interaction.response.send_message
        if guild is None:
            await reply("I can't find the server right now. Try again later.", ephemeral=True)
            return
        member = guild.get_member(user.id)
        if member is None:
            await reply("You're not in the server any more.", ephemeral=True)
            return
        if not sess["photos"]:
            await reply("Send at least one photo first.", ephemeral=True)
            return
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            now = self._now()
            if not g["launched"] or not engine.entries_open(now, self._sched(g), launched=True):
                self._sessions.pop(user.id, None)
                await interaction.response.edit_message(content="Entries are closed.", view=None)
                return
            if engine.is_excluded(self._role_ids(member), g["excluded_role_ids"]):
                await reply("Sorry, your account can't take part in this contest.", ephemeral=True)
                return
            old = g["entries"].get(str(user.id))
            if old and old["status"] == "approved":
                self._sessions.pop(user.id, None)
                await interaction.response.edit_message(content=f"You're already in as Costume #{old['number']}.", view=None)
                return
            channel = guild.get_channel(g["approval_channel_id"] or 0)
            if channel is None:
                await reply("I can't reach the review channel. Tell a mod.", ephemeral=True)
                return
            if old:
                self._drop_files(guild, old)
            folder = self._data_dir(guild)
            folder.mkdir(parents=True, exist_ok=True)
            names = []
            for i, blob in enumerate(sess["photos"][:MAX_PHOTOS]):
                name = f"{user.id}_{int(now)}_{i + 1}.jpg"
                (folder / name).write_bytes(blob)
                names.append(name)
            entry = {"user_id": user.id, "status": "pending", "photos": names, "title": sess["title"],
                     "submitted_ts": now, "queue_msg_id": None, "number": None, "contest_msg_id": None,
                     "approved_ts": 0.0, "is_mod": await self._is_staff(member), "reason": ""}
            try:
                card = await channel.send(
                    embed=embeds.queue_embed(entry, member_name=member.display_name, is_mod=entry["is_mod"],
                                             resubmitted=bool(old)),
                    files=self._photo_files(guild, entry), view=queue_view(user.id), allowed_mentions=_NO_MENTIONS)
            except _DISCORD_ERRORS:
                log.warning("contest: could not post an entry in the review channel")
                self._drop_files(guild, entry)
                await reply("I couldn't send your entry for review. Tell a mod.", ephemeral=True)
                return
            entry["queue_msg_id"] = card.id
            if old and old.get("queue_msg_id"):
                await self._delete_message(channel, old["queue_msg_id"])
            async with conf.entries() as entries:
                entries[str(user.id)] = entry
        self._sessions.pop(user.id, None)
        await interaction.response.edit_message(
            content=(f"{TICK} **Submitted.** The mods will review it, usually within a day, and I'll DM you the result. "
                     f"Nobody but the mods can see it until {engine.fmt_pt(g['open_ts'])}."), view=None)

    @staticmethod
    async def _delete_message(channel, message_id) -> None:
        try:
            await channel.get_partial_message(message_id).delete()
        except _DISCORD_ERRORS:
            pass

    # ---- review

    async def _staff_only(self, interaction) -> bool:
        if interaction.guild is not None and await self._is_staff(interaction.user):
            return True
        await interaction.response.send_message("Only the mods can review entries.", ephemeral=True)
        return False

    async def _on_approve(self, interaction, uid: int) -> None:
        if not await self._staff_only(interaction):
            return
        guild, user = interaction.guild, interaction.user
        reply = interaction.response.send_message
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            entry = g["entries"].get(str(uid))
            if g["revealed"] or g["purged"]:
                await reply("The contest is over, so nothing can be approved now.", ephemeral=True)
                return
            if not entry or entry["status"] != "pending":
                await reply("That entry was already handled or has been replaced.", ephemeral=True)
                return
            if user.id == uid and not await self._is_owner(user):
                await reply("You can't approve your own entry. Ask another mod.", ephemeral=True)
                return
            channel = guild.get_channel(g["contest_channel_id"] or 0)
            if channel is None:
                await reply("The contest channel is missing. Open `.contest` and check the setup.", ephemeral=True)
                return
            number = engine.next_number(self._approved(g))
            try:
                msg = await channel.send(embeds.entry_caption(number, entry["title"]),
                                         files=self._photo_files(guild, entry, f"costume_{number}"),
                                         view=vote_view(number), allowed_mentions=_NO_MENTIONS)
            except _DISCORD_ERRORS:
                log.warning("contest: could not post Costume #%s", number)
                await reply("I couldn't post it in the contest channel. Check my permissions there.", ephemeral=True)
                return
            entry.update(status="approved", number=number, contest_msg_id=msg.id, approved_ts=self._now())
            async with conf.entries() as entries:
                entries[str(uid)] = entry
        member = guild.get_member(uid)
        who = member.display_name if member else str(uid)
        await interaction.response.edit_message(
            embed=embeds.queue_embed(entry, member_name=who, is_mod=entry.get("is_mod", False),
                                     status=f"{TICK} Approved as Costume #{number} by {user.display_name}"), view=None)
        await self._dm(member, f"{TICK} Your {CONTEST_NAME} entry was approved. You're **Costume #{number}**. "
                               f"It becomes visible to members on {engine.fmt_pt(g['open_ts'])}. Good luck!")
        await self._refresh_status(guild)

    async def _on_reject_button(self, interaction, uid: int) -> None:
        if not await self._staff_only(interaction):
            return
        entry = (await self.config.guild(interaction.guild).entries()).get(str(uid))
        if not entry or entry["status"] != "pending":
            await interaction.response.send_message("That entry was already handled or has been replaced.", ephemeral=True)
            return
        await interaction.response.send_message("Why is it rejected? The reason goes to the entrant.",
                                                view=reject_view(uid), ephemeral=True)

    async def _on_reject_reason(self, interaction, uid: int) -> None:
        if not await self._staff_only(interaction):
            return
        guild, user = interaction.guild, interaction.user
        values = (interaction.data or {}).get("values") or []
        key = values[0] if values else "other"
        reason = embeds.reason_text(key)
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            entry = g["entries"].get(str(uid))
            if not entry or entry["status"] != "pending":
                await interaction.response.edit_message(content="That entry was already handled or has been replaced.", view=None)
                return
            self._drop_files(guild, entry)
            entry.update(status="rejected", reason=reason, photos=[])
            async with conf.entries() as entries:
                entries[str(uid)] = entry
            card_channel = guild.get_channel(g["approval_channel_id"] or 0)
        member = guild.get_member(uid)
        if card_channel is not None and entry.get("queue_msg_id"):
            try:
                await card_channel.get_partial_message(entry["queue_msg_id"]).edit(
                    embed=embeds.queue_embed(entry, member_name=member.display_name if member else str(uid),
                                             is_mod=entry.get("is_mod", False),
                                             status=f"{CROSS} Rejected by {user.display_name}: {reason}"),
                    view=None, attachments=[])
            except _DISCORD_ERRORS:
                log.warning("contest: could not update the review card")
        await interaction.response.edit_message(content=f"Rejected: {reason}", view=None)
        await self._dm(member, f"{CROSS} Your {CONTEST_NAME} entry wasn't approved: **{reason}**.\n"
                               f"You can enter again any time before {engine.fmt_pt(g['entries_close_ts'])}: press "
                               f"**Enter the contest** in the server.")

    @staticmethod
    async def _dm(member, text: str) -> None:
        if member is None:
            return
        try:
            await member.send(text)
        except _DISCORD_ERRORS:
            log.info("contest: could not DM %s", getattr(member, "id", "?"))

    # ---- voting

    async def _on_vote(self, interaction, number: int) -> None:
        guild, user = interaction.guild, interaction.user
        reply = interaction.response.send_message
        if guild is None:
            return
        g = await self.config.guild(guild).all()
        s, now = self._sched(g), self._now()
        if not engine.voting_open(now, s, launched=g["launched"], purged=g["purged"]):
            if g["launched"] and now < s["open_ts"]:
                await reply(f"Voting opens {engine.fmt_pt(s['open_ts'])}.", ephemeral=True)
            else:
                await reply("Voting is closed. Results are coming soon!", ephemeral=True)
            return
        if engine.is_excluded(self._role_ids(user), g["excluded_role_ids"]):
            await reply("Sorry, your account can't vote in this contest.", ephemeral=True)
            return
        level = await self._get_level(user)
        if level is None:
            await reply("I couldn't check your level just now. Try again in a minute.", ephemeral=True)
            return
        if level < MIN_VOTE_LEVEL:
            await reply(f"You need to be level {MIN_VOTE_LEVEL} or higher to vote.", ephemeral=True)
            return
        today = engine.pt_date(now)
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            problem = engine.vote_problem(g["votes"], user.id, number, today, self._approved(g))
            if problem:
                await reply(problem, ephemeral=True)
                return
            votes = engine.cast_vote(g["votes"], user.id, number, today)
            await conf.votes.set(votes)
        total = len(engine.voting_days(s))
        await reply(f"\N{BALLOT BOX WITH BALLOT} Your vote for today is **Costume #{number}**. You can change it until midnight "
                    f"Pacific, but you can't vote for the same costume on another day. "
                    f"Days voted: {engine.days_voted(votes, user.id)} of {total}.", ephemeral=True)

    # ------------------------------------------------------------------------ channel and status

    async def open_channel(self, guild, now: float | None = None) -> str:
        """Make the contest channel visible to everyone except the excluded roles. Idempotent."""
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            channel = guild.get_channel(g["contest_channel_id"] or 0)
            if channel is None:
                raise PanelError("The contest channel is missing.")
            try:
                ow = channel.overwrites_for(guild.default_role)
                ow.view_channel, ow.read_message_history = True, True
                await channel.set_permissions(guild.default_role, overwrite=ow, reason=f"{CONTEST_NAME} opens")
                for rid in g["excluded_role_ids"]:
                    role = guild.get_role(rid)
                    if role is None:
                        continue
                    o = channel.overwrites_for(role)
                    o.view_channel = False
                    await channel.set_permissions(role, overwrite=o, reason=f"{CONTEST_NAME}: excluded role")
            except _DISCORD_ERRORS as exc:
                raise PanelError(f"Discord refused the permission change ({exc.__class__.__name__}). Give me Manage Channels and Manage Roles.")
            ok, report = self._visibility_report(guild, channel, g["excluded_role_ids"], expect_open=True)
            await conf.opened.set(ok)
        if ok:
            try:
                await channel.send("\N{JACK-O-LANTERN} **Voting is open!** Press **Vote** under the costumes you like. "
                                   "One vote a day, never your own, never the same costume twice.")
            except _DISCORD_ERRORS:
                pass
            await self._refresh_status(guild, now)
        await self._alert(guild, f"{TICK if ok else CROSS} **Contest channel visibility check**\n{report}")
        if not ok:
            raise PanelError(report)
        return report

    async def hide_channel(self, guild) -> str:
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            channel = guild.get_channel(g["contest_channel_id"] or 0)
            if channel is None:
                raise PanelError("The contest channel is missing.")
            ow = channel.overwrites_for(guild.default_role)
            ow.view_channel = False
            try:
                await channel.set_permissions(guild.default_role, overwrite=ow, reason=f"{CONTEST_NAME}: hidden again")
            except _DISCORD_ERRORS:
                raise PanelError("Discord refused the permission change.")
            await conf.opened.set(False)
        return "The contest channel is hidden again (mods only). It reopens automatically at the open time if that has passed."

    @staticmethod
    def _visibility_report(guild, channel, excluded_ids, *, expect_open: bool):
        lines, ok = [], True
        everyone = channel.overwrites_for(guild.default_role).view_channel
        good = (everyone is True) if expect_open else (everyone is not True)
        ok &= good
        lines.append(f"{TICK if good else CROSS} @everyone {'can' if everyone else 'cannot'} view the channel")
        for rid in excluded_ids:
            role = guild.get_role(rid)
            if role is None:
                continue
            blocked = channel.overwrites_for(role).view_channel is False
            ok &= blocked
            lines.append(f"{TICK if blocked else CROSS} {role.name} {'cannot' if blocked else 'CAN'} view it")
        chat = channel.overwrites_for(guild.default_role)
        quiet = chat.send_messages is False and chat.add_reactions is False
        ok &= quiet
        lines.append(f"{TICK if quiet else CROSS} chat and reactions are {'off' if quiet else 'ON'}")
        return ok, "\n".join(lines)

    async def _refresh_status(self, guild, now: float | None = None) -> None:
        now = now if now is not None else self._now()
        conf = self.config.guild(guild)
        g = await conf.all()
        channel = guild.get_channel(g["contest_channel_id"] or 0)
        if channel is None or g["purged"]:
            return
        phase = engine.phase(now, self._sched(g), launched=g["launched"], revealed=g["revealed"], purged=g["purged"])
        today = engine.pt_date(now)
        entries = [e for e in g["entries"].values() if e["status"] == "approved"]
        new_today = sum(1 for e in entries if engine.pt_date(e.get("approved_ts") or 0) == today)
        embed = embeds.status_embed(g, phase=phase, approved=len(entries), today_new=new_today, now=now)
        try:
            if g["status_message_id"]:
                await channel.get_partial_message(g["status_message_id"]).edit(embed=embed)
            else:
                msg = await channel.send(embed=embed)
                await conf.status_message_id.set(msg.id)
                try:
                    await msg.pin()
                except _DISCORD_ERRORS:
                    pass
        except _DISCORD_ERRORS:
            log.warning("contest: could not refresh the status message")
        await conf.status_date.set(today)

    # ------------------------------------------------------------------------ reveal and purge

    async def reveal(self, guild, now: float | None = None) -> str:
        """Count the votes, pay out, give the roles, post the results. Safe to run twice: the ledger prevents double pay."""
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            if g["revealed"]:
                return "The results were already revealed."
            if not g["launched"]:
                raise PanelError("The contest has not been launched.")
            pod = self._podium(g)
            picks = [p if p in pod["eligible"] else None for p in g["mods_picks"]]
            mc = engine.mc_places(picks)
            tiers = engine.assign_tiers(pod["approved"], pod["pc"], mc)
            coins = {k: g[f"coins_{k}"] for k in ("participant", "third", "second", "gold")}
            ledger, paid = list(g["ledger"]), {row["user_id"] for row in g["ledger"]}
            roles = {"bronze": guild.get_role(g["bronze_role_id"] or 0), "gold": guild.get_role(g["gold_role_id"] or 0)}
            gold_winners, problems = [], []
            for uid, info in sorted(tiers.items()):
                if uid in paid:
                    continue
                amount = engine.coins_for(info["tier"], coins)
                member = guild.get_member(uid)
                row = {"user_id": uid, "tier": info["tier"], "coins": amount, "ts": self._now(), "skipped": ""}
                if member is None:
                    row["skipped"] = "left the server"
                else:
                    try:
                        await bank.deposit_credits(member, amount)
                    except Exception:
                        log.exception("contest: payout failed for %s", uid)
                        row["skipped"] = "payout failed"
                        problems.append(f"<@{uid}> ({amount} coins)")
                    role = roles[engine.role_kind(info["tier"])]
                    if role is not None:
                        try:
                            await member.add_roles(role, reason=f"{CONTEST_NAME}")
                        except _DISCORD_ERRORS:
                            problems.append(f"<@{uid}> (role {role.name})")
                    if info["tier"] == 1 and not row["skipped"]:
                        gold_winners.append(uid)
                ledger.append(row)
                await conf.ledger.set(ledger)
            await conf.revealed.set(True)
            channel = guild.get_channel(g["contest_channel_id"] or 0)
            owners = pod["approved"]
            entries = {e["number"]: e for e in g["entries"].values() if e["status"] == "approved"}
            urls = {n: f"https://discord.com/channels/{guild.id}/{channel.id}/{e['contest_msg_id']}"
                    for n, e in entries.items() if channel is not None and e.get("contest_msg_id")}
            winners = {**pod["pc"], **mc}
            mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
            if channel is not None:
                try:
                    await channel.send(embed=embeds.results_embed(pod["pc"], mc, owners, {n: urls.get(n) for n in winners},
                                                                  entrants=len(owners), shop_item=g["shop_item"]),
                                       allowed_mentions=mentions)
                except _DISCORD_ERRORS:
                    log.warning("contest: could not post the results in the contest channel")
            results = guild.get_channel(g["results_channel_id"] or 0)
            if results is not None:
                try:
                    await results.send(
                        f"\N{JACK-O-LANTERN} **{CONTEST_NAME}: the results**\n\n"
                        + embeds.results_lines(pod["pc"], mc, owners, {}, mention=True)
                        + f"\n\nThank you to all {len(owners)} entrants. Coins and pumpkin roles have been handed out!",
                        allowed_mentions=mentions)
                except _DISCORD_ERRORS:
                    log.warning("contest: could not post the results in the results channel")
        await self._refresh_status(guild, now)
        note = [f"{TICK} Results posted and prizes paid to {len(tiers)} entrant(s)."]
        if gold_winners:
            note.append(f"\N{TROPHY} Deliver **{g['shop_item']}** to: " + ", ".join(f"<@{u}>" for u in gold_winners))
        if problems:
            note.append(f"{CROSS} Check these by hand: " + ", ".join(problems))
        await self._alert(guild, "\n".join(note))
        return "\n".join(note)

    def purge_preview(self, g: dict) -> str:
        entries = list(g["entries"].values())
        photos = sum(len(e.get("photos") or []) for e in entries)
        cards = sum(1 for e in entries if e.get("queue_msg_id"))
        return (f"Will delete: the contest channel <#{g['contest_channel_id']}>, {cards} review card(s), {photos} stored photo(s), "
                f"{len(entries)} entry record(s) and {len(g['votes'])} voter record(s). "
                f"Kept: the payout ledger ({len(g['ledger'])} row(s)).")

    async def purge(self, guild, *, force: bool = False) -> str:
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            if g["purged"]:
                return "Already purged."
            if not g["launched"]:
                raise PanelError("The contest has not been launched.")
            if not g["revealed"] and not force:
                raise PanelError("Results have not been revealed yet.")
            card_channel = guild.get_channel(g["approval_channel_id"] or 0)
            for e in g["entries"].values():
                if card_channel is not None and e.get("queue_msg_id"):
                    await self._delete_message(card_channel, e["queue_msg_id"])
            channel = guild.get_channel(g["contest_channel_id"] or 0)
            if channel is not None:
                try:
                    await channel.delete(reason=f"{CONTEST_NAME} is over")
                except _DISCORD_ERRORS:
                    log.warning("contest: could not delete the contest channel")
            announce = guild.get_channel(g["announce_channel_id"] or 0)
            if announce is not None and g["announce_message_id"]:
                try:
                    await announce.get_partial_message(g["announce_message_id"]).edit(
                        content=f"{CONTEST_NAME} has ended. Thank you to everyone who took part!", embed=None, view=None)
                except _DISCORD_ERRORS:
                    pass
            shutil.rmtree(self._data_dir(guild), ignore_errors=True)
            await conf.entries.set({})
            await conf.votes.set({})
            await conf.contest_channel_id.set(None)
            await conf.status_message_id.set(None)
            await conf.purged.set(True)
        self._sessions.clear()
        await self._alert(guild, f"{TICK} {CONTEST_NAME} purged: channel, review cards, photos and votes are gone.")
        return "Purged."

    async def remove_entry(self, guild, number: int) -> str:
        async with self._lock(guild.id):
            conf = self.config.guild(guild)
            g = await conf.all()
            entry = next((e for e in g["entries"].values() if e["status"] == "approved" and e["number"] == number), None)
            if entry is None:
                raise PanelError(f"There is no approved Costume #{number}.")
            channel = guild.get_channel(g["contest_channel_id"] or 0)
            if channel is not None and entry.get("contest_msg_id"):
                await self._delete_message(channel, entry["contest_msg_id"])
            card_channel = guild.get_channel(g["approval_channel_id"] or 0)
            if card_channel is not None and entry.get("queue_msg_id"):
                try:
                    await card_channel.get_partial_message(entry["queue_msg_id"]).edit(
                        embed=embeds.queue_embed(entry, member_name=str(entry["user_id"]),
                                                 status=f"{CROSS} Removed from the contest"), view=None, attachments=[])
                except _DISCORD_ERRORS:
                    pass
            self._drop_files(guild, entry)
            entry.update(status="removed", photos=[])
            async with conf.entries() as entries:
                entries[str(entry["user_id"])] = entry
            await conf.votes.set(engine.drop_entry_votes(g["votes"], number))
            picks = [None if p == number else p for p in g["mods_picks"]]
            await conf.mods_picks.set(picks)
        await self._refresh_status(guild)
        return f"Costume #{number} was removed, along with the votes for it."

    # ------------------------------------------------------------------------ panel callbacks

    def _setup_problems(self, guild, g: dict, lurker_role_id) -> list[str]:
        problems = []
        for key, label in (("approval_channel_id", "the review channel"), ("announce_channel_id", "the announcement channel"),
                           ("results_channel_id", "the results channel")):
            if guild.get_channel(g[key] or 0) is None:
                problems.append(f"Pick {label}.")
        excluded = set(g["excluded_role_ids"]) | ({lurker_role_id} if lurker_role_id else set())
        if not excluded:
            problems.append("Pick the excluded roles (Lurker, 13-17, Muted).")
        c = [g["coins_participant"], g["coins_third"], g["coins_second"], g["coins_gold"]]
        if min(c) < 1 or not (c[0] <= c[1] <= c[2] <= c[3]):
            problems.append("Set the coin amounts (each tier at least 1, gold >= 2nd >= 3rd >= participant).")
        if not g["shop_item"].strip():
            problems.append("Name the shop item for the gold winners.")
        bad = engine.validate_schedule(self._sched(g))
        if bad:
            problems.append(bad)
        if self._now() >= g["entries_close_ts"]:
            problems.append("The entry deadline has already passed. Fix the schedule.")
        return problems

    async def _check_setup(self, guild) -> str:
        g = await self.config.guild(guild).all()
        me = guild.me
        perms = getattr(me, "guild_permissions", None)
        lines = []

        def mark(ok, text):
            lines.append(f"{TICK if ok else CROSS} {text}")

        lurker = await self._lurker_role_id(guild)
        problems = self._setup_problems(guild, g, lurker)
        mark(not problems, "Launch settings are complete" if not problems else "Missing: " + " ".join(problems))
        mark(bool(perms and perms.manage_channels), "I can manage channels")
        mark(bool(perms and perms.manage_roles), "I can manage roles")
        for key, name in (("bronze_role_id", "Bronze Pumpkin role"), ("gold_role_id", "Golden Pumpkin role")):
            role = guild.get_role(g[key] or 0)
            if role is None:
                lines.append(f"\N{INFORMATION SOURCE} {name}: will be created at launch")
            else:
                mark(me.top_role > role, f"{name} sits below my top role")
        approval = guild.get_channel(g["approval_channel_id"] or 0)
        if approval is not None:
            private = not getattr(approval.permissions_for(guild.default_role), "view_channel", False)
            mark(private, "Review channel is private (photos land there)")
        mark(self.bot.get_cog("LevelUp") is not None, "LevelUp is loaded (needed to check level 1 for voting)")
        mark(lurker is not None, "Lurker role found via the Lurker cog (added to the excluded roles at launch)")
        try:
            mark(bool(await bank.get_currency_name(guild)), "Economy is reachable for coin payouts")
        except Exception:
            mark(False, "Economy is reachable for coin payouts")
        mark(True, "Photo handling (Pillow) is available" if self._has_pillow() else "Pillow is missing")
        return "\n".join(lines)

    @staticmethod
    def _has_pillow() -> bool:
        try:
            import PIL  # noqa: F401
            return True
        except ImportError:
            return False

    async def _apply_launch(self, guild, member, _value) -> str:
        conf = self.config.guild(guild)
        async with self._lock(guild.id):
            g = await conf.all()
            if g["launched"]:
                return "The contest is already launched."
            lurker = await self._lurker_role_id(guild)
            problems = self._setup_problems(guild, g, lurker)
            if problems:
                raise PanelError("Not ready to launch:\n" + "\n".join(f"- {p}" for p in problems))
            excluded = list(dict.fromkeys([*g["excluded_role_ids"], *([lurker] if lurker else [])]))
            await conf.excluded_role_ids.set(excluded)
            # roles first: each id is saved as soon as it exists, so a retry never creates duplicates
            for key, name, color in (("bronze_role_id", BRONZE_ROLE_NAME, BRONZE_COLOR), ("gold_role_id", GOLD_ROLE_NAME, GOLD_COLOR)):
                if guild.get_role(g[key] or 0) is None:
                    role = await self._create_role(guild, name, color)
                    await getattr(conf, key).set(role.id)
            announce = guild.get_channel(g["announce_channel_id"] or 0)
            channel = guild.get_channel(g["contest_channel_id"] or 0)
            if channel is None:
                category = guild.get_channel(g["category_id"] or 0) or getattr(announce, "category", None)
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=False, send_messages=False, add_reactions=False, create_public_threads=False,
                        create_private_threads=False, send_messages_in_threads=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                          embed_links=True, read_message_history=True, manage_messages=True),
                }
                for rid in STAFF_ROLE_IDS:
                    role = guild.get_role(rid)
                    if role is not None:
                        overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                                       send_messages=False)
                try:
                    channel = await guild.create_text_channel(
                        CHANNEL_NAME, category=category, overwrites=overwrites,
                        topic=f"{CONTEST_NAME}: vote for your favourite costume. Chat is off.",
                        reason=f"{CONTEST_NAME} launch")
                except _DISCORD_ERRORS:
                    raise PanelError("I couldn't create the contest channel. Give me Manage Channels.")
                await conf.contest_channel_id.set(channel.id)
            g = await conf.all()
            try:
                msg = await announce.send(embed=embeds.announcement_embed(g), view=enter_view())
            except _DISCORD_ERRORS:
                raise PanelError(f"I couldn't post in {announce.mention}. Check my permissions there.")
            await conf.announce_message_id.set(msg.id)
            await conf.launched.set(True)
        await self._refresh_status(guild)
        await self._alert(guild, f"\N{JACK-O-LANTERN} **{CONTEST_NAME} is live.** Entries will appear here with Approve / Reject "
                                 f"buttons. The contest channel {channel.mention} is visible to mods only until "
                                 f"{engine.fmt_pt(g['open_ts'])}.")
        return (f"Launched. The announcement is in {announce.mention}, the hidden contest channel is {channel.mention}, "
                f"and entries will arrive in the review channel.")

    async def _create_role(self, guild, name: str, color: int):
        try:
            return await guild.create_role(name=name, colour=discord.Colour(color), hoist=False, mentionable=False,
                                           display_icon=images.pumpkin_icon(color), reason=f"{CONTEST_NAME} prize role")
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            pass   # icons need a boost level the server may not have: fall back to a plain coloured role
        try:
            return await guild.create_role(name=name, colour=discord.Colour(color), hoist=False, mentionable=False,
                                           reason=f"{CONTEST_NAME} prize role")
        except _DISCORD_ERRORS:
            raise PanelError("I couldn't create the pumpkin roles. Give me Manage Roles.")

    # ---- panel setters (each returns the message shown in the panel)

    def _check_private(self, guild, channel):
        """Return the real channel if @everyone cannot see it. The picker hands over a light channel stub
        (id, name, mention only), so resolve it first; refuse when it can't be resolved (fail closed)."""
        real = guild.get_channel(channel.id)
        if real is None or not hasattr(real, "permissions_for"):
            raise PanelError(f"I can't see {channel.mention} to check its permissions. Give me View Channel there and try again.")
        if getattr(real.permissions_for(guild.default_role), "view_channel", False):
            raise PanelError(f"{channel.mention} can be seen by @everyone. Entry photos land there: pick a private channel.")
        return real

    async def _apply_approval_channel(self, guild, member, channel) -> str:
        if channel is None:
            raise PanelError("The review channel is required.")
        self._check_private(guild, channel)
        await self.config.guild(guild).approval_channel_id.set(channel.id)
        return f"Entries wait for review in {channel.mention}. Photos stay there until the purge."

    async def _apply_announce_channel(self, guild, member, channel) -> str:
        if channel is None:
            raise PanelError("The announcement channel is required.")
        await self.config.guild(guild).announce_channel_id.set(channel.id)
        return f"The announcement and Enter button go in {channel.mention}."

    async def _apply_results_channel(self, guild, member, channel) -> str:
        if channel is None:
            raise PanelError("The results channel is required.")
        await self.config.guild(guild).results_channel_id.set(channel.id)
        return f"The winners post (text only, no photos) goes in {channel.mention}."

    async def _apply_category(self, guild, member, category) -> str:
        await self.config.guild(guild).category_id.set(category.id if category else None)
        return f"The contest channel will be created in {category.name}." if category else \
            "The contest channel will be created next to the announcement channel."

    async def _apply_excluded(self, guild, member, roles) -> str:
        ids = [getattr(r, "id", r) for r in (roles or [])]
        await self.config.guild(guild).excluded_role_ids.set(ids)
        return ("Excluded from the channel, entering and voting: " + ", ".join(f"<@&{i}>" for i in ids)) if ids else \
            "No excluded roles set (the Lurker role is still added at launch)."

    async def _apply_role(self, guild, member, role, key: str, label: str) -> str:
        await getattr(self.config.guild(guild), key).set(role.id if role else None)
        return f"{label}: {role.mention}." if role else f"{label} will be created at launch."

    async def _apply_coins(self, guild, member, values) -> str:
        try:
            amounts = {k: int(str(values[k]).replace(",", "").strip()) for k in ("participant", "third", "second", "gold")}
        except (KeyError, ValueError):
            raise PanelError("Coin amounts must be whole numbers.")
        if min(amounts.values()) < 1:
            raise PanelError("Every amount must be at least 1.")
        if not (amounts["participant"] <= amounts["third"] <= amounts["second"] <= amounts["gold"]):
            raise PanelError("Amounts should not go down the podium: gold >= 2nd >= 3rd >= participant.")
        conf = self.config.guild(guild)
        for k, v in amounts.items():
            await getattr(conf, f"coins_{k}").set(v)
        return (f"Gold {amounts['gold']:,} · 2nd {amounts['second']:,} · 3rd {amounts['third']:,} · "
                f"participant {amounts['participant']:,}. Prizes are paid at the reveal; they show in the announcement.")

    async def _apply_shop(self, guild, member, values) -> str:
        item = engine.clip(values.get("item", ""), 80)
        if not item:
            raise PanelError("Name the shop item.")
        await self.config.guild(guild).shop_item.set(item)
        return f"Gold winners get: {item}. I post the delivery list at the reveal."

    async def _apply_schedule(self, guild, member, values) -> str:
        conf = self.config.guild(guild)
        g = await conf.all()
        if g["launched"] and g["revealed"]:
            raise PanelError("The contest is over; the schedule is locked.")
        new = self._sched(g)
        for key in engine.SCHEDULE_KEYS:
            text = (values.get(key) or "").strip()
            if text:
                try:
                    new[key] = engine.parse_pt(text)
                except ValueError as exc:
                    raise PanelError(f"{engine.SCHEDULE_LABELS[key]}: {exc}")
        bad = engine.validate_schedule(new)
        if bad:
            raise PanelError(bad)
        for key, value in new.items():
            await getattr(conf, key).set(value)
        await self._refresh_status(guild)
        return "Saved. " + " · ".join(f"{engine.SCHEDULE_LABELS[k]}: {engine.fmt_pt(new[k])}" for k in engine.SCHEDULE_KEYS) \
            + ("\nThe announcement already posted still shows the old dates." if g["launched"] else "")

    async def _apply_picks(self, guild, member, values) -> str:
        conf = self.config.guild(guild)
        g = await conf.all()
        if g["revealed"]:
            raise PanelError("Results are already revealed.")
        try:
            picks = [engine.parse_pick(values.get(k, "")) for k in ("first", "second", "third")]
        except ValueError as exc:
            raise PanelError(str(exc))
        pod = self._podium(g)
        bad = engine.picks_problem(picks, pod["eligible"])
        if bad:
            raise PanelError(bad + f" Pickable now: {', '.join('#' + str(n) for n in pod['eligible']) or 'none'}.")
        await conf.mods_picks.set(picks)
        return "Mods' Choice: " + " · ".join(f"{engine.ordinal(i + 1)} {'#' + str(p) if p else '(blank)'}" for i, p in enumerate(picks))

    async def _apply_remove(self, guild, member, values) -> str:
        try:
            number = engine.parse_pick(values.get("number", ""))
        except ValueError as exc:
            raise PanelError(str(exc))
        if number is None:
            raise PanelError("Enter a costume number.")
        return await self.remove_entry(guild, number)

    async def _apply_open_now(self, guild, member, _value) -> str:
        g = await self.config.guild(guild).all()
        if not g["launched"]:
            raise PanelError("Launch the contest first.")
        await self.open_channel(guild)
        return "The contest channel is now visible to everyone except the excluded roles. Check the visibility report in the review channel."

    async def _apply_hide(self, guild, member, _value) -> str:
        return await self.hide_channel(guild)

    async def _apply_reveal(self, guild, member, _value) -> str:
        return await self.reveal(guild)

    async def _apply_purge_preview(self, guild, member, _value) -> str:
        return self.purge_preview(await self.config.guild(guild).all())

    async def _apply_purge(self, guild, member, _value) -> str:
        return await self.purge(guild, force=True)

    async def _apply_hold(self, guild, member, on) -> str:
        await self.config.guild(guild).hold_purge.set(bool(on))
        return "The automatic purge is on hold." if on else "The automatic purge will run at its scheduled time."

    async def _apply_check(self, guild, member, _value) -> str:
        return await self._check_setup(guild)

    async def _summary(self, guild) -> str:
        g = await self.config.guild(guild).all()
        now = self._now()
        phase = engine.phase(now, self._sched(g), launched=g["launched"], revealed=g["revealed"], purged=g["purged"])
        entries = list(g["entries"].values())
        by = lambda s: sum(1 for e in entries if e["status"] == s)  # noqa: E731
        pod = self._podium(g)
        lines = [f"**{CONTEST_NAME}** v{COG_VERSION} · phase: **{phase}**",
                 f"Approved {by('approved')} · waiting {by('pending')} · rejected {by('rejected')} · "
                 f"voters {len(g['votes'])} · votes cast {sum(len(v) for v in g['votes'].values())}"]
        if phase in ("picks", "voting"):
            lines.append("Mods' Choice pickable: " + (", ".join(f"#{n}" for n in pod["eligible"]) or "none yet"))
        lines += [f"{engine.SCHEDULE_LABELS[k]}: {engine.fmt_pt(g[k])}" for k in engine.SCHEDULE_KEYS]
        return "\n".join(lines)

    async def _apply_summary(self, guild, member, _value) -> str:
        return await self._summary(guild)

    # ------------------------------------------------------------------------ commands

    @commands.group(name="contest", invoke_without_command=True)
    @commands.guild_only()
    async def contest(self, ctx: commands.Context):
        """Wonderween 2026 costume contest. Admins: open the control panel. Mods: show the status."""
        if not await self._is_staff(ctx.author):
            await ctx.send("Only the mods can use this.")
            return
        perms = ctx.author.guild_permissions
        if perms.administrator or perms.manage_guild:
            await open_panel(ctx, self, panel_specs.build_specs(), title=CONTEST_NAME, intro=panel_specs.INTRO)
            return
        await ctx.send(await self._summary(ctx.guild), allowed_mentions=_NO_MENTIONS)

    @contest.command(name="version")
    async def contest_version(self, ctx: commands.Context):
        """Confirm which build of the contest cog is loaded."""
        await ctx.send(f"Contest v{COG_VERSION}")
