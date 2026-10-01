"""On-disk day documents: one small JSON file per guild per local date.

Only the day being written is rewritten (~10 KB), so history can grow forever
without making every flush slower, and the files are trivially readable for
analysis or backup. Writes are atomic (temp file + os.replace).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .models import DATE_RE, date_range, new_day_doc, parse_date_key


def guild_dir(base_dir: Path, guild_id: int) -> Path:
    return Path(base_dir) / "days" / str(guild_id)


def day_path(base_dir: Path, guild_id: int, date_key: str) -> Path:
    if not DATE_RE.match(date_key):
        raise ValueError(f"bad date key: {date_key!r}")
    return guild_dir(base_dir, guild_id) / f"{date_key}.json"


def _normalise(doc: dict) -> dict:
    out = new_day_doc()
    out.update(doc)
    for key in ("h", "u", "ub"):
        if not isinstance(out.get(key), dict):
            out[key] = {}
    return out


def load_day(base_dir: Path, guild_id: int, date_key: str) -> dict:
    path = day_path(base_dir, guild_id, date_key)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return _normalise(json.load(fh))
    except FileNotFoundError:
        return new_day_doc()


def save_day(base_dir: Path, guild_id: int, date_key: str, doc: dict) -> None:
    path = day_path(base_dir, guild_id, date_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(tmp, path)


def list_dates(base_dir: Path, guild_id: int) -> list[str]:
    folder = guild_dir(base_dir, guild_id)
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if DATE_RE.match(p.stem))


def load_range(base_dir: Path, guild_id: int, start: date, end: date) -> dict[str, dict]:
    """Existing day docs for start..end inclusive (missing days are simply absent)."""
    out: dict[str, dict] = {}
    for d in date_range(start, end):
        key = d.isoformat()
        if day_path(base_dir, guild_id, key).exists():
            out[key] = load_day(base_dir, guild_id, key)
    return out


def apply_snapshot(base_dir: Path, guild_id: int, snap) -> None:
    """Merge a tracker Snapshot into the day files (hour records absolute, counters additive)."""
    dates = {h[0] for h in snap.hours} | set(snap.user_deltas) | set(snap.joins) | set(snap.leaves)
    for date_key in sorted(dates):
        doc = load_day(base_dir, guild_id, date_key)
        for d, key, rec, _version, _closed in snap.hours:
            if d == date_key:
                doc["h"][key] = rec
        for uid, n in snap.user_deltas.get(date_key, {}).items():
            doc["u"][str(uid)] = doc["u"].get(str(uid), 0) + n
        if date_key in snap.joins:
            doc["joins"] = int(doc.get("joins", 0)) + snap.joins[date_key]
        if date_key in snap.leaves:
            doc["leaves"] = int(doc.get("leaves", 0)) + snap.leaves[date_key]
        save_day(base_dir, guild_id, date_key, doc)


def purge_user_counts(base_dir: Path, guild_id: int, before: date) -> int:
    """Drop per-user counts (`u`/`ub`) from days older than `before`; hourly totals stay forever."""
    purged = 0
    for date_key in list_dates(base_dir, guild_id):
        if parse_date_key(date_key) >= before:
            continue
        doc = load_day(base_dir, guild_id, date_key)
        if doc["u"] or doc["ub"]:
            doc["u"], doc["ub"] = {}, {}
            save_day(base_dir, guild_id, date_key, doc)
            purged += 1
    return purged


# -- in-progress hour state (sets of user ids), so a restart/reload loses nothing ----

def open_state_path(base_dir: Path, guild_id: int) -> Path:
    return Path(base_dir) / "state" / f"{guild_id}.json"


def load_open_state(base_dir: Path, guild_id: int) -> dict:
    try:
        with open_state_path(base_dir, guild_id).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_open_state(base_dir: Path, guild_id: int, state: dict) -> None:
    path = open_state_path(base_dir, guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, separators=(",", ":"))
    os.replace(tmp, path)


def delete_user(base_dir: Path, guild_id: int, user_id: int) -> int:
    """Remove one user's per-day counts everywhere (Red's end-user-data deletion). Returns days touched."""
    touched = 0
    uid = str(user_id)
    for date_key in list_dates(base_dir, guild_id):
        doc = load_day(base_dir, guild_id, date_key)
        if uid in doc["u"] or uid in doc["ub"]:
            doc["u"].pop(uid, None)
            doc["ub"].pop(uid, None)
            save_day(base_dir, guild_id, date_key, doc)
            touched += 1
    return touched
