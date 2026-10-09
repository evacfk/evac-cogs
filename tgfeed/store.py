"""Discord message -> Telegram origin map, for takedowns. A small sqlite file instead
of Red's JSON Config: it holds thousands of rows and is only ever looked up by id."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional


class PostedStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS posted ("
                "discord_msg_id INTEGER PRIMARY KEY, channel_id INTEGER, topic_id INTEGER, "
                "tg_ids TEXT, ts REAL)"
            )
            con.execute("CREATE INDEX IF NOT EXISTS posted_ts ON posted(ts)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def add(self, discord_msg_id: int, channel_id: int, topic_id: int, tg_ids: list, ts: Optional[float] = None) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO posted VALUES (?,?,?,?,?)",
                (int(discord_msg_id), int(channel_id), int(topic_id), json.dumps([int(i) for i in tg_ids]), ts if ts is not None else time.time()),
            )

    def get(self, discord_msg_id: int) -> Optional[dict]:
        with self._conn() as con:
            row = con.execute(
                "SELECT channel_id, topic_id, tg_ids, ts FROM posted WHERE discord_msg_id=?", (int(discord_msg_id),)
            ).fetchone()
        if row is None:
            return None
        return {"channel_id": row[0], "topic_id": row[1], "tg_ids": json.loads(row[2]), "ts": row[3]}

    def delete(self, discord_msg_id: int) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM posted WHERE discord_msg_id=?", (int(discord_msg_id),))

    def prune(self, older_than_ts: float) -> int:
        with self._conn() as con:
            return con.execute("DELETE FROM posted WHERE ts < ?", (older_than_ts,)).rowcount

    def count(self) -> int:
        with self._conn() as con:
            return con.execute("SELECT COUNT(*) FROM posted").fetchone()[0]
