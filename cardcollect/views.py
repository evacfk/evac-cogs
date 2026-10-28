"""Discord UI for cardcollect: the pick buttons under every drop, the `.card`
collection tabs, `.card find`, `.card sell`, the card-request review buttons,
and the admin series-import views. All game logic lives in the cog (and
engine.py); this module is only the interaction plumbing.

The pick buttons and the request-review buttons are *persistent* (fixed
custom_ids, a shared instance registered in `CardCollect.cog_load`), so a press
that arrives after a restart still reaches the cog and is answered, instead of
Discord's generic "This interaction failed".
"""

import asyncio
import io
import logging
from collections import Counter

import discord

from . import embeds, engine, import_characters, imagegen
from .constants import (
    ART_UPLOAD_WAIT_SECONDS,
    GALLERY_PAGE_SIZE,
    GALLERY_VIEW_TIMEOUT_SECONDS,
    MAX_DROP_SIZE,
    PICK_BUTTON_PREFIX,
    REQUEST_APPROVE_ID,
    REQUEST_DECLINE_ID,
    REQUEST_MAX_LENGTH,
    SERIES_IMPORT_DEFAULT,
    SERIES_VIEW_TIMEOUT_SECONDS,
    TIERS,
)

log = logging.getLogger("red.evac-cogs.cardcollect")

DRAW_OVER_TEXT = "This drop is over — its draw has already closed."


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
        view.message = await interaction.edit_original_response(content=None, view=view, **view.payload())
        self.stop()


