"""Wonderland pixel canvas: everything a member or mod sees. Buttons, modals and ephemeral replies only.

The cog hands in a CanvasService (all money and storage logic lives there) and a couple of callbacks;
nothing here talks to the bank or the database directly except through the service.

Flow for a member: the hub's "Pixel Canvas" entry opens `MainView` (ephemeral). Paint / Paint area open a
modal, then (if no hex colour was typed) a pastel dropdown, then a confirm screen with the price. Confirm
re-checks the price under the service's lock; if it moved, the screen is redrawn with the new price and
nobody is charged.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Awaitable, Callable, Optional

import discord

from . import canvas_render
from .canvas_service import CanvasService, canvas_color_name, coins, coords_text, esc
from .canvas_store import BASE_PRICE, GRID_H, GRID_W, MAX_TEXT, clean_text, in_grid, parse_color, parse_xy, rect_cells

log = logging.getLogger("red.evac-cogs.gamble.canvas")

VIEW_TIMEOUT = 900
COLOR = discord.Color.from_rgb(255, 183, 197)
NOAMENTIONS = discord.AllowedMentions.none()


def _file(data: bytes, name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=name)


def _swatch(color: Optional[str]) -> discord.Color:
    return discord.Color(int(color.lstrip("#"), 16)) if color else COLOR


def _when(ts: float) -> str:
    return f"<t:{int(ts)}:R>"


def parse_user_id(text: str) -> Optional[int]:
    m = re.search(r"\d{15,20}", text or "")
    return int(m.group(0)) if m else None


# ============================================================================ member side


def main_embed(service: CanvasService, note: str = "") -> discord.Embed:
    embed = discord.Embed(
        title="🎨 Wonderland Canvas",
        description=(f"{note}\n\n" if note else "") +
                    f"A shared **{GRID_W}×{GRID_H}** pixel canvas. A blank pixel costs **{coins(BASE_PRICE)}**; every "
                    f"paint makes that pixel **1.2×** dearer and the previous owner gets nothing. All coins spent are "
                    f"destroyed.\nCoordinates are `x,y`, from `0,0` at the top left.",
        color=COLOR)
    embed.set_image(url="attachment://canvas.png")
    embed.set_footer(text=f"{service.store.painted_count():,} of {GRID_W * GRID_H:,} pixels painted")
    return embed


class MainView(discord.ui.View):
    def __init__(self, service: CanvasService, member):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.service, self.member = service, member

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("Open your own canvas from the #wondercasino menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Zoom", emoji="🔍", style=discord.ButtonStyle.secondary, row=0)
    async def zoom_button(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(XYModal("Zoom in", "Centre (x,y)", "e.g. 50,25", self._zoom))

    @discord.ui.button(label="Inspect", emoji="🔎", style=discord.ButtonStyle.secondary, row=0)
    async def inspect_button(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(XYModal("Inspect a pixel", "Pixel (x,y)", "e.g. 42,17", self._inspect))

    @discord.ui.button(label="Paint", emoji="🖌️", style=discord.ButtonStyle.primary, row=0)
    async def paint_button(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(PaintModal(self.service, self.member, area=False))

    @discord.ui.button(label="Paint area", emoji="🧱", style=discord.ButtonStyle.primary, row=0)
    async def area_button(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(PaintModal(self.service, self.member, area=True))

    @discord.ui.button(label="Leaderboard", emoji="🏆", style=discord.ButtonStyle.secondary, row=0)
    async def leaderboard_button(self, interaction: discord.Interaction, button):
        await interaction.response.send_message(embed=leaderboard_embed(self.service.leaderboard()), ephemeral=True)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_button(self, interaction: discord.Interaction, button):
        await interaction.response.defer()
        data = await self.service.image()
        await interaction.edit_original_response(embed=main_embed(self.service), attachments=[_file(data, "canvas.png")],
                                                 view=MainView(self.service, self.member))

    async def _zoom(self, interaction: discord.Interaction, xy: tuple):
        cx, cy = xy
        if not in_grid(cx, cy):
            await interaction.response.send_message(f"x must be 0–{GRID_W - 1} and y 0–{GRID_H - 1}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        data = await self.service.zoom_image(cx, cy)
        x0, y0 = canvas_render.zoom_window(cx, cy)
        embed = discord.Embed(title=f"🔍 Close-up around ({cx},{cy})", color=COLOR,
                              description=f"Showing x {x0}–{x0 + 9}, y {y0}–{y0 + 9}. Each cell shows what it costs to paint "
                                          f"it now (blank = {coins(BASE_PRICE)}). Your centre is outlined in red.")
        embed.set_image(url="attachment://zoom.png")
        await interaction.followup.send(embed=embed, file=_file(data, "zoom.png"), ephemeral=True)

    async def _inspect(self, interaction: discord.Interaction, xy: tuple):
        x, y = xy
        if not in_grid(x, y):
            await interaction.response.send_message(f"x must be 0–{GRID_W - 1} and y 0–{GRID_H - 1}.", ephemeral=True)
            return
        await interaction.response.send_message(embed=inspect_embed(self.service, x, y), ephemeral=True)


def inspect_embed(service: CanvasService, x: int, y: int) -> discord.Embed:
    pixel = service.store.get_pixel(x, y)
    if pixel is None:
        return discord.Embed(title=f"🔎 Pixel ({x},{y})", color=COLOR,
                             description=f"Blank. It costs **{coins(BASE_PRICE)}** to claim.")
    embed = discord.Embed(title=f"🔎 Pixel ({x},{y})", color=_swatch(pixel.color))
    embed.add_field(name="Colour", value=canvas_color_name(pixel.color))
    embed.add_field(name="Owner", value=f"<@{pixel.owner}>")
    embed.add_field(name="Costs to paint now", value=coins(pixel.price))
    embed.add_field(name="Last painted", value=_when(pixel.painted_at))
    embed.add_field(name="Text", value=esc(pixel.text) if pixel.text else "*none*", inline=False)
    return embed


def leaderboard_embed(data: dict) -> discord.Embed:
    def spenders(rows):
        return "\n".join(f"**{i}.** <@{uid}> — {coins(spent)} ({n} px)" for i, (uid, spent, n) in enumerate(rows, 1)) \
            or "*nobody yet*"

    pixels = "\n".join(f"**{i}.** <@{uid}> — {n} px" for i, (uid, n) in enumerate(data["pixels"], 1)) or "*nobody yet*"
    embed = discord.Embed(title="🏆 Canvas leaderboard", color=COLOR)
    embed.add_field(name="Top spenders, last 7 days", value=spenders(data["week"]), inline=False)
    embed.add_field(name="Top spenders, all time", value=spenders(data["all_time"]), inline=False)
    embed.add_field(name="Most pixels owned", value=pixels, inline=False)
    embed.set_footer(text=f"{coins(data['burned_total'])} destroyed so far · {data['painted']:,} pixels painted")
    return embed


class XYModal(discord.ui.Modal):
    """One box for 'x,y'. `on_ok(interaction, (x, y))` runs when it parses."""

    def __init__(self, title: str, label: str, placeholder: str, on_ok: Callable[..., Awaitable[None]]):
        super().__init__(title=title, timeout=VIEW_TIMEOUT)
        self.on_ok = on_ok
        self.xy = discord.ui.TextInput(label=label, placeholder=placeholder, max_length=12)
        self.add_item(self.xy)

    async def on_submit(self, interaction: discord.Interaction):
        parsed = parse_xy(self.xy.value)
        if parsed is None:
            await interaction.response.send_message("I need two numbers like `42,17`.", ephemeral=True)
            return
        await self.on_ok(interaction, parsed)


class PaintModal(discord.ui.Modal):
    def __init__(self, service: CanvasService, member, area: bool):
        super().__init__(title="Paint an area (up to 10×10)" if area else "Paint a pixel", timeout=VIEW_TIMEOUT)
        self.service, self.member, self.area = service, member, area
        if area:
            self.first = discord.ui.TextInput(label="Top-left corner (x,y)", placeholder="e.g. 40,10", max_length=12)
            self.second = discord.ui.TextInput(label="Bottom-right corner (x,y)", placeholder="e.g. 49,19", max_length=12)
            self.add_item(self.first)
            self.add_item(self.second)
        else:
            self.first = discord.ui.TextInput(label="Pixel (x,y)", placeholder="e.g. 42,17", max_length=12)
            self.second = None
            self.add_item(self.first)
        self.color = discord.ui.TextInput(label="Hex colour (leave empty to pick a pastel)", placeholder="#FFB7C5",
                                          required=False, max_length=7)
        self.text = discord.ui.TextInput(label="Text (optional, shown when someone inspects)", required=False,
                                         max_length=MAX_TEXT)
        self.add_item(self.color)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        a = parse_xy(self.first.value)
        b = parse_xy(self.second.value) if self.second is not None else a
        if a is None or b is None:
            await interaction.response.send_message("I need coordinates like `42,17`.", ephemeral=True)
            return
        cells = rect_cells(*a, *b)
        bad = self.service.validate_cells(cells)
        if bad:
            await interaction.response.send_message(bad, ephemeral=True)
            return
        block = await self.service.blocked(self.member)
        if block:
            await interaction.response.send_message(self.service.BLOCK_MESSAGES[block], ephemeral=True)
            return
        color = None
        if self.color.value.strip():
            color = parse_color(self.color.value)
            if color is None:
                await interaction.response.send_message("That isn't a hex colour. Try `#FFB7C5` or `fb7`, or leave it "
                                                        "empty to pick a pastel.", ephemeral=True)
                return
        draft = Draft(cells, clean_text(self.text.value), color)
        if color is None:
            view = PaletteView(self.service, self.member, draft)
            await interaction.response.send_message(embed=palette_embed(draft), view=view, ephemeral=True)
            return
        await send_confirm(interaction, self.service, self.member, draft)


class Draft:
    def __init__(self, cells: list, text: str, color: Optional[str]):
        self.cells, self.text, self.color = cells, text, color


def palette_embed(draft: Draft) -> discord.Embed:
    return discord.Embed(title="🎨 Pick a colour", color=COLOR,
                         description=f"For {coords_text(draft.cells)}. Pick a pastel below, or cancel and use "
                                     f"Paint again with a hex colour.")


class PaletteView(discord.ui.View):
    def __init__(self, service, member, draft: Draft):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.service, self.member, self.draft = service, member, draft
        select = discord.ui.Select(placeholder="Pastel palette…", options=[
            discord.SelectOption(label=name, value=hex_, description=hex_) for name, hex_ in canvas_render.PASTELS])
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction):
        self.draft.color = parse_color(interaction.data["values"][0])
        await send_confirm(interaction, self.service, self.member, self.draft, edit=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(content="Cancelled. Nothing was charged.", embed=None, view=None)


async def confirm_embed(service: CanvasService, member, draft: Draft, prices: tuple, note: str = "") -> discord.Embed:
    total = sum(prices)
    n = len(draft.cells)
    embed = discord.Embed(title="🖌️ Confirm your paint", color=_swatch(draft.color))
    if note:
        embed.description = note
    embed.add_field(name="Where", value=coords_text(draft.cells) + (f" · {n} pixels" if n > 1 else ""))
    embed.add_field(name="Colour", value=canvas_color_name(draft.color))
    embed.add_field(name="Price", value=f"**{coins(total)}**" + (
        f"\n{sum(1 for p in prices if p == BASE_PRICE)} blank at {coins(BASE_PRICE)}, "
        f"{sum(1 for p in prices if p != BASE_PRICE)} owned" if n > 1 else ""))
    embed.add_field(name="Text", value=esc(draft.text) if draft.text else "*none*", inline=False)
    balance = await service.balance(member)
    embed.set_footer(text=f"Your balance: {coins(balance)} · the coins are destroyed, and the previous owner gets nothing")
    if balance < total:
        embed.add_field(name="Not enough coins", value=f"You're short by {coins(total - balance)}.", inline=False)
    return embed


async def send_confirm(interaction: discord.Interaction, service, member, draft: Draft, edit: bool = False):
    prices = service.store.quote(draft.cells)
    embed = await confirm_embed(service, member, draft, prices)
    view = ConfirmView(service, member, draft, prices, can_afford=await service.balance(member) >= sum(prices))
    if edit:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ConfirmView(discord.ui.View):
    def __init__(self, service: CanvasService, member, draft: Draft, prices: tuple, can_afford: bool = True):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.service, self.member, self.draft, self.prices = service, member, draft, prices
        self.working = False
        self.confirm.disabled = not can_afford

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("That isn't your paint.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button):
        if self.working:   # a double click must never paint (or charge) twice
            await interaction.response.defer()
            return
        self.working = True
        await interaction.response.defer()
        try:
            outcome = await self.service.paint(self.member, self.draft.cells, self.draft.color, self.draft.text, self.prices)
        except Exception:
            log.exception("canvas: paint crashed")
            self.working = False
            await interaction.edit_original_response(
                embed=discord.Embed(title="Something went wrong", description="Nothing was painted. If you were "
                                    "charged, tell a mod.", color=discord.Color.red()), view=None)
            return
        if outcome.status == "ok":
            interaction.client.dispatch("wonderland_quest", self.member, "canvas", 1)   # daily quests (wonderland)
            data = await self.service.image()
            embed = discord.Embed(title="✅ Painted", color=_swatch(self.draft.color),
                                  description=f"{coords_text(self.draft.cells)} is yours for {coins(outcome.total)}."
                                              + (f" It now costs **{coins(outcome.next_price)}** to take it from you."
                                                 if outcome.next_price else ""))
            embed.set_image(url="attachment://canvas.png")
            await interaction.edit_original_response(embed=embed, view=None, attachments=[_file(data, "canvas.png")])
            return
        if outcome.status == "price_changed":
            draft = self.draft
            note = (f"⚠️ **The price moved** from {coins(sum(self.prices))} to {coins(outcome.total)} while you were "
                    f"deciding. **You were not charged.** Confirm again if you still want it.")
            embed = await confirm_embed(self.service, self.member, draft, outcome.prices, note)
            view = ConfirmView(self.service, self.member, draft, outcome.prices,
                               can_afford=await self.service.balance(self.member) >= outcome.total)
            await interaction.edit_original_response(embed=embed, view=view)
            return
        text = outcome.message
        if outcome.status == "insufficient" and outcome.balance is not None:
            text += f" You have {coins(outcome.balance)}. You weren't charged."
        await interaction.edit_original_response(
            embed=discord.Embed(title="Not painted", description=text, color=discord.Color.red()), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button):
        self.working = True
        await interaction.response.edit_message(content="Cancelled. Nothing was charged.", embed=None, view=None)


def weekly_embed(report: dict) -> discord.Embed:
    """The Sunday post for #cuddle. No pixel text ever goes in it."""
    embed = discord.Embed(title="🎨 Wonderland Canvas — the week in paint", color=COLOR)
    embed.set_image(url="attachment://canvas.png")
    top, priciest = report["top_spender"], report["priciest"]
    if priciest:
        embed.add_field(name="Most expensive paint", value=f"({priciest['x']},{priciest['y']}) for "
                        f"**{coins(priciest['paid'])}**, by <@{priciest['user_id']}>")
    if top:
        embed.add_field(name="Top spender", value=f"<@{top[0]}> burned **{coins(top[1])}** on {top[2]} pixels")
    embed.add_field(name="The week", inline=False, value=f"{coins(report['burned'])} destroyed · {report['actions']} paints "
                    f"by {report['painters']} people. Open **Pixel Canvas** in the #wondercasino menu to join in.")
    return embed


