"""classify_message / msg_topic_id on duck-typed messages. Proves the media-only rule
(and the exclusions) on fakes; the real Telethon objects are NOT exercised here."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from tgfeed import source


class MessageMediaPhoto: ...
class MessageMediaDocument: ...
class MessageMediaWebPage: ...


def msg(media, **kw):
    base = dict(id=10, media=media, sticker=None, photo=None, video=None, video_note=None, grouped_id=None,
                file=NS(size=1234, duration=0, width=640, height=480, mime_type="image/jpeg"),
                reply_to=None, date=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
                message="THE CAPTION", sender_id=555, post_author="someone")
    base.update(kw)
    return NS(**base)


def test_photo():
    item = source.classify_message(msg(MessageMediaPhoto(), photo=object()))
    assert item.kind == "photo" and item.msg_id == 10 and item.ext == ".jpg" and item.height == 480


def test_video_mp4_and_mkv_extensions():
    f = NS(size=9_000_000, duration=12.5, width=1280, height=720, mime_type="video/mp4")
    item = source.classify_message(msg(MessageMediaDocument(), video=object(), file=f))
    assert item.kind == "video" and item.ext == ".mp4" and item.duration == 12.5
    f2 = NS(size=1, duration=1, width=1, height=1, mime_type="video/x-matroska")
    assert source.classify_message(msg(MessageMediaDocument(), video=object(), file=f2)).ext == ".mkv"


def test_album_id_carried():
    assert source.classify_message(msg(MessageMediaPhoto(), photo=object(), grouped_id=42)).grouped_id == 42


def test_text_only_and_link_previews_are_not_media():
    assert source.classify_message(msg(None)) is None
    assert source.classify_message(msg(MessageMediaWebPage(), photo=object())) is None


def test_stickers_round_videos_documents_and_self_destruct_excluded():
    assert source.classify_message(msg(MessageMediaDocument(), video=object(), sticker=object())) is None
    assert source.classify_message(msg(MessageMediaDocument(), video=object(), video_note=object())) is None
    assert source.classify_message(msg(MessageMediaDocument())) is None     # a plain file
    media = MessageMediaPhoto()
    media.ttl_seconds = 10
    assert source.classify_message(msg(media, photo=object())) is None


def test_item_holds_no_text_sender_or_caption():
    item = source.classify_message(msg(MessageMediaPhoto(), photo=object()))
    flat = repr(vars(item).keys())
    for word in ("caption", "message", "text", "sender", "author", "name"):
        assert word not in flat
    assert "THE CAPTION" not in repr(item) and "someone" not in repr(item)


class TestTopicId:
    def test_post_directly_in_topic(self):
        assert source.msg_topic_id(NS(reply_to=NS(forum_topic=True, reply_to_msg_id=7, reply_to_top_id=None))) == 7

    def test_reply_inside_topic_uses_top_id(self):
        assert source.msg_topic_id(NS(reply_to=NS(forum_topic=True, reply_to_msg_id=99, reply_to_top_id=7))) == 7

    def test_general_has_no_forum_header(self):
        assert source.msg_topic_id(NS(reply_to=None)) == 1
        assert source.msg_topic_id(NS(reply_to=NS(forum_topic=False, reply_to_msg_id=3, reply_to_top_id=None))) == 1