class AdjustModal(discord.ui.Modal):
    changes = discord.ui.TextInput(
        label="Delete a line to leave it out",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000,
    )

    def __init__(self, preview_view: "SeriesPreviewView"):
        super().__init__(title="Adjust the import", timeout=600)
        self.preview_view = preview_view
        selected = import_characters.select_top(
            preview_view.preview.built, preview_view.limit, preview_view.preview.overrides, preview_view.preview.skips
        )
        self.changes.default = "\n".join(f"{e['name']}: {e['rarity']}" for e in selected["entries"])[:4000]
        self.changes.placeholder = "Name: epic   |   skip 3, 7-9   |   unskip 3"

    async def on_submit(self, interaction: discord.Interaction):
        view = self.preview_view
        preview = view.preview
        entries = preview.built["entries"]  # every new character checked, not just the top slice
        base = {e["rank"]: e["rarity"] for e in entries}
        text = str(self.changes.value or "")
        parsed = import_characters.parse_adjustments(text, entries)
        notes = list(parsed["problems"])
        if text.strip():
            for rank, tier in parsed["overrides"].items():
                if tier == base[rank]:
                    preview.overrides.pop(rank, None)  # back to what its favourites say
                else:
                    preview.overrides[rank] = tier
            mentioned = set(parsed["overrides"])  # characters that still have a line: they are imported
            preview.skips -= mentioned
            # a character shown in the box whose line was deleted is left out -- but only when
            # the box still has character lines (not skip-only edits) and every line was readable
            if mentioned and not parsed["problems"]:
                shown = {e["rank"] for e in entries if e["rank"] < view.limit}
                preview.skips |= shown - mentioned
            elif mentioned:
                notes.append("Because of that, deleted lines were NOT treated as skips. Fix it and open Adjust again.")
            preview.skips |= parsed["skip"]
            preview.skips -= parsed["unskip"]
        view.refresh_buttons()
        await interaction.response.edit_message(view=view, **view.payload())
        if notes:
            await interaction.followup.send("Couldn't apply:\n" + "\n".join(f"• {x}" for x in notes[:10]), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("cardcollect: adjust modal error", exc_info=error)


RarityModal = AdjustModal  # the pop-up's old name


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
        self._listening = False

        self.import_top = discord.ui.Button(label=f"Import top {self.limit}", style=discord.ButtonStyle.success)
        self.import_top.callback = self._import_top
        self.add_item(self.import_top)
        self.import_all = None
        if self.total > self.limit:
            self.import_all = discord.ui.Button(label=f"Import all {self.total}", style=discord.ButtonStyle.primary)
            self.import_all.callback = self._import_all
            self.add_item(self.import_all)
        self.replace_art = discord.ui.Button(label="Replace art", style=discord.ButtonStyle.secondary)
        self.replace_art.callback = self._replace_art
        self.add_item(self.replace_art)
        self.adjust = discord.ui.Button(label="Adjust", style=discord.ButtonStyle.secondary)
        self.adjust.callback = self._adjust
        self.add_item(self.adjust)
        self.cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
        self.cancel.callback = self._cancel
        self.add_item(self.cancel)

        self.refresh_buttons()

    def refresh_buttons(self):
        """Grey out Import when nothing would be added, and the edit buttons
        when there is nothing new to edit."""
        preview = self.preview
        top = import_characters.select_top(preview.built, self.limit, skip=preview.skips)
        self.import_top.disabled = not top["entries"] and not top["links"]
        nothing_new = not preview.built["entries"]
        self.adjust.disabled = nothing_new
        self.replace_art.disabled = nothing_new
        if self.import_all is not None:
            everything = import_characters.select_top(preview.built, self.total, skip=preview.skips)
            self.import_all.disabled = not everything["entries"] and not everything["links"]

    def payload(self, send: bool = False) -> dict:
        """What the preview message shows right now: the embed with its
        numbered art grid. `send=True` is for a brand-new message (which takes
        `files=`); an edit replaces its attachments instead."""
        embed = embeds.series_preview_embed(self.preview, self.limit)
        files = []
        if any(e["rank"] < self.limit for e in self.preview.built["entries"]):
            embed.set_image(url="attachment://contact_sheet.png")
            files = [self.cog.contact_sheet_file(self.preview, self.limit)]
        return {"embed": embed, "files" if send else "attachments": files}

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
        await interaction.edit_original_response(content=None, embed=embed, attachments=[], view=None)
        self.stop()

    async def _import_top(self, interaction: discord.Interaction):
        await self._run_import(interaction, self.limit)

    async def _import_all(self, interaction: discord.Interaction):
        await self._run_import(interaction, self.total)

    async def _adjust(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AdjustModal(self))

    async def _replace_art(self, interaction: discord.Interaction):
        if self._listening:
            await interaction.response.send_message("Already waiting for an image from you.", ephemeral=True)
            return
        self._listening = True
        await interaction.response.send_message(
            "Send the new picture in this channel as an image upload, with the card **number** (or name) as the "
            f"message text, like `4`. I'll wait {ART_UPLOAD_WAIT_SECONDS}s for each one, so you can send several in "
            "a row. Type `done` when you're finished.",
            ephemeral=True,
        )
        try:
            while not self.is_finished():
                message = await self.cog.wait_for_upload(interaction.channel.id, interaction.user.id, ART_UPLOAD_WAIT_SECONDS)
                if message is None or (message.content or "").strip().lower() in ("done", "stop", "cancel"):
                    break
                if self.is_finished() or self._busy:
                    break  # the import started or was cancelled while we waited; leave the result message alone
                entry, problem = import_characters.resolve_entry(message.content, self.preview.built["entries"])
                if entry is None:
                    await interaction.followup.send(f"{problem} Send the image again with its number.", ephemeral=True)
                    continue
                data, problem = await self.cog.read_uploaded_art(message)
                if data is None:
                    await interaction.followup.send(problem, ephemeral=True)
                    continue
                self.preview.art[entry["rank"]] = data
                self.preview.skips.discard(entry["rank"])
                number = import_characters.entry_numbers(self.preview.built)[entry["rank"]]
                if self.message is not None and entry["rank"] < self.limit:
                    await self.message.edit(view=self, **self.payload())
                await interaction.followup.send(
                    f"Art for **{entry['name']}** (#{number}) replaced. Send another, or type `done`.", ephemeral=True
                )
        finally:
            self._listening = False

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Import cancelled. Nothing was added.", embed=None, attachments=[], view=None
        )
        self.stop()


