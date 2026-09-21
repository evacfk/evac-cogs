"""reacttowin -- new, modeled on cray-bounty/bounty-cogs' FTR (first-to-win).

First click wins, running win-streak/last-winner tracked per guild via the
shared stats module. No top-level `.reacttowin` command -- your currently
loaded `minigames` cog already owns that name (design doc command-safety
check), so this only exists nested under `.minigamehub`.
"""
import logging
import random

import discord
from redbot.core import bank
from redbot.core.errors import BalanceTooHigh

from .. import pacing, stats
from .base import register

log = logging.getLogger("red.minigamehub.reacttowin")


class _ClickView(discord.ui.View):
    def __init__(self, timeout: float):
        super().__init__(timeout=timeout)
        self.winner: discord.Member = None

    @discord.ui.button(label="CLICK HERE!", style=discord.ButtonStyle.green)
    async def click(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.winner is not None:
            await interaction.response.send_message("Someone already beat you to it!", ephemeral=True)
            return
        self.winner = interaction.user
        button.disabled = True
        button.label = f"Won by {interaction.user.display_name}"
        await interaction.response.edit_message(view=self)
        self.stop()


@register("reacttowin")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "reacttowin"
    try:
        view = _ClickView(timeout=game_conf["response_timeout"])
        message = await channel.send(game_conf["spawn_message"], view=view)
        await view.wait()

        if view.winner is None:
            try:
                await message.edit(content=f"{game_conf['spawn_message']}\n*Nobody clicked in time.*", view=None)
            except discord.HTTPException:
                pass
            return

        member = guild.get_member(view.winner.id) or view.winner
        streak = await stats.record_result(cog.config, member, "reacttowin", good=True)

        min_r, max_r = game_conf["reward_range"]
        base = random.randint(min_r, max_r)
        bonus = int(base * (min(streak, 10) * game_conf["streak_bonus_pct"] / 100))
        total_base = base + bonus
        actual, _ = await pacing.apply_pacing(cog.config, member, total_base)
        try:
            await bank.deposit_credits(member, actual)
        except BalanceTooHigh as e:
            bal = await bank.get_balance(member)
            new_bal = await bank.set_balance(member, e.max_balance)
            actual = new_bal - bal
        await pacing.record_payout(cog.config, member, actual)

        currency = await bank.get_currency_name(guild)
        streak_txt = f" (streak: {streak})" if streak > 1 else ""
        try:
            await message.edit(content=f"{member.mention} won {actual:,} {currency}!{streak_txt}", view=None)
        except discord.HTTPException:
            pass
    finally:
        cog.active_game.pop(guild.id, None)
