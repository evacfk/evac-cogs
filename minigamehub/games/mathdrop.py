"""mathdrop -- ported from flare/cashdrop (Flare-Cogs), arithmetic-only.

Design doc: all four operators (+, -, x, /), division made integer-safe so
the answer is never ambiguous. Drops cashdrop's `pickup`-word sub-mode --
mathdrop is arithmetic only now. Text-collision risk in a busy channel is
accepted as-is per design review (locked decision #10) -- raw text matching,
same as cashdrop always did.
"""
import asyncio
import logging
import operator
import random

import discord
from redbot.core import bank
from redbot.core.errors import BalanceTooHigh
from redbot.core.utils.predicates import MessagePredicate

from .. import pacing, stats
from .base import register

log = logging.getLogger("red.minigamehub.mathdrop")

_OPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
}


def _generate_question(enabled_ops: list) -> tuple:
    op_symbol = random.choice(enabled_ops)
    if op_symbol == "/":
        # Integer-safe: pick the answer and a divisor first, derive the dividend,
        # so "12 / 4" never comes out as a repeating decimal.
        divisor = random.randint(2, 10)
        answer = random.randint(1, 12)
        dividend = divisor * answer
        return f"What is {dividend} / {divisor}?", answer
    num1 = random.randint(0, 12)
    num2 = random.randint(1, 10)
    answer = _OPS[op_symbol](num1, num2)
    return f"What is {num1} {op_symbol} {num2}?", int(answer)


@register("mathdrop")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "mathdrop"
    try:
        question, answer = _generate_question(game_conf["operators"])
        message = await channel.send(question)

        pred = MessagePredicate.equal_to(str(answer), channel=channel)
        try:
            answer_msg = await cog.bot.wait_for(
                "message", check=pred, timeout=game_conf["response_timeout"]
            )
        except asyncio.TimeoutError:
            try:
                await message.edit(content=f"{question}\n{game_conf['timeout_message']}")
            except discord.HTTPException:
                pass
            return

        member = guild.get_member(answer_msg.author.id) or answer_msg.author
        min_r, max_r = game_conf["reward_range"]
        base = random.randint(min_r, max_r)
        actual, _ = await pacing.apply_pacing(cog.config, member, base)
        try:
            await bank.deposit_credits(member, actual)
        except BalanceTooHigh as e:
            bal = await bank.get_balance(member)
            new_bal = await bank.set_balance(member, e.max_balance)
            actual = new_bal - bal
        await pacing.record_payout(cog.config, member, actual)
        await stats.record_result(cog.config, member, "mathdrop", good=True)

        currency = await bank.get_currency_name(guild)
        try:
            await message.edit(content=f"{question}\nCorrect! {member.mention} got {actual:,} {currency}!")
        except discord.HTTPException:
            pass
    finally:
        cog.active_game.pop(guild.id, None)
