"""On-disk storage for join records: one JSON file per guild per join month.

All records live in memory (a few hundred KB per year at Wonderland's join
rate); only months that changed are rewritten, and the cog flushes them at
most every few minutes, so a busy chat full of new members never turns into a
write per message. Writes are atomic (temp file + os.replace).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import cohorts

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class CohortStore:
    def __init__(self, base_dir, guild_id: int):
        self.dir = Path(base_dir) / "cohorts" / str(guild_id)
        self.records: dict[str, dict] = {}
        self.meta: dict = {}
        self.dirty: set[str] = set()
        self.meta_dirty = False
        self._open_by_user: dict[int, str] = {}

    # -- load / save --------------------------------------------------------

    def load(self) -> None:
        self.records.clear()
        if self.dir.exists():
            for path in sorted(self.dir.glob("*.json")):
                if not _MONTH_RE.match(path.stem):
                    continue
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        data = json.load(fh)
                except (ValueError, OSError):
                    continue
                for key, rec in (data.get("records") or {}).items():
                    self.records[key] = rec
            try:
                with (self.dir / "meta.json").open("r", encoding="utf-8") as fh:
                    self.meta = json.load(fh) or {}
            except (FileNotFoundError, ValueError):
                self.meta = {}
        self._reindex()

    def _reindex(self) -> None:
        self._open_by_user = {}
        for key, rec in sorted(self.records.items(), key=lambda kv: kv[1]["j"]):
            if rec["l"] is None:
                self._open_by_user[rec["u"]] = key
            elif self._open_by_user.get(rec["u"]) and self.records[self._open_by_user[rec["u"]]]["j"] < rec["j"]:
                self._open_by_user.pop(rec["u"], None)

    def _write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)

    def flush(self) -> int:
        """Write every dirty month (and meta). Returns files written."""
        written = 0
        for month in sorted(self.dirty):
            recs = {k: r for k, r in self.records.items() if cohorts.month_key(r["j"]) == month}
            path = self.dir / f"{month}.json"
            if recs:
                self._write(path, {"v": 1, "records": recs})
            elif path.exists():
                path.unlink()
            written += 1
        self.dirty.clear()
        if self.meta_dirty:
            self._write(self.dir / "meta.json", self.meta)
            self.meta_dirty = False
            written += 1
        return written

    # -- meta ---------------------------------------------------------------

    @property
    def tracking_since(self) -> float | None:
        return self.meta.get("tracking_since")

    def ensure_tracking_since(self, ts: float) -> float:
        if self.meta.get("tracking_since") is None:
            self.meta["tracking_since"] = float(ts)
            self.meta_dirty = True
        return self.meta["tracking_since"]

    # -- records ------------------------------------------------------------

    def touch(self, rec: dict) -> None:
        self.dirty.add(cohorts.month_key(rec["j"]))

    def add(self, rec: dict) -> str:
        key = cohorts.record_key(rec)
        self.records[key] = rec
        if rec["l"] is None:
            self._open_by_user[rec["u"]] = key
        self.touch(rec)
        return key

    def open_record(self, uid: int) -> dict | None:
        key = self._open_by_user.get(uid)
        rec = self.records.get(key) if key else None
        return rec if rec is not None and rec["l"] is None else None

    def close(self, uid: int) -> None:
        self._open_by_user.pop(uid, None)

    def watched_users(self, now_ts: float) -> dict[int, str]:
        return {
            uid: key for uid, key in self._open_by_user.items()
            if key in self.records and cohorts.watching(self.records[key], now_ts)
        }

    def replace_rebuilt(self, rebuilt: list[dict], before_ts: float) -> int:
        """Swap in records rebuilt from the join/leave log: drop earlier log/member records
        that joined before `before_ts`, add the new ones. Live records are never touched."""
        for key in [k for k, r in self.records.items() if r.get("o") != "live" and r["j"] < before_ts]:
            self.touch(self.records.pop(key))
        for rec in rebuilt:
            if rec["j"] < before_ts:
                self.records[cohorts.record_key(rec)] = rec
                self.touch(rec)
        self._reindex()
        return len(rebuilt)

    def in_range(self, start_ts: float, end_ts: float) -> list[dict]:
        return [r for r in self.records.values() if start_ts <= r["j"] < end_ts]
