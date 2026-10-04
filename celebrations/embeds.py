"""Discord embeds for celebrations."""
from __future__ import annotations

import discord

from . import engine
from .constants import ANNIVERSARY_LIST_MAX, COG_VERSION, COLOR_ANNIVERSARY, COLOR_BIRTHDAY


def _avatar(member):
    av = getattr(member, "display_avatar", None)
    return getattr(av, "url", None)


def birthday_embed(member, *, age, gift: int, currency: str, star: str):
    e = discord.Embed(
        title=f"\N{BIRTHDAY CAKE} Happy Birthday, {member.display_name}!",
        description=(
            f"Today is {member.mention}'s birthday"
            + (f" and they're turning **{age}**" if age else "")
            + "! Drop them some love in chat \N{SPARKLING HEART}"
        ),
        color=discord.Color(COLOR_BIRTHDAY),
    )
    if gift:
        e.add_field(name="\N{WRAPPED PRESENT} Birthday gift", value=f"**{gift:,}** {currency}", inline=False)
    url = _avatar(member)
    if url:
        e.set_thumbnail(url=url)
    if star:
        e.set_footer(text=f"React {star} to send this to the starboard")
    return e


def announcement_embed(member, *, chat_channel_id):
    where = f" in <#{chat_channel_id}>" if chat_channel_id else ""
    e = discord.Embed(
        title=f"\N{BIRTHDAY CAKE} It's {member.display_name}'s birthday today!",
        description=f"Go wish {member.mention} a happy birthday{where} \N{PARTY POPPER}",
        color=discord.Color(COLOR_BIRTHDAY),
    )
    url = _avatar(member)
    if url:
        e.set_thumbnail(url=url)
    e.set_footer(text="This post disappears when their birthday ends.")
    return e


def anniversary_embed(rows, *, star: str, gift: int, currency: str):
    """rows: [(member, years)] sorted longest-first."""
    lines = []
    for member, years in rows[:ANNIVERSARY_LIST_MAX]:
        lines.append(f"{member.mention} — **{years} year{'s' if years != 1 else ''}** with us")
    if len(rows) > ANNIVERSARY_LIST_MAX:
        lines.append(f"…and {len(rows) - ANNIVERSARY_LIST_MAX} more!")
    e = discord.Embed(
        title="\N{PARTY POPPER} Wonderland anniversaries today",
        description="\n".join(lines),
        color=discord.Color(COLOR_ANNIVERSARY),
    )
    if gift:
        e.add_field(name="\N{WRAPPED PRESENT} Anniversary gift", value=f"**{gift:,}** {currency} each", inline=False)
    if star:
        e.set_footer(text=f"React {star} to send this to the starboard")
    return e


def settings_text(s: dict, *, role_ok: str, version: str = COG_VERSION) -> str:
    gift = (
        "off" if not s["gift_max"]
        else f"{s['gift_min']:,}" if s["gift_min"] == s["gift_max"]
        else f"{s['gift_min']:,}–{s['gift_max']:,}"
    )

    def ch(cid):
        return f"<#{cid}>" if cid else "not set"

    return "\n".join([
        f"**Celebrations v{version}**",
        f"Birthday + anniversary posts: {ch(s['channel_id'])} (stay up, {s['star_emoji'] or 'no'} reaction added)",
        f"Birthday announcement: {ch(s['announce_channel_id'])} (deleted when the day ends)",
        f"Birthday role: {('<@&' + str(s['role_id']) + '>') if s['role_id'] else 'not set'} {role_ok}",
        f"Birthday gift: {gift} (members who joined in the last {s['gift_min_member_days']} days get no gift)",
        f"Posts go out at {s['announce_hour']}:00 Pacific; role + gift start at midnight Pacific",
        f"Anniversaries: {'on' if s['anniversaries'] else 'off'}"
        + (f", gift {s['anniversary_gift']:,}" if s["anniversary_gift"] else ""),
    ])
