"""Discord-facing text and embed builders. No Cog logic here."""
from typing import Iterable, List

import discord

from . import constants as C
from .models import Interest


def rabbit_embed() -> "discord.Embed":
    """The cryptic message pinned in Pillow Talk. Deliberately says very little."""
    embed = discord.Embed(
        title="\U0001F407",
        description=(
            "*I'm late, I'm late.*\n\n"
            "There is a door that only regulars ever find. "
            "Tap the rabbit and see whether it opens for you."
        ),
        color=discord.Color.dark_grey(),
    )
    embed.set_footer(text="Curiouser and curiouser.")
    return embed


def panel_embed(interests: Iterable[Interest], warn_days: float, remove_days: float) -> "discord.Embed":
    interests = list(interests)
    embed = discord.Embed(
        title="Pick your rabbit holes",
        description=(
            "Tap a button to join a channel. Tap it again to leave.\n\n"
            f"Stay active there: post or react at least once every {int(remove_days)} days "
            f"(you'll get a nudge after {int(warn_days)}) or access is removed. "
            f"To come back after that, {C.CONTACT_TEXT[0].lower()}{C.CONTACT_TEXT[1:]}"
        ),
        color=discord.Color.dark_grey(),
    )
    if interests:
        lines = [f"{i.emoji} **{i.name}**".strip() for i in interests]
        embed.add_field(name="Open now", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Open now", value="Nothing yet.", inline=False)
    return embed


def invite_embed(guild_name: str, ttl_days: int) -> "discord.Embed":
    embed = discord.Embed(
        title="\U0001F407 The rabbit noticed you",
        description=(
            f"You've been invited to the **Rabbit Hole** in {guild_name}: the adults-only (18+) "
            "part of the server.\n\n"
            "**Accept** to unlock it. **Decline** and you won't be asked again."
        ),
        color=discord.Color.dark_grey(),
    )
    embed.set_footer(text=f"This invitation expires in {ttl_days} days.")
    return embed


def invite_result_embed(kind: str) -> "discord.Embed":
    titles = {
        "accepted": ("\U0001F407 You're in", "The rabbit nods. Look for the new channels.", discord.Color.green()),
        "declined": ("Invitation declined", "No problem. You won't be asked again.", discord.Color.dark_grey()),
        "expired": ("Invitation expired", "This invitation is no longer valid.", discord.Color.dark_grey()),
    }
    title, text, color = titles.get(kind, titles["expired"])
    return discord.Embed(title=title, description=text, color=color)


def warning_text(interest_name: str, remaining_text: str) -> str:
    return (
        f"Heads up: you haven't posted or reacted in **{interest_name}** for a while. "
        f"Access will be removed in about {remaining_text} unless you do. "
        "A message or a reaction there is enough."
    )


def removal_text(interest_name: str) -> str:
    return (
        f"Your access to **{interest_name}** was removed because you were inactive there. "
        f"{C.CONTACT_TEXT}"
    )


def status_lines(pairs: Iterable[tuple]) -> str:
    """Render (label, value) pairs as aligned plain text for `.afterdark status`."""
    pairs = list(pairs)
    width = max((len(label) for label, _ in pairs), default=0)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in pairs)


def check_lines(name: str, rows: List[tuple]) -> str:
    return f"**{name}**\n" + "\n".join(f"{'✅' if ok else '❌'} {text}" for ok, text in rows)
