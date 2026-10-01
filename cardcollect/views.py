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

from . import imagegen
from .constants import GALLERY_VIEW_TIMEOUT_SECONDS

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


class GalleryView(discord.ui.View):
    """Prev/Next paging for `.card`'s collection gallery. Unlike ClaimView
    this is NOT persistent -- it's a read-only viewer over a snapshot of one
    member's collection taken when `.card` was run, so there's no state to
    lose if it goes stale after a restart or times out; it just stops
    responding, same as any other timed-out view.

    `pages` is a list of (Card, image_bytes) lists, already chunked by the
    caller (constants.GALLERY_PAGE_SIZE per page). Only page 0 ever shows
    the showcase header row, matching the pre-pagination gallery where the
    showcase sat above the one big grid. The header is looked up against
    `all_entries` (the member's *whole* collection, unpaginated) rather than
    just the current page, since a showcased card can land on any page --
    see render_gallery's showcase_pool docstring for why that distinction
    matters."""

    def __init__(self, invoker_id: int, pages, all_entries, showcase_card_ids, quantities, whose: str):
        super().__init__(timeout=GALLERY_VIEW_TIMEOUT_SECONDS)
        self.invoker_id = invoker_id
        self.pages = pages
        self.all_entries = all_entries
        self.showcase_card_ids = showcase_card_ids
        self.quantities = quantities
        self.whose = whose
        self.page = 0
        self.message = None  # set by the caller right after sending
        self._render_lock = asyncio.Lock()
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= len(self.pages) - 1

    def render_current(self, page=None):
        """Render one gallery page. Synchronous Pillow work -- call it through
        render_current_async from anything running on the event loop."""
        page = self.page if page is None else page
        showcase = self.showcase_card_ids if page == 0 else ()
        gallery = imagegen.render_gallery(
            self.pages[page],
            showcase_card_ids=showcase,
            quantities=self.quantities,
            showcase_pool=self.all_entries,
        )
        label = f"{self.whose} collection: (page {page + 1}/{len(self.pages)})"
        return gallery, label

    async def render_current_async(self, page=None):
        """render_current in a worker thread, so a slow render never stalls the
        event loop (and with it every other button and command on the bot)."""
        return await asyncio.to_thread(self.render_current, page)

    # NOTE: deliberately NOT named `_refresh`. discord.ui.View has its own sync
    # `_refresh(components)` that discord.py calls internally; an async override
    # of that name made discord.py create a coroutine it never awaited.
    async def _refresh_page(self, interaction: discord.Interaction):
        # Acknowledge first. A component interaction must be answered within
        # 3 seconds or the member sees "This interaction failed", and the render
        # below can take longer than that when the bot is busy.
        await interaction.response.defer()
        async with self._render_lock:  # the final edit always matches the final page
            page = self.page
            self._sync_buttons()
            gallery, label = await self.render_current_async(page)
            await interaction.edit_original_response(
                content=label, attachments=[discord.File(gallery, filename="collection.png")], view=self
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # belt-and-braces: discord.py's own dispatch calls this before a
        # button callback, but the check is repeated inside each callback
        # too (see _reject_if_not_invoker) so the same guard holds even when
        # a callback is invoked directly, bypassing View's dispatch (as the
        # test suite does to simulate a click).
        return await self._reject_if_not_invoker(interaction)

    async def _reject_if_not_invoker(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran `.card` can page through this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._reject_if_not_invoker(interaction):
            return
        self.page = max(0, self.page - 1)
        await self._refresh_page(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._reject_if_not_invoker(interaction):
            return
        self.page = min(len(self.pages) - 1, self.page + 1)
        await self._refresh_page(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.warning("cardcollect: couldn't disable a timed-out gallery view", exc_info=True)
