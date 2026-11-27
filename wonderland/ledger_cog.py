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
- A module that did `from redbot.core.bank import set_balance` keeps its own reference to the original and would
  bypass the wrapper. On load every module global holding Red's `set_balance` is swapped for the wrapper (and put
  back on unload); the status line names them.
- Not covered: code that writes balances straight to the bank's Config (ExtendedEconomy's auto-payday loop,
  BankDecay for members who left, bank backup restores, Red's wipe and prune). The reconciliation against the
  `.wld economy` snapshots shows that gap as "unattributed".
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import sys
import time
from typing import Optional

import discord

from . import embeds, ledger

log = logging.getLogger("red.evac-cogs.wonderland.ledger")

FLUSH_EVERY = 10          # seconds between writes to SQLite
HEARTBEAT_EVERY = 30      # seconds between "the ledger is alive" marks, used to spot stretches when it was off
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


def _is_red_set_balance(value) -> bool:
    return (inspect.isfunction(value) and getattr(value, "__module__", None) == "redbot.core.bank"
            and value.__name__ == "set_balance")


def find_direct_importers(current, modules) -> list:
    """[(module name, attribute)] for modules that hold their own reference to `bank.set_balance`.

    Matches `current` (what `bank.set_balance` is now) or Red's own function. Never matches:
    - a live wrapper made by this ledger (a stale one left by a load that never unloaded is covered again);
    - a module that defines `current`. That is another cog's override of the bank, and the original it keeps in
      its globals is what it chains to: swapping that for our wrapper would make the override call itself forever.
    """
    found = []
    current_home = getattr(current, "__module__", None)
    for name, module in list(modules.items()):
        if name == "redbot.core.bank" or name.startswith("wonderland.ledger") or name == current_home:
            continue
        try:
            items = list(vars(module).items())
        except TypeError:
            continue
        for attr, value in items:
            stale = is_ours(value) and not getattr(value, MARKER).get("enabled", True)   # left by a load that never unloaded
            if is_ours(value) and not stale:
                continue
            if not (stale or value is current or _is_red_set_balance(value)):
                continue
            if getattr(value, "__module__", None) == name:
                continue
            found.append((name, attr))
    return found


class LedgerMixin:
    """Needs `self.bot` from the cog it is mixed into. Call `_ledger_setup()` from __init__."""

    def _ledger_setup(self) -> None:
        self._ledger_state = {"enabled": False, "errors": 0}
        self._ledger_buffer: list = []
        self._ledger_dropped = 0
        self._ledger_store: Optional[ledger.LedgerStore] = None
        self._ledger_task: Optional[asyncio.Task] = None
        self._ledger_bank = None
        self._ledger_wrapper = None
        self._ledger_covered: list = []   # [(module, attribute, what it held)] swapped for the wrapper
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
        now = time.time()
        try:
            alive = float(store.meta_get("alive") or 0)
            if alive and now - alive > ledger.NOT_RUNNING_AFTER:
                store.gap_add(alive, now)         # unloaded or bot down since the last heartbeat
            store.meta_set("alive", str(now))
        except Exception:
            log.exception("ledger: heartbeat check failed")
        current = bank.set_balance
        while is_ours(current):  # a wrapper left behind by an earlier load that never unloaded cleanly
            getattr(current, MARKER)["enabled"] = False
            current = current.__ledger_original__
        if not ledger.signature_ok(current):
            self._ledger_status = "off: Red's bank.set_balance changed shape, so the ledger is not wrapping it"
            log.error("ledger: bank.set_balance signature changed; not wrapping it")
            return False
        self._ledger_state = {"enabled": True, "errors": 0}
        self._ledger_wrapper = make_wrapper(current, bank, self._ledger_record, lambda: self.bot.extensions,
                                            self._ledger_state)
        bank.set_balance = self._ledger_wrapper
        self._ledger_bank = bank
        self._ledger_status = "on"
        self._ledger_cover_importers(self._ledger_wrapper, current)
        if loop_task:
            self._ledger_task = asyncio.create_task(self._ledger_loop())
        return True

    def _ledger_cover_importers(self, wrapper, current) -> None:
        """Swap every module-level copy of `set_balance` for the wrapper so direct imports are recorded too."""
        self._ledger_covered = []
        try:
            for name, attr in find_direct_importers(current, sys.modules):
                module = sys.modules[name]
                held = getattr(module, attr)
                setattr(module, attr, wrapper)
                self._ledger_covered.append((module, attr, held))
            if self._ledger_covered:
                log.warning("ledger: covered direct set_balance imports in %s", self._ledger_covered_names())
        except Exception:
            log.exception("ledger: scanning for direct set_balance imports failed")

    def _ledger_covered_names(self) -> str:
        return ", ".join(sorted({f"{m.__name__}.{a}" for m, a, _ in self._ledger_covered}))

    def _ledger_stop(self) -> None:
        self._ledger_state["enabled"] = False
        for module, attr, held in self._ledger_covered:
            if getattr(module, attr, None) is self._ledger_wrapper:    # not if something else replaced it since
                setattr(module, attr, held)
        self._ledger_covered = []
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
        if self._ledger_store is not None:
            try:
                self._ledger_store.meta_set("alive", str(time.time()))   # a clean unload starts the not-running clock here
            except Exception:
                log.exception("ledger: final heartbeat failed")

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
        last_rollup = last_beat = 0.0
        while True:
            await asyncio.sleep(FLUSH_EVERY)
            await self._ledger_flush()
            if time.time() - last_beat >= HEARTBEAT_EVERY:
                last_beat = time.time()
                try:
                    await asyncio.to_thread(self._ledger_store.meta_set, "alive", str(last_beat))
                except Exception:
                    log.exception("ledger: heartbeat failed")
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
        if self._ledger_covered:
            line += f" · also covering direct set_balance imports in {self._ledger_covered_names()}"
        if self._ledger_dropped:
            line += f" · {self._ledger_dropped:,} rows dropped after write failures"
        try:
            import redbot
            line += f" · Red {redbot.__version__}"
        except Exception:
            pass
        return line

    async def _ledger_report(self, guild, currency: str, recon: Optional[dict]) -> discord.Embed:
        """The coin-flow embed for `.wld economy`. `recon` comes from `_eco_report`: {"ts", "supply", "base"},
        the supply read now and the previous snapshot, so the ledger can check itself against them."""
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
            result = None
            if recon is not None:
                base = recon.get("base")
                since = float(base["ts"]) if base and base.get("ts") is not None else None
                keep_from = ledger.day_start(now, ledger.RETENTION_DAYS)
                net = store.net_between(since, recon["ts"], scope) if since is not None else 0
                gaps = store.gaps_between(since, recon["ts"]) if since is not None else []
                result = ledger.reconcile(base, recon["supply"], recon["ts"], net, started, keep_from, gaps)
                result["supply_now"] = recon["supply"]
            return started, week, month, result

        started, week, month, result = await asyncio.to_thread(gather)
        return embeds.ledger_embed(currency, started, week, month, result, self._ledger_status_line())

    async def _ledger_member(self, guild, member, currency: str) -> discord.Embed:
        store = self._ledger_store
        if store is None:
            return embeds.ledger_off_embed(self._ledger_status_line())
        await self._ledger_flush()
        scope = await self._ledger_scope(guild)
        rows = await asyncio.to_thread(store.history, member.id, scope)
        return embeds.ledger_member_embed(currency, member.display_name, rows)
