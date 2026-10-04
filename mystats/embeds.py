"""Discord embed for mystats."""
from __future__ import annotations

import discord

from .constants import COLOR
from .engine import clip


def stats_embed(member, sections, balance_text: str | None) -> discord.Embed:
    e = discord.Embed(title=f"\N{BAR CHART} Stats: {member.display_name}", color=discord.Color(COLOR))
    if balance_text:
        e.description = balance_text
    if not sections:
        e.add_field(name="Nothing yet", value="No game stats yet. Join in on the games in the bot guide and they will show up here.",
                    inline=False)
    for title, lines in sections:
        e.add_field(name=title, value=clip("\n".join(lines)), inline=False)
    e.set_footer(text="Fishing keeps its own stats: .fishinfo")
    avatar = getattr(member, "display_avatar", None)
    if avatar is not None:
        e.set_thumbnail(url=avatar.url)
    return e
