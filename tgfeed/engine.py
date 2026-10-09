"""Pure logic for tgfeed: albums, cursors, packing, rate caps, throttling, channel
names. No discord/redbot/telethon imports, so all of it is unit-testable."""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from . import constants
from .models import MediaItem, TopicInfo

# -- Albums and ordering ----------------------------------------------------------


def group_units(items: Iterable[MediaItem]) -> list:
    """Sort by message id and fold albums (same grouped_id, adjacent) into one unit.
    A unit is the thing that is posted and retried as a whole."""
    units: list = []
    for item in sorted(items, key=lambda i: i.msg_id):
        last = units[-1] if units else None
        if last and item.grouped_id is not None and last[-1].grouped_id == item.grouped_id:
            last.append(item)
        else:
            units.append([item])
    return units


def unit_is_ready(unit: Sequence[MediaItem], now: float, settle: float = constants.ALBUM_SETTLE_SECONDS) -> bool:
    """A lone photo/video is ready at once. An album waits until its newest file is
    `settle` seconds old so we never post half of one."""
    if unit[0].grouped_id is None:
        return True
    return now - max(i.date for i in unit) >= settle


def split_ready(units: list, now: float, settle: float = constants.ALBUM_SETTLE_SECONDS,
                max_units: int = constants.MAX_UNITS_PER_TOPIC_PER_CYCLE) -> tuple:
    """(ready, pending). Order is preserved: the first unit that is not ready, and
    everything after it, waits. Also caps how much one topic does per cycle."""
    ready: list = []
    for index, unit in enumerate(units):
        if len(ready) >= max_units or not unit_is_ready(unit, now, settle):
            return ready, units[index:]
        ready.append(unit)
    return ready, []


def next_cursor(cursor: int, last_done_id: Optional[int], pending_first_id: Optional[int], scan_max: int) -> int:
    """Where this topic's cursor moves after a pass.

    Never goes backwards. Past every unit that was handled. If nothing is waiting,
    past everything that was scanned (text-only messages included). If something is
    waiting, up to just before it: the ids in between were scanned and hold nothing
    of this topic's media, so they are safe to skip."""
    new = cursor
    if last_done_id is not None:
        new = max(new, last_done_id)
    if pending_first_id is None:
        new = max(new, scan_max)
    else:
        new = max(new, pending_first_id - 1)
    return new


def pack_batches(sizes: Sequence[int], limit: int, max_files: int = constants.MAX_FILES_PER_MESSAGE) -> list:
    """Indexes of `sizes`, grouped in order into messages: at most `max_files` each and
    at most `limit` bytes in total. A file bigger than `limit` alone gets no batch."""
    batches: list = []
    current: list = []
    total = 0
    for index, size in enumerate(sizes):
        if size > limit:
            continue
        if current and (len(current) >= max_files or total + size > limit):
            batches.append(current)
            current, total = [], 0
        current.append(index)
        total += size
    if current:
        batches.append(current)
    return batches


def upload_limit(guild_limit: Optional[int]) -> int:
    """Bytes we may upload to this server's channels in one message."""
    limit = int(guild_limit or constants.DEFAULT_GUILD_UPLOAD_BYTES)
    return max(0, min(limit, constants.HARD_MAX_UPLOAD_BYTES) - constants.UPLOAD_MARGIN_BYTES)


# -- Rate caps ----------------------------------------------------------------------


def prune_rate_log(stamps: Sequence[float], now: float, horizon: float = 86400.0) -> list:
    return [s for s in stamps if now - s < horizon]


def slots_available(stamps: Sequence[float], now: float, per_hour: int, per_day: int) -> int:
    """How many more downloads fit under both caps right now."""
    hour = sum(1 for s in stamps if now - s < 3600)
    day = sum(1 for s in stamps if now - s < 86400)
    return max(0, min(per_hour - hour, per_day - day))


def seconds_until_slot(stamps: Sequence[float], now: float, per_hour: int, per_day: int) -> float:
    """How long until one download slot frees up (0 if one is free now)."""
    if slots_available(stamps, now, per_hour, per_day) > 0:
        return 0.0
    waits = []
    hour = sorted(s for s in stamps if now - s < 3600)
    if len(hour) >= per_hour:
        waits.append(hour[len(hour) - per_hour] + 3600 - now)
    day = sorted(s for s in stamps if now - s < 86400)
    if len(day) >= per_day:
        waits.append(day[len(day) - per_day] + 86400 - now)
    return max(0.0, max(waits)) if waits else 0.0


def can_start_unit(available: int, unit_size: int, per_hour: int) -> bool:
    return available >= min(unit_size, per_hour)


# -- Flood handling --------------------------------------------------------------------


def flood_sleep_seconds(seconds: int) -> float:
    return float(seconds) + constants.FLOOD_MARGIN_SECONDS


def should_pause_after_flood(events: Sequence[float], now: float) -> bool:
    """True once enough FloodWaits landed inside the window (this one included)."""
    recent = [e for e in events if now - e < constants.FLOOD_WINDOW_SECONDS]
    return len(recent) >= constants.FLOOD_PAUSE_COUNT


# -- Randomised timing -------------------------------------------------------------------


def jittered(base: float, rng, fraction: float = constants.POLL_JITTER_FRACTION) -> float:
    return base * (1.0 + rng.uniform(-fraction, fraction))


def random_gap(low: float, high: float, rng) -> float:
    low = max(low, constants.MIN_FILE_GAP_SECONDS)
    high = max(high, low)
    return rng.uniform(low, high)


# -- Topics and channel names ------------------------------------------------------------


def slugify_channel_name(title: str, prefix: str = "", topic_id: int = 0) -> str:
    """A Discord-safe text channel name from a topic title."""
    text = re.sub(r"[/\\|+]", " ", (title or "").lower())     # slashes and pipes read as word breaks, not as nothing
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^\w-]", "", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    if not text:
        text = f"topic-{topic_id}"
    return (prefix + text)[: constants.CHANNEL_NAME_MAX]


def resolve_topic(arg: str, topics: Sequence[TopicInfo]):
    """(TopicInfo, None) or (None, reason). Matches an id, then an exact title
    (case-insensitive); more than one title match is refused rather than guessed."""
    text = (arg or "").strip()
    if not text:
        return None, "Give me a topic id or its exact title."
    if text.isdigit():
        for topic in topics:
            if topic.id == int(text):
                return topic, None
        return None, f"No topic with id {text}."
    matches = [t for t in topics if t.title.strip().lower() == text.lower()]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"No topic titled \"{text}\". `.tgfeed topics` lists them."
    return None, "More than one topic has that title; use its id instead."


def chunk_lines(lines: Sequence[str], limit: int = 1900) -> list:
    """Join lines into messages that each fit under Discord's length limit."""
    chunks: list = []
    current = ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
