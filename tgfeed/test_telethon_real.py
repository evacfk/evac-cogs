"""Checks against the REAL telethon library (skipped if it isn't installed): the
objects my classifier reads, the client kwargs, and the forum-topics request in both
API layouts. No network."""
from datetime import datetime, timezone

import pytest

telethon = pytest.importorskip("telethon")
from telethon import TelegramClient  # noqa: E402
from telethon.tl import types  # noqa: E402

from tgfeed import source  # noqa: E402

DT = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def real_message(media=None, topic=7, top=None, grouped=None, text="SECRET CAPTION"):
    reply = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic, reply_to_top_id=top) if topic else None
    msg = types.Message(id=42, peer_id=types.PeerChannel(1), date=DT, message=text, media=media, reply_to=reply,
                        grouped_id=grouped, from_id=types.PeerUser(555))
    return msg


def real_photo():
    photo = types.Photo(id=1, access_hash=1, file_reference=b"", date=DT,
                        sizes=[types.PhotoSize(type="x", w=800, h=600, size=5000)], dc_id=1)
    return types.MessageMediaPhoto(photo=photo)


def real_doc(attrs, mime="video/mp4", size=9000):
    doc = types.Document(id=2, access_hash=2, file_reference=b"", date=DT, mime_type=mime, size=size, dc_id=1, attributes=attrs)
    return types.MessageMediaDocument(document=doc)


VIDEO = types.DocumentAttributeVideo(duration=12.0, w=1280, h=720)


def test_real_photo_message_is_classified_and_carries_no_text():
    item = source.classify_message(real_message(real_photo(), grouped=77))
    assert item.kind == "photo" and item.topic_id == 7 and item.grouped_id == 77 and item.msg_id == 42
    assert "SECRET CAPTION" not in repr(item) and "555" not in repr(vars(item).keys())


def test_real_video_message():
    item = source.classify_message(real_message(real_doc([VIDEO])))
    assert item.kind == "video" and item.ext == ".mp4" and item.duration == 12.0 and item.height == 720 and item.size == 9000


def test_real_telegram_gif_counts_as_a_clip():
    item = source.classify_message(real_message(real_doc([types.DocumentAttributeAnimated(), VIDEO])))
    assert item is not None and item.kind == "video"


def test_real_video_sticker_and_round_video_and_plain_file_are_skipped():
    sticker = types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty())
    assert source.classify_message(real_message(real_doc([sticker, VIDEO], mime="video/webm"))) is None
    round_video = types.DocumentAttributeVideo(duration=5.0, w=240, h=240, round_message=True)
    assert source.classify_message(real_message(real_doc([round_video]))) is None
    assert source.classify_message(real_message(real_doc([types.DocumentAttributeFilename(file_name="x.pdf")], mime="application/pdf"))) is None


def test_real_text_only_message_is_skipped():
    assert source.classify_message(real_message(None)) is None


def test_real_topic_ids():
    assert source.msg_topic_id(real_message(real_photo(), topic=7)) == 7
    assert source.msg_topic_id(real_message(real_photo(), topic=99, top=7)) == 7
    assert source.msg_topic_id(real_message(real_photo(), topic=None)) == 1


def test_client_kwargs_are_accepted_by_the_real_client(tmp_path):
    client = TelegramClient(str(tmp_path / "s"), 12345, "hash", **source.CLIENT_KWARGS)
    assert client is not None


def test_forum_request_builds_with_the_installed_layout():
    req = source.build_forum_topics_request(types.InputPeerChannel(channel_id=1, access_hash=2), None, 0, 0, 100)
    assert type(req).__name__ == "GetForumTopicsRequest" and req.limit == 100


def test_forum_request_supports_the_older_channels_layout(monkeypatch):
    seen = {}

    class OldReq:
        def __init__(self, channel, offset_date, offset_id, offset_topic, limit):
            seen.update(channel=channel, limit=limit)

    monkeypatch.setattr(source, "_forum_request_candidates", lambda: [(OldReq, "channel")])
    source.build_forum_topics_request("ENTITY", None, 0, 0, 50)
    assert seen == {"channel": "ENTITY", "limit": 50}


def test_forum_request_without_support_says_what_to_do(monkeypatch):
    monkeypatch.setattr(source, "_forum_request_candidates", lambda: [])
    with pytest.raises(source.SourceError, match="update"):
        source.build_forum_topics_request("E", None, 0, 0)


def test_guard_translates_real_telethon_errors():
    import asyncio

    from telethon import errors

    async def run(exc):
        async with source._Guard():
            raise exc

    with pytest.raises(source.SourceFlood) as flood:
        asyncio.run(run(errors.FloodWaitError(request=None, capture=90)))
    assert flood.value.seconds == 90
    with pytest.raises(source.SourceAuthError):
        asyncio.run(run(errors.AuthKeyUnregisteredError(request=None)))
    with pytest.raises(source.SourceAuthError):
        asyncio.run(run(errors.SessionRevokedError(request=None)))
    with pytest.raises(source.SourceError):
        asyncio.run(run(errors.ChannelPrivateError(request=None)))