# ---------------------------------------------------------------------------
# the draw: one button per card under every drop
# ---------------------------------------------------------------------------


class DrawView(discord.ui.View):
    """The buttons under a drop: one per card. Pressing one enters (or moves)
    the member's pick for that card; the draw itself happens when the window
    closes (CardCollect._resolve_drop).

    Two flavours share this class. A drop's own view (`cards` given) labels each
    button with its card and times out shortly after the draw. The *persistent*
    view (`cards=None`, registered once in cog_load) has generic labels and
    never times out: it exists so a press on a drop that predates a restart
    still lands in the cog and is answered ("this drop is over") instead of
    Discord's "This interaction failed"."""

    def __init__(self, cog, cards=None, window: float = 90):
        super().__init__(timeout=None if cards is None else window + 120)
        self.cog = cog
        count = MAX_DROP_SIZE if cards is None else len(cards)
        for position in range(count):
            card = cards[position] if cards is not None else None
            label = _clip(f"{position + 1}  {card.name}", 80) if card is not None else f"Card {position + 1}"
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PICK_BUTTON_PREFIX}{position}",
                emoji=embeds.RARITY_EMOJI.get(card.rarity) if card is not None else None,
            )
            button.callback = self._callback_for(position)
            self.add_item(button)

    def _callback_for(self, position: int):
        async def pressed(interaction: discord.Interaction):
            await self._pressed(interaction, position)

        return pressed

    async def _pressed(self, interaction: discord.Interaction, position: int):
        # Record the pick BEFORE acknowledging the interaction: the acknowledgement
        # is a network round trip whose speed varies per member, and a press made
        # before the draw closes must count however slow it is.
        drop, previous = self.cog.register_pick(interaction.user, interaction.message.id, position)
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            log.warning("cardcollect: couldn't acknowledge a pick", exc_info=True)
        if drop is None:
            text = DRAW_OVER_TEXT
        else:
            try:
                text = await self.cog.pick_reply(interaction.user, drop, position, previous)
            except Exception:
                log.exception("cardcollect: building a pick reply failed")
                text = "Your pick was recorded, but I couldn't build the confirmation."
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            log.warning("cardcollect: couldn't send a pick reply", exc_info=True)


# ---------------------------------------------------------------------------
# `.card`: the collection with Collection / Sets / Wishlist tabs
# ---------------------------------------------------------------------------


