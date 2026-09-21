"""pet -- ported from vrt/pupper (vertyco/vrt-cogs), changed to multi-claim.

Design doc: commands preserved 1:1 in spirit (spawn message, reaction, and
goodbye message are all independently settable, same as pupper's hello/thanks
setters), but the mechanic itself changed from single-winner (first reactor
wins, vrt/pupper's original behavior) to multi-claim -- the prompt stays open
for `window_seconds` and *everyone* who reacted before it closes gets paid.
"""
import asyncio
import logging
import random

import discord
from redbot.core import bank
from redbot.core.errors import BalanceTooHigh

from .. import pacing, stats
from .base import register

log = logging.getLogger("red.minigamehub.pet")


@register("pet")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "pet"
    try:
        message = await channel.send(game_conf["spawn_message"])
        try:
            await message.add_reaction(game_conf["pet_reaction"])
        except discord.HTTPException:
            log.warning("Couldn't add pet_reaction %r in guild %s -- is it a valid emoji?", game_conf["pet_reaction"], guild.id)
            return

        await asyncio.sleep(game_conf["window_seconds"])

        try:
            message = await channel.fetch_message(message.id)
        except discord.NotFound:
            return

        petters = []
        for reaction in message.reactions:
            if str(reaction.emoji) != str(game_conf["pet_reaction"]):
                continue
            async for user in reaction.users():
                if user.bot:
                    continue
                petters.append(user)
            break

        if not petters:
            try:
                await message.edit(content=f"{game_conf['spawn_message']}\n\n*No one came to pet...* \U0001F97A")
            except discord.HTTPException:
                pass
            return

        min_r, max_r = game_conf["reward_range"]
        paid = []
        for user in petters:
            member = guild.get_member(user.id) or user
            base = random.randint(min_r, max_r)
            actual, _ = await pacing.apply_pacing(cog.config, member, base)
            try:
                await bank.deposit_credits(member, actual)
            except BalanceTooHigh as e:
                bal = await bank.get_balance(member)
                new_bal = await bank.set_balance(member, e.max_balance)
                actual = new_bal - bal
            await pacing.record_payout(cog.config, member, actual)
            await stats.record_result(cog.config, member, "pet", good=True)
            paid.append((member, actual))

        currency = await bank.get_currency_name(guild)
        names = ", ".join(m.mention for m, _ in paid[:15])
        extra = f" (+{len(paid) - 15} more)" if len(paid) > 15 else ""
        total = sum(a for _, a in paid)
        goodbye = (
            f"{game_conf['goodbye_message']}\n"
            f"-# {len(paid)} petter(s) got a total of {total:,} {currency}: {names}{extra}"
        )
        try:
            await message.edit(content=goodbye)
        except discord.HTTPException:
            pass
    finally:
        cog.active_game.pop(guild.id, None)
