"""Dataclasses with to_dict()/from_dict() at the Config boundary."""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Interest:
    """One opt-in interest channel (e.g. feet). Access is a role, a per-user
    overwrite, or both depending on the guild's access_mode."""

    key: str
    name: str
    emoji: str
    channel_id: int                      # the first channel (kept as-is for old saved data)
    role_id: Optional[int] = None
    extra_channel_ids: List[int] = field(default_factory=list)   # any further channels

    @property
    def channel_ids(self) -> List[int]:
        """Every channel this interest opens, first one first, no duplicates."""
        out = [self.channel_id]
        out += [c for c in self.extra_channel_ids if c not in out]
        return out

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "emoji": self.emoji,
            "channel_id": self.channel_id,
            "role_id": self.role_id,
            "extra_channel_ids": list(self.extra_channel_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Interest":
        role = data.get("role_id")
        return cls(
            key=str(data["key"]),
            name=str(data.get("name") or data["key"]),
            emoji=str(data.get("emoji") or ""),
            channel_id=int(data["channel_id"]),
            role_id=int(role) if role else None,
            extra_channel_ids=[int(c) for c in (data.get("extra_channel_ids") or [])],
        )


@dataclass
class Invite:
    """An outstanding Rabbit Hole invitation DM."""

    ts: float
    channel_id: Optional[int] = None
    message_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {"ts": self.ts, "channel_id": self.channel_id, "message_id": self.message_id}

    @classmethod
    def from_dict(cls, data: dict) -> "Invite":
        cid = data.get("channel_id")
        mid = data.get("message_id")
        return cls(
            ts=float(data.get("ts", 0)),
            channel_id=int(cid) if cid else None,
            message_id=int(mid) if mid else None,
        )


@dataclass
class ExcludeEntry:
    """Why/who/when a member was put on the exclude list."""

    by: int
    reason: str
    ts: float

    def to_dict(self) -> dict:
        return {"by": self.by, "reason": self.reason, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: dict) -> "ExcludeEntry":
        return cls(
            by=int(data.get("by", 0)),
            reason=str(data.get("reason") or ""),
            ts=float(data.get("ts", 0)),
        )


@dataclass
class InterestMembership:
    """A member's tracked presence in one interest channel."""

    since: float
    last: float
    warned: float = 0.0

    def to_dict(self) -> dict:
        return {"since": self.since, "last": self.last, "warned": self.warned}

    @classmethod
    def from_dict(cls, data: dict) -> "InterestMembership":
        return cls(
            since=float(data.get("since", 0)),
            last=float(data.get("last", 0)),
            warned=float(data.get("warned", 0)),
        )