class CollectionView(_InvokerView):
    """`.card`: tabs over one member's collection. Collection is the image
    gallery (paged); Sets shows series progress; Wishlist (your own only) lists
    the cards you're waiting for, with a ping switch and a remover. Only the
    person who ran the command can use the buttons."""

    reject_text = "Only the person who ran `.card` can use these buttons."

    def __init__(self, cog, invoker_id: int, target, own: bool):
        super().__init__(invoker_id, GALLERY_VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.target = target
        self.own = own
        self.mode = "collection"
        self.page = 0
        self.pages = []
        self._render_lock = asyncio.Lock()
        self._rebuild()

    # -- items --------------------------------------------------------------

    def _rebuild(self, wish_cards=(), pings_off=False):
        self.clear_items()
        self.prev = self.next = self.pings = self.remover = None
        if self.mode == "collection" and len(self.pages) > 1:
            self.prev = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
            self.prev.callback = self._on_prev
            self.next = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= len(self.pages) - 1
            )
            self.next.callback = self._on_next
            self.add_item(self.prev)
            self.add_item(self.next)
        tabs = [("collection", "Collection"), ("sets", "Sets")] + ([("wishlist", "★ Wishlist")] if self.own else [])
        for mode, label in tabs:
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if mode == self.mode else discord.ButtonStyle.secondary,
                row=1,
            )
            button.callback = self._tab_callback(mode)
            self.add_item(button)
        if self.mode == "wishlist":
            self.pings = discord.ui.Button(
                label="Pings: OFF" if pings_off else "Pings: ON",
                style=discord.ButtonStyle.secondary if pings_off else discord.ButtonStyle.success,
                row=2,
            )
            self.pings.callback = self._on_pings
            self.add_item(self.pings)
            if wish_cards:
                self.remover = discord.ui.Select(
                    placeholder="Remove from wishlist…",
                    min_values=1,
                    max_values=len(wish_cards),
                    options=[
                        discord.SelectOption(label=_clip(c.name, 100), value=str(c.card_id), description=_clip(c.series, 100) or None)
                        for c in wish_cards
                    ],
                    row=3,
                )
                self.remover.callback = self._on_remove
                self.add_item(self.remover)

    # -- rendering -----------------------------------------------------------

    async def render(self, mode=None) -> dict:
        """What the message should show for `mode`: {content, embed, file}, and
        the items rebuilt to match."""
        if mode is not None:
            self.mode = mode
        guild = self.target.guild
        state = await self.cog._member_state(self.target)
        whose = "Your" if self.own else f"{self.target.display_name}'s"
        payload = {"content": None, "embed": None, "file": None}
        wish_cards, pings_off = (), False
        if self.mode == "sets":
            payload["embed"] = await self.cog.sets_embed(guild, state, whose)
        elif self.mode == "wishlist":
            wish_cards = await self.cog.wishlist_cards(guild, state)
            pings_off = state.wish_pings_off
            payload["embed"] = await self.cog.wishlist_embed(guild, state)
        else:
            entries = await self.cog.collection_entries(guild, state)
            if not entries:
                payload["embed"] = discord.Embed(
                    title=f"{whose} collection",
                    description=(
                        "No cards yet. Watch for drops, or try `.card daily`."
                        if self.own
                        else f"{self.target.display_name} hasn't collected any cards yet."
                    ),
                    color=embeds.EMBED_COLOR,
                )
                self.pages = []
            else:
                self.pages = [entries[i : i + GALLERY_PAGE_SIZE] for i in range(0, len(entries), GALLERY_PAGE_SIZE)]
                self.page = min(self.page, len(self.pages) - 1)
                gallery = await asyncio.to_thread(
                    imagegen.render_gallery,
                    self.pages[self.page],
                    showcase_card_ids=state.showcase_card_ids if self.page == 0 else (),
                    quantities=Counter(state.collection),
                    showcase_pool=entries,
                )
                payload["file"] = (gallery, "collection.png")
                label = f"{whose} collection"
                if len(self.pages) > 1:
                    label += f": (page {self.page + 1}/{len(self.pages)})"
                elif not self.own:
                    label = f"{self.target.display_name}'s collection:"
                else:
                    label = None
                payload["content"] = label
        self._rebuild(wish_cards, pings_off)
        return payload

    @staticmethod
    def for_send(payload: dict) -> dict:
        kwargs = {"content": payload["content"], "embed": payload["embed"]}
        if payload["file"] is not None:
            kwargs["file"] = discord.File(payload["file"][0], filename=payload["file"][1])
        return kwargs

    @staticmethod
    def for_edit(payload: dict) -> dict:
        files = [discord.File(payload["file"][0], filename=payload["file"][1])] if payload["file"] is not None else []
        return {"content": payload["content"], "embed": payload["embed"], "attachments": files}

    async def _show(self, interaction: discord.Interaction, mode=None):
        await interaction.response.defer()  # rendering can take longer than Discord's 3 seconds
        async with self._render_lock:  # the final edit always matches the final state
            payload = await self.render(mode)
            await interaction.edit_original_response(view=self, **self.for_edit(payload))

    # -- callbacks -----------------------------------------------------------

    def _tab_callback(self, mode: str):
        async def callback(interaction: discord.Interaction):
            if mode == "collection":
                self.page = 0
            await self._show(interaction, mode)

        return callback

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        self.page = min(len(self.pages) - 1, self.page + 1)
        await self._show(interaction)

    async def _on_pings(self, interaction: discord.Interaction):
        state = await self.cog._member_state(self.target)
        await self.cog.set_wish_pings(self.target, not state.wish_pings_off)
        await self._show(interaction)

    async def _on_remove(self, interaction: discord.Interaction):
        await self.cog.remove_wishes(self.target, [int(v) for v in self.remover.values])
        await self._show(interaction)


