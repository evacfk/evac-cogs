"""Mod-channel embeds for rejoinwatch. Only plain Embed features, so they run under the test stub."""
from typing import List

import discord

from . import engine
from .constants import MAX_HISTORY_SHOWN


def _who(user_id: int, name: str) -> str:
    return f"<@{user_id}> (`{name}`, {user_id})"


def _stamp_lines(stamps: List[float]) -> str:
    shown = stamps[-MAX_HISTORY_SHOWN:]
    lines = [f"<t:{int(t)}:f>" for t in shown]
    hidden = len(stamps) - len(shown)
    if hidden > 0:
        lines.insert(0, f"(+{hidden} earlier)")
    return "\n".join(lines) or "none"


def leave_alert(user_id: int, name: str, stamps: List[float], retention_days: int) -> "discord.Embed":
    """Posted when someone leaves for the 2nd+ time. Goes with the Ban / No buttons."""
    embed = discord.Embed(
        title="Repeat leaver",
        description=f"{_who(user_id, name)} has left {engine.count_phrase(len(stamps))} now.",
        color=discord.Color.orange(),
    )
    embed.add_field(name=f"Leaves in the last {retention_days} days", value=_stamp_lines(stamps), inline=False)
    embed.set_footer(text="Ban them? Yes bans immediately. No dismisses this.")
    return embed


def rejoin_warned(user_id: int, name: str, stamps: List[float], retention_days: int,
                  delivery_text: str, created_ts: float) -> "discord.Embed":
    """Posted when someone rejoins after one leave and was just warned."""
    embed = discord.Embed(
        title="Rejoined after leaving",
        description=(
            f"{_who(user_id, name)} has rejoined after leaving {engine.count_phrase(len(stamps))}. "
            f"{delivery_text}"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(name=f"Leaves in the last {retention_days} days", value=_stamp_lines(stamps), inline=False)
    embed.add_field(name="Account created", value=f"<t:{int(created_ts)}:R>", inline=True)
    return embed


def rejoin_escalated(user_id: int, name: str, stamps: List[float], retention_days: int,
                     created_ts: float) -> "discord.Embed":
    """Posted when someone rejoins after 2+ leaves. The user is not messaged; mods decide."""
    embed = discord.Embed(
        title="Rejoined after repeated leaves",
        description=(
            f"{_who(user_id, name)} has joined after previously leaving the server "
            f"{engine.times_phrase(len(stamps))}."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(name=f"Leaves in the last {retention_days} days", value=_stamp_lines(stamps), inline=False)
    embed.add_field(name="Account created", value=f"<t:{int(created_ts)}:R>", inline=True)
    embed.set_footer(text="Ban them? Yes bans immediately. No dismisses this.")
    return embed


def resolve(embed: "discord.Embed", text: str, banned: bool) -> "discord.Embed":
    """Mark an alert as handled (the buttons are removed by the caller)."""
    embed.color = discord.Color.red() if banned else discord.Color.dark_grey()
    embed.set_footer(text=text)
    return embed
