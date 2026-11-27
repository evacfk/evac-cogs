"""Coin ledger for `.wld economy`: every wondercoin balance change, who moved it, and where it went.

Pure: no discord or redbot imports (the bank wrapper lives in ledger_cog.py). Three parts:

- `signature_ok`: the shape of Red's `bank.set_balance` this ledger was written against. If a Red
  update changes it, the cog refuses to wrap and reports the ledger as off instead of guessing.
- `attribute`: walks the Python call stack from inside `set_balance` to the first frame that belongs
  to a loaded cog (a module inside one of `bot.extensions`), and finds the command, button or
  function that started it.
- `LedgerStore`: SQLite. Individual rows for RETENTION_DAYS, then daily per-source totals forever.
"""
from __future__ import annotations

import inspect
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

RETENTION_DAYS = 30          # individual rows; older ones are rolled into daily totals
HISTORY_LIMIT = 25           # rows shown for one member
MAX_FRAMES = 80              # how far up the stack attribution looks
LEDGER_TZ = "America/Los_Angeles"   # day boundaries for rollups, same as the coin report's snapshots
UNKNOWN = "unknown"
UNATTRIBUTED_ALERT = 0.01   # a gap above this share of the supply is listed first in the report
NOT_RUNNING_AFTER = 90      # seconds without a heartbeat before the ledger counts as having been off

# Red 3.5.x: `async def set_balance(member, amount) -> int`.
EXPECTED_SET_BALANCE_PARAMS = ("member", "amount")

# Sources where the members' loss is the house's gain. Names are cog package names (folder names).
# Not `gamble`: its only coin movement is pixel canvas purchases, a sink that already shows under Sinks.
# Not `minigamehub`: its coins are pacing rewards and member-to-member plant drops, not a house.
HOUSE_SOURCES = ("casino", "blackjacktable", "duel", "minigames", "heist", "lottery")

# Frames with these names are bank plumbing (Red's own, or another cog's wrapper), never the source.
BANK_API_NAMES = frozenset({"set_balance", "deposit_credits", "withdraw_credits", "transfer_credits"})

_RANDOM_ID = re.compile(r"^[0-9a-f]{32}$")   # discord.py's auto-generated custom_id


def signature_ok(func) -> bool:
    """True when `func` is a coroutine function taking exactly (member, amount), positional or keyword."""
    if not inspect.iscoroutinefunction(inspect.unwrap(func)):
        return False
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return (tuple(params) == EXPECTED_SET_BALANCE_PARAMS
            and all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD and p.default is inspect.Parameter.empty
                    for p in params.values()))


# ------------------------------------------------------------------ attribution

def cog_of(module_name: Optional[str], extensions) -> Optional[str]:
    """The cog package a module belongs to, or None when it is not inside a loaded extension.

    'casino.engine' -> 'casino' when 'casino' is loaded; 'redbot.cogs.economy.economy' -> 'economy'.
    """
    if not module_name:
        return None
    parts = module_name.split(".")
    for i in range(1, len(parts) + 1):
        prefix = ".".join(parts[:i])
        if prefix in extensions:
            return prefix.rsplit(".", 1)[-1]
    return None


def _is_context(obj) -> bool:
    command = getattr(obj, "command", None)
    return (command is not None and hasattr(command, "qualified_name")
            and hasattr(obj, "invoked_with") and hasattr(obj, "message"))


def _is_interaction(obj) -> bool:
    return obj is not None and hasattr(obj, "response") and hasattr(obj, "data") and hasattr(obj, "type")