# ---------------------------------------------------------------------------
# `.card find`: search the pool, open a card
# ---------------------------------------------------------------------------


class RequestModal(discord.ui.Modal):
    """The "Request this" pop-up: which character, and from what."""

    text = discord.ui.TextInput(
        label="Character and series",
        placeholder="e.g. Frieren, Sousou no Frieren",
        max_length=REQUEST_MAX_LENGTH,
        required=True,
    )

    def __init__(self, cog, prefill: str = ""):
        super().__init__(title="Request a card", timeout=300)
        self.cog = cog
        self.text.default = prefill[:REQUEST_MAX_LENGTH]

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reply = await self.cog.submit_request(interaction.guild, interaction.user, str(self.text.value))
        await interaction.followup.send(reply, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("cardcollect: request modal error", exc_info=error)


class RequestReviewView(discord.ui.View):
    """Approve / Decline under a request in the private review channel.
    Persistent: a request can sit unreviewed across restarts."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id=REQUEST_APPROVE_ID)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.review_request(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id=REQUEST_DECLINE_ID)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.review_request(interaction, False)


class FindView(_InvokerView):
    """`.card find <query>`: a result list and a card page in one message.

    With several matches you get the list (a dropdown opens any of them); with
    exactly one you land on its page. A button switches between searching the
    whole pool and just your own cards. A card's page shows its art, rarity and
    owners, plus what you can do with it: showcase or wishlist it, and -- for
    admins -- change its rarity, swap its art, or retire it. A search that finds
    nothing offers "Request this"."""

    reject_text = "Only the person who searched can use this."

    def __init__(self, cog, member, query: str):
        super().__init__(member.id, 600)
        self.cog = cog
        self.member = member
        self.query = query
        self.scope = "pool"
        self.cards = []
        self.card = None  # the card whose page is open, or None for the list
        self.admin = False
        self.from_list = False
        self._listening = False
        self.requests_open = False

    # -- opening ---------------------------------------------------------------

    async def start(self, ctx):
        self.admin = await self.cog._is_pool_admin(self.member)
        self.cards = await self.cog.search_pool(self.member.guild, self.member, self.query, self.scope)
        if len(self.cards) == 1:
            self.card = self.cards[0]
        payload = await self.render()
        self.message = await ctx.send(view=self, **self._for_send(payload))

    @staticmethod
    def _for_send(payload: dict) -> dict:
        kwargs = {"embed": payload["embed"]}
        if payload["file"] is not None:
            kwargs["file"] = payload["file"]
        return kwargs

    @staticmethod
    def _for_edit(payload: dict) -> dict:
        return {"embed": payload["embed"], "attachments": [payload["file"]] if payload["file"] is not None else []}

    async def render(self) -> dict:
        """The embed (and art file) for the current state; items rebuilt to match."""
        guild = self.member.guild
        self.clear_items()
        self.requests_open = bool(await self.cog.config.guild(guild).request_channel_id())
        if self.card is not None:
            embed, file, quantity, _owners, state = await self.cog.card_detail(guild, self.member, self.card)
            self._build_detail(quantity, state)
            return {"embed": embed, "file": file}
        embed = await self._list_embed()
        self._build_list()
        return {"embed": embed, "file": None}

    async def _list_embed(self) -> discord.Embed:
        state = await self.cog._member_state(self.member)
        owned = set(state.collection)
        shown = discord.utils.escape_mentions(discord.utils.escape_markdown(self.query))[:60]
        scope = "your cards" if self.scope == "mine" else "the pool"
        embed = discord.Embed(color=embeds.EMBED_COLOR)
        if not self.cards:
            embed.title = f"Nothing in {scope} matches “{shown}”"
            if self.scope == "pool":
                embed.description = (
                    "Not in the pool yet? Press **Request this** and an admin will take a look."
                    if self.requests_open
                    else "It isn't in the pool yet."
                )
            else:
                embed.description = "None of your cards match. Switch to the whole pool with the button."
            return embed
        embed.title = f"{len(self.cards)} match{'es' if len(self.cards) != 1 else ''} for “{shown}” in {scope}"
        lines = []
        for c in self.cards[:25]:
            mark = "✅" if c.card_id in owned else ("★" if c.card_id in state.wishlist else "⬜")
            tail = " · retired" if c.retired else ""
            lines.append(f"{mark} {embeds.RARITY_EMOJI.get(c.rarity, '')} **{_clip(discord.utils.escape_markdown(c.name), 60)}**"
                         f" — {_clip(discord.utils.escape_markdown(c.series or '—'), 50)} `#{c.card_id}`{tail}")
        if len(self.cards) > 25:
            lines.append(f"…and {len(self.cards) - 25} more. Narrow the search to see them.")
        embed.description = "\n".join(lines)
        embed.set_footer(text="✅ owned · ★ wishlisted · open one with the dropdown")
        return embed

    def _build_list(self):
        if self.cards:
            self.opener = discord.ui.Select(
                placeholder="Open a card…",
                options=[
                    discord.SelectOption(
                        label=_clip(c.name, 100), value=str(c.card_id), description=_clip(c.series, 100) or None,
                        emoji=embeds.RARITY_EMOJI.get(c.rarity),
                    )
                    for c in self.cards[:25]
                ],
            )
            self.opener.callback = self._on_open
            self.add_item(self.opener)
        self.scope_button = discord.ui.Button(
            label="Search the whole pool" if self.scope == "mine" else "Only my cards", style=discord.ButtonStyle.secondary, row=1
        )
        self.scope_button.callback = self._on_scope
        self.add_item(self.scope_button)
        if self.scope == "pool" and self.requests_open:
            label = "Request this" if not self.cards else "Not here? Request it"
            self.request_button = discord.ui.Button(label=label, style=discord.ButtonStyle.success, row=1)
            self.request_button.callback = self._on_request
            self.add_item(self.request_button)

    def _build_detail(self, quantity: int, state):
        card = self.card
        if self.from_list:
            self.back = discord.ui.Button(label="◀ Results", style=discord.ButtonStyle.secondary, row=1)
            self.back.callback = self._on_back
            self.add_item(self.back)
        if quantity:
            on_showcase = card.card_id in state.showcase_card_ids
            self.personal = discord.ui.Button(
                label="Remove from showcase" if on_showcase else "Add to showcase", style=discord.ButtonStyle.secondary, row=1
            )
            self.personal.callback = self._on_showcase
            self.add_item(self.personal)
        else:
            wished = card.card_id in state.wishlist
            self.personal = discord.ui.Button(
                label="★ On your wishlist (remove)" if wished else "☆ Add to wishlist",
                style=discord.ButtonStyle.primary if not wished else discord.ButtonStyle.secondary,
                row=1,
            )
            self.personal.callback = self._on_wish
            self.add_item(self.personal)
        if self.requests_open:
            self.request_button = discord.ui.Button(label="Wrong one? Request another", style=discord.ButtonStyle.secondary, row=1)
            self.request_button.callback = self._on_request
            self.add_item(self.request_button)
        if self.admin:
            self.rarity = discord.ui.Select(
                placeholder="Change rarity…",
                options=[
                    discord.SelectOption(label=t.title(), value=t, emoji=embeds.RARITY_EMOJI[t], default=(t == card.rarity))
                    for t in TIERS
                ],
                row=2,
            )
            self.rarity.callback = self._on_rarity
            self.add_item(self.rarity)
            self.replace_art = discord.ui.Button(label="Replace art", style=discord.ButtonStyle.secondary, row=3)
            self.replace_art.callback = self._on_replace_art
            self.add_item(self.replace_art)
            self.retire = discord.ui.Button(
                label="Restore to drops" if card.retired else "Retire from drops",
                style=discord.ButtonStyle.success if card.retired else discord.ButtonStyle.danger,
                row=3,
            )
            self.retire.callback = self._on_retire
            self.add_item(self.retire)

    # -- shared refresh ------------------------------------------------------------

    async def _refresh(self, interaction: discord.Interaction, notice: str = None):
        """Re-render after a change and edit the message (and say what happened, privately)."""
        if self.card is not None:  # pick up the card's latest rarity / retired state
            fresh = await self.cog._card_by_id(self.member.guild, self.card.card_id)
            if fresh is not None:
                self.card = fresh
        payload = await self.render()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self, **self._for_edit(payload))
        else:
            await interaction.response.edit_message(view=self, **self._for_edit(payload))
        if notice:
            await interaction.followup.send(notice, ephemeral=True)

    # -- callbacks -----------------------------------------------------------------

    async def _on_open(self, interaction: discord.Interaction):
        wanted = int(self.opener.values[0])
        self.card = next((c for c in self.cards if c.card_id == wanted), None)
        self.from_list = True
        await interaction.response.defer()
        await self._refresh(interaction)

    async def _on_back(self, interaction: discord.Interaction):
        self.card = None
        await interaction.response.defer()
        await self._refresh(interaction)

    async def _on_scope(self, interaction: discord.Interaction):
        self.scope = "mine" if self.scope == "pool" else "pool"
        self.cards = await self.cog.search_pool(self.member.guild, self.member, self.query, self.scope)
        self.card = None
        await interaction.response.defer()
        await self._refresh(interaction)

    async def _on_request(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RequestModal(self.cog, self.query))

    async def _on_showcase(self, interaction: discord.Interaction):
        text = await self.cog.toggle_showcase(self.member.guild, self.member, self.card.card_id)
        await interaction.response.defer()
        await self._refresh(interaction, text)

    async def _on_wish(self, interaction: discord.Interaction):
        text = await self.cog.toggle_wish(self.member.guild, self.member, self.card.card_id)
        await interaction.response.defer()
        await self._refresh(interaction, text)

    async def _on_rarity(self, interaction: discord.Interaction):
        tier = self.rarity.values[0]
        if tier == self.card.rarity:
            await interaction.response.send_message(f"**{self.card.name}** is already {tier}.", ephemeral=True)
            return
        try:
            card = await self.cog.apply_card_rarity(interaction.guild, self.card.card_id, tier)
        except KeyError:
            await interaction.response.send_message("That card is no longer in the pool.", ephemeral=True)
            return
        self.card = card
        await interaction.response.defer()
        await self._refresh(
            interaction,
            f"**{card.name}** is now **{tier}**. This changes how it drops from now on and how it looks in "
            "everyone's collection; sell tokens already issued keep the value they were made with.",
        )

    async def _on_retire(self, interaction: discord.Interaction):
        retire = not self.card.retired
        try:
            card = await self.cog.set_card_retired(interaction.guild, self.card.card_id, retire)
        except KeyError:
            await interaction.response.send_message("That card is no longer in the pool.", ephemeral=True)
            return
        self.card = card
        await interaction.response.defer()
        await self._refresh(
            interaction,
            f"**{card.name}** " + ("won't drop any more. Owners keep their copies." if retire else "is back in the drops."),
        )

    async def _on_replace_art(self, interaction: discord.Interaction):
        if self._listening:
            await interaction.response.send_message("Already waiting for an image from you.", ephemeral=True)
            return
        self._listening = True
        await interaction.response.send_message(
            f"Send the new picture for **{self.card.name}** in this channel as an image upload "
            f"(I'll wait {ART_UPLOAD_WAIT_SECONDS}s). Only the picture changes; owners keep their copies.",
            ephemeral=True,
        )
        try:
            message = await self.cog.wait_for_upload(interaction.channel.id, interaction.user.id, ART_UPLOAD_WAIT_SECONDS)
            if message is None:
                await interaction.followup.send("No image arrived, so the art was left as it was.", ephemeral=True)
                return
            data, problem = await self.cog.read_uploaded_art(message)
            if data is None:
                await interaction.followup.send(f"{problem} The art was left as it was.", ephemeral=True)
                return
            try:
                await self.cog.replace_card_art(interaction.guild, self.card.card_id, data)
            except KeyError:
                await interaction.followup.send("That card is no longer in the pool.", ephemeral=True)
                return
            payload = await self.render()
            if self.message is not None:
                await self.message.edit(view=self, **self._for_edit(payload))
            await interaction.followup.send(f"New art saved for **{self.card.name}**.", ephemeral=True)
        finally:
            self._listening = False


# ---------------------------------------------------------------------------
# `.card sell`
# ---------------------------------------------------------------------------


class SellView(_InvokerView):
    """`.card sell`: your sell tokens, sold all at once or one at a time."""

    reject_text = "Only the person who ran `.card sell` can use this."

    def __init__(self, cog, member):
        super().__init__(member.id, 300)
        self.cog = cog
        self.member = member
        self._busy = False

    async def start(self, ctx):
        embed = await self._build()
        if embed is None:
            await ctx.send("No sell tokens on hand. Winning a card you're already at the copy limit for gives you one.")
            return
        self.message = await ctx.send(embed=embed, view=self)

    async def _build(self):
        """The token summary embed (items rebuilt to match), or None if there are none."""
        guild = self.member.guild
        state = await self.cog._member_state(self.member)
        tokens = state.sell_tokens
        self.clear_items()
        if not tokens:
            return None
        prices = await self.cog.config.guild(guild).sell_prices()
        values = [engine.sell_price(t.rarity, prices) for t in tokens]
        counts = Counter(t.rarity for t in tokens)
        embed = discord.Embed(title="Your sell tokens", color=embeds.EMBED_COLOR)
        embed.description = "\n".join(
            f"{embeds.RARITY_EMOJI[t]} {t.title()} × {counts[t]} — {counts[t] * prices.get(t, 0):,}" for t in reversed(TIERS) if counts[t]
        )
        embed.set_footer(text=f"{len(tokens)} token{'s' if len(tokens) != 1 else ''} worth {sum(values):,} wondercoin")
        sell_all = discord.ui.Button(label=f"Sell all ({len(tokens)} · {sum(values):,})", style=discord.ButtonStyle.success)
        sell_all.callback = self._on_sell_all
        self.add_item(sell_all)
        self.sell_all = sell_all
        names = {}
        for t in tokens[:25]:
            card = await self.cog._card_by_id(guild, t.card_id)
            names[t.token_id] = card.name if card is not None else f"card #{t.card_id}"
        self.one = discord.ui.Select(
            placeholder="Sell just one…",
            options=[
                discord.SelectOption(
                    label=_clip(f"{t.rarity.title()} — {names[t.token_id]}", 100),
                    value=t.token_id,
                    description=f"{engine.sell_price(t.rarity, prices):,} wondercoin",
                    emoji=embeds.RARITY_EMOJI[t.rarity],
                )
                for t in tokens[:25]
            ],
            row=1,
        )
        self.one.callback = self._on_sell_one
        self.add_item(self.one)
        return embed

    async def _sell(self, interaction: discord.Interaction, token_ids):
        if self._busy:
            await interaction.response.send_message("One moment, still selling.", ephemeral=True)
            return
        self._busy = True
        try:
            await interaction.response.defer()
            count, total = await self.cog.sell_tokens(interaction.guild, self.member, token_ids)
            embed = await self._build()
            note = f"Sold {count} token{'s' if count != 1 else ''} for **{total:,}** wondercoin." if count else "Those tokens were already gone."
            if embed is None:
                done = discord.Embed(title="Sold", description=note, color=embeds.EMBED_COLOR)
                await interaction.edit_original_response(embed=done, view=None)
                self.stop()
            else:
                await interaction.edit_original_response(embed=embed, view=self)
                await interaction.followup.send(note, ephemeral=True)
        finally:
            self._busy = False

    async def _on_sell_all(self, interaction: discord.Interaction):
        await self._sell(interaction, None)

    async def _on_sell_one(self, interaction: discord.Interaction):
        await self._sell(interaction, list(self.one.values))
