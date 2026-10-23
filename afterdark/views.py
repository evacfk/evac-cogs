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


class ReviewView(discord.ui.View):
    """Yes / Not now on the moderator prompt that precedes every invitation."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Send invitation", style=discord.ButtonStyle.success, custom_id=C.REVIEW_YES_ID)
    async def yes(self, interaction, button):
        await self.cog.handle_review(interaction, True)

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary, custom_id=C.REVIEW_NO_ID)
    async def no(self, interaction, button):
        await self.cog.handle_review(interaction, False)

    @discord.ui.button(label="Stop for today", style=discord.ButtonStyle.danger, custom_id=C.REVIEW_STOP_ID)
    async def stop(self, interaction, button):
        await self.cog.handle_review_stop(interaction)


class InterestView(discord.ui.View):
    """One toggle button per configured interest, plus a Leave Rabbit Hole button.
    Discord allows 25 buttons per message, so interests are capped at 24."""

    MAX_BUTTONS = 24

    def __init__(self, cog, interests: Iterable[Interest]):
        super().__init__(timeout=None)
        self.cog = cog
        for interest in list(interests)[: self.MAX_BUTTONS]:
            button = discord.ui.Button(
                label=interest.name,
                style=discord.ButtonStyle.secondary,
                custom_id=C.INTEREST_ID_PREFIX + interest.key,
            )
            button.callback = self._make_callback(interest.key)
            self.add_item(button)
        leave = discord.ui.Button(
            label="Leave Rabbit Hole", style=discord.ButtonStyle.danger, custom_id=C.LEAVE_ID,
        )
        leave.callback = self._leave_callback
        self.add_item(leave)

    async def _leave_callback(self, interaction):
        await self.cog.handle_rabbit_leave(interaction)

    def _make_callback(self, key: str):
        async def callback(interaction):
            await self.cog.handle_interest_toggle(interaction, key)

        return callback


class LeaveConfirmView(discord.ui.View):
    """Ephemeral "are you sure" shown after Leave Rabbit Hole. Not persistent on purpose:
    if the bot restarts, the member just taps Leave again."""

    def __init__(self, cog, member):
        super().__init__(timeout=60)
        self.cog, self.member = cog, member

    @discord.ui.button(label="Yes, leave", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await self.cog.handle_rabbit_leave_confirm(interaction, self.member)
        self.stop()

    @discord.ui.button(label="Stay", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        try:
            await interaction.response.edit_message(content="Still in. \U0001F407", view=None)
        except (discord.HTTPException, discord.NotFound):
            pass
        self.stop()