def _component_label(obj) -> Optional[str]:
    """A button or select's human name: its label, else placeholder, else a hand-written custom_id."""
    for attr in ("label", "placeholder"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    custom_id = getattr(obj, "custom_id", None)
    if isinstance(custom_id, str) and custom_id and not _RANDOM_ID.match(custom_id):
        return custom_id
    emoji = getattr(obj, "emoji", None)
    return str(emoji) if emoji else None


def _looks_like_component(obj) -> bool:
    return obj is not None and hasattr(obj, "custom_id") and hasattr(obj, "callback") and hasattr(obj, "view")


def _looks_like_modal(obj) -> bool:
    return obj is not None and hasattr(obj, "on_submit") and isinstance(getattr(obj, "title", None), str)


def _interaction_action(interaction, component, modal) -> str:
    command = getattr(interaction, "command", None)
    if command is not None and hasattr(command, "qualified_name"):
        return f"/{command.qualified_name}"
    if modal is not None:
        return f"modal: {modal.title}"
    label = _component_label(component) if component is not None else None
    if label is None:
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if isinstance(custom_id, str) and custom_id and not _RANDOM_ID.match(custom_id):
            label = custom_id
    return f"button: {label}" if label else "button"


def attribute(frame, extensions) -> Tuple[str, str]:
    """(cog, action) for the balance change whose `set_balance` caller is `frame`.

    cog: the first frame (walking outward) whose module is inside a loaded extension, skipping
    bank plumbing. If that frame is the body of the command being run (for example a cog that
    replaced Economy's payday callback), the command's own cog wins, since that is what mods think of.
    action: `.command name`, `/app command`, `button: Label`, `modal: Title`, or `fn: Qualname` of the
    outermost frame in that cog for loops and listeners (the loop body, the `on_message` listener).
    """
    cog = None
    cog_code = entry_code = None   # first frame in the cog, and the outermost one seen (a loop or listener)
    ctx = interaction = component = modal = None
    f = frame
    depth = 0
    while f is not None and depth < MAX_FRAMES:
        code = f.f_code
        if code.co_name not in BANK_API_NAMES and (cog is None or ctx is None):
            found = cog_of(f.f_globals.get("__name__"), extensions)
            if found is not None and cog is None:
                cog, cog_code = found, code
            if found is not None and found == cog:
                entry_code = code
        if ctx is None and interaction is None:
            local = f.f_locals
            for name in ("ctx", "context"):
                if _is_context(local.get(name)):
                    ctx = local[name]
                    break
            if ctx is None:
                for name in ("interaction", "inter"):
                    if _is_interaction(local.get(name)):
                        interaction = local[name]
                        break
        if interaction is not None and component is None and modal is None:
            local = f.f_locals
            for name in ("button", "item", "select", "self"):
                obj = local.get(name)
                if _looks_like_component(obj):
                    component = obj
                    break
                if name == "self" and _looks_like_modal(obj):
                    modal = obj
                    break
        if cog is not None and ctx is not None:
            break
        f = f.f_back
        depth += 1

    action = None
    if ctx is not None:
        command = ctx.command
        action = f".{command.qualified_name}"
        callback_code = getattr(getattr(command, "callback", None), "__code__", None)
        if cog_code is not None and callback_code is cog_code:
            owner = cog_of(type(getattr(command, "cog", None)).__module__, extensions)
            if owner is not None:
                cog = owner
    elif interaction is not None:
        action = _interaction_action(interaction, component, modal)
    elif entry_code is not None:
        action = f"fn: {getattr(entry_code, 'co_qualname', entry_code.co_name)}"
    return (cog or UNKNOWN), (action or UNKNOWN)[:100]


# ------------------------------------------------------------------ storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    old_balance INTEGER NOT NULL,
    new_balance INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    cog TEXT NOT NULL,
    action TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS changes_ts ON changes (ts);
CREATE INDEX IF NOT EXISTS changes_user ON changes (user_id, ts);
CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    cog TEXT NOT NULL,
    action TEXT NOT NULL,
    coins_in INTEGER NOT NULL,
    coins_out INTEGER NOT NULL,
    events INTEGER NOT NULL,
    PRIMARY KEY (day, guild_id, cog, action)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gaps (start REAL NOT NULL, end REAL NOT NULL);
"""

Row = Tuple[float, int, int, int, int, int, str, str]  # ts, guild, user, old, new, delta, cog, action


def local_day(ts: float, tz: str = LEDGER_TZ) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).date().isoformat()


def day_start(ts: float, days_back: int = 0, tz: str = LEDGER_TZ) -> float:
    """Unix time of local midnight `days_back` days before the day containing `ts`."""
    zone = ZoneInfo(tz)
    day = datetime.fromtimestamp(ts, zone).date() - timedelta(days=days_back)
    return datetime(day.year, day.month, day.day, tzinfo=zone).timestamp()


class LedgerStore:
    """Blocking SQLite access. Call from a thread (asyncio.to_thread); a lock serialises writers."""

    def __init__(self, path, tz: str = LEDGER_TZ):
        self.path = str(path)
        self.tz = tz
        self._lock = threading.Lock()
        with self._lock, self._connect() as con:
            con.executescript(SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('started_at', ?)", (str(time.time()),))

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    # writes ---------------------------------------------------------

    def add(self, rows: Iterable[Row]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock, self._connect() as con:
            con.executemany(
                "INSERT INTO changes (ts, guild_id, user_id, old_balance, new_balance, delta, cog, action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def rollup(self, now: Optional[float] = None) -> int:
        """Fold rows from before local midnight RETENTION_DAYS ago into `daily`, in one transaction."""
        cutoff = day_start(now if now is not None else time.time(), RETENTION_DAYS, self.tz)
        with self._lock, self._connect() as con:
            totals = defaultdict(lambda: [0, 0, 0])
            for ts, guild_id, cog, action, delta in con.execute(
                    "SELECT ts, guild_id, cog, action, delta FROM changes WHERE ts < ?", (cutoff,)):
                t = totals[(local_day(ts, self.tz), guild_id, cog, action)]
                if delta > 0:
                    t[0] += delta
                else:
                    t[1] += -delta
                t[2] += 1
            con.executemany(
                "INSERT INTO daily (day, guild_id, cog, action, coins_in, coins_out, events) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (day, guild_id, cog, action) DO UPDATE SET coins_in = coins_in + excluded.coins_in, "
                "coins_out = coins_out + excluded.coins_out, events = events + excluded.events",
                [(*key, *vals) for key, vals in totals.items()])
            moved = con.execute("DELETE FROM changes WHERE ts < ?", (cutoff,)).rowcount
            con.execute("DELETE FROM gaps WHERE end < ?", (cutoff,))
        return moved

    def delete_user(self, user_id: int) -> int:
        with self._lock, self._connect() as con:
            return con.execute("DELETE FROM changes WHERE user_id = ?", (int(user_id),)).rowcount

    def gap_add(self, start: float, end: float) -> None:
        """Record a stretch when the ledger was not running (cog unloaded, bot down)."""
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO gaps (start, end) VALUES (?, ?)", (start, end))

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (key, value))

    # reads ----------------------------------------------------------

    def meta_get(self, key: str) -> Optional[str]:
        with self._connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def flows(self, since: float, guild_id: Optional[int] = None) -> dict:
        """{cog: {"in", "out", "events", "actions": {action: net}}} for changes at or after `since`.

        Rolled-up days are included whole from the local day containing `since`."""
        where, args = ("", ()) if guild_id is None else (" AND guild_id = ?", (guild_id,))
        out: dict = {}

        def bucket(cog):
            return out.setdefault(cog, {"in": 0, "out": 0, "events": 0, "actions": defaultdict(int)})

        with self._connect() as con:
            for cog, action, coins_in, coins_out, events in con.execute(
                    "SELECT cog, action, SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), "
                    "SUM(CASE WHEN delta < 0 THEN -delta ELSE 0 END), COUNT(*) FROM changes "
                    f"WHERE ts >= ?{where} GROUP BY cog, action", (since, *args)):
                b = bucket(cog)
                b["in"] += coins_in
                b["out"] += coins_out
                b["events"] += events
                b["actions"][action] += coins_in - coins_out
            for cog, action, coins_in, coins_out, events in con.execute(
                    "SELECT cog, action, SUM(coins_in), SUM(coins_out), SUM(events) FROM daily "
                    f"WHERE day >= ?{where} GROUP BY cog, action", (local_day(since, self.tz), *args)):
                b = bucket(cog)
                b["in"] += coins_in
                b["out"] += coins_out
                b["events"] += events
                b["actions"][action] += coins_in - coins_out
        for b in out.values():
            b["actions"] = dict(b["actions"])
        return out

    def net_between(self, start: float, end: float, guild_id: Optional[int] = None) -> int:
        where, args = ("", ()) if guild_id is None else (" AND guild_id = ?", (guild_id,))
        with self._connect() as con:
            row = con.execute(f"SELECT COALESCE(SUM(delta), 0) FROM changes WHERE ts >= ? AND ts < ?{where}",
                              (start, end, *args)).fetchone()
        return int(row[0])

    def history(self, user_id: int, guild_id: Optional[int] = None, limit: int = HISTORY_LIMIT) -> list:
        """Newest first: [(ts, old, new, delta, cog, action)]."""
        where, args = ("", ()) if guild_id is None else (" AND guild_id = ?", (guild_id,))
        with self._connect() as con:
            return con.execute(
                "SELECT ts, old_balance, new_balance, delta, cog, action FROM changes "
                f"WHERE user_id = ?{where} ORDER BY ts DESC, id DESC LIMIT ?", (int(user_id), *args, limit)).fetchall()

    def gaps_between(self, start: float, end: float) -> list:
        """[(start, end)] of not-running stretches that overlap [start, end]."""
        with self._connect() as con:
            return con.execute("SELECT start, end FROM gaps WHERE end > ? AND start < ? ORDER BY start",
                               (start, end)).fetchall()

    def count(self) -> int:
        with self._connect() as con:
            return con.execute("SELECT COUNT(*) FROM changes").fetchone()[0]


# ------------------------------------------------------------------ reconciliation

def reconcile(base: Optional[dict], supply: int, now: float, net: int, started_at: float,
              keep_from: float, gaps=()) -> dict:
    """Does the change in total supply since the `base` snapshot equal the ledger's net since then?

    `base` is an economy snapshot with `ts` and `supply` (older snapshots have neither). `net` is the sum
    of recorded deltas between base["ts"] and `now`; `supply` is the supply read at `now`. `keep_from` is
    the oldest time individual rows are still kept. Result["status"]:
      "no_baseline": no earlier snapshot carries a time and a supply yet,
      "too_early":   the ledger started after the baseline, so it can't have seen the whole window,
      "too_old":     the window starts before the kept rows, so its net can't be summed,
      "ok":          gap = supply change - net. Positive means coins appeared that the ledger never saw.
    """
    if not base or base.get("ts") is None or base.get("supply") is None:
        return {"status": "no_baseline"}
    since = float(base["ts"])
    if since < started_at:
        return {"status": "too_early", "since": since, "started": started_at}
    if since < keep_from:
        return {"status": "too_old", "since": since}
    change = int(supply) - int(base["supply"])
    gap = change - int(net)
    pct = abs(gap) / supply if supply > 0 else (0.0 if gap == 0 else 1.0)
    off = sum(max(0.0, min(end, now) - max(start, since)) for start, end in gaps)
    return {"status": "ok", "since": since, "hours": (now - since) / 3600, "supply": change, "ledger": int(net),
            "gap": gap, "pct": pct, "alert": pct > UNATTRIBUTED_ALERT, "off_seconds": off}


# ------------------------------------------------------------------ report maths

def rank(flows: dict, limit: int = 8):
    """(faucets, sinks): [(cog, net, in, out, top_action)], biggest first. Net is from the members' side."""
    rows = []
    for cog, b in flows.items():
        net = b["in"] - b["out"]
        top = max(b["actions"].items(), key=lambda kv: abs(kv[1]), default=(None, 0))[0]
        rows.append((cog, net, b["in"], b["out"], top))
    faucets = sorted((r for r in rows if r[1] > 0), key=lambda r: -r[1])[:limit]
    sinks = sorted((r for r in rows if r[1] < 0), key=lambda r: r[1])[:limit]
    return faucets, sinks


def house(flows: dict, sources=HOUSE_SOURCES):
    """[(source, house_result, events)] for each gambling source: positive means the house is up
    (members lost coins to it), negative means it paid out more than it took."""
    return [(s, flows[s]["out"] - flows[s]["in"], flows[s]["events"]) if s in flows else (s, 0, 0)
            for s in sources]


def total_net(flows: dict) -> int:
    return sum(b["in"] - b["out"] for b in flows.values())
