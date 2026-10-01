"""Live message tracking -- pure logic, no discord/redbot imports.

`GuildTracker` accumulates per-hour statistics in memory. The cog periodically
takes a `Snapshot`, writes it to disk, and only then calls `commit()`, so a
failed write loses nothing: the next snapshot simply includes everything again.

Hour records are written as *absolute* values (the tracker holds the full
in-progress hour, hydrated from disk after a restart); per-user counts, joins
and leaves are written as *deltas* and subtracted on commit.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable

from .constants import CONCURRENCY_WINDOW_SECONDS, VALID_MESSAGE_TYPES
from .models import HOUR_SECONDS, date_key_for_ts, slot_for_ts


def is_command_message(content: str, prefixes: Iterable[str]) -> bool:
    """True for things like '.gamble 100' (prefix immediately followed by a letter/digit).

    '...lol' or '. hello' are ordinary chat, not commands.
    """
    for prefix in prefixes:
        if prefix and content.startswith(prefix) and len(content) > len(prefix) and content[len(prefix)].isalnum():
            return True
    return False


def should_count(
    *,
    is_bot: bool,
    webhook_id,
    type_name: str,
    content: str,
    channel_key: int,
    ignored: Iterable[int],
    exclude_commands: bool,
    prefixes: Iterable[str],
) -> bool:
    """The single source of truth for 'does this message count as chat?'."""
    if is_bot or webhook_id:
        return False
    if type_name not in VALID_MESSAGE_TYPES:
        return False
    if channel_key in set(ignored):
        return False
    if exclude_commands and content and is_command_message(content, prefixes):
        return False
    return True


@dataclass
class ChanAcc:
    msgs: int = 0
    users: set = field(default_factory=set)
    u_base: int = 0  # distinct count already on disk when the user set was lost (crash recovery)

    def count(self) -> int:
        return max(len(self.users), self.u_base)


@dataclass
class HourAcc:
    date: str
    key: str
    hour: int
    start_ts: int
    end_ts: int
    msgs: int = 0
    users: set = field(default_factory=set)
    u_base: int = 0
    peak: int = 0
    conc_sum: int = 0
    inner_gap: float = 0.0
    gap_start: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    channels: dict = field(default_factory=dict)
    version: int = 0

    def add(self, ts: float, uid: int, channel_key: str, concurrency: int) -> None:
        self.msgs += 1
        self.users.add(uid)
        if concurrency > self.peak:
            self.peak = concurrency
        self.conc_sum += concurrency
        if self.first_ts is None:
            self.first_ts = ts
        elif self.last_ts is not None:
            gap = ts - self.last_ts
            if gap > self.inner_gap:
                self.inner_gap = gap
                self.gap_start = int(self.last_ts - self.start_ts)
        self.last_ts = ts
        ch = self.channels.setdefault(channel_key, ChanAcc())
        ch.msgs += 1
        ch.users.add(uid)
        self.version += 1

    def to_record(self) -> dict:
        return {
            "m": self.msgs,
            "u": max(len(self.users), self.u_base),
            "pk": self.peak,
            "cs": self.conc_sum,
            "gi": int(round(self.inner_gap)),
            "gs": int(self.gap_start),
            "f": int(self.first_ts - self.start_ts) if self.first_ts is not None else 0,
            "l": int(self.last_ts - self.start_ts) if self.last_ts is not None else 0,
            "c": {k: [v.msgs, v.count()] for k, v in self.channels.items()},
        }

    @classmethod
    def from_record(cls, slot, rec: dict, users: Iterable[int] = (), ch_users: dict | None = None) -> "HourAcc":
        """Rebuild an in-progress hour from disk after a restart."""
        acc = cls(slot.date, slot.key, slot.hour, slot.start_ts, slot.end_ts)
        acc.msgs = int(rec.get("m", 0))
        acc.users = set(users)
        acc.u_base = int(rec.get("u", 0))
        acc.peak = int(rec.get("pk", 0))
        acc.conc_sum = int(rec.get("cs", 0))
        acc.inner_gap = float(rec.get("gi", 0))
        acc.gap_start = int(rec.get("gs", 0))
        if acc.msgs:
            acc.first_ts = slot.start_ts + int(rec.get("f", 0))
            acc.last_ts = slot.start_ts + int(rec.get("l", 0))
        ch_users = ch_users or {}
        for ckey, pair in (rec.get("c") or {}).items():
            ch = ChanAcc(msgs=int(pair[0]), users=set(ch_users.get(ckey, ())), u_base=int(pair[1]))
            acc.channels[ckey] = ch
        return acc


@dataclass
class Snapshot:
    hours: list  # (date, key, record, version, closed)
    user_deltas: dict  # date -> {uid: n}
    joins: dict
    leaves: dict
    open_state: dict

    @property
    def empty(self) -> bool:
        return not (self.hours or self.user_deltas or self.joins or self.leaves)


class GuildTracker:
    def __init__(self, floor_ts: float = 0.0):
        self.accs: dict[tuple[str, str], HourAcc] = {}
        self.floor_ts = float(floor_ts)
        self.last_message_ts: float | None = None
        self.user_deltas: dict[str, Counter] = defaultdict(Counter)
        self.joins: Counter = Counter()
        self.leaves: Counter = Counter()
        self._window: deque = deque()
        self._wcount: Counter = Counter()
        self._mono = 0.0
        self.changes = 0  # bumped on every event; lets the cog skip no-op flushes

    # -- recording ------------------------------------------------------
    def record(self, ts: float, uid: int, channel_key: str) -> None:
        # Keep time monotonic (late-processed events shift by milliseconds, never
        # backwards) and never write into an hour that was already finalised.
        ts = max(float(ts), self.floor_ts, self._mono)
        self._mono = ts
        self.last_message_ts = ts

        self._window.append((ts, uid))
        self._wcount[uid] += 1
        self._prune(ts)
        concurrency = len(self._wcount)

        slot = slot_for_ts(ts)
        acc = self.accs.get((slot.date, slot.key))
        if acc is None:
            acc = HourAcc(slot.date, slot.key, slot.hour, slot.start_ts, slot.end_ts)
            self.accs[(slot.date, slot.key)] = acc
        acc.add(ts, uid, channel_key, concurrency)
        self.user_deltas[slot.date][uid] += 1
        self.changes += 1

    def record_join(self, ts: float) -> None:
        self.joins[date_key_for_ts(ts)] += 1
        self.changes += 1

    def record_leave(self, ts: float) -> None:
        self.leaves[date_key_for_ts(ts)] += 1
        self.changes += 1

    def hydrate_slot(self, slot, rec: dict, users: Iterable[int] = (), ch_users: dict | None = None) -> None:
        """Restore the in-progress hour from disk (never overwrites a live one)."""
        if (slot.date, slot.key) in self.accs:
            return
        acc = HourAcc.from_record(slot, rec, users, ch_users)
        self.accs[(slot.date, slot.key)] = acc
        if acc.last_ts is not None:
            self._mono = max(self._mono, acc.last_ts)
            self.last_message_ts = max(self.last_message_ts or 0.0, acc.last_ts)

    # -- live reads -----------------------------------------------------
    def _prune(self, now_ts: float) -> None:
        cutoff = now_ts - CONCURRENCY_WINDOW_SECONDS
        while self._window and self._window[0][0] < cutoff:
            _, uid = self._window.popleft()
            self._wcount[uid] -= 1
            if self._wcount[uid] <= 0:
                del self._wcount[uid]

    def concurrency_now(self, now_ts: float) -> int:
        self._prune(now_ts)
        return len(self._wcount)

    def open_acc(self, now_ts: float) -> HourAcc | None:
        slot = slot_for_ts(now_ts)
        return self.accs.get((slot.date, slot.key))

    # -- persistence handshake ------------------------------------------
    def snapshot(self, now_ts: float) -> Snapshot:
        hours = []
        open_slots = []
        for (d, k), acc in self.accs.items():
            closed = acc.end_ts <= now_ts
            hours.append((d, k, acc.to_record(), acc.version, closed))
            if not closed:
                open_slots.append(
                    {
                        "date": d,
                        "key": k,
                        "users": sorted(acc.users),
                        "ch_users": {ck: sorted(c.users) for ck, c in acc.channels.items()},
                    }
                )
        return Snapshot(
            hours=hours,
            user_deltas={d: dict(c) for d, c in self.user_deltas.items() if c},
            joins={d: n for d, n in self.joins.items() if n},
            leaves={d: n for d, n in self.leaves.items() if n},
            open_state={"slots": open_slots},
        )

    def commit(self, snap: Snapshot) -> None:
        """Call only after `snap` was durably written."""
        for d, k, _rec, version, closed in snap.hours:
            acc = self.accs.get((d, k))
            # a straggler that arrived while the write was in flight bumps the
            # version -- keep that hour so the next flush writes it
            if closed and acc is not None and acc.version == version:
                del self.accs[(d, k)]
                self.floor_ts = max(self.floor_ts, float(acc.end_ts))
        for d, users in snap.user_deltas.items():
            counter = self.user_deltas.get(d)
            if counter is None:
                continue
            for uid, n in users.items():
                counter[uid] -= n
                if counter[uid] <= 0:
                    del counter[uid]
            if not counter:
                del self.user_deltas[d]
        for src, done in ((self.joins, snap.joins), (self.leaves, snap.leaves)):
            for d, n in done.items():
                src[d] -= n
                if src[d] <= 0:
                    del src[d]
