"""Discord-facing rendering for IntroForm."""
import discord

from . import engine
from .constants import BRAND_COLOR


def build_intro_embed(member, answers: dict) -> "discord.Embed":
    embed = discord.Embed(
        title=answers.get("name") or member.display_name,
        description=engine.build_description(member.mention, answers),
        color=discord.Color(BRAND_COLOR),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")
    return embed


def build_panel_embed(enforce: bool) -> "discord.Embed":
    lines = [
        "Press **Create / Edit My Intro**, fill in the form, and I'll post it here for you.",
        "You get one intro. Press the button again any time to edit it.",
        "Looking for someone? Press **Find an Intro** to look up a member or search by keyword.",
    ]
    if enforce:
        lines.append("Regular messages in this channel are removed, so use the form!")
    return discord.Embed(
        title="Introduce yourself!",
        description="\n".join(lines),
        color=discord.Color(BRAND_COLOR),
    )
