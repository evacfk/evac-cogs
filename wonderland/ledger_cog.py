"""Discord side of the coin ledger: wraps Red's `bank.set_balance` so every balance change is recorded.

Red 3.5.24 sends no bank events, and even where a Red version does, an event can't say which cog moved
the coins (listeners run in a new task, without the caller's stack). Every deposit, withdrawal and
transfer in Red's bank module ends in `set_balance`, looked up as a module global, so replacing that one
attribute sees all of them, from every cog, command, button and background loop, without editing any cog.

Rules:
- Fail open. Nothing the ledger does may stop a balance change: attribution, the balance read and the
  record are each wrapped; the original `set_balance` is called exactly once, outside them.
- Unload puts the original back. If something else wrapped `set_balance` on top of ours since, ours is
  switched to a plain pass-through instead, so their wrapper is not torn off.
- Refuse to wrap if `set_balance` no longer has the (member, amount) shape this was written against.
- Not covered: code that writes balances straight to the bank's Config (ExtendedEconomy's auto-payday,
  `bank set-role` and backpay, Red's bank wipe and prune). The report's reconciliation line shows that gap.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import sys
import time
from typing import Optional

import discord

from . import embeds, ledger

log = logging.getLogger("red.evac-cogs.wonderland.ledger")

FLUSH_EVERY = 10          # seconds between writes to SQLite
ROLLUP_EVERY = 3600       # seconds between rollup passes
BUFFER_CAP = 50_000       # rows kept in memory if SQLite keeps failing; oldest dropped past this
MARKER = "__wonderland_ledger__"


def make_wrapper(original, bank, record, extensions, state: dict):
    """A drop-in `set_balance` that records (member, old, new, cog, action) through `record`.

    `state` is shared with the cog: {"enabled": bool, "errors": int}. `extensions()` returns the loaded
    extension names (bot.extensions)."""

    @functools.wraps(original)
    async def set_balance(member, amount):
        pending = None
        if state["enabled"] and not getattr(member, "bot", False):
            try:
                pending = ledger.attribute(sys._getframe(1), extensions())
            except Exception:
                state["errors"] += 1
                log.debug("ledger: attribution failed", exc_info=True)
        old = None
        if pending is not None:
            try:
                old = await bank.get_balance(member)
            except Exception:
                state["errors"] += 1
                pending = None
        new = await original(member, amount)   # the only call that matters; never inside a try
        if pending is not None:
            try:
                record(member, old, new, *pending)
            except Exception:
                state["errors"] += 1
                log.debug("ledger: recording failed", exc_info=True)
        return new

    setattr(set_balance, MARKER, state)
    set_balance.__ledger_original__ = original
    return set_balance


def is_ours(func) -> bool:
    return getattr(func, MARKER, None) is not None


class LedgerMixin:
    """Needs `self.bot` from the cog it is mixed into. Call `_ledger_setup()` from __init__."""

    def _ledger_setup(self) -> None:
        self._ledger_state = {"enabled": False, "errors": 0}
        self._ledger_buffer: list = []
        self._ledger_dropped = 0
        self._ledger_store: Optional[ledger.LedgerStore] = None
        self._ledger_task: Optional[asyncio.Task] = None
        self._ledger_bank = None
        self._ledger_status = "not started"

    # ------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        await super().cog_load()
        try:
            from redbot.core import bank
            from redbot.core.data_manager import cog_data_path
            store = ledger.LedgerStore(cog_data_path(self) / "ledger.sqlite3")
            self._ledger_start(bank, store)
        except Exception as e:
            log.exception("ledger: could not start")
            self._ledger_status = f"off: could not start ({type(e).__name__})"

    def cog_unload(self) -> None:
        try:
            self._ledger_stop()
        except Exception:
            log.exception("ledger: stopping failed")
        super().cog_unload()

    def _ledger_start(self, bank, store, loop_task: bool = True) -> bool:
        """Open the store and wrap `bank.set_balance`. Returns False (ledger off) if Red's shape changed."""
        self._ledger_store = store
        current = bank.set_balance
        while is_ours(current):  # a wrapper left behind by an earlier load that never unloaded cleanly
            getattr(current, MARKER)["enabled"] = False
            current = current.__ledger_original__
        if not ledger.signature_ok(current):
            self._ledger_status = "off: Red's bank.set_balance changed shape, so the ledger is not wrapping it"
            log.error("ledger: bank.set_balance signature changed; not wrapping it")
            return False
        self._ledger_state = {"enabled": True, "errors": 0}
        bank.set_balance = make_wrapper(current, bank, self._ledger_record, lambda: self.bot.extensions,
                                        self._ledger_state)
        self._ledger_bank = bank
        self._ledger_status = "on"
        if loop_task:
            self._ledger_task = asyncio.create_task(self._ledger_loop())
        return True

    def _ledger_stop(self) -> None:
        self._ledger_state["enabled"] = False
        bank = self._ledger_bank
        if bank is not None:
            current = bank.set_balance
            if getattr(current, MARKER, None) is self._ledger_state:
                bank.set_balance = current.__ledger_original__
            else:
                log.warning("ledger: something wrapped bank.set_balance after us; leaving it, ours is now a pass-through")
            self._ledger_bank = None
        if self._ledger_task is not None:
            self._ledger_task.cancel()
            self._ledger_task = None
        self._ledger_status = "off: unloaded"
        self._ledger_flush_now()

    # ------------------------------------------------------------ recording

    def _ledger_record(self, member, old, new, cog: str, action: str) -> None:
        delta = int(new) - int(old)
        if delta == 0:
            return
        guild = getattr(member, "guild", None)
        self._ledger_buffer.append((time.time(), int(getattr(guild, "id", 0) or 0), int(member.id),
                                    int(old), int(new), delta, cog, action))
        if len(self._ledger_buffer) > BUFFER_CAP:
            del self._ledger_buffer[0]
            self._ledger_dropped += 1

    def _ledger_flush_now(self) -> None:
        """Blocking write of everything buffered (unload, tests)."""
        if self._ledger_store is None or not self._ledger_buffer:
            return
        rows, self._ledger_buffer = self._ledger_buffer, []
        try:
            self._ledger_store.add(rows)
        except Exception:
            log.exception("ledger: writing %d rows failed", len(rows))
            self._ledger_buffer[:0] = rows

    async def _ledger_flush(self) -> None:
        if self._ledger_store is None or not self._ledger_buffer:
            return
        rows, self._ledger_buffer = self._ledger_buffer, []
        try:
            await asyncio.to_thread(self._ledger_store.add, rows)
        except Exception:
            log.exception("ledger: writing %d rows failed; will retry", len(rows))
            self._ledger_buffer[:0] = rows

    async def _ledger_loop(self) -> None:
        last_rollup = 0.0
        while True:
            await asyncio.sleep(FLUSH_EVERY)
            await self._ledger_flush()
            if time.time() - last_rollup >= ROLLUP_EVERY:
                last_rollup = time.time()
                try:
                    moved = await asyncio.to_thread(self._ledger_store.rollup)
                    if moved:
                        log.info("ledger: rolled %d rows into daily totals", moved)
                except Exception:
                    log.exception("ledger: rollup failed")

    async def _ledger_delete_user(self, user_id: int) -> None:
        self._ledger_buffer = [r for r in self._ledger_buffer if r[2] != user_id]
        if self._ledger_store is not None:
            await asyncio.to_thread(self._ledger_store.delete_user, user_id)

    # ------------------------------------------------------------ reports

    async def _ledger_scope(self, guild) -> Optional[int]:
        """Guild id to filter on, or None for a global bank (balances are shared across servers)."""
        from redbot.core import bank
        return None if await bank.is_global() else guild.id

    def _ledger_status_line(self) -> str:
        line = f"Ledger {self._ledger_status}"
        if self._ledger_state.get("errors"):
            line += f" · {self._ledger_state['errors']:,} changes it could not attribute or read (they still went through)"
        if self._ledger_dropped:
            line += f" · {self._ledger_dropped:,} rows dropped after write failures"
        try:
            import redbot
            line += f" · Red {redbot.__version__}"
        except Exception:
            pass
        return line

    async def _ledger_report(self, guild, currency: str, supply_total: Optional[int]) -> discord.Embed:
        """The coin-flow embed for `.wld economy`. `supply_total` is every non-bot account's balance now
        (members and leavers), used to reconcile against the ledger since the last run."""
        store = self._ledger_store
        if store is None:
            return embeds.ledger_off_embed(self._ledger_status_line())
        await self._ledger_flush()
        scope = await self._ledger_scope(guild)
        now = time.time()

        def gather():
            started = float(store.meta_get("started_at") or now)
            week = store.flows(now - 7 * 86400, scope)
            month = store.flows(now - 30 * 86400, scope)
            recon = None
            key = f"last_supply:{scope or 0}"
            if supply_total is not None:
                previous = store.meta_get(key)
                if previous:
                    then, then_total = previous.split(":")
                    then, then_total = float(then), int(then_total)
                    if then >= started:
                        recon = {"since": then, "supply": supply_total - then_total,
                                 "ledger": store.net_between(then, now, scope)}
                store.meta_set(key, f"{now}:{supply_total}")
            return started, week, month, recon

        started, week, month, recon = await asyncio.to_thread(gather)
        return embeds.ledger_embed(currency, started, week, month, recon, self._ledger_status_line())

    async def _ledger_member(self, guild, member, currency: str) -> discord.Embed:
        store = self._ledger_store
        if store is None:
            return embeds.ledger_off_embed(self._ledger_status_line())
        await self._ledger_flush()
        scope = await self._ledger_scope(guild)
        rows = await asyncio.to_thread(store.history, member.id, scope)
        return embeds.ledger_member_embed(currency, member.display_name, rows)
