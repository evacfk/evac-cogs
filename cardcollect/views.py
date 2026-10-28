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

from . import embeds, import_characters, imagegen
from .constants import GALLERY_VIEW_TIMEOUT_SECONDS, SERIES_IMPORT_DEFAULT, SERIES_VIEW_TIMEOUT_SECONDS, TIERS

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

    reject_text = "Only the person who ran `.card` can page through this."

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
            await interaction.response.send_message(self.reject_text, ephemeral=True)
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


class PoolSearchView(GalleryView):
    """Prev/Next paging for `.card pool <keyword>`. Same pager as the
    collection gallery, but `pages` holds plain Cards (not (Card, bytes)
    pairs) and each page's art is read only when that page is shown -- a
    broad keyword can match hundreds of cards, and loading them all up
    front would be wasted I/O. There is no showcase row; the caption is
    built by the cog (`caption_for(page_index, cards)`) so it can mark which
    cards the searcher owns."""

    reject_text = "Only the person who ran this search can page through it."

    def __init__(self, invoker_id: int, pages, quantities, read_image, caption_for):
        super().__init__(invoker_id, pages, [], (), quantities, "")
        self.read_image = read_image
        self.caption_for = caption_for

    def render_current(self, page=None):
        page = self.page if page is None else page
        cards = self.pages[page]
        entries = []
        for card in cards:
            image_bytes = self.read_image(card.card_id)
            if image_bytes is not None:
                entries.append((card, image_bytes))
        gallery = imagegen.render_gallery(entries, quantities=self.quantities)
        return gallery, self.caption_for(page, cards)


# ---------------------------------------------------------------------------
# admin views: `.card importseries` and the rarity dropdown on `.card info`
# ---------------------------------------------------------------------------


def _clip(text: str, length: int) -> str:
    text = str(text)
    return text if len(text) <= length else text[: length - 1] + "…"


