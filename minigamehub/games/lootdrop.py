"""lootdrop -- ported from calamari/lootdrop (CalaMariGold/CalaMari-Cogs).

Design doc: the deepest game type, most worth preserving faithfully. All 54
seed scenarios (scenarios.SEED_LOOTDROP_SCENARIOS) plus the streak-bonus math
and party-drop sub-mode are ported as-is since they already worked well.
Scenario CRUD / JSON import-export live in minigamehub.py's admin commands,
not here -- this module is just the spawn/resolve logic.
"""
import logging
import random
import time

import discord
from redbot.core import bank

from .. import pacing, stats
from ..payout_format import format_payout_lines
from .base import register

log = logging.getLogger("red.minigamehub.lootdrop")


class _ClaimView(discord.ui.View):
    def __init__(self, cog, scenario: dict, timeout: float, dry_run: bool = False):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.scenario = scenario
        self.claimed = False
        self.dry_run = dry_run
        self.add_item(_ClaimButton(scenario))

    async def on_timeout(self):
        if not self.claimed and self.message:
            try:
                await self.message.edit(content=f"{self.scenario['start']}\n*The opportunity has passed...*", view=None)
            except discord.HTTPException:
                pass


class _ClaimButton(discord.ui.Button):
    def __init__(self, scenario: dict):
        super().__init__(style=discord.ButtonStyle.success, emoji=scenario["button_emoji"], label=scenario["button_text"])
        self.scenario = scenario

    async def callback(self, interaction: discord.Interaction):
        view: _ClaimView = self.view
        if view.claimed:
            await interaction.response.send_message("This drop has already been claimed!", ephemeral=True)
            return
        view.claimed = True
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(view=view)
        await _resolve_claim(
            view.cog, interaction, view.scenario,
            interaction.channel.guild.get_member(interaction.user.id) or interaction.user,
            dry_run=view.dry_run,
        )
        view.stop()


async def _resolve_claim(cog, interaction: discord.Interaction, scenario: dict, member: discord.Member, dry_run: bool = False) -> None:
    config = cog.config
    game_conf = (await config.guild(member.guild).games())["lootdrop"]
    currency = await bank.get_currency_name(member.guild)
    note = " (test -- no currency actually moved)" if dry_run else ""

    is_bad = random.randint(1, 100) <= game_conf["bad_outcome_chance"]
    base = random.randint(*game_conf["reward_range"])

    if dry_run:
        # Preview against the real streak without touching it -- no Config write.
        streak_data = await config.member(member).lootdrop_streak()
        now = time.time()
        hours_since = (now - streak_data["last_claim"]) / 3600 if streak_data["last_claim"] else 999
        current_streak = 0 if hours_since > game_conf["streak_timeout"] else streak_data["streak"]

        if is_bad:
            penalty = await pacing.settle_penalty(member, base, dry_run=True)
            message = scenario["bad"].format(user=member.mention, amount=f"{penalty:,}", currency=currency) + note
        else:
            streak = min(current_streak, game_conf["streak_max"])
            bonus = int(base * (streak * game_conf["streak_bonus"] / 100))
            actual = await pacing.settle_reward(config, member, base + bonus, dry_run=True)
            message = scenario["good"].format(user=member.mention, amount=f"{actual:,}", currency=currency) + note
            if bonus > 0:
                message += f"\n(Base: {base:,} + Streak Bonus: {bonus:,} [{streak}x])"
        try:
            await interaction.followup.send(message)
        except discord.HTTPException:
            pass
        return

    async with config.member(member).lootdrop_streak() as streak_data:
        now = time.time()
        hours_since = (now - streak_data["last_claim"]) / 3600 if streak_data["last_claim"] else 999
        if hours_since > game_conf["streak_timeout"]:
            streak_data["streak"] = 0
        streak_data["last_claim"] = now

        if is_bad:
            penalty = await pacing.settle_penalty(member, base, dry_run=False)
            streak_data["streak"] = 0
            message = scenario["bad"].format(user=member.mention, amount=f"{penalty:,}", currency=currency)
            await stats.record_result(config, member, "lootdrop", good=False)
        else:
            streak = min(streak_data["streak"], game_conf["streak_max"])
            bonus = int(base * (streak * game_conf["streak_bonus"] / 100))
            total_base = base + bonus
            actual = await pacing.settle_reward(config, member, total_base, dry_run=False)
            streak_data["streak"] += 1
            streak_data["highest_streak"] = max(streak_data["highest_streak"], streak_data["streak"])
            message = scenario["good"].format(user=member.mention, amount=f"{actual:,}", currency=currency)
            if bonus > 0:
                message += f"\n(Base: {base:,} + Streak Bonus: {bonus:,} [{streak}x])"
            await stats.record_result(config, member, "lootdrop", good=True)

    try:
        await interaction.followup.send(message)
    except discord.HTTPException:
        pass


