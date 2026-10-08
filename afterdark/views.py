"""Persistent button views. All custom_ids are fixed (never per-message) so the
buttons keep working across bot restarts once registered with bot.add_view().
Callbacks only delegate to the cog; no logic lives here.
"""
from typing import Iterable

import discord

from . import constants as C
from .models import Interest


class RabbitView(discord.ui.View):
    """The single 🐇 button in Pillow Talk."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(emoji="\U0001F407", style=discord.ButtonStyle.secondary, custom_id=C.RABBIT_CLAIM_ID)
    async def claim(self, interaction, button):
        await self.cog.handle_rabbit_claim(interaction)


class InviteView(discord.ui.View):
    """Accept / Decline buttons on the invitation DM."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id=C.INVITE_ACCEPT_ID)
    async def accept(self, interaction, button):
        await self.cog.handle_invite_accept(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, custom_id=C.INVITE_DECLINE_ID)
    async def decline(self, interaction, button):
        await self.cog.handle_invite_decline(interaction)


class InterestView(discord.ui.View):
    """One toggle button per configured interest (max 25 -- Discord's limit)."""

    MAX_BUTTONS = 25

    def __init__(self, cog, interests: Iterable[Interest]):
        super().__init__(timeout=None)
        self.cog = cog
        for interest in list(interests)[: self.MAX_BUTTONS]:
            button = discord.ui.Button(
                label=interest.name,
                emoji=interest.emoji or None,
                style=discord.ButtonStyle.secondary,
                custom_id=C.INTEREST_ID_PREFIX + interest.key,
            )
            button.callback = self._make_callback(interest.key)
            self.add_item(button)

    def _make_callback(self, key: str):
        async def callback(interaction):
            await self.cog.handle_interest_toggle(interaction, key)

        return callback
