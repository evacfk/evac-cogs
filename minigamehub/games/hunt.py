"""hunt -- ported from vrt/hunting (vertyco/vrt-cogs), with real fixes.

Design doc per-game-type notes:
1. Bug fix -- exact match, not substring. vrt/hunting's check was
   `"bang" in message.content.lower().strip()`, so "bangqiuwewuqneiuqe"
   counted as a valid shot. Fixed here to `== shoot_word` (and same for the
   safe word). Reactions were already exact (emoji equality), unchanged.
2. Custom animal pool, Config-editable, not birds-only.
3. Multiple safe animals, each independently toggleable.
4. Per-safe-animal penalty as a % of the shooter's balance, not a flat range.
5. Stays single-winner on purpose (locked decision #13) -- first valid
   bang/salute resolves the whole spawn.
"""
import asyncio
import logging
import random

import discord
from redbot.core import bank
from redbot.core.errors import BalanceTooHigh

from .. import pacing, stats
from .base import register

log = logging.getLogger("red.minigamehub.hunt")


@register("hunt")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "hunt"
    try:
        animals = game_conf["animals"]
        if not animals:
            return
        animal_key = random.choice(list(animals.keys()))
        animal = animals[animal_key]
        is_safe = animal_key in game_conf["safe_animals"]

        message = await channel.send(f"{animal['emoji']} {animal['text']}")
        try:
            await message.add_reaction(game_conf["shoot_reaction"])
            if is_safe:
                await message.add_reaction(game_conf["safe_reaction"])
        except discord.HTTPException:
            pass

        shoot_word = game_conf["shoot_word"].strip().lower()
        safe_word = game_conf["safe_word"].strip().lower()

        def msg_check(m: discord.Message):
            if m.channel.id != channel.id or m.author.bot or not m.content:
                return False
            content = m.content.strip().lower()
            return content == shoot_word or (is_safe and content == safe_word)

        def reaction_check(payload: discord.RawReactionActionEvent):
            if payload.channel_id != channel.id or payload.message_id != message.id:
                return False
            if payload.member is None or payload.member.bot:
                return False
            emoji = str(payload.emoji)
            return emoji == game_conf["shoot_reaction"] or (is_safe and emoji == game_conf["safe_reaction"])

        futures = [
            asyncio.ensure_future(cog.bot.wait_for("message", check=msg_check)),
            asyncio.ensure_future(cog.bot.wait_for("raw_reaction_add", check=reaction_check)),
        ]
        try:
            done, pending = await asyncio.wait(
                futures, return_when=asyncio.FIRST_COMPLETED, timeout=game_conf["response_timeout"]
            )
        finally:
            for f in futures:
                if not f.done():
                    f.cancel()

        if not done:
            escaped = f"The {animal_key} flew away!"
            try:
                await channel.send(escaped)
            except discord.HTTPException:
                pass
            return

        result = done.pop().result()
        if isinstance(result, discord.Message):
            author = result.author
            saluted = is_safe and result.content.strip().lower() == safe_word
        else:
            author = result.member
            saluted = is_safe and str(result.emoji) == game_conf["safe_reaction"]

        member = guild.get_member(author.id) or author
        currency = await bank.get_currency_name(guild)

        if is_safe and not saluted:
            # Shot a safe animal -- penalty as % of the shooter's current balance.
            penalty_pct = game_conf["safe_animals"][animal_key]["penalty_pct"]
            balance = await bank.get_balance(member)
            penalty = max(1, round(balance * (penalty_pct / 100))) if balance > 0 else 0
            if penalty > 0:
                penalty = min(penalty, balance)
                await bank.withdraw_credits(member, penalty)
            await stats.record_result(cog.config, member, "hunt", good=False)
            try:
                await channel.send(f"Oh no! {member.mention} shot the safe {animal_key} and paid {penalty:,} {currency} in fines!")
            except discord.HTTPException:
                pass
            return

        if is_safe and saluted:
            try:
                await channel.send(f"{member.mention} saluted the {animal_key}. Good instincts!")
            except discord.HTTPException:
                pass
            return

        min_r, max_r = game_conf["reward_range"]
        base = random.randint(min_r, max_r) if max_r > 0 else 0
        actual, _ = await pacing.apply_pacing(cog.config, member, base)
        if actual > 0:
            try:
                await bank.deposit_credits(member, actual)
            except BalanceTooHigh as e:
                bal = await bank.get_balance(member)
                new_bal = await bank.set_balance(member, e.max_balance)
                actual = new_bal - bal
            await pacing.record_payout(cog.config, member, actual)
        await stats.record_result(cog.config, member, "hunt", good=True)
        try:
            reward_txt = f" and earned {actual:,} {currency}!" if actual > 0 else "!"
            await channel.send(f"{member.mention} shot the {animal_key}{reward_txt}")
        except discord.HTTPException:
            pass
    finally:
        cog.active_game.pop(guild.id, None)
