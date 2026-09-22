"""Discord embed builders for cardcollect. This module (unlike engine.py /
models.py / storage.py) is allowed to import discord -- it's the
presentation layer, not game logic, mirroring how minigamehub and
blackjacktable split their embeds.py out from the main cog file.
"""

import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import discord

from .constants import ACTIVITY_TIMEZONE, TIERS
from .models import Card, SellToken

RARITY_EMOJI = {
    "common": "⚪",
    "rare": "\U0001f535",
    "epic": "\U0001f7e3",
    "legendary": "\U0001f7e1",
}

EMBED_COLOR = discord.Color.from_rgb(155, 89, 182)
TEST_COLOR = discord.Color.from_rgb(231, 76, 60)


def _rarity_line(rarity: str) -> str:
    return f"{RARITY_EMOJI.get(rarity, '')} {rarity.title()}"


def claim_result_embed(
    card: Card,
    claimant: discord.abc.User,
    is_dupe: bool,
    price: Optional[int] = None,
    is_test: bool = False,
) -> discord.Embed:
    if is_test:
        title = "Test claim (nothing awarded)"
        color = TEST_COLOR
    elif is_dupe:
        title = "Duplicate — converted to a sell token"
        color = EMBED_COLOR
    else:
        title = "New card claimed!"
        color = EMBED_COLOR

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Card", value=f"**{card.name}** ({card.series})", inline=True)
    embed.add_field(name="Rarity", value=_rarity_line(card.rarity), inline=True)
    embed.add_field(name="Claimed by", value=claimant.mention, inline=True)
    if is_dupe and not is_test and price is not None:
        embed.add_field(
            name="Sell token",
            value=f"Already in your collection — sell with `.card sell` for {price} wondercoin.",
            inline=False,
        )
    if is_test:
        embed.add_field(
            name="Test mode",
            value="This was a test drop. No card, token, or currency changed hands.",
            inline=False,
        )
    return embed


def sell_result_embed(token: SellToken, price: int, member: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="Card sold", color=EMBED_COLOR)
    embed.add_field(name="Rarity", value=_rarity_line(token.rarity), inline=True)
    embed.add_field(name="Sold by", value=member.mention, inline=True)
    embed.add_field(name="Payout", value=f"{price} wondercoin", inline=True)
    return embed


def give_result_embed(card: Card, giver: discord.abc.User, receiver: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="Card gifted", color=EMBED_COLOR)
    embed.add_field(name="Card", value=f"**{card.name}** ({card.series})", inline=True)
    embed.add_field(name="Rarity", value=_rarity_line(card.rarity), inline=True)
    embed.add_field(name="From → To", value=f"{giver.mention} → {receiver.mention}", inline=False)
    return embed


def leaderboard_embed(entries: Sequence[Tuple[discord.abc.User, int]], guild_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"Card Collection Leaderboard — {guild_name}", color=EMBED_COLOR)
    if not entries:
        embed.description = "Nobody has claimed a card yet."
        return embed
    lines = []
    medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
    for i, (member, count) in enumerate(entries):
        prefix = medals[i] if i < len(medals) else f"`#{i + 1}`"
        lines.append(f"{prefix} {member.mention} — **{count}** cards")
    embed.description = "\n".join(lines)
    return embed


def settings_embed(guild_config: dict, guild_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"cardcollect settings — {guild_name}", color=EMBED_COLOR)
    channel_id = guild_config.get("channel_id")
    embed.add_field(name="Drop channel", value=f"<#{channel_id}>" if channel_id else "Not set", inline=True)
    embed.add_field(name="Drop chance", value=f"{guild_config.get('drop_chance', 0) * 100:.2f}% / message", inline=True)
    embed.add_field(name="Drop cooldown", value=f"{guild_config.get('drop_cooldown_seconds', 0)}s", inline=True)
    embed.add_field(name="Claim window", value=f"{guild_config.get('claim_window_seconds', 0)}s", inline=True)
    embed.add_field(name="Claim cooldown", value=f"{guild_config.get('claim_cooldown_seconds', 0)}s", inline=True)
    embed.add_field(name="Decoys enabled", value=str(guild_config.get("decoys_enabled", True)), inline=True)

    weights = guild_config.get("drop_weights", {})
    weight_lines = [f"{_rarity_line(t)}: {weights.get(t, 0)}" for t in TIERS]
    embed.add_field(name="Drop weights", value="\n".join(weight_lines), inline=True)

    prices = guild_config.get("sell_prices", {})
    price_lines = [f"{_rarity_line(t)}: {prices.get(t, 0)}" for t in TIERS]
    embed.add_field(name="Sell prices", value="\n".join(price_lines), inline=True)

    pool_size = len(guild_config.get("pool", {}))
    embed.add_field(name="Pool size", value=f"{pool_size} characters", inline=True)

    test_mode = guild_config.get("test_mode", False)
    embed.add_field(name="Test mode", value="ON — drops award nothing" if test_mode else "off", inline=True)
    return embed


def diagnostics_embed(
    hourly_buckets: Dict[str, int],
    suggested_drop_chance: float,
    suggested_drop_cooldown_seconds: int,
    sampling_since: float,
    guild_name: str,
) -> discord.Embed:
    embed = discord.Embed(title=f"cardcollect diagnostics — {guild_name}", color=EMBED_COLOR)
    since = datetime.datetime.fromtimestamp(sampling_since, tz=datetime.timezone.utc)
    embed.description = (
        f"Sampling since {since.strftime('%Y-%m-%d %H:%M UTC')}. "
        f"Hour-of-day buckets are in {ACTIVITY_TIMEZONE}."
    )

    total = sum(hourly_buckets.values())
    if total == 0:
        embed.add_field(name="Activity", value="No messages tracked yet — run this again after some traffic.", inline=False)
    else:
        lines = []
        for hour in range(24):
            count = hourly_buckets.get(str(hour), 0)
            bar_len = int((count / max(hourly_buckets.values())) * 20) if hourly_buckets.values() else 0
            lines.append(f"`{hour:02d}:00` {'█' * bar_len} {count}")
        # keep the embed within field-length limits
        chunk = "\n".join(lines)
        if len(chunk) > 1024:
            chunk = chunk[:1000] + "\n… (see attached JSON for full data)"
        embed.add_field(name="Messages by hour", value=chunk, inline=False)

    embed.add_field(name="Suggested drop_chance", value=f"{suggested_drop_chance * 100:.3f}%", inline=True)
    embed.add_field(name="Suggested drop_cooldown_seconds", value=str(suggested_drop_cooldown_seconds), inline=True)
    embed.set_footer(text="Full data attached as JSON. Re-run periodically as activity patterns shift.")
    return embed


def card_added_embed(card: Card) -> discord.Embed:
    embed = discord.Embed(title="Character added to pool", color=EMBED_COLOR)
    embed.add_field(name="Name", value=card.name, inline=True)
    embed.add_field(name="Series", value=card.series or "—", inline=True)
    embed.add_field(name="Rarity", value=_rarity_line(card.rarity), inline=True)
    embed.add_field(name="Card ID", value=str(card.card_id), inline=True)
    return embed


def card_removed_embed(card_id: int, name: str) -> discord.Embed:
    embed = discord.Embed(title="Character removed from pool", color=EMBED_COLOR)
    embed.description = f"**{name}** (ID {card_id}) will no longer drop. Members who already own it keep their copy."
    return embed


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="cardcollect", description=message, color=discord.Color.red())
