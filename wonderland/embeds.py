"""Discord embeds for the guide. All text comes from engine/content."""
from __future__ import annotations

from typing import Callable, List, Optional

import discord

from . import engine
from .constants import EMBED_COLOR, FOOTER
from .content import HOME_KEY, MAP_KEY, Page


def _base(title: str, description: str, footer: str = FOOTER) -> discord.Embed:
    embed = discord.Embed(title=title, description=engine.clamp(description), color=discord.Color(EMBED_COLOR))
    embed.set_footer(text=footer)
    return embed


def home_embed(pages: List[Page]) -> discord.Embed:
    lines = [f"{p.emoji} **{p.title}**: {p.blurb}" for p in pages if not p.mod_only]
    lines.append("🗺️ **Server Map**: what every channel is for")
    mod = [p for p in pages if p.mod_only]
    if mod:
        lines.append(f"{mod[0].emoji} **{mod[0].title}**: {mod[0].blurb}")
    body = (
        "Pick a topic from the menu below.\n\n" + "\n".join(lines)
        + "\n\nQuick jumps: `.wld map` · `.wld stats` · `.wld search <word>`"
    )
    return _base("🐇 Wonderland Guide", body)


def page_embed(page: Page, lines: List[str], footer: str = FOOTER) -> discord.Embed:
    body = page.blurb + "\n\n" + ("\n".join(lines) if lines else "Nothing to show here right now.")
    return _base(f"{page.emoji} {page.title}", body, footer)


def map_embed(resolve: Callable[[object], object], footer: str = FOOTER) -> discord.Embed:
    parts = []
    for title, lines in engine.map_blocks(resolve):
        parts.append(f"**{title.upper()}**\n" + "\n".join(lines))
    return _base("🗺️ Server Map", "\n\n".join(parts), footer)


def search_embed(query: str, hits) -> discord.Embed:
    if not hits:
        return _base("🔎 Search", f"Nothing found for **{discord.utils.escape_markdown(query)}**. Try a shorter word, "
                                  "or open `.wld` and browse the topics.")
    lines = [f"{page.emoji} {line}" for page, line in hits]
    return _base(f"🔎 Results for “{discord.utils.escape_markdown(query)}”", "\n".join(lines))


def key_label(key: str) -> str:
    return {HOME_KEY: "Home", MAP_KEY: "Server Map"}.get(key, key)
