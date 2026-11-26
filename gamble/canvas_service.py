"""Wonderland pixel canvas: the money and moderation flow. No discord imports (the cog hands in the
bank module, the Lurker check and the mod-log poster), so every path here is testable with fakes.

The rule that matters: coins are taken with bank.withdraw_credits BEFORE the pixels are saved, and
refunded if the save fails. Everything that checks a price, takes coins and saves runs under one lock,
so two people painting the same pixel can never both pay the old price.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Sequence

from . import canvas_render
from .canvas_store import (BASE_PRICE, GRID_H, GRID_W, MAX_AREA_SIDE, MAX_TEXT, CanvasStore, clean_text, in_grid)

log = logging.getLogger("red.evac-cogs.gamble.canvas")

REVIEW_AFTER_DAYS = 21
RETIRE_BELOW_PAINTERS = 10


def coins(n: int) -> str:
    return f"{n:,} wc"


def esc(text: str) -> str:
    """Make user text safe to show in Discord: no pings, no markdown tricks."""
    out = []
    for ch in str(text):
        out.append("\\" + ch if ch in "\\*_~`|>[]()#-" else ch)
    return "".join(out).replace("@", "@​")


def coords_text(cells: Sequence[tuple]) -> str:
    xs, ys = [c[0] for c in cells], [c[1] for c in cells]
    if len(cells) == 1:
        return f"({xs[0]},{ys[0]})"
    return f"({min(xs)},{min(ys)})–({max(xs)},{max(ys)})"


@dataclass
class Outcome:
    status: str                    # ok | price_changed | insufficient | banned | lurker | error | refund_failed
    message: str = ""
    prices: tuple = ()             # price_changed: the current prices
    paid: list = field(default_factory=list)   # ok: [(x, y, paid)]
    total: int = 0
    balance: Optional[int] = None  # insufficient: what they have
    next_price: Optional[int] = None   # ok, single pixel: what it costs now


class CanvasService:
    def __init__(self, store: CanvasStore, bank, is_lurker: Optional[Callable[[object], Awaitable[bool]]] = None,
                 modlog: Optional[Callable[[str], Awaitable[None]]] = None, clock: Callable[[], float] = time.time):
        self.store, self.bank = store, bank
        self.is_lurker, self.modlog, self.clock = is_lurker, modlog, clock
        self.lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._image: Optional[bytes] = None
        self._image_rev = -1

    # ------------------------------------------------------------------ eligibility

    async def blocked(self, member) -> Optional[str]:
        """'banned' / 'lurker' when this member may not paint, else None."""
        if self.store.is_banned(member.id):
            return "banned"
        if self.is_lurker is not None:
            try:
                if await self.is_lurker(member):
                    return "lurker"
            except Exception:
                log.exception("canvas: Lurker check failed; letting %s through", member.id)
        return None

    BLOCK_MESSAGES = {
        "banned": "You've been banned from painting on the canvas.",
        "lurker": "Lurkers can't paint. Say something in chat to get your roles back first.",
    }

    async def balance(self, member) -> int:
        return await self.bank.get_balance(member)

    # ------------------------------------------------------------------ validation

    @staticmethod
    def validate_cells(cells: Sequence[tuple]) -> Optional[str]:
        if not cells:
            return "Nothing to paint."
        if len(set(cells)) != len(cells):
            return "That selection repeats a pixel."
        if any(not in_grid(x, y) for x, y in cells):
            return f"Coordinates must be x 0–{GRID_W - 1}, y 0–{GRID_H - 1}."
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        if max(xs) - min(xs) + 1 > MAX_AREA_SIDE or max(ys) - min(ys) + 1 > MAX_AREA_SIDE:
            return f"An area can be at most {MAX_AREA_SIDE}×{MAX_AREA_SIDE}."
        return None

    # ------------------------------------------------------------------ painting

    async def paint(self, member, cells: Sequence[tuple], color: str, text: str, confirmed: tuple) -> Outcome:
        """Re-check everything under the lock, take the coins, save, or refund. All or nothing."""
        cells = list(cells)
        bad = self.validate_cells(cells)
        if bad:
            return Outcome("error", bad)
        text = clean_text(text)[:MAX_TEXT]
        async with self.lock:
            block = await self.blocked(member)
            if block:
                return Outcome(block, self.BLOCK_MESSAGES[block])
            current = self.store.quote(cells)
            if current != tuple(confirmed):
                return Outcome("price_changed", "The price moved before you confirmed.", prices=current,
                               total=sum(current))
            total = sum(current)
            self.store.meta_set("pending", json.dumps({"user": member.id, "amount": total, "ts": self.clock(),
                                                       "cells": coords_text(cells)}))
            try:
                await self.bank.withdraw_credits(member, total)
            except ValueError:
                self.store.meta_delete("pending")
                try:
                    balance = await self.bank.get_balance(member)
                except Exception:
                    balance = None
                return Outcome("insufficient", f"You need {coins(total)} for that.", total=total, balance=balance)
            except Exception:
                self.store.meta_delete("pending")
                log.exception("canvas: withdrawing %s from %s failed", total, member.id)
                return Outcome("error", "The bank refused that payment. You weren't charged.", total=total)
            try:
                paid = self.store.paint(member.id, cells, color, text, current, now=self.clock())
            except Exception:
                log.exception("canvas: saving a paint failed after withdrawing %s from %s; refunding", total, member.id)
                try:
                    await self.bank.deposit_credits(member, total)
                except Exception:
                    log.exception("canvas: REFUND FAILED for %s (%s)", member.id, total)
                    await self._modlog(f"🎨 **Canvas refund failed**: <@{member.id}> was charged {coins(total)} for "
                                       f"{coords_text(cells)} but the pixels didn't save and the refund failed. "
                                       f"Please refund them by hand.")
                    return Outcome("refund_failed", "Saving failed and the refund failed too. A mod has been told "
                                   "and will refund you.", total=total)
                finally:
                    self.store.meta_delete("pending")
                return Outcome("error", "Saving failed, so you were refunded. Try again.", total=total)
            self.store.meta_delete("pending")
        out = Outcome("ok", total=total, paid=paid)
        if len(cells) == 1:
            out.next_price = self.store.price_at(*cells[0])
        if text:
            await self._modlog(f"🎨 **Canvas text** — <@{member.id}> (`{member.id}`) set text at "
                               f"{coords_text(cells)} ({canvas_color_name(color)}): {esc(text)}")
        return out

    async def _modlog(self, content: str) -> None:
        if self.modlog is None:
            return
        try:
            await self.modlog(content)
        except Exception:
            log.exception("canvas: posting to #mod-log failed")

    async def take_pending(self) -> Optional[dict]:
        """An unfinished charge left by a crash between withdrawing and saving. Returned once, then cleared."""
        raw = self.store.meta_get("pending")
        if raw is None:
            return None
        self.store.meta_delete("pending")
        try:
            return json.loads(raw)
        except ValueError:
            return {"user": 0, "amount": 0, "cells": "?"}

    # ------------------------------------------------------------------ images (cached)

    async def image(self) -> bytes:
        """The rendered canvas; redrawn only when a paint, wipe or reset changed it."""
        if self._image is not None and self._image_rev == self.store.revision:
            return self._image
        async with self._render_lock:
            rev = self.store.revision
            if self._image is not None and self._image_rev == rev:
                return self._image
            colors = self.store.colors()
            data = await asyncio.to_thread(canvas_render.render_canvas, colors)
            self._image, self._image_rev = data, rev
            return data

    async def zoom_image(self, cx: int, cy: int) -> bytes:
        x0, y0 = canvas_render.zoom_window(cx, cy)
        window = self.store.window(x0, y0, canvas_render.ZOOM_SIDE, canvas_render.ZOOM_SIDE)

        def lookup(x, y):
            p = window.get((x, y))
            return (p.color, p.price) if p else None

        return await asyncio.to_thread(canvas_render.render_zoom, cx, cy, lookup)

    # ------------------------------------------------------------------ reports

    def leaderboard(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        week = now - 7 * 86400
        return {"week": self.store.spenders(week, 10), "all_time": self.store.spenders(None, 10),
                "pixels": self.store.most_pixels(10), "burned_total": self.store.burned(),
                "painted": self.store.painted_count()}

    def usage(self, now: Optional[float] = None) -> dict:
        """The numbers the mod panel shows, and the review verdict."""
        now = self.clock() if now is None else now
        launched = self.store.launched_at()
        review_at = launched + REVIEW_AFTER_DAYS * 86400
        painters_all = self.store.painters()
        return {
            "per_day": self.store.paints_per_day(7, now),
            "painters_7d": self.store.painters(now - 7 * 86400),
            "painters_all": painters_all,
            "burned_weeks": self.store.burned_per_week(4, now),
            "burned_total": self.store.burned(),
            "painted": self.store.painted_count(),
            "banned": self.store.banned_ids(),
            "launched": launched,
            "review_at": review_at,
            "review_due": now >= review_at,
            "retire_suggested": now >= review_at and painters_all < RETIRE_BELOW_PAINTERS,
        }

    def weekly_report(self, boundary: float) -> Optional[dict]:
        """The week ending at `boundary` (a Sunday 6 PM PT), or None when nobody painted."""
        start = boundary - 7 * 86400
        if self.store.paint_actions(start, boundary) == 0:
            return None
        spenders = self.store.spenders(start, 1, until=boundary)
        return {"start": start, "end": boundary, "top_spender": spenders[0] if spenders else None,
                "priciest": self.store.priciest_paint(start, boundary),
                "burned": self.store.burned(start, boundary), "painters": self.store.painters(start, boundary),
                "actions": self.store.paint_actions(start, boundary)}

    def weekly_last_posted(self) -> float:
        return float(self.store.meta_get("weekly_posted") or 0)

    def weekly_mark_posted(self, boundary: float) -> None:
        self.store.meta_set("weekly_posted", str(boundary))

    # ------------------------------------------------------------------ moderation

    async def mod_clear_text(self, mod, x: int, y: int) -> str:
        if not in_grid(x, y):
            return f"({x},{y}) is outside the grid."
        pixel = self.store.get_pixel(x, y)
        if pixel is None or not pixel.text:
            return f"({x},{y}) has no text to clear."
        old = pixel.text
        self.store.clear_text(x, y)
        await self._modlog(f"🎨 **Canvas text cleared** at ({x},{y}) by <@{mod.id}>. It said: {esc(old)} "
                           f"(owner <@{pixel.owner}>)")
        return f"Cleared the text at ({x},{y})."

    def wipe_preview(self, user_id: int) -> dict:
        owned = self.store.owned_by(user_id)
        return {"count": len(owned), "worth": sum(p.price for p in owned)}

    async def mod_wipe(self, mod, user_id: int) -> str:
        n = self.store.wipe_member(user_id)
        await self._modlog(f"🎨 **Canvas wipe**: <@{mod.id}> blanked {n} pixel(s) owned by <@{user_id}>. "
                           f"Coins aren't refunded.")
        return f"Blanked {n} pixel(s) owned by <@{user_id}> (back to {coins(BASE_PRICE)} each)."

    async def mod_ban(self, mod, user_id: int) -> str:
        added = self.store.ban(user_id, mod.id, now=self.clock())
        if added:
            await self._modlog(f"🎨 **Canvas ban**: <@{mod.id}> banned <@{user_id}> from painting.")
            return f"<@{user_id}> can't paint any more. Their existing pixels stay until you wipe them."
        return f"<@{user_id}> was already banned from painting."

    async def mod_unban(self, mod, user_id: int) -> str:
        if self.store.unban(user_id):
            await self._modlog(f"🎨 **Canvas unban**: <@{mod.id}> let <@{user_id}> paint again.")
            return f"<@{user_id}> can paint again."
        return f"<@{user_id}> wasn't banned."

    def reset_preview(self) -> dict:
        rows = self.store.db.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT owner) AS o, COALESCE(SUM(price),0) AS w "
                                     "FROM pixels").fetchone()
        return {"pixels": rows["n"], "owners": rows["o"], "worth": rows["w"]}

    async def mod_reset(self, mod) -> str:
        n = self.store.reset()
        await self._modlog(f"🎨 **Canvas reset**: <@{mod.id}> blanked all {n} painted pixel(s). Coins burned so far "
                           f"stay on the record.")
        return f"The canvas is blank again ({n} pixel(s) cleared). Every pixel costs {coins(BASE_PRICE)}."


def canvas_color_name(color: str) -> str:
    for name, hex_ in canvas_render.PASTELS:
        if hex_.lower() == color.lower():
            return f"{name} {color}"
    return color
