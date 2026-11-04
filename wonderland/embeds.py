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
    lines.append("🗺️ **Channel Guide**: what every channel is for")
    mod = [p for p in pages if p.mod_only]
    if mod:
        lines.append(f"{mod[0].emoji} **{mod[0].title}**: {mod[0].blurb}")
    body = (
        "Tap a topic below.\n\n" + "\n".join(lines)
        + "\n\nQuick jumps: `.wld map` · `.wld stats` · `.wld search <word>`"
    )
    return _base("🐇 Wonderland Guide", body)


def page_embed(page: Page, lines: List[str], footer: str = FOOTER, resolve=None) -> discord.Embed:
    blurb, extra = page.blurb, page.body
    if resolve is not None:
        blurb, extra = engine.fill_channels(blurb, resolve), engine.fill_channels(extra, resolve)
    head = blurb + ("\n\n" + extra if extra else "")
    body = head + "\n\n" + ("\n".join(lines) if lines else "Nothing to show here right now.")
    return _base(f"{page.emoji} {page.title}", body, footer)


def map_embed(resolve: Callable[[object], object], footer: str = FOOTER) -> discord.Embed:
    parts = []
    for title, lines in engine.map_blocks(resolve):
        parts.append(f"**{title.upper()}**\n" + "\n".join(lines))
    return _base("🗺️ Wonderland Channel Guide", "\n\n".join(parts), footer)


MOD_HIDDEN_NOTE = "\n\n🛡️ Mod-only matches are hidden in the channel. `/wonderland search` shows them privately."


def search_embed(query: str, hits, mod_hidden: bool = False) -> discord.Embed:
    """`mod_hidden`: the person is a mod and there are matches this public copy left out."""
    note = MOD_HIDDEN_NOTE if mod_hidden else ""
    if not hits:
        return _base("🔎 Search", f"Nothing found for **{discord.utils.escape_markdown(query)}**. Try a shorter word, "
                                  "or open `.wld` and browse the topics." + note)
    lines = [f"{page.emoji} {line}" for page, line in hits]
    return _base(f"🔎 Results for “{discord.utils.escape_markdown(query)}”", "\n".join(lines) + note)


def _signed(n: int) -> str:
    return f"{n:+,}"


def _signed_pct(p) -> str:
    return "" if p is None else f" ({p:+.1f}%)"


def economy_embed(currency: str, stats: dict, top, gone_n: int, gone_total: int, delta) -> discord.Embed:
    """The mod-only coin report. `top` is [(name, balance)], `delta` is economy.change() or None."""
    unit = currency or "coins"
    if not stats["holders"]:
        return _base(f"💰 {unit} report", f"Nobody holds any {unit} yet.", "Mods only")
    e = _base(
        f"💰 {unit} report",
        f"**{stats['total']:,}** {unit} are held by **{stats['holders']:,}** members "
        f"({stats['accounts']:,} have an account).",
        "Mods only · saved as today's snapshot",
    )
    e.add_field(
        name="Spread (holders only)",
        value=(f"Median **{stats['median']:,}** · average {stats['mean']:,}\n"
               f"Top 10% start at {stats['p90']:,} · top 1% at {stats['p99']:,}\n"
               f"Richest: {stats['max']:,}"),
        inline=False,
    )
    e.add_field(
        name="Who holds it",
        value=(f"The top 10 hold **{stats['top10_share']:.0%}** of all {unit}\n"
               f"The top 1% ({stats['top1_n']:,} members) hold **{stats['top1pct_share']:.0%}**"),
        inline=False,
    )
    total = stats["total"] or 1
    band_lines = [
        f"{b['label']}: {b['count']:,} members, {b['coins'] / total:.0%} of {unit}"
        for b in stats["bands"] if b["count"]
    ]
    e.add_field(name="Balance bands", value="\n".join(band_lines), inline=False)
    if top:
        medals = [f"{i}. {discord.utils.escape_markdown(name)}: {balance:,}" for i, (name, balance) in enumerate(top, 1)]
        e.add_field(name="Top holders", value="\n".join(medals), inline=False)
    if delta:
        e.add_field(
            name=f"Since {delta['since']}",
            value=(f"Total {_signed(delta['total'])}{_signed_pct(delta['total_pct'])}\n"
                   f"Holders {_signed(delta['holders'])} · median {_signed(delta['median'])}{_signed_pct(delta['median_pct'])}\n"
                   f"Top 10 share {delta['top10_share'] * 100:+.1f} points"),
            inline=False,
        )
    else:
        e.add_field(name="Change over time", value="First snapshot saved. Run this again later to see the change.", inline=False)
    if gone_n:
        e.add_field(name="Not counted", value=f"{gone_n:,} accounts of members who left ({gone_total:,} {unit}).", inline=False)
    return e


def key_label(key: str) -> str:
    return {HOME_KEY: "Home", MAP_KEY: "Channel Guide"}.get(key, key)
