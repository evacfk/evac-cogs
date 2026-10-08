"""Dataclasses + Config-boundary (de)serialization for redditfeed. No discord/redbot imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SubredditMapping:
    """One subreddit's config. Many subreddits can share a channel list, but each
    subreddit gets its own mapping/state -- that's what lets `feet` and `feetish`
    both post into #feet while tracking their own last-poll/last-post independently.
    """

    subreddit: str                      # normalized, lowercase, no leading r/
    channel_ids: list[int] = field(default_factory=list)
    require_keywords: list[str] = field(default_factory=list)   # OR-matched, case-insensitive
    block_keywords: list[str] = field(default_factory=list)     # any match blocks the post
    paused: bool = False
    last_poll_ts: Optional[float] = None
    last_post_found_ts: Optional[float] = None
    last_post_id: Optional[str] = None
    last_error: Optional[str] = None
    approval: str = "manual"            # constants.APPROVAL_*; manual = mod queue first
    added_ts: Optional[float] = None    # no backfill: manual mode never queues posts older than this

    def to_dict(self) -> dict:
        return {
            "approval": self.approval,
            "added_ts": self.added_ts,
            "subreddit": self.subreddit,
            "channel_ids": list(self.channel_ids),
            "require_keywords": list(self.require_keywords),
            "block_keywords": list(self.block_keywords),
            "paused": self.paused,
            "last_poll_ts": self.last_poll_ts,
            "last_post_found_ts": self.last_post_found_ts,
            "last_post_id": self.last_post_id,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubredditMapping":
        return cls(
            subreddit=data["subreddit"],
            channel_ids=list(data.get("channel_ids", [])),
            require_keywords=list(data.get("require_keywords", [])),
            block_keywords=list(data.get("block_keywords", [])),
            paused=bool(data.get("paused", False)),
            last_poll_ts=data.get("last_poll_ts"),
            last_post_found_ts=data.get("last_post_found_ts"),
            last_post_id=data.get("last_post_id"),
            last_error=data.get("last_error"),
            approval=data.get("approval") if data.get("approval") in ("manual", "auto") else "manual",
            added_ts=data["added_ts"] if "added_ts" in data else data.get("last_poll_ts"),
        )


@dataclass
class MediaItem:
    """One postable unit extracted from a Reddit post. A single post can yield
    multiple MediaItems (galleries); video/redgifs posts yield a single link item.
    """

    kind: str            # one of constants.MEDIA_KIND_*
    url: str
    is_link_only: bool = False   # True => post the URL as plain text (let Discord unfurl it)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "url": self.url, "is_link_only": self.is_link_only}

    @classmethod
    def from_dict(cls, data: dict) -> "MediaItem":
        return cls(kind=data["kind"], url=data["url"], is_link_only=bool(data.get("is_link_only", False)))


@dataclass
class QueueEntry:
    """One post waiting for (or already past) a moderator decision. Stored in
    Config keyed by the queue message id, so the persistent buttons can find it."""

    subreddit: str
    post: dict                                   # trimmed: id, title, score, permalink, created_utc
    media: list = field(default_factory=list)    # MediaItem.to_dict() each
    channel_ids: list = field(default_factory=list)
    created_ts: float = 0.0
    status: str = "pending"
    resolved_ts: Optional[float] = None
    resolved_by: Optional[int] = None
    queue_channel_id: Optional[int] = None

    def media_items(self) -> list:
        return [MediaItem.from_dict(m) for m in self.media]

    def to_dict(self) -> dict:
        return {
            "subreddit": self.subreddit,
            "post": dict(self.post),
            "media": list(self.media),
            "channel_ids": list(self.channel_ids),
            "created_ts": self.created_ts,
            "status": self.status,
            "resolved_ts": self.resolved_ts,
            "resolved_by": self.resolved_by,
            "queue_channel_id": self.queue_channel_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueueEntry":
        return cls(
            subreddit=data["subreddit"],
            post=dict(data.get("post", {})),
            media=list(data.get("media", [])),
            channel_ids=[int(c) for c in data.get("channel_ids", [])],
            created_ts=float(data.get("created_ts", 0)),
            status=data.get("status", "pending"),
            resolved_ts=data.get("resolved_ts"),
            resolved_by=data.get("resolved_by"),
            queue_channel_id=data.get("queue_channel_id"),
        )
