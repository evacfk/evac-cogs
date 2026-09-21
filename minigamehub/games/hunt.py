"""hunt -- ported from vrt/hunting (vertyco/vrt-cogs), with real fixes.

Design doc per-game-type notes:
1. Bug fix -- word-bounded match, not substring. vrt/hunting's check was
   `"bang" in message.content.lower().strip()`, so "bangqiuwewuqneiuqe"
   counted as a valid shot. Fixed to require the shoot/safe word exactly,
   optionally followed by punctuation/whitespace (`bang`, `bang!`, `BANG.`
   all match; `bangqiuwewuqneiuqe` doesn't). Reactions were already exact
   (emoji equality), unchanged.
2. Custom animal pool, Config-editable, not birds-only.
3. Multiple safe animals, each independently toggleable.
4. Per-safe-animal penalty as a % of the shooter's balance, not a flat range.
5. Stays single-winner on purpose (locked decision #13) -- first valid
   bang/salute resolves the whole spawn.
6. `trigger_mode` ("both"/"word"/"reaction", default "both") toggles whether
   hunt accepts the typed word, the reaction, or either -- added on request,
   the word itself stays Config-editable via `shoot_word`/`safe_word`.
7. Bug fix -- custom emoji reactions compared by ID via emoji_utils, not raw
   string equality. Raw `str(reaction.emoji) == game_conf["shoot_reaction"]`
   broke the moment an admin set a custom emoji, since anything short of a
   byte-for-byte identical string (a stray copy-paste space was enough)
   silently failed to match, and the bot's own add_reaction() call round-
   tripped the raw string through manual `<>` stripping instead of Discord.py's
   own emoji parsing.
"""
import asyncio
import logging
import random
import re

import discord
from redbot.core import bank

from .. import pacing, stats
from ..emoji_utils import emoji_matches, parse_emoji
from .base import register

log = logging.getLogger("red.minigamehub.hunt")


@register("hunt")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict, dry_run: bool = False) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "hunt"
    try:
        animals = game_conf["animals"]
        if not animals:
            return
        animal_key = random.choice(list(animals.keys()))
        animal = animals[animal_key]
        is_safe = animal_key in game_conf["safe_animals"]
        mode = game_conf.get("trigger_mode", "both")
        accepts_word = mode in ("both", "word")
        accepts_reaction = mode in ("both", "reaction")

        prefix = "\U0001F9EA **[TEST]** " if dry_run else ""
        message = await channel.send(f"{prefix}{animal['emoji']} {animal['text']}")
        if accepts_reaction:
            try:
                await message.add_reaction(parse_emoji(game_conf["shoot_reaction"]))
                if is_safe:
                    await message.add_reaction(parse_emoji(game_conf["safe_reaction"]))
            except discord.HTTPException:
                pass

        shoot_word = game_conf["shoot_word"].strip().lower()
        safe_word = game_conf["safe_word"].strip().lower()
        # Exact-equality was too strict in practice -- "bang!" or "BANG." never
        # matched, only bare "bang" did, which just pushed everyone onto the
        # reaction instead. Allow trailing punctuation/whitespace while still
        # requiring the word itself to be exact, so "bangqiuwewuqneiuqe" is
        # still rejected (the bug this was fixed for in the first place).
        shoot_pattern = re.compile(rf"^{re.escape(shoot_word)}[!.?\s]*$")
        safe_pattern = re.compile(rf"^{re.escape(safe_word)}[!.?\s]*$")

        def msg_check(m: discord.Message):
            if m.channel.id != channel.id or m.author.bot or not m.content:
                return False
            content = m.content.strip().lower()
            return bool(shoot_pattern.match(content)) or (is_safe and bool(safe_pattern.match(content)))

        def reaction_check(payload: discord.RawReactionActionEvent):
            if payload.channel_id != channel.id or payload.message_id != message.id:
                return False
            if payload.member is None or payload.member.bot:
                return False
            return emoji_matches(game_conf["shoot_reaction"], payload.emoji) or (
                is_safe and emoji_matches(game_conf["safe_reaction"], payload.emoji)
            )

        futures = []
        if accepts_word:
            futures.append(asyncio.ensure_future(cog.bot.wait_for("message", check=msg_check)))
        if accepts_reaction:
            futures.append(asyncio.ensure_future(cog.bot.wait_for("raw_reaction_add", check=reaction_check)))
        if not futures:
            log.warning("hunt trigger_mode %r left no valid trigger in guild %s -- skipping spawn", mode, guild.id)
            return

        try:
            done, pending = await asyncio.wait(
                futures, return_when=asyncio.FIRST_COMPLETED, timeout=game_conf["response_timeout"]
            )
        finally:
            for f in futures:
                if not f.done():
                    f.cancel()

        if not done:
            escaped = f"{prefix}The {animal_key} flew away!"
            try:
                await channel.send(escaped)
            except discord.HTTPException:
                pass
            return

        result = done.pop().result()
        if isinstance(result, discord.Message):
            author = result.author
            saluted = is_safe and bool(safe_pattern.match(result.content.strip().lower()))
        else:
            author = result.member
            saluted = is_safe and emoji_matches(game_conf["safe_reaction"], result.emoji)

        member = guild.get_member(author.id) or author
        currency = await bank.get_currency_name(guild)

        note = " (test -- no currency actually moved)" if dry_run else ""

        if is_safe and not saluted:
            # Shot a safe animal -- penalty as % of the shooter's current balance.
            penalty_pct = game_conf["safe_animals"][animal_key]["penalty_pct"]
            balance = await bank.get_balance(member)
            raw_penalty = max(1, round(balance * (penalty_pct / 100))) if balance > 0 else 0
            penalty = await pacing.settle_penalty(member, raw_penalty, dry_run=dry_run)
            if not dry_run:
                await stats.record_result(cog.config, member, "hunt", good=False)
            try:
                await channel.send(f"{prefix}Oh no! {member.mention} shot the safe {animal_key} and paid {penalty:,} {currency} in fines!{note}")
            except discord.HTTPException:
                pass
            return

        if is_safe and saluted:
            try:
                await channel.send(f"{prefix}{member.mention} saluted the {animal_key}. Good instincts!")
            except discord.HTTPException:
                pass
            return

        min_r, max_r = game_conf["reward_range"]
        base = random.randint(min_r, max_r) if max_r > 0 else 0
        actual = await pacing.settle_reward(cog.config, member, base, dry_run=dry_run)
        if not dry_run:
            await stats.record_result(cog.config, member, "hunt", good=True)
        try:
            reward_txt = f" and earned {actual:,} {currency}!{note}" if actual > 0 else "!"
            await channel.send(f"{prefix}{member.mention} shot the {animal_key}{reward_txt}")
        except discord.HTTPException:
            pass
    finally:
        cog.active_game.pop(guild.id, None)
