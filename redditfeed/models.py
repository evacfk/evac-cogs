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

    def to_dict(self) -> dict:
        return {
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
