"""plant: a member-funded coin drop, started from the #wondercasino hub (gamble's "More..." dropdown).

A member puts in coins, 10% is burned and the rest is posted in #cuddle for others to claim (Grab: first
click takes it all; Rain: everyone who clicks within 30 seconds splits it). It moves coins between members
and burns some; it never creates any, so it skips the daily payout cap. Unclaimed drops refund the planter.
Every open drop is saved in Config, so a restart or reload refunds it instead of eating the coins.
"""
import asyncio
import logging
import time
from datetime import datetime

import discord

from .constants import RESET_TIMEZONE

try:  # absent under the dev test stub; tests inject a fake
    from redbot.core import bank
except ImportError:
    bank = None

log = logging.getLogger("red.minigamehub.plant")

BURN_PCT = 10
RAIN_SECONDS = 30
EXPIRE_SECONDS = 600
COOLDOWN_SECONDS = 600
CHANNEL_SLUG = "cuddle"


def parse_amount(text):
    t = str(text).strip().lower().replace(",", "").replace(" ", "")
    mult = 1000 if t.endswith("k") else 1_000_000 if t.endswith("m") else 1
    try:
        value = round(float(t.rstrip("km")) * mult)
    except (ValueError, OverflowError):
        return None
    return value if value > 0 else None


def parse_mode(text):
    t = str(text).strip().lower()
    return "rain" if t.startswith("r") else "grab" if t.startswith("g") or not t else None


def split_amount(total, n):
    share, extra = divmod(total, n)
    return [share + (1 if i < extra else 0) for i in range(n)]


def week_key(now=None):
    year, week, _ = (now or datetime.now(RESET_TIMEZONE)).isocalendar()
    return f"{year}-W{week:02d}"


def find_channel(guild):
    """The live #cuddle: names carry decoration ("🧸・cuddle"), so match the tail after a non-word character."""
    for ch in guild.text_channels:
        name = ch.name.lower().strip()
        head = name[:-len(CHANNEL_SLUG)]
        if name.endswith(CHANNEL_SLUG) and not (head[-1:].isalnum() or head[-1:] == "-"):
            return ch
    return None


async def _is_lurker(bot, member):
    cog = bot.get_cog("Lurker")
    try:
        role_id = await cog.config.guild(member.guild).lurker_role_id() if cog else None
    except Exception:
        return False
    return bool(role_id) and any(r.id == role_id for r in member.roles)


async def _bump(cog, guild, uid, drops=0, planted=0, burned=0):
    async with cog.config.guild(guild).plant() as p:
        if p["week"] != week_key():
            p["week"], p["week_planters"] = week_key(), {}
        p["drops"] += drops
        p["planted"] += planted
        p["burned"] += burned
        if planted:
            p["week_planters"][str(uid)] = max(0, p["week_planters"].get(str(uid), 0) + planted)


async def _refund(cog, guild, planter_id, amount):
    """Give coins back to the planter (they may have left the server: then they are simply lost)."""
    member = guild.get_member(planter_id)
    if member is None or amount <= 0:
        return
    try:
        await bank.deposit_credits(member, amount)
        await _bump(cog, guild, planter_id, planted=-amount)
    except Exception:
        log.exception("plant: refund of %s to %s failed", amount, planter_id)


async def _close(cog, guild, key):
    """Remove a drop's saved record. True only for the caller that removed it: that caller alone may pay or refund."""
    async with cog.config.guild(guild).plant() as p:
        return p["open"].pop(key, None) is not None


