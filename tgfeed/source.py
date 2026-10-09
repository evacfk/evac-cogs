"""The Telegram side: read-only, through a user session (Telethon).

Safety posture, by construction:
- Never calls `client.start()` or any login method. It only `connect()`s to an
  existing session file made once by login.py, and refuses to run if that session
  is not authorised (SourceAuthError). A bad session is never retried in a loop.
- Never sends, joins, reads-marks or edits anything. Only history reads, the topic
  list and downloads.
- `flood_sleep_threshold=0` so every FloodWait surfaces to the cog, which owns the
  cool-down (Telethon would otherwise sleep silently and carry on).
- `receive_updates=False`: no live update stream, just polling.
- One history scan per cycle for the whole group (not one request stream per topic).

`classify_message` / `msg_topic_id` are duck-typed and need no telethon import.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from . import constants
from .models import GroupInfo, MediaItem, TopicInfo

log = logging.getLogger("red.tgfeed.source")


class SourceError(Exception):
    """Anything that went wrong talking to Telegram (retry later)."""


class SourceFlood(SourceError):
    def __init__(self, seconds: int):
        super().__init__(f"Telegram asked us to wait {seconds}s")
        self.seconds = int(seconds)


class SourceAuthError(SourceError):
    """The session is missing, expired or revoked. Needs a human to log in again."""


# Read-only, no live update stream, and every FloodWait surfaces to the cog (it owns the cool-down).
CLIENT_KWARGS = dict(
    receive_updates=False, flood_sleep_threshold=0,
    request_retries=3, connection_retries=3, retry_delay=5, auto_reconnect=True,
)


def _forum_request_candidates() -> list:
    """(request class, name of its peer argument). Telegram moved getForumTopics from
    `channels` to `messages` (and renamed `channel` to `peer`), so support both layouts."""
    found = []
    for module, arg in (("messages", "peer"), ("channels", "channel")):
        try:
            mod = __import__(f"telethon.tl.functions.{module}", fromlist=["GetForumTopicsRequest"])
        except ImportError:
            continue
        cls = getattr(mod, "GetForumTopicsRequest", None)
        if cls is not None:
            found.append((cls, arg))
    return found


def build_forum_topics_request(entity, offset_date, offset_id, offset_topic, limit=100):
    candidates = _forum_request_candidates()
    if not candidates:
        raise SourceError("This Telethon version can't list forum topics; update it (`pip install -U telethon`).")
    cls, arg = candidates[0]
    return cls(**{arg: entity}, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=limit)


# -- Classification (pure, duck-typed) ------------------------------------------------


def msg_topic_id(msg) -> int:
    """Forum topic a message belongs to. Posts directly in a topic carry the topic id
    as reply_to_msg_id; replies inside it carry it as reply_to_top_id. Anything else
    is in General (id 1)."""
    header = getattr(msg, "reply_to", None)
    if header is not None and getattr(header, "forum_topic", False):
        topic = getattr(header, "reply_to_top_id", None) or getattr(header, "reply_to_msg_id", None)
        if topic:
            return int(topic)
    return constants.GENERAL_TOPIC_ID


def classify_message(msg) -> Optional[MediaItem]:
    """A MediaItem for a plain photo or video; None for everything else (text,
    stickers, round videos, documents, link previews, self-destructing media).
    Reads no text, caption, sender or file name."""
    media = getattr(msg, "media", None)
    if media is None or getattr(media, "ttl_seconds", None):
        return None
    if getattr(msg, "sticker", None):
        return None
    cls = type(media).__name__
    file = getattr(msg, "file", None)
    if cls == "MessageMediaPhoto" and getattr(msg, "photo", None):
        kind, ext = constants.KIND_PHOTO, constants.PHOTO_EXT
    elif cls == "MessageMediaDocument" and getattr(msg, "video", None) and not getattr(msg, "video_note", None):
        mime = (getattr(file, "mime_type", "") or "").lower()
        kind, ext = constants.KIND_VIDEO, constants.MIME_EXTENSIONS.get(mime, constants.DEFAULT_VIDEO_EXT)
    else:
        return None
    date = getattr(msg, "date", None)
    return MediaItem(
        msg_id=int(msg.id),
        topic_id=msg_topic_id(msg),
        kind=kind,
        grouped_id=getattr(msg, "grouped_id", None),
        size=int(getattr(file, "size", 0) or 0),
        duration=float(getattr(file, "duration", 0) or 0),
        width=int(getattr(file, "width", 0) or 0),
        height=int(getattr(file, "height", 0) or 0),
        ext=ext,
        date=date.timestamp() if date is not None else 0.0,
        ref=msg,
    )


# -- Telethon implementation ----------------------------------------------------------


class TelethonSource:
    def __init__(self, api_id: int, api_hash: str, session_path: str):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session_path = session_path
        self._client = None
        self._entity = None

    # -- connection ---------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    async def connect(self) -> None:
        if self.connected:
            return
        if not os.path.exists(self.session_path + ".session"):
            raise SourceAuthError("No Telegram session file yet. Run the one-time login (see the cog's install message).")
        from telethon import TelegramClient

        self._client = TelegramClient(self.session_path, self.api_id, self.api_hash, **CLIENT_KWARGS)
        async with self._guard():
            await self._client.connect()
            if not await self._client.is_user_authorized():
                await self._client.disconnect()
                self._client = None
                raise SourceAuthError("The Telegram session is no longer logged in. Run the one-time login again.")

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _guard(self):
        return _Guard()

    # -- reads ----------------------------------------------------------------------

    async def resolve_group(self, link_or_id) -> GroupInfo:
        """Find a group the account is ALREADY in. Never joins one."""
        async with self._guard():
            try:
                entity = await self._client.get_entity(link_or_id)
            except ValueError as exc:
                raise SourceError(f"Couldn't resolve that group (is this account a member?): {exc}") from exc
        self._entity = entity
        return GroupInfo(
            id=int(entity.id), title=getattr(entity, "title", "") or "",
            username=getattr(entity, "username", None), is_forum=bool(getattr(entity, "forum", False)),
        )

    async def _group_entity(self, group: GroupInfo):
        if self._entity is None or int(self._entity.id) != group.id:
            from telethon.tl.types import PeerChannel

            async with self._guard():
                try:
                    self._entity = await self._client.get_entity(PeerChannel(group.id))
                except ValueError:
                    # Not in the session's entity cache (fresh session file): one pass over
                    # the chat list repopulates it. Read-only, and only ever needed once.
                    self._entity = None
                    async for dialog in self._client.iter_dialogs():
                        if int(getattr(dialog.entity, "id", 0)) == group.id:
                            self._entity = dialog.entity
                            break
            if self._entity is None:
                raise SourceError("That group isn't among this account's chats any more.")
        return self._entity

    async def list_topics(self, group: GroupInfo) -> list:
        entity = await self._group_entity(group)
        topics: dict = {}
        offset_date, offset_id, offset_topic = None, 0, 0
        for _ in range(20):
            async with self._guard():
                res = await self._client(build_forum_topics_request(entity, offset_date, offset_id, offset_topic, 100))
            batch = [t for t in res.topics if hasattr(t, "title")]
            for t in batch:
                topics[int(t.id)] = TopicInfo(id=int(t.id), title=t.title)
            if len(res.topics) < 100 or not res.topics:
                break
            last = res.topics[-1]
            by_id = {m.id: m for m in res.messages}
            top = by_id.get(getattr(last, "top_message", 0))
            offset_topic, offset_id = int(last.id), int(getattr(last, "top_message", 0) or 0)
            offset_date = top.date if top is not None else None
        return sorted(topics.values(), key=lambda t: t.id)

    async def latest_id(self, group: GroupInfo) -> int:
        """Newest message id in the whole group. Message ids only ever rise group-wide,
        so this is a valid 'from now on' cursor for any topic."""
        entity = await self._group_entity(group)
        async with self._guard():
            async for message in self._client.iter_messages(entity, limit=1):
                return int(message.id)
        return 0

    async def fetch_new(self, group: GroupInfo, min_id: int, ceiling: int = constants.SCAN_CEILING):
        """(items, scan_max_id): every photo/video newer than `min_id`, any topic,
        oldest first. One scan for the whole group. Only the media items are kept;
        text is read by the transport and dropped unseen."""
        entity = await self._group_entity(group)
        items: list = []
        scan_max = min_id
        async with self._guard():
            async for message in self._client.iter_messages(
                entity, min_id=min_id, limit=ceiling, wait_time=constants.SCAN_WAIT_SECONDS
            ):
                scan_max = max(scan_max, int(message.id))
                item = classify_message(message)
                if item is not None:
                    items.append(item)
        items.sort(key=lambda i: i.msg_id)
        return items, scan_max

    # -- downloads ------------------------------------------------------------------

    async def download_bytes(self, item: MediaItem) -> bytes:
        async with self._guard():
            data = await item.ref.download_media(file=bytes)
        if not data:
            raise SourceError("Telegram returned no data for a photo")
        return data

    async def download_file(self, item: MediaItem, path: str) -> str:
        async with self._guard():
            out = await item.ref.download_media(file=path)
        if not out or not os.path.exists(out):
            raise SourceError("Telegram returned no data for a video")
        return str(out)


class _Guard:
    """Translate Telethon's exceptions into ours, so the rest of the cog never
    imports telethon."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc is None or isinstance(exc, SourceError):
            return False
        name = type(exc).__name__
        seconds = getattr(exc, "seconds", None)
        if name in ("FloodWaitError", "FloodError", "SlowModeWaitError") and seconds is not None:
            raise SourceFlood(seconds) from exc
        if name == "PeerFloodError":
            raise SourceFlood(3600) from exc
        if name in (
            "AuthKeyError", "AuthKeyUnregisteredError", "AuthKeyInvalidError", "AuthKeyDuplicatedError",
            "SessionRevokedError", "SessionExpiredError", "UnauthorizedError",
            "UserDeactivatedError", "UserDeactivatedBanError",
        ):
            raise SourceAuthError(f"Telegram rejected the session ({name}). Run the one-time login again.") from exc
        if name in ("ChannelPrivateError", "ChannelInvalidError", "ChatForbiddenError", "UserBannedInChannelError"):
            raise SourceError(f"No access to that group ({name}).") from exc
        return False
