"""Download -> shrink -> hand to Discord -> clean up, for one topic's new media.
Free of discord/redbot/telethon imports: everything external is injected through
`Deps`, so the whole flow is tested with fakes."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import constants, engine
from .models import MediaItem, PreparedFile, TopicMapping, TopicStats
from .source import SourceAuthError, SourceFlood

log = logging.getLogger("red.tgfeed.pipeline")


class RateCapReached(Exception):
    """The hourly/daily download cap has no room for this unit yet."""


@dataclass
class Deps:
    source: Any
    group: Any
    limit_bytes: int
    tmpdir: str
    gap: tuple
    rng: Any
    sleep: Callable[[float], Awaitable[None]]
    now: Callable[[], float]
    reserve: Callable[[int], bool]                       # take n download slots, False if no room
    send_batch: Callable[[int, list], Awaitable[int]]    # (topic_id, [PreparedFile]) -> discord message id
    shrink: Callable[[str, str, MediaItem, int], Awaitable[Optional[int]]]   # -> final size or None
    fails: dict = field(default_factory=dict)
    _downloads: int = 0


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _prepare(item: MediaItem, index: int, deps: Deps, stats: TopicStats, temp_paths: list) -> Optional[PreparedFile]:
    """Download one item and make it postable; None if it has to be skipped."""
    if deps._downloads > 0:
        await deps.sleep(engine.random_gap(deps.gap[0], deps.gap[1], deps.rng))
    deps._downloads += 1

    if item.kind == constants.KIND_PHOTO:
        data = await deps.source.download_bytes(item)
        if len(data) > deps.limit_bytes:
            stats.skipped_oversize += 1
            return None
        return PreparedFile(name=f"file-{index}{item.ext}", size=len(data), data=data)

    if item.size and item.size > constants.MAX_SOURCE_VIDEO_BYTES:
        stats.skipped_oversize += 1
        return None
    src = os.path.join(deps.tmpdir, f"{item.topic_id}-{item.msg_id}-src{item.ext}")
    temp_paths.append(src)
    await deps.source.download_file(item, src)
    size = os.path.getsize(src)
    if size <= deps.limit_bytes and item.ext in constants.PLAYABLE_VIDEO_EXTS:
        return PreparedFile(name=f"file-{index}{item.ext}", size=size, path=src)

    dst = os.path.join(deps.tmpdir, f"{item.topic_id}-{item.msg_id}-out.mp4")
    temp_paths.append(dst)
    final = await deps.shrink(src, dst, item, deps.limit_bytes)
    if final is None:
        stats.skipped_too_long += 1
        return None
    stats.shrunk += 1
    return PreparedFile(name=f"file-{index}.mp4", size=final, path=dst)


async def deliver_unit(unit: list, deps: Deps, stats: TopicStats) -> int:
    """Post one photo/video or one whole album. Returns files posted. Temp files are
    removed whatever happens."""
    if not deps.reserve(len(unit)):
        raise RateCapReached()
    topic_id = unit[0].topic_id
    temp_paths: list = []
    posted = 0
    try:
        files: list = []
        for index, item in enumerate(unit, start=1):
            prepared = await _prepare(item, index, deps, stats, temp_paths)
            if prepared is not None:
                files.append(prepared)
        for batch in engine.pack_batches([f.size for f in files], deps.limit_bytes):
            batch_files = [files[i] for i in batch]
            await deps.send_batch(topic_id, batch_files)
            stats.posted_messages += 1
            stats.posted_files += len(batch_files)
            posted += len(batch_files)
        return posted
    finally:
        for path in temp_paths:
            _remove(path)


async def process_topic(mapping: TopicMapping, items: list, scan_max: int, deps: Deps, stats: TopicStats) -> Optional[str]:
    """Handle one topic's new media, oldest first. `mapping.cursor` is advanced as
    work completes (the caller persists it in a `finally`, so a flood or crash keeps
    progress). Returns 'rate_cap' / 'error' if it stopped early, else None.
    SourceFlood and SourceAuthError propagate: they stop the whole cycle."""
    fresh = [i for i in items if i.msg_id > mapping.cursor]
    ready, pending = engine.split_ready(engine.group_units(fresh), deps.now())
    stop: Optional[str] = None
    unfinished: list = []

    for position, unit in enumerate(ready):
        key = (mapping.topic_id, unit[0].msg_id)
        try:
            await deliver_unit(unit, deps, stats)
        except RateCapReached:
            stop, unfinished = "rate_cap", ready[position:]
            break
        except (SourceFlood, SourceAuthError):
            raise
        except Exception as exc:  # noqa: BLE001 -- one bad unit must not wedge the topic forever
            deps.fails[key] = deps.fails.get(key, 0) + 1
            stats.errors += 1
            mapping.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("tgfeed: topic %s unit %s failed (%s/%s): %s", mapping.topic_id, unit[0].msg_id,
                        deps.fails[key], constants.MAX_UNIT_RETRIES, exc)
            if deps.fails[key] >= constants.MAX_UNIT_RETRIES:
                stats.skipped_failed += 1
                deps.fails.pop(key, None)
                mapping.cursor = engine.next_cursor(mapping.cursor, unit[-1].msg_id, None, mapping.cursor)
                continue
            stop, unfinished = "error", ready[position:]
            break
        else:
            deps.fails.pop(key, None)
            mapping.cursor = engine.next_cursor(mapping.cursor, unit[-1].msg_id, None, mapping.cursor)
            mapping.last_post_ts = deps.now()
            mapping.last_error = None

    waiting = (unfinished or []) + pending
    pending_first = waiting[0][0].msg_id if waiting else None
    mapping.cursor = engine.next_cursor(mapping.cursor, None, pending_first, scan_max)
    return stop
