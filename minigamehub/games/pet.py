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

from .. import pacing, stats
from ..emoji_utils import emoji_matches, parse_emoji
from .base import register

log = logging.getLogger("red.minigamehub.pet")


@register("pet")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict, dry_run: bool = False) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "pet"
    try:
        prefix = "\U0001F9EA **[TEST]** " if dry_run else ""
        message = await channel.send(prefix + game_conf["spawn_message"])
        try:
            # Parse into a PartialEmoji and react with the object directly --
            # more robust than round-tripping through the raw config string,
            # which broke on custom emoji with stray copy-paste whitespace.
            await message.add_reaction(parse_emoji(game_conf["pet_reaction"]))
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
            if not emoji_matches(game_conf["pet_reaction"], reaction.emoji):
                continue
            async for user in reaction.users():
                if user.bot:
                    continue
                petters.append(user)
            break

        if not petters:
            try:
                await message.edit(content=f"{prefix}{game_conf['spawn_message']}\n\n*No one came to pet...* \U0001F97A")
            except discord.HTTPException:
                pass
            return

        min_r, max_r = game_conf["reward_range"]
        paid = []
        for user in petters:
            member = guild.get_member(user.id) or user
            base = random.randint(min_r, max_r)
            actual = await pacing.settle_reward(cog.config, member, base, dry_run=dry_run)
            if not dry_run:
                await stats.record_result(cog.config, member, "pet", good=True)
            paid.append((member, actual))

        currency = await bank.get_currency_name(guild)
        names = ", ".join(m.mention for m, _ in paid[:15])
        extra = f" (+{len(paid) - 15} more)" if len(paid) > 15 else ""
        total = sum(a for _, a in paid)
        note = " (test -- no currency actually paid)" if dry_run else ""
        goodbye = (
            f"{prefix}{game_conf['goodbye_message']}\n"
            f"-# {len(paid)} petter(s) got a total of {total:,} {currency}{note}: {names}{extra}"
        )
        try:
            await message.edit(content=goodbye)
        except discord.HTTPException:
            pass
    finally:
        cog.active_game.pop(guild.id, None)