class _PartyView(discord.ui.View):
    def __init__(self, timeout: float):
        super().__init__(timeout=timeout)
        self.claimed_users = {}  # user_id -> claim timestamp
        self.start_time = time.time()

    @discord.ui.button(label="Claim Party Drop!", emoji="\U0001F38A", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            return
        if interaction.user.id in self.claimed_users:
            await interaction.response.send_message("You've already claimed this party drop!", ephemeral=True)
            return
        self.claimed_users[interaction.user.id] = time.time()
        button.label = f"Join Party! ({len(self.claimed_users)} joined)"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("You've joined the party! Wait for rewards...", ephemeral=True)


async def _resolve_party(cog, message: discord.Message, view: _PartyView, guild: discord.Guild, game_conf: dict, dry_run: bool = False) -> None:
    if not view.claimed_users:
        try:
            await message.edit(content="No one joined the party... \U0001F622", view=None)
        except discord.HTTPException:
            pass
        return

    currency = await bank.get_currency_name(guild)
    min_c, max_c = game_conf["party_drop_min"], game_conf["party_drop_max"]
    timeout = game_conf["party_drop_timeout"]
    credit_range = max_c - min_c
    note = " (test)" if dry_run else ""

    results = []
    for user_id, claim_time in sorted(view.claimed_users.items(), key=lambda kv: kv[1]):
        member = guild.get_member(user_id)
        if not member:
            continue
        pct = ((claim_time - view.start_time) / timeout) * 100 if timeout else 100
        if pct <= 20:
            credits = max_c - int((pct / 20) * (credit_range * 0.2))
        elif pct <= 40:
            credits = int(max_c * 0.8) - int(((pct - 20) / 20) * (credit_range * 0.2))
        elif pct <= 60:
            credits = int(max_c * 0.6) - int(((pct - 40) / 20) * (credit_range * 0.2))
        elif pct <= 80:
            credits = int(max_c * 0.4) - int(((pct - 60) / 20) * (credit_range * 0.2))
        else:
            credits = min_c

        actual = await pacing.settle_reward(cog.config, member, credits, dry_run=dry_run)
        if not dry_run:
            await stats.record_result(cog.config, member, "lootdrop", good=True)
        results.append((member, actual))

    # Grouped by identical amount so a party of a dozen people who all
    # claimed near-simultaneously (and landed on the same payout tier)
    # reads as one line instead of one per person.
    lines = format_payout_lines(results, currency)
    try:
        await message.edit(
            content=f"\U0001F389 **Party Drop Results!**{note} \U0001F389\n" + "\n".join(lines),
            view=None,
        )
    except discord.HTTPException:
        pass


@register("lootdrop")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict, dry_run: bool = False) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "lootdrop"
    try:
        prefix = "\U0001F9EA **[TEST]** " if dry_run else ""
        is_party = random.randint(1, 100) <= game_conf["party_drop_chance"]
        if is_party:
            view = _PartyView(timeout=float(game_conf["party_drop_timeout"]))
            message = await channel.send(
                f"{prefix}\U0001F389 **PARTY DROP!** \U0001F389\n"
                f"Everyone who clicks the button in the next {game_conf['party_drop_timeout']} seconds gets a prize!",
                view=view,
            )
            await view.wait()
            await _resolve_party(cog, message, view, guild, game_conf, dry_run=dry_run)
            return

        scenarios = game_conf.get("scenarios") or []
        if not scenarios:
            return
        scenario = random.choice(scenarios)
        view = _ClaimView(cog, scenario, timeout=float(game_conf["claim_timeout"]), dry_run=dry_run)
        message = await channel.send(prefix + scenario["start"], view=view)
        view.message = message
        await view.wait()
    finally:
        cog.active_game.pop(guild.id, None)