class _View(discord.ui.View):
    def __init__(self, drop):
        super().__init__(timeout=None)
        self.drop = drop
        self.claim.label = "Catch the rain!" if drop.mode == "rain" else "Grab it!"

    @discord.ui.button(emoji="\U0001F4B0", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.drop.on_click(interaction)


class Drop:
    def __init__(self, cog, guild, planter, amount, mode, currency):
        self.cog, self.guild, self.planter = cog, guild, planter
        self.amount, self.mode, self.currency = amount, mode, currency
        self.claimers = {}                 # member id -> member, in click order
        self.rain_open = mode == "rain"    # while True, clicks join the split instead of ending the drop
        self.done = False                  # set synchronously by whoever resolves the drop, so it resolves once
        self.message = None
        self.key = ""

    async def on_click(self, interaction):
        m = interaction.user
        refusal = ("This drop is already gone." if self.done else "You can't claim your own drop." if m.id == self.planter.id
                   else "Lurkers can't claim drops." if await _is_lurker(self.cog.bot, m) else None)
        if refusal or self.done:
            await interaction.response.send_message(refusal or "This drop is already gone.", ephemeral=True)
        elif self.rain_open:
            first = m.id not in self.claimers
            self.claimers[m.id] = m
            await interaction.response.send_message("You're in! The coins split when the rain ends." if first
                                                    else "You're already in the rain.", ephemeral=True)
        else:
            self.done = True
            await interaction.response.defer()
            await self.pay([m])

    async def run(self):
        try:
            await asyncio.sleep(RAIN_SECONDS if self.mode == "rain" else EXPIRE_SECONDS)
            if self.mode == "rain":
                self.rain_open = False     # a lone clicker takes everything; nobody means it stays open as a Grab
                if self.claimers and not self.done:
                    self.done = True
                    await self.pay(list(self.claimers.values()))
                    return
                await asyncio.sleep(EXPIRE_SECONDS - RAIN_SECONDS)
            if not self.done:
                self.done = True
                if await _close(self.cog, self.guild, self.key):
                    await _refund(self.cog, self.guild, self.planter.id, self.amount)
                    await self._say(f"\U0001F4A8 Nobody claimed {self.planter.mention}'s drop. "
                                    f"{self.amount:,} {self.currency} went back to them.")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("plant: drop %s crashed", self.key)

    async def pay(self, members):
        if not await _close(self.cog, self.guild, self.key):
            return
        paid, lost = [], 0
        for m, share in zip(members, split_amount(self.amount, len(members))):
            try:
                await bank.deposit_credits(m, share)
                paid.append(m)
            except Exception:
                log.exception("plant: deposit of %s to %s failed", share, m.id)
                lost += share
        await _refund(self.cog, self.guild, self.planter.id, lost)
        if paid:  # the daily quest counts only a drop someone else really claimed, so it can't be farmed by letting drops expire
            self.cog.bot.dispatch("wonderland_quest", self.planter, "plant", 1)
        who = ", ".join(m.mention for m in paid[:12]) + (f" (+{len(paid) - 12} more)" if len(paid) > 12 else "")
        if not paid:
            text = f"\U0001F4A8 {self.planter.mention}'s drop couldn't be paid out, so they got it back."
        elif self.mode == "rain" and len(paid) > 1:
            text = f"\U0001F327️ {self.planter.mention}'s {self.amount:,} {self.currency} rained on {who}."
        else:
            text = f"\U0001F4B0 {who} grabbed {self.planter.mention}'s {self.amount:,} {self.currency}!"
        await self._say(text)

    async def _say(self, text):
        try:
            await self.message.edit(content=text, view=None)
        except (discord.HTTPException, AttributeError):
            pass


async def start(cog, guild, planter, amount_text, mode_text):
    """Validate, take the coins, post the drop. Shared by the hub modal and `.plant`; returns the reply for the planter."""
    amount, mode = parse_amount(amount_text), parse_mode(mode_text)
    p = await cog.config.guild(guild).plant()
    if not p["enabled"]:
        return "Coin drops are off right now."
    if amount is None or mode is None:
        return "Put a number for the amount (like 5000 or 5k) and Grab or Rain for the mode."
    if not p["min"] <= amount <= p["max"]:
        return f"A drop has to be between {p['min']:,} and {p['max']:,}."
    channel = find_channel(guild)
    if channel is None:
        return "I can't find #cuddle right now. Let a mod know."
    async with cog._plant_lock:
        wait = cog._plant_last.get((guild.id, planter.id), 0) + COOLDOWN_SECONDS - time.time()
        if wait > 0:
            return f"You can drop again in {int(wait // 60) + 1} min."
        try:
            await bank.withdraw_credits(planter, amount)
        except ValueError:
            return "You don't have enough coins for that."
        burned = amount * BURN_PCT // 100
        currency = await bank.get_currency_name(guild)
        drop = Drop(cog, guild, planter, amount - burned, mode, currency)
        hint = f"Everyone who clicks in the next {RAIN_SECONDS} seconds splits it." if mode == "rain" else "First click takes it all."
        try:
            drop.message = await channel.send(
                f"\U0001F4B0 {planter.mention} dropped **{drop.amount:,}** {currency}! {hint} ({BURN_PCT}% burned)",
                view=_View(drop))
        except discord.HTTPException:
            await bank.deposit_credits(planter, amount)
            return "I couldn't post in #cuddle, so you got your coins back."
        cog._plant_last[(guild.id, planter.id)] = time.time()
        drop.key = str(drop.message.id)
        async with cog.config.guild(guild).plant() as p:
            p["open"][drop.key] = {"planter": planter.id, "amount": drop.amount, "channel": channel.id}
    await _bump(cog, guild, planter.id, drops=1, planted=amount, burned=burned)
    task = asyncio.create_task(drop.run())
    cog._plant_tasks.add(task)
    task.add_done_callback(cog._plant_tasks.discard)
    return f"Dropped {drop.amount:,} {currency} in {channel.mention} ({burned:,} burned)."


async def open_modal(cog, interaction):
    guild = interaction.guild
    p = await cog.config.guild(guild).plant() if guild else None
    if p is None or not p["enabled"]:
        await interaction.response.send_message("Coin drops are off right now.", ephemeral=True)
        return
    wait = cog._plant_last.get((guild.id, interaction.user.id), 0) + COOLDOWN_SECONDS - time.time()
    if wait > 0:
        await interaction.response.send_message(f"You can drop again in {int(wait // 60) + 1} min.", ephemeral=True)
        return
    await interaction.response.send_modal(PlantModal(cog, p["min"], p["max"]))


class PlantModal(discord.ui.Modal):
    def __init__(self, cog, lo, hi):
        super().__init__(title="Drop coins in #cuddle")
        self.cog = cog
        self.amount = discord.ui.TextInput(label=f"Amount ({lo:,} to {hi:,})", placeholder="5000 or 5k", max_length=12)
        self.mode = discord.ui.TextInput(label="Mode: Grab (first click) or Rain (shared)", default="Grab", max_length=5)
        self.add_item(self.amount)
        self.add_item(self.mode)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await start(self.cog, interaction.guild, interaction.user, self.amount.value, self.mode.value), ephemeral=True)


