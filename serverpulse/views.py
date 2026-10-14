"""Buttons and the edit box for recap drafts.

The buttons carry only a static custom_id (`serverpulse:recap:<action>`); the cog's
on_interaction listener answers them and finds the draft from the message the button
sits on. No View has to be re-registered after a restart, so a draft left overnight
still works. Same approach as rejoinwatch.
"""
from __future__ import annotations

import discord

from . import recap
from .constants import RECAP_PUBLIC_MAX


def recap_view() -> "discord.ui.View":
    view = discord.ui.View(timeout=None)
    for label, style, action in (
        ("Approve & post", discord.ButtonStyle.success, "approve"),
        ("Edit", discord.ButtonStyle.secondary, "edit"),
        ("Regenerate", discord.ButtonStyle.primary, "regen"),
        ("Reject", discord.ButtonStyle.danger, "reject"),
    ):
        view.add_item(discord.ui.Button(label=label, style=style, custom_id=recap.make_custom_id(action)))
    return view


def edit_modal(current: str, on_submit):
    """A one-box form pre-filled with the public text. Built on demand (nothing here runs at import)."""

    class _Edit(discord.ui.Modal):
        def __init__(self):
            super().__init__(title="Edit the recap")
            self.box = discord.ui.TextInput(
                label="Recap text (what members will see)", style=discord.TextStyle.paragraph,
                default=current, min_length=1, max_length=RECAP_PUBLIC_MAX, required=True,
            )
            self.add_item(self.box)

        async def on_submit(self, interaction):
            await on_submit(interaction, str(self.box.value))

    return _Edit()
