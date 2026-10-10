"""Ban / No buttons for rejoinwatch mod alerts.

The buttons carry everything in their custom_id (`rejoinwatch:<action>:<user id>`) and are
answered by the cog's on_interaction listener, not by View callbacks. That keeps them
working after a bot restart, since no in-memory View has to be re-registered.
"""
import discord

from . import engine


def ban_view(user_id: int) -> "discord.ui.View":
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Yes, ban", style=discord.ButtonStyle.danger,
        custom_id=engine.make_custom_id("ban", user_id),
    ))
    view.add_item(discord.ui.Button(
        label="No", style=discord.ButtonStyle.secondary,
        custom_id=engine.make_custom_id("keep", user_id),
    ))
    return view
