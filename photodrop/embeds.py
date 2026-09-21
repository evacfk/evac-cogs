"""Embed builders. Pure functions of already-known data (no Config/bank calls
here) so they're easy to unit test -- the cog class in photodrop.py is what
wires these to live Discord/Config state.
"""
from __future__ import annotations

from pathlib import Path

import discord

from .constants import STATUS_EMOJI, STATUS_FULL, STATUS_TARDY

COLOR_FULL = discord.Color.gold()
COLOR_TARDY = discord.Color.orange()
COLOR_NO_SHOW = discord.Color.red()
COLOR_NEUTRAL = discord.Color.dark_grey()


def shift_report_embed(member_display_name: str, outcome: str, photo_count: int, quota: int, streak: int) -> discord.Embed:
    if outcome == STATUS_FULL:
        title = f"{STATUS_EMOJI[STATUS_FULL]} Shift logged — {photo_count}/{quota} photos received"
        color = COLOR_FULL
    else:
        title = f"{STATUS_EMOJI[STATUS_TARDY]} Shift logged — {photo_count}/{quota} photos received — Tardy"
        color = COLOR_TARDY

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Streak", value=f"\N{FIRE} {streak} day{'s' if streak != 1 else ''}", inline=True)
    embed.set_footer(text=member_display_name)
    return embed


def status_embed(member_display_name: str, streak: int, strikes_in_window: int, strike_threshold: int, strike_window_days: int, has_role: bool) -> discord.Embed:
    embed = discord.Embed(title=f"{member_display_name}'s status", color=COLOR_FULL if has_role else COLOR_NO_SHOW)
    embed.add_field(name="Streak", value=f"\N{FIRE} {streak} day{'s' if streak != 1 else ''}", inline=True)
    embed.add_field(
        name="Strikes",
        value=f"{strikes_in_window}/{strike_threshold} in the last {strike_window_days} days",
        inline=True,
    )
    embed.add_field(name="Role", value="\N{WHITE HEAVY CHECK MARK} Active" if has_role else "\N{CROSS MARK} Removed", inline=True)
    return embed


def calendar_embed(member_display_name: str, month_label: str, day_entries: list[tuple[int, str | None]]) -> discord.Embed:
    """`day_entries` is [(day_of_month, status_or_None), ...] in order."""
    lines = []
    for day_num, status in day_entries:
        if status is None:
            continue
        emoji = STATUS_EMOJI.get(status, "\N{WHITE QUESTION MARK ORNAMENT}")
        lines.append(f"{emoji} `{day_num:>2}` {status.replace('_', ' ').title()}")

    embed = discord.Embed(
        title=f"{member_display_name} — {month_label}",
        description="\n".join(lines) if lines else "No entries this month.",
        color=COLOR_NEUTRAL,
    )
    return embed


def build_photo_message(entries: list[tuple[str, str, Path]]) -> tuple[list[discord.Embed], list[discord.File]]:
    """Build the photo embeds + attached files for a poll's lead-in message.

    `entries` is [(letter, label, image_path), ...]. Discord shows one image
    per embed, so this returns one embed per entry (all in a single message,
    up to Discord's 10-embeds-per-message cap) plus the matching File objects
    to attach -- each embed's image references its file via attachment://.
    """
    embeds: list[discord.Embed] = []
    files: list[discord.File] = []
    for letter, label, path in entries:
        filename = f"{letter}_{path.name}"
        files.append(discord.File(str(path), filename=filename))
        embed = discord.Embed(title=f"{letter} — {label}", color=COLOR_NEUTRAL)
        embed.set_image(url=f"attachment://{filename}")
        embeds.append(embed)
    return embeds, files


def poll_question_text() -> str:
    return "Which day's photos were best?"


def pollday_question_text(date_label: str) -> str:
    return f"Best photo from {date_label}?"
