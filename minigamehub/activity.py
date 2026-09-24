"""Activity tracking: powers both the spawn activity-gate and diagnostics.

Design doc locked decision #2 (still activity-gated even with one fixed
channel) and #16 (hour-of-day breakdown, not one flat aggregate, because
evac's actual pattern is bursts of 15+ concurrent talkers alternating with
10-120 minute dead stretches -- a single average would fit neither regime).

Rather than backfilling channel.history() on every diagnostics run (slow and
rate-limit-risky on an active channel per design review), this module
accumulates hourly buckets in Config as messages come in, seeded by one
bounded recent-history fetch on first load for an immediate rough estimate.
"""
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Deque, Dict

import discord
from redbot.core import Config

from .constants import RESET_TIMEZONE

HISTORY_SEED_HOURS = 48
HISTORY_SEED_LIMIT = 2000  # hard cap on the one-time seed fetch, regardless of window
CONCURRENCY_WINDOW_SECONDS = 300  # 5-minute rolling window for the "concurrent talkers" proxy


def _hour_key(dt: datetime) -> str:
    return str(dt.astimezone(RESET_TIMEZONE).hour)


def _empty_bucket() -> dict:
    return {"message_count": 0, "concurrency_samples": [], "max_gap_seconds": 0}


class ActivityTracker:
    """One instance per guild, held by the cog. Cheap in-memory state plus
    periodic Config flushes -- exact-to-the-message accuracy isn't the goal,
    a reasonable estimate for tuning is.
    """

    def __init__(self, config: Config, guild: discord.Guild):
        self.config = config
        self.guild = guild
        self.last_message_at: float = 0.0
        # rolling window of (timestamp, author_id) for the concurrency proxy
        self._recent_authors: Deque[tuple] = deque()
        self._dirty_buckets: Dict[str, dict] = defaultdict(_empty_bucket)
        self._flush_counter = 0

    def on_message(self, message: discord.Message) -> None:
        now = time.time()
        gap = now - self.last_message_at if self.last_message_at else 0
        self.last_message_at = now

        hour = _hour_key(datetime.now(RESET_TIMEZONE))
        bucket = self._dirty_buckets[hour]
        bucket["message_count"] += 1
        if gap > bucket["max_gap_seconds"]:
            bucket["max_gap_seconds"] = gap

        self._recent_authors.append((now, message.author.id))
        cutoff = now - CONCURRENCY_WINDOW_SECONDS
        while self._recent_authors and self._recent_authors[0][0] < cutoff:
            self._recent_authors.popleft()
        distinct_now = len({a for _, a in self._recent_authors})
        bucket["concurrency_samples"].append(distinct_now)

        self._flush_counter += 1
        # Flushing on every message would be one Config write per message on
        # an active channel -- batch it instead.

    def current_concurrency(self) -> int:
        """Live "how busy is it right now" reading -- distinct authors seen
        in the last CONCURRENCY_WINDOW_SECONDS (5 min), same definition as
        the avg_concurrency stat in diagnostics. Used by the scheduler's
        adaptive pacing (minigamehub.py:_pacing_multiplier) to speed up or
        slow down spawn frequency to match how busy the channel actually is.

        In-memory only, like `last_message_at` -- resets to 0 on every
        process restart/cog reload until live messages repopulate it (see
        the startup-grace handling in _pacing_multiplier, which is why this
        doesn't try to backfill from `seed_from_history` the way the gate's
        `last_message_at` does: a stale post-restart concurrency reading
        would be actively misleading, whereas a stale "was recently active"
        boolean degrades safely to just re-opening on the next real message).
        """
        now = time.time()
        cutoff = now - CONCURRENCY_WINDOW_SECONDS
        while self._recent_authors and self._recent_authors[0][0] < cutoff:
            self._recent_authors.popleft()
        return len({a for _, a in self._recent_authors})

    async def maybe_flush(self, force: bool = False) -> None:
        if not self._dirty_buckets:
            return
        if not force and self._flush_counter < 20:
            return
        async with self.config.guild(self.guild).activity_tracking() as tracking:
            if not tracking.get("sampling_since"):
                tracking["sampling_since"] = time.time()
            buckets = tracking.setdefault("hourly_buckets", {})
            for hour, delta in self._dirty_buckets.items():
                existing = buckets.setdefault(hour, {"message_count": 0, "concurrency_sum": 0, "concurrency_n": 0, "max_gap_seconds": 0})
                existing["message_count"] += delta["message_count"]
                existing["concurrency_sum"] += sum(delta["concurrency_samples"])
                existing["concurrency_n"] += len(delta["concurrency_samples"])
                existing["max_gap_seconds"] = max(existing["max_gap_seconds"], delta["max_gap_seconds"])
        self._dirty_buckets.clear()
        self._flush_counter = 0

    async def seed_from_history(self, channel: discord.abc.Messageable) -> None:
        """One-time bounded history fetch for a rough day-one estimate.
        Never re-runs once sampling_since is already set."""
        tracking = await self.config.guild(self.guild).activity_tracking()
        if tracking.get("sampling_since"):
            return  # already seeded or already accumulating live data

        cutoff_dt = datetime.now(RESET_TIMEZONE) - timedelta(hours=HISTORY_SEED_HOURS)
        seed_buckets: Dict[str, dict] = defaultdict(lambda: {"message_count": 0, "concurrency_sum": 0, "concurrency_n": 0, "max_gap_seconds": 0})
        recent_authors: Deque[tuple] = deque()
        last_ts = None
        newest_ts = None  # timestamp of the most recent (non-bot) message seen
        count = 0
        try:
            async for msg in channel.history(limit=HISTORY_SEED_LIMIT, after=cutoff_dt, oldest_first=False):
                if msg.author.bot:
                    continue
                count += 1
                ts = msg.created_at.timestamp()
                if newest_ts is None:
                    newest_ts = ts  # first hit, newest-first order -- this IS the most recent message
                hour = _hour_key(msg.created_at)
                bucket = seed_buckets[hour]
                bucket["message_count"] += 1
                if last_ts is not None:
                    gap = abs(last_ts - ts)
                    bucket["max_gap_seconds"] = max(bucket["max_gap_seconds"], gap)
                last_ts = ts

                recent_authors.append((ts, msg.author.id))
                cutoff = ts - CONCURRENCY_WINDOW_SECONDS
                while recent_authors and recent_authors[0][0] < cutoff:
                    recent_authors.popleft()
                bucket["concurrency_sum"] += len({a for _, a in recent_authors})
                bucket["concurrency_n"] += 1
        except discord.Forbidden:
            return  # no read-history perms; live accumulation still works going forward

        async with self.config.guild(self.guild).activity_tracking() as tracking:
            tracking["sampling_since"] = time.time()
            tracking["hourly_buckets"] = dict(seed_buckets)

        # Bootstrap the live spawn gate too -- without this, is_channel_active()
        # stays blocked after every restart until a brand-new live message
        # arrives, no matter how recently active the channel actually was,
        # since it only ever looks at self.last_message_at (in-memory).
        if newest_ts is not None:
            self.last_message_at = newest_ts


async def is_channel_active(config: Config, channel: discord.abc.Messageable, tracker: "ActivityTracker") -> bool:
    """The spawn gate: has there been a real message within activity_window?"""
    guild = channel.guild
    window = await config.guild(guild).activity_window()
    if not tracker.last_message_at:
        return False
    return (time.time() - tracker.last_message_at) < window