async def recover(cog):
    """After a restart or reload the open drops are dead: refund every saved one to its planter."""
    await cog.bot.wait_until_red_ready()
    for guild in cog.bot.guilds:
        async with cog.config.guild(guild).plant() as p:
            leftovers, p["open"] = dict(p["open"]), {}
        for key, rec in leftovers.items():
            await _refund(cog, guild, rec["planter"], rec["amount"])
            channel = guild.get_channel(rec["channel"])
            try:
                await channel.get_partial_message(int(key)).edit(content="\U0001F4A8 This drop ended early (bot restart). "
                                                                 "The coins went back to whoever dropped them.", view=None)
            except (discord.HTTPException, AttributeError):
                pass


async def stats_lines(cog, guild):
    p = await cog.config.guild(guild).plant()
    if not p["drops"]:
        return []
    lines = [f"- Coin drops: {p['drops']:,} posted, {p['planted']:,} planted, {p['burned']:,} burned"]
    top = sorted(p["week_planters"].items(), key=lambda kv: kv[1], reverse=True)[:5] if p["week"] == week_key() else []
    if top:
        names = [f"{i + 1}. {m.display_name if (m := guild.get_member(int(uid))) else uid} ({amt:,})" for i, (uid, amt) in enumerate(top)]
        lines.append("- Top planters this week: " + ", ".join(names))
    return lines