def live_embed(service: CanvasService) -> discord.Embed:
    """The pinned, constantly refreshed canvas in #cuddle. No pixel text, ever."""
    embed = discord.Embed(
        title="🎨 Wonderland Canvas — live", color=COLOR,
        description=f"Updated <t:{int(service.clock())}:R>. Every paint makes that pixel 1.2× dearer, the previous owner "
                    f"gets nothing, and the coins are destroyed. A blank pixel is {coins(BASE_PRICE)}.\n"
                    f"Press **Open canvas** to zoom, inspect and paint. Only you see it.")
    embed.set_image(url="attachment://canvas.png")
    embed.set_footer(text=f"{service.store.painted_count():,} of {GRID_W * GRID_H:,} pixels painted · "
                          f"{coins(service.store.burned())} destroyed so far")
    return embed


class LiveView(discord.ui.View):
    """The button under the live message. Persistent: fixed custom_id, registered once at cog load."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open canvas", emoji="🎨", style=discord.ButtonStyle.primary,
                       custom_id="wonderland:canvas:open")
    async def open(self, interaction: discord.Interaction, button):
        service = self.cog._canvas(interaction.guild)
        try:
            service.count_open()
        except Exception:
            log.exception("canvas: counting an Open canvas press failed")
        await open_canvas(interaction, service)


async def open_canvas(interaction: discord.Interaction, service: CanvasService) -> None:
    """The hub entry point: the canvas, privately, with its buttons."""
    await interaction.response.defer(ephemeral=True)
    data = await service.image()
    await interaction.followup.send(embed=main_embed(service), file=_file(data, "canvas.png"),
                                    view=MainView(service, interaction.user), ephemeral=True)


# ============================================================================ mod side


def _day(d) -> str:
    return d.strftime("%a %b %d").replace(" 0", " ")


def mod_embed(service: CanvasService, note: str = "") -> discord.Embed:
    u = service.usage()
    embed = discord.Embed(title="🎨 Canvas — mod panel", color=COLOR, description=note or None)
    embed.add_field(name="Paints per day (actions · pixels)", inline=False, value="\n".join(
        f"{_day(d)}: {a} · {p}" for d, a, p in u["per_day"]))
    embed.add_field(name="Distinct painters", value=f"{u['painters_7d']} in the last 7 days\n{u['painters_all']} all time")
    now_weeks = u["burned_weeks"]
    labels = ["22–28 days ago", "15–21 days ago", "8–14 days ago", "last 7 days"]
    embed.add_field(name="Coins burned per week", value="\n".join(
        f"{label}: {coins(c)}" for label, (_, c) in zip(labels, now_weeks)) + f"\n**Total: {coins(u['burned_total'])}**")
    embed.add_field(name="Canvas", value=f"{u['painted']:,} of {GRID_W * GRID_H:,} pixels painted\n"
                                         f"{len(u['banned'])} painter(s) banned\n"
                                         f"{u['live_opens']:,} presses of the live message's Open canvas button")
    verdict = (f"Review on <t:{int(u['review_at'])}:D> (3 weeks after launch): compare coins burned with `.wld economy`."
               if not u["review_due"] else
               "**Review is due.** Compare coins burned with `.wld economy`. " + (
                   f"Only {u['painters_all']} people have painted: **propose retiring it.**" if u["retire_suggested"]
                   else f"{u['painters_all']} people have painted, so it stays."))
    embed.add_field(name="Review", value=verdict, inline=False)
    if u["banned"]:
        embed.add_field(name="Banned painters", value=" ".join(f"<@{i}>" for i in u["banned"][:20]), inline=False)
    return embed


class ModPanelView(discord.ui.View):
    def __init__(self, service: CanvasService, mod, is_mod: Callable[[object], Awaitable[bool]],
                 repost: Optional[Callable[..., Awaitable[str]]] = None):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.service, self.mod, self.is_mod, self.repost_live = service, mod, is_mod, repost

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.mod.id or not await self.is_mod(interaction.user):
            await interaction.response.send_message("Mods only.", ephemeral=True)
            return False
        return True

    def _again(self) -> "ModPanelView":
        return ModPanelView(self.service, self.mod, self.is_mod, self.repost_live)

    @discord.ui.button(label="Clear a pixel's text", style=discord.ButtonStyle.secondary, row=0)
    async def clear_text(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(XYModal("Clear a pixel's text", "Pixel (x,y)", "e.g. 42,17", self._clear))

    @discord.ui.button(label="Wipe a member", style=discord.ButtonStyle.danger, row=0)
    async def wipe(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(MemberModal("Wipe a member's pixels", self._wipe_preview))

    @discord.ui.button(label="Ban painter", style=discord.ButtonStyle.danger, row=1)
    async def ban(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(MemberModal("Ban a painter", self._ban))

    @discord.ui.button(label="Unban painter", style=discord.ButtonStyle.secondary, row=1)
    async def unban(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(MemberModal("Unban a painter", self._unban))

    @discord.ui.button(label="Reset canvas", style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, button):
        p = self.service.reset_preview()
        embed = discord.Embed(title="⚠️ Reset the whole canvas? (dry run)", color=discord.Color.red(), description=(
            f"This blanks **{p['pixels']:,}** painted pixels owned by **{p['owners']}** people (worth "
            f"{coins(p['worth'])} to repaint). Every pixel goes back to {coins(BASE_PRICE)}. Coins are **not** refunded; "
            f"the record of coins burned and the leaderboards stay. Nothing has changed yet."))
        await interaction.response.edit_message(embed=embed, view=ConfirmDestructive(self, "Yes, reset everything",
                                                                                     self._do_reset))

    @discord.ui.button(label="Repost live canvas", style=discord.ButtonStyle.secondary, row=1)
    async def repost(self, interaction: discord.Interaction, button):
        if self.repost_live is None:
            await interaction.response.send_message("Reposting isn't wired up here.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            note = await self.repost_live(interaction)
        except Exception:
            log.exception("canvas: reposting the live message failed")
            note = "Reposting failed; see the bot log."
        await interaction.edit_original_response(embed=mod_embed(self.service, note), view=self._again())

    @discord.ui.button(label="Refresh counts", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(embed=mod_embed(self.service), view=self._again())

    async def _show(self, interaction: discord.Interaction, note: str):
        await interaction.response.edit_message(embed=mod_embed(self.service, note), view=self._again())

    async def _clear(self, interaction, xy):
        await self._show(interaction, await self.service.mod_clear_text(interaction.user, *xy))

    async def _ban(self, interaction, user_id):
        await self._show(interaction, await self.service.mod_ban(interaction.user, user_id))

    async def _unban(self, interaction, user_id):
        await self._show(interaction, await self.service.mod_unban(interaction.user, user_id))

    async def _wipe_preview(self, interaction, user_id):
        p = self.service.wipe_preview(user_id)
        if not p["count"]:
            await self._show(interaction, f"<@{user_id}> doesn't own any pixels.")
            return
        embed = discord.Embed(title="⚠️ Wipe this member's pixels? (dry run)", color=discord.Color.red(), description=(
            f"<@{user_id}> owns **{p['count']}** pixels, worth {coins(p['worth'])} to repaint. Wiping blanks them "
            f"(back to {coins(BASE_PRICE)}). Coins are **not** refunded. Nothing has changed yet."))

        async def go(inter):
            return await self.service.mod_wipe(inter.user, user_id)

        await interaction.response.edit_message(embed=embed, view=ConfirmDestructive(self, "Yes, wipe them", go))

    async def _do_reset(self, interaction):
        return await self.service.mod_reset(interaction.user)


class ConfirmDestructive(discord.ui.View):
    """The second step of every destructive mod action: the dry-run screen above, then this."""

    def __init__(self, panel: ModPanelView, label: str, action: Callable[..., Awaitable[str]]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.panel, self.action, self.done = panel, action, False
        self.go.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.panel.interaction_check(interaction)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, button):
        if self.done:
            await interaction.response.defer()
            return
        self.done = True
        note = await self.action(interaction)
        await interaction.response.edit_message(embed=mod_embed(self.panel.service, note), view=self.panel._again())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button):
        self.done = True
        await interaction.response.edit_message(embed=mod_embed(self.panel.service, "Cancelled. Nothing changed."),
                                                view=self.panel._again())


class MemberModal(discord.ui.Modal):
    def __init__(self, title: str, on_ok: Callable[..., Awaitable[None]]):
        super().__init__(title=title, timeout=VIEW_TIMEOUT)
        self.on_ok = on_ok
        self.who = discord.ui.TextInput(label="Member (mention or ID)", placeholder="@someone or 1234567890", max_length=40)
        self.add_item(self.who)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = parse_user_id(self.who.value)
        if user_id is None:
            await interaction.response.send_message("Paste a mention or a numeric user ID.", ephemeral=True)
            return
        await self.on_ok(interaction, user_id)


class OpenModPanel(discord.ui.View):
    """The one-line stub `.gamblehub canvas` leaves in the channel (a prefix command can't reply privately)."""

    def __init__(self, service: CanvasService, is_mod: Callable[[object], Awaitable[bool]],
                 repost: Optional[Callable[..., Awaitable[str]]] = None):
        super().__init__(timeout=300)
        self.service, self.is_mod, self.repost = service, is_mod, repost
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Open canvas mod panel (only you will see it)", emoji="🎨", style=discord.ButtonStyle.primary)
    async def open(self, interaction: discord.Interaction, button):
        if not await self.is_mod(interaction.user):
            await interaction.response.send_message("Mods only.", ephemeral=True)
            return
        await interaction.response.send_message(embed=mod_embed(self.service),
                                                view=ModPanelView(self.service, interaction.user, self.is_mod,
                                                                  self.repost),
                                                ephemeral=True)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass
