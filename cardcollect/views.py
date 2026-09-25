"""Discord UI for cardcollect's claim flow: the Claim button under every drop
and the pop-up (modal) it opens for typing a card's code. All claim logic
lives in the cog (`CardCollect.submit_code` and friends); this module is only
the interaction plumbing.

The button is *persistent* (timeout=None, fixed custom_id) and a shared
instance is registered in `CardCollect.cog_load`. That way a click on a drop
posted before a restart still reaches the cog, which answers "this drop is
over" -- instead of Discord's generic "This interaction failed".
"""

import asyncio
import logging

import discord

log = logging.getLogger("red.evac-cogs.cardcollect")

CLAIM_BUTTON_ID = "cardcollect:claim"


class CodeModal(discord.ui.Modal):
    code = discord.ui.TextInput(
        label="Code shown on the card",
        placeholder="e.g. K3+9T",
        min_length=1,
        max_length=16,
        required=True,
    )

    def __init__(self, cog, drop_message_id: int):
        super().__init__(title="Claim a card", timeout=300)
        self.cog = cog
        self.drop_message_id = drop_message_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.handle_submit(interaction, str(self.code.value))

    async def handle_submit(self, interaction: discord.Interaction, raw_code: str):
        # Start the claim BEFORE acknowledging the interaction. Who wins a
        # near-tie is decided by the order submissions reach the cog's claim
        # lock, and the acknowledgement is a network round trip whose speed
        # varies per member -- awaiting it first would let a slow ack lose a
        # race the member actually won. A task starts running the next time
        # the loop is free, in creation order, with no await ahead of the lock.
        claim_task = asyncio.ensure_future(
            self.cog.submit_code(interaction.user, interaction.channel, self.drop_message_id, raw_code)
        )
        try:
            # a claim can involve several Config reads/writes; defer so the
            # 3-second interaction deadline can never turn a real claim into
            # an error on the member's screen
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            log.warning("cardcollect: couldn't acknowledge a claim submission", exc_info=True)
        try:
            text = await claim_task
        except Exception:
            log.exception("cardcollect: claim submission crashed")
            text = "Something went wrong handling that -- try again."
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            log.warning("cardcollect: couldn't send a claim reply", exc_info=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("cardcollect: claim modal error", exc_info=error)


class ClaimView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Claim a card", style=discord.ButtonStyle.primary, custom_id=CLAIM_BUTTON_ID, emoji="\U0001f524")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_id = interaction.message.id
        refusal = self.cog.claim_button_refusal(interaction.user, message_id)
        if refusal is not None:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.send_modal(CodeModal(self.cog, message_id))
