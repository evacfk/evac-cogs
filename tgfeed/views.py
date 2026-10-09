"""The mod-only X under each mirrored post. Custom id is fixed and the view is
registered with bot.add_view(), so the button keeps working after a restart."""
from __future__ import annotations

import discord

from . import constants


class FeedPostView(discord.ui.View):
    """Everyone sees the small X; only moderators can use it."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, custom_id=constants.FEED_X_ID)
    async def remove(self, interaction, button):
        await self.cog.handle_feed_x(interaction)
