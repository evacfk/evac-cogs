"""The button object for puzzles whose hunt object is clicked instead of reacted to.

One persistent view serves every such puzzle: the four buttons have fixed custom ids, and the cog works out which
puzzle and which room a click belongs to from the message it happened on. Replies are private (ephemeral), so a
solver's result is never visible to anyone else and a decoy looks exactly like the real thing.
"""
import discord

BUTTON_KEYS = ("drink", "eat", "knock", "look")


class ObjectView(discord.ui.View):
    """Drink / Eat / Knock / Look under a pinned hunt object. Never times out; registered once at cog load."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Drink", custom_id="secrethunt:drink", style=discord.ButtonStyle.secondary)
    async def drink(self, interaction: discord.Interaction, button):
        await self.cog.handle_click(interaction, "drink")

    @discord.ui.button(label="Eat", custom_id="secrethunt:eat", style=discord.ButtonStyle.secondary)
    async def eat(self, interaction: discord.Interaction, button):
        await self.cog.handle_click(interaction, "eat")

    @discord.ui.button(label="Knock", custom_id="secrethunt:knock", style=discord.ButtonStyle.secondary)
    async def knock(self, interaction: discord.Interaction, button):
        await self.cog.handle_click(interaction, "knock")

    @discord.ui.button(label="Look", custom_id="secrethunt:look", style=discord.ButtonStyle.secondary)
    async def look(self, interaction: discord.Interaction, button):
        await self.cog.handle_click(interaction, "look")


class WitnessView(discord.ui.View):
    """The single button under a trial puzzle's witness. Persistent, registered once at cog load; the cog works out which
    witness (room) and which member from the message and the clicker, and the reply is private."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Hear the witness", custom_id="secrethunt:witness", style=discord.ButtonStyle.secondary)
    async def witness(self, interaction: discord.Interaction, button):
        await self.cog.handle_witness(interaction)
