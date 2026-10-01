"""History import support: a resumable on-disk stage + the merge into day files.

Reading Discord history is slow and can be interrupted by a restart, so each
channel's counted messages are written to a tiny binary file (16 bytes per
message) the moment that channel finishes. `.pulse backfill resume` skips
channels that already have a file. Once every channel is staged the rows are
merged in time order, run through the same tracker logic as live traffic (so
backfilled and live numbers mean exactly the same thing) and written to the day
files with *replace* semantics -- re-running a backfill never double counts.

Backfill never touches hours at or after `live_since`; those belong to live
tracking.
"""
from __future__ import annotations

import heapq
import json
import shutil
import struct
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Iterator

from . import storage
from .models import date_range, day_slots, local_dt, new_day_doc
from .tracker import GuildTracker

ROW = struct.Struct("<dq")  # timestamp (double), user id (int64)


class Stage:
    def __init__(self, base_dir: Path, guild_id: int):
        self.dir = Path(base_dir) / "backfill" / str(guild_id)

    # -- lifecycle ------------------------------------------------------
    def meta(self) -> dict | None:
        try:
            return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def start(self, meta: dict) -> None:
        self.clear()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- per-channel files ----------------------------------------------
    def _path(self, channel_key: str) -> Path:
        return self.dir / f"{int(channel_key)}.bin"

    def done_channels(self) -> set[str]:
        if not self.dir.exists():
            return set()
        return {p.stem for p in self.dir.glob("*.bin")}

    def write_channel(self, channel_key: str, rows: Iterable[tuple[float, int]]) -> int:
        """Persist one finished channel. The rename is the 'done' marker."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{int(channel_key)}.tmp"
        n = 0
        with tmp.open("wb") as fh:
            for ts, uid in sorted(rows):
                fh.write(ROW.pack(ts, uid))
                n += 1
        tmp.replace(self._path(channel_key))
        return n

    def _read(self, channel_key: str) -> Iterator[tuple[float, int, str]]:
        with self._path(channel_key).open("rb") as fh:
            while chunk := fh.read(ROW.size * 4096):
                for ts, uid in ROW.iter_unpack(chunk):
                    yield ts, uid, channel_key

    def merged_rows(self) -> Iterator[tuple[float, int, str]]:
        """Every staged row from every channel, in time order."""
        return heapq.merge(*(self._read(c) for c in sorted(self.done_channels())), key=lambda r: r[0])


def build_days(rows: Iterable[tuple[float, int, str]], start_ts: int, end_ts: int) -> dict[str, dict]:
    """Run staged rows through the live tracker -> {date_key: day doc with 'h' and 'ub'}."""
    tracker = GuildTracker(floor_ts=start_ts)
    for ts, uid, ckey in rows:
        if start_ts <= ts < end_ts:
            tracker.record(ts, uid, ckey)
    snap = tracker.snapshot(now_ts=end_ts)
    docs: dict[str, dict] = {}
    for date_key, key, rec, _v, _closed in snap.hours:
        docs.setdefault(date_key, new_day_doc())["h"][key] = rec
    for date_key, users in snap.user_deltas.items():
        docs.setdefault(date_key, new_day_doc())["ub"] = {str(uid): n for uid, n in users.items()}
    return docs


def apply_backfill(base_dir: Path, guild_id: int, docs: dict[str, dict], start_ts: int, end_ts: int) -> int:
    """Replace [start_ts, end_ts) in the day files with `docs`. Returns days written."""
    first, last = local_dt(start_ts).date(), local_dt(end_ts - 1).date()
    written = 0
    for d in date_range(first, last):
        date_key = d.isoformat()
        doc = storage.load_day(base_dir, guild_id, date_key)
        for info in day_slots(date_key):
            if start_ts <= info.start_ts < end_ts:
                doc["h"].pop(info.key, None)
        fresh = docs.get(date_key)
        if fresh:
            doc["h"].update(fresh["h"])
        doc["ub"] = dict(fresh["ub"]) if fresh else {}
        if doc["h"] or doc["u"] or doc["ub"] or doc.get("joins") or doc.get("leaves"):
            storage.save_day(base_dir, guild_id, date_key, doc)
            written += 1
    return written