class _InvokerView(discord.ui.View):
    """A view only the person who opened it may use."""

    reject_text = "Only the admin who ran this can use it."

    def __init__(self, invoker_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.message = None  # set by whoever sends the message carrying this view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(self.reject_text, ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.warning("cardcollect: couldn't disable a timed-out admin view", exc_info=True)


class SeriesPickView(_InvokerView):
    """Step 1 of `.card importseries`: choose which AniList match is meant."""

    def __init__(self, cog, invoker_id: int, results):
        super().__init__(invoker_id, SERIES_VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.results = {str(m["id"]): m for m in results}
        options = []
        for m in results:
            detail = " · ".join(
                x for x in (str(m.get("format") or "").replace("_", " ").title(), str(m.get("year") or "")) if x
            )
            options.append(
                discord.SelectOption(label=_clip(m["title"], 100), value=str(m["id"]), description=_clip(detail, 100) or None)
            )
        self.select = discord.ui.Select(placeholder="Pick a series…", options=options)
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction):
        media = self.results[self.select.values[0]]
        await interaction.response.defer()  # fetching its characters can take a few seconds
        preview = await self.cog.build_series_preview(interaction.guild, media)
        if isinstance(preview, str):  # a friendly failure message
            await interaction.edit_original_response(content=preview, embed=None, view=None)
            self.stop()
            return
        view = SeriesPreviewView(self.cog, self.invoker_id, preview)
        view.message = await interaction.edit_original_response(
            content=None, embed=embeds.series_preview_embed(preview, view.limit), view=view
        )
        self.stop()


class RarityModal(discord.ui.Modal):
    changes = discord.ui.TextInput(
        label="One per line, like  Frieren: legendary",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000,
    )

    def __init__(self, preview_view: "SeriesPreviewView"):
        super().__init__(title="Adjust rarities", timeout=600)
        self.preview_view = preview_view
        selected = import_characters.select_top(
            preview_view.preview.built, preview_view.limit, preview_view.preview.overrides
        )
        self.changes.default = "\n".join(f"{e['name']}: {e['rarity']}" for e in selected["entries"])[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        view = self.preview_view
        entries = view.preview.built["entries"]  # every new character checked, not just the top slice
        base = {e["rank"]: e["rarity"] for e in entries}
        overrides, problems = import_characters.parse_rarity_overrides(str(self.changes.value), entries)
        for rank, tier in overrides.items():
            if tier == base[rank]:
                view.preview.overrides.pop(rank, None)  # back to what its favourites say
            else:
                view.preview.overrides[rank] = tier
        await interaction.response.edit_message(embed=embeds.series_preview_embed(view.preview, view.limit), view=view)
        if problems:
            await interaction.followup.send(
                "Couldn't apply:\n" + "\n".join(f"• {x}" for x in problems[:10]), ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("cardcollect: rarity modal error", exc_info=error)


class SeriesPreviewView(_InvokerView):
    """Step 2 of `.card importseries`: the dry-run preview. Nothing is written
    until one of the Import buttons is pressed."""

    def __init__(self, cog, invoker_id: int, preview):
        super().__init__(invoker_id, SERIES_VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.preview = preview
        self.total = len(preview.info["characters"])
        self.limit = min(SERIES_IMPORT_DEFAULT, self.total) or SERIES_IMPORT_DEFAULT
        self._busy = False

        self.import_top = discord.ui.Button(label=f"Import top {self.limit}", style=discord.ButtonStyle.success)
        self.import_top.callback = self._import_top
        self.add_item(self.import_top)
        self.import_all = None
        if self.total > self.limit:
            self.import_all = discord.ui.Button(label=f"Import all {self.total}", style=discord.ButtonStyle.primary)
            self.import_all.callback = self._import_all
            self.add_item(self.import_all)
        self.adjust = discord.ui.Button(label="Adjust rarities", style=discord.ButtonStyle.secondary)
        self.adjust.callback = self._adjust
        self.add_item(self.adjust)
        self.cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
        self.cancel.callback = self._cancel
        self.add_item(self.cancel)

        top = import_characters.select_top(preview.built, self.limit)
        if not top["entries"] and not top["links"]:
            self.import_top.disabled = True  # nothing to do at this size
        if not preview.built["entries"]:
            self.adjust.disabled = True
            if self.import_all is not None:
                everything = import_characters.select_top(preview.built, self.total)
                self.import_all.disabled = not everything["entries"] and not everything["links"]

    async def _run_import(self, interaction: discord.Interaction, limit: int):
        if self._busy:
            await interaction.response.send_message("Already importing, give it a moment.", ephemeral=True)
            return
        self._busy = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Importing, downloading the art…", view=self)
        try:
            embed = await self.cog.commit_series(interaction.guild, self.preview, limit)
        except Exception:
            log.exception("cardcollect: series import crashed")
            await interaction.edit_original_response(
                content="Something went wrong during the import. Some characters may have been added; "
                "check `.card viewpool` before running it again.",
                embed=None,
                view=None,
            )
            self.stop()
            return
        await interaction.edit_original_response(content=None, embed=embed, view=None)
        self.stop()

    async def _import_top(self, interaction: discord.Interaction):
        await self._run_import(interaction, self.limit)

    async def _import_all(self, interaction: discord.Interaction):
        await self._run_import(interaction, self.total)

    async def _adjust(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RarityModal(self))

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Import cancelled. Nothing was added.", embed=None, view=None)
        self.stop()


class CardAdminView(_InvokerView):
    """The admin-only "Change rarity" dropdown under `.card info <card>`."""

    def __init__(self, cog, invoker_id: int, card, owner_count: int, owner_mentions, has_image: bool):
        super().__init__(invoker_id, 300)
        self.cog = cog
        self.card = card
        self.owner_count = owner_count
        self.owner_mentions = owner_mentions
        self.has_image = has_image
        self.select = discord.ui.Select(
            placeholder="Change rarity…",
            options=[
                discord.SelectOption(label=t.title(), value=t, emoji=embeds.RARITY_EMOJI[t], default=(t == card.rarity))
                for t in TIERS
            ],
        )
        self.select.callback = self._changed
        self.add_item(self.select)

    async def _changed(self, interaction: discord.Interaction):
        tier = self.select.values[0]
        if tier == self.card.rarity:
            await interaction.response.send_message(f"**{self.card.name}** is already {tier}.", ephemeral=True)
            return
        try:
            card = await self.cog.apply_card_rarity(interaction.guild, self.card.card_id, tier)
        except KeyError:
            await interaction.response.send_message("That card is no longer in the pool.", ephemeral=True)
            return
        self.card = card
        for option in self.select.options:
            option.default = option.value == tier
        embed = embeds.card_info_embed(card, self.owner_count, self.owner_mentions)
        if self.has_image:
            embed.set_image(url="attachment://card_info.png")
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"**{card.name}** is now **{tier}**. This changes how it drops from now on and how it looks in "
            "everyone's collection; sell tokens already issued keep the value they were made with.",
            ephemeral=True,
        )
