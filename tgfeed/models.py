"""Dataclasses at the Config / pipeline boundary. No discord/redbot/telethon imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MediaItem:
    """One photo or video from Telegram, stripped to what posting needs.
    There is deliberately no field for text, caption, sender or file name."""

    msg_id: int
    topic_id: int
    kind: str
    grouped_id: Optional[int] = None
    size: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    ext: str = ".jpg"
    date: float = 0.0
    ref: Any = field(default=None, repr=False, compare=False)   # the source's own handle, opaque to us


@dataclass
class TopicInfo:
    id: int
    title: str


@dataclass
class GroupInfo:
    id: int
    title: str
    username: Optional[str] = None
    is_forum: bool = False


@dataclass
class PreparedFile:
    name: str
    size: int
    data: Optional[bytes] = None
    path: Optional[str] = None


@dataclass
class TopicMapping:
    topic_id: int
    title: str = ""
    channel_id: int = 0
    paused: bool = False
    cursor: int = 0                   # highest Telegram message id handled for this topic
    added_ts: float = 0.0
    last_post_ts: float = 0.0
    last_error: Optional[str] = None
    posted_total: int = 0

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id, "title": self.title, "channel_id": self.channel_id,
            "paused": self.paused, "cursor": self.cursor, "added_ts": self.added_ts,
            "last_post_ts": self.last_post_ts, "last_error": self.last_error,
            "posted_total": self.posted_total,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TopicMapping":
        return cls(
            topic_id=int(raw["topic_id"]), title=raw.get("title", "") or "",
            channel_id=int(raw.get("channel_id") or 0), paused=bool(raw.get("paused", False)),
            cursor=int(raw.get("cursor") or 0), added_ts=float(raw.get("added_ts") or 0.0),
            last_post_ts=float(raw.get("last_post_ts") or 0.0), last_error=raw.get("last_error"),
            posted_total=int(raw.get("posted_total") or 0),
        )


@dataclass
class TopicStats:
    """What one topic did in one cycle; summed into the `status` line."""

    posted_files: int = 0
    posted_messages: int = 0
    skipped_oversize: int = 0
    skipped_too_long: int = 0
    skipped_failed: int = 0
    shrunk: int = 0
    errors: int = 0

    def add(self, other: "TopicStats") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))
