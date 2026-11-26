"""Wonderland pixel canvas: storage and rules. SQLite, pure Python, no discord/redbot imports.

One database file per guild, in the cog's data path. Only painted pixels have a row; a pixel with no
row is blank and costs BASE_PRICE. Every paint is also appended to `paints` (never edited), which is
what the leaderboard, the weekly post and the mod-panel usage counts are computed from, so a wipe or a
reset of the canvas never erases the record of coins burned.

All money maths lives here (`next_price`, `quote`, `paint`) so it can be tested without a bot.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

GRID_W = 100
GRID_H = 50
BASE_PRICE = 1000
PRICE_MULTIPLIER_NUM = 12      # next price = paid * 12 / 10, rounded to the nearest 100
MAX_TEXT = 100
MAX_AREA_SIDE = 10
PACIFIC = ZoneInfo("America/Los_Angeles")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pixels (
    x INTEGER NOT NULL, y INTEGER NOT NULL,
    color TEXT NOT NULL, owner INTEGER NOT NULL,
    price INTEGER NOT NULL,            -- what the NEXT painter pays
    painted_at REAL NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (x, y)
);
CREATE INDEX IF NOT EXISTS pixels_owner ON pixels(owner);
CREATE TABLE IF NOT EXISTS paints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch INTEGER NOT NULL,            -- one per confirmed paint / paint area
    ts REAL NOT NULL,
    user_id INTEGER NOT NULL,
    x INTEGER NOT NULL, y INTEGER NOT NULL,
    paid INTEGER NOT NULL,
    color TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS paints_ts ON paints(ts);
CREATE INDEX IF NOT EXISTS paints_user ON paints(user_id);
CREATE TABLE IF NOT EXISTS banned (
    user_id INTEGER PRIMARY KEY, by INTEGER, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# ------------------------------------------------------------------ pure helpers


def next_price(paid: int) -> int:
    """1.2x what was paid, rounded to the nearest 100 (half rounds up). Always strictly higher."""
    nxt = ((paid * PRICE_MULTIPLIER_NUM + 500) // 1000) * 100
    return max(nxt, paid + 100)


def in_grid(x: int, y: int) -> bool:
    return 0 <= x < GRID_W and 0 <= y < GRID_H


_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_color(text: str) -> Optional[str]:
    """'#abc', 'abc', '#AABBCC' -> '#aabbcc'. None when it isn't a hex colour."""
    m = _HEX.match((text or "").strip())
    if not m:
        return None
    digits = m.group(1).lower()
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits


def clean_text(text: str) -> str:
    """One line, no control characters, at most MAX_TEXT characters."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text or "")
    return re.sub(r" {2,}", " ", text).strip()


_COORD = re.compile(r"^\s*\(?\s*(-?\d+)\s*[,;\s]\s*(-?\d+)\s*\)?\s*$")


def parse_xy(text: str) -> Optional[tuple]:
    """'12,7' / '12 7' / '(12, 7)' -> (12, 7). None when it isn't two integers."""
    m = _COORD.match(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def rect_cells(x1: int, y1: int, x2: int, y2: int) -> list:
    """Every (x, y) in the rectangle spanned by two corners (any order), row by row."""
    lo_x, hi_x = sorted((x1, x2))
    lo_y, hi_y = sorted((y1, y2))
    return [(x, y) for y in range(lo_y, hi_y + 1) for x in range(lo_x, hi_x + 1)]


def short_price(n: int) -> str:
    """Compact price for the zoom grid: 1,200 / 12k / 1.2M."""
    if n < 10_000:
        return f"{n:,}"
    if n < 999_500:
        return f"{n / 1000:.0f}k"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{n / 1_000_000_000:.1f}B".replace(".0B", "B")


def last_weekly_boundary(now_ts: float) -> float:
    """Unix time of the most recent Sunday 18:00 Pacific at or before `now_ts`."""
    local = datetime.fromtimestamp(now_ts, PACIFIC)
    candidate = local.replace(hour=18, minute=0, second=0, microsecond=0)
    candidate -= timedelta(days=(local.weekday() + 1) % 7)   # Sunday is weekday 6
    if candidate > local:
        candidate -= timedelta(days=7)
    return candidate.timestamp()


@dataclass(frozen=True)
class Pixel:
    x: int
    y: int
    color: str
    owner: int
    price: int
    painted_at: float
    text: str


class PriceChanged(Exception):
    """The prices under a confirmed paint are no longer the ones the painter agreed to."""

    def __init__(self, prices: tuple):
        super().__init__("prices changed")
        self.prices = prices


# ------------------------------------------------------------------ the store


class CanvasStore:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.revision = 0   # bumped on every change to the pixels; the image cache keys on it
        if self.meta_get("launched_at") is None:
            self.meta_set("launched_at", str(time.time()))

    def close(self) -> None:
        self.db.close()

    # ---- meta

    def meta_get(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def meta_delete(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM meta WHERE key=?", (key,))

    def launched_at(self) -> float:
        return float(self.meta_get("launched_at") or time.time())

    # ---- reading pixels

    def _row_to_pixel(self, row) -> Pixel:
        return Pixel(row["x"], row["y"], row["color"], row["owner"], row["price"], row["painted_at"], row["text"])

    def get_pixel(self, x: int, y: int) -> Optional[Pixel]:
        row = self.db.execute("SELECT * FROM pixels WHERE x=? AND y=?", (x, y)).fetchone()
        return self._row_to_pixel(row) if row else None

    def price_at(self, x: int, y: int) -> int:
        row = self.db.execute("SELECT price FROM pixels WHERE x=? AND y=?", (x, y)).fetchone()
        return row["price"] if row else BASE_PRICE

    def quote(self, cells: Sequence[tuple]) -> tuple:
        """Current price of each cell, in order. This tuple is what a painter confirms."""
        return tuple(self.price_at(x, y) for x, y in cells)

    def colors(self) -> dict:
        """{(x, y): '#rrggbb'} for every painted pixel."""
        return {(r["x"], r["y"]): r["color"] for r in self.db.execute("SELECT x, y, color FROM pixels")}

    def window(self, x0: int, y0: int, w: int, h: int) -> dict:
        """{(x, y): Pixel} for the painted pixels inside a window."""
        rows = self.db.execute("SELECT * FROM pixels WHERE x>=? AND x<? AND y>=? AND y<?",
                               (x0, x0 + w, y0, y0 + h))
        return {(r["x"], r["y"]): self._row_to_pixel(r) for r in rows}

    def painted_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM pixels").fetchone()["n"]

    def owned_by(self, user_id: int) -> list:
        rows = self.db.execute("SELECT * FROM pixels WHERE owner=? ORDER BY y, x", (user_id,))
        return [self._row_to_pixel(r) for r in rows]

    # ---- painting

    def paint(self, user_id: int, cells: Sequence[tuple], color: str, text: str, expected: tuple,
              now: Optional[float] = None) -> list:
        """Paint every cell, all or nothing, in one transaction.

        `expected` is the price tuple the painter confirmed; if any price differs, nothing is written and
        PriceChanged carries the current prices. Returns [(x, y, paid)]. The caller has already taken
        the coins and must refund if this raises anything.
        """
        now = time.time() if now is None else now
        text = clean_text(text)[:MAX_TEXT]
        with self.db:   # commits on success, rolls everything back on any exception
            current = self.quote(cells)
            if current != tuple(expected):
                raise PriceChanged(current)
            batch = (self.db.execute("SELECT COALESCE(MAX(batch), 0) AS b FROM paints").fetchone()["b"]) + 1
            out = []
            for (x, y), paid in zip(cells, current):
                self.db.execute(
                    "INSERT INTO pixels(x, y, color, owner, price, painted_at, text) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(x, y) DO UPDATE SET color=excluded.color, owner=excluded.owner, "
                    "price=excluded.price, painted_at=excluded.painted_at, text=excluded.text",
                    (x, y, color, user_id, next_price(paid), now, text))
                self.db.execute(
                    "INSERT INTO paints(batch, ts, user_id, x, y, paid, color, text) VALUES(?,?,?,?,?,?,?,?)",
                    (batch, now, user_id, x, y, paid, color, text))
                out.append((x, y, paid))
        self.revision += 1
        return out

    # ---- moderation

    def clear_text(self, x: int, y: int) -> bool:
        with self.db:
            n = self.db.execute("UPDATE pixels SET text='' WHERE x=? AND y=? AND text<>''", (x, y)).rowcount
        return n > 0

    def wipe_member(self, user_id: int) -> int:
        """Blank every pixel the member owns (price back to BASE_PRICE). Returns how many."""
        with self.db:
            n = self.db.execute("DELETE FROM pixels WHERE owner=?", (user_id,)).rowcount
        if n:
            self.revision += 1
        return n

    def reset(self) -> int:
        with self.db:
            n = self.db.execute("DELETE FROM pixels").rowcount
        self.revision += 1
        return n

    def ban(self, user_id: int, by: int, now: Optional[float] = None) -> bool:
        with self.db:
            n = self.db.execute("INSERT OR IGNORE INTO banned(user_id, by, ts) VALUES(?,?,?)",
                                (user_id, by, time.time() if now is None else now)).rowcount
        return n > 0

    def unban(self, user_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM banned WHERE user_id=?", (user_id,)).rowcount > 0

    def is_banned(self, user_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM banned WHERE user_id=?", (user_id,)).fetchone() is not None

    def banned_ids(self) -> list:
        return [r["user_id"] for r in self.db.execute("SELECT user_id FROM banned ORDER BY ts")]

    # ---- statistics (all computed from `paints`, so wipes and resets don't erase them)

    def spenders(self, since: Optional[float] = None, limit: int = 10, until: Optional[float] = None) -> list:
        """[(user_id, coins_spent, pixels_painted)] biggest spender first. `since` None means all time."""
        rows = self.db.execute(
            "SELECT user_id, SUM(paid) AS spent, COUNT(*) AS n FROM paints WHERE ts>=? AND ts<? "
            "GROUP BY user_id ORDER BY spent DESC, user_id LIMIT ?",
            (since or 0, until if until is not None else float("inf"), limit))
        return [(r["user_id"], r["spent"], r["n"]) for r in rows]

    def most_pixels(self, limit: int = 10) -> list:
        rows = self.db.execute("SELECT owner, COUNT(*) AS n FROM pixels GROUP BY owner "
                               "ORDER BY n DESC, owner LIMIT ?", (limit,))
        return [(r["owner"], r["n"]) for r in rows]

    def priciest_paint(self, since: float, until: Optional[float] = None) -> Optional[dict]:
        """The single most expensive pixel paid for in the window."""
        row = self.db.execute(
            "SELECT user_id, x, y, paid, color, ts FROM paints WHERE ts>=? AND ts<? "
            "ORDER BY paid DESC, ts LIMIT 1", (since, until if until is not None else float("inf"))).fetchone()
        return dict(row) if row else None

    def burned(self, since: float = 0.0, until: Optional[float] = None) -> int:
        row = self.db.execute("SELECT COALESCE(SUM(paid), 0) AS s FROM paints WHERE ts>=? AND ts<?",
                              (since, until if until is not None else float("inf"))).fetchone()
        return row["s"]

    def painters(self, since: float = 0.0, until: Optional[float] = None) -> int:
        row = self.db.execute("SELECT COUNT(DISTINCT user_id) AS n FROM paints WHERE ts>=? AND ts<?",
                              (since, until if until is not None else float("inf"))).fetchone()
        return row["n"]

    def paint_actions(self, since: float = 0.0, until: Optional[float] = None) -> int:
        row = self.db.execute("SELECT COUNT(DISTINCT batch) AS n FROM paints WHERE ts>=? AND ts<?",
                              (since, until if until is not None else float("inf"))).fetchone()
        return row["n"]

    def paints_per_day(self, days: int = 7, now: Optional[float] = None) -> list:
        """[(date, paint_actions, pixels)] for the last `days` Pacific days, oldest first, zeros included."""
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, PACIFIC).date()
        first = today - timedelta(days=days - 1)
        start = datetime(first.year, first.month, first.day, tzinfo=PACIFIC).timestamp()
        buckets = {first + timedelta(days=i): [set(), 0] for i in range(days)}
        for r in self.db.execute("SELECT batch, ts FROM paints WHERE ts>=?", (start,)):
            day = datetime.fromtimestamp(r["ts"], PACIFIC).date()
            if day in buckets:
                buckets[day][0].add(r["batch"])
                buckets[day][1] += 1
        return [(d, len(b[0]), b[1]) for d, b in buckets.items()]

    def burned_per_week(self, weeks: int = 4, now: Optional[float] = None) -> list:
        """[(week_start_ts, coins)] for the last `weeks` rolling 7-day windows ending now, oldest first."""
        now = time.time() if now is None else now
        out = []
        for i in range(weeks - 1, -1, -1):
            end = now - i * 7 * 86400
            start = end - 7 * 86400
            out.append((start, self.burned(start, end if i else float("inf"))))
        return out
