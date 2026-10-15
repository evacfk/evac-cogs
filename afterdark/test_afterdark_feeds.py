"""Afterdark's unified interests + feeds flow, against fake RedditFeed / TGFeed cogs."""
from types import SimpleNamespace

import pytest

from afterdark.settings_panel import PanelError
from afterdark.test_afterdark_cog import FEET_CHANNEL, FakeChannel, set_cfg, w  # noqa: F401

CAT = 777


class ForeignError(Exception):
    pass


ForeignError.__name__ = "PanelError"


class FakeMappings:
    def __init__(self, data):
        self.data = data

    async def __call__(self):
        return self.data


class FakeReddit:
    def __init__(self):
        self.config = SimpleNamespace(mappings=FakeMappings({}))
        self.fail = False

    async def _apply_add(self, guild, member, text, channels):
        if self.fail:
            raise ForeignError("not NSFW")
        self.config.mappings.data[text] = {"channel_ids": [c.id for c in channels], "paused": False, "approval": "manual"}
        return f"Added r/{text}."

    async def _mapping_or_raise(self, name):
        if name not in self.config.mappings.data:
            raise ForeignError("unknown")
        raw = self.config.mappings.data[name]
        return SimpleNamespace(paused=raw["paused"], require_keywords=[], block_keywords=[])

    async def _apply_feed(self, guild, member, name, req, block, channels):
        self.config.mappings.data[name]["channel_ids"] = [c.id for c in channels]
        return "moved"

    async def _apply_remove(self, guild, member, name):
        self.config.mappings.data.pop(name)
        return "removed"

    async def _apply_pause(self, guild, member, name, paused):
        self.config.mappings.data[name]["paused"] = paused
        return "paused" if paused else "resumed"

    async def _apply_switch_mode(self, guild, member, name):
        raw = self.config.mappings.data[name]
        raw["approval"] = "auto" if raw["approval"] == "manual" else "manual"
        return raw["approval"]


class FakeTG:
    def __init__(self):
        self.config = SimpleNamespace(mappings=FakeMappings({}))

    async def _apply_map(self, guild, member, topic, channel=None):
        self.config.mappings.data[topic] = {"channel_id": channel.id, "paused": False, "title": f"T{topic}"}
        return "mapped"

    async def _mapped_or_raise(self, topic):
        return SimpleNamespace(paused=self.config.mappings.data[topic]["paused"])

    async def _apply_unmap(self, guild, member, topic):
        self.config.mappings.data.pop(topic)
        return "unmapped"

    async def _apply_pause_topic(self, guild, member, topic):
        self.config.mappings.data[topic]["paused"] = True
        return "paused"

    async def _apply_resume_topic(self, guild, member, topic):
        self.config.mappings.data[topic]["paused"] = False
        return "resumed"


@pytest.fixture
def f(w):
    w.reddit, w.tg = FakeReddit(), FakeTG()
    w.bot.cogs.update({"RedditFeed": w.reddit, "TGFeed": w.tg})
    w.guild.add_channel(CAT, "cat")
    w.guild.default_role = object()
    w.guild.me = w.member("bot", roles=[])
    w.created = []

    async def create(name, **kw):
        ch = FakeChannel(5000 + len(w.created), name)
        ch.deleted = False

        async def delete(reason=None):
            ch.deleted = True
        ch.delete = delete
        ch.mention = f"<#{ch.id}>"
        w.guild.channels[ch.id] = ch
        w.created.append(ch)
        return ch
    w.guild.create_text_channel = create
    return w


async def save(f, item, values, picked=()):
    return await f.cog._apply_feed_save(f.guild, f.member("admin"), item, values, list(picked))


async def test_add_reddit_to_picked_channel_and_interest(f):
    await set_cfg(f, interests={"feet": {"key": "feet", "name": "Feet", "emoji": "x", "channel_id": 1}})
    msg = await save(f, None, {"reddit": "feet", "interest": "feet"}, [f.feet])
    assert f.reddit.config.mappings.data["feet"]["channel_ids"] == [FEET_CHANNEL]
    saved = (await f.cog.config.guild(f.guild).interests())["feet"]
    assert FEET_CHANNEL in saved["extra_channel_ids"] and "Feet interest" in msg


async def test_add_without_interest_and_needs_exactly_one_source(f):
    await save(f, None, {"telegram": "5"}, [f.feet])
    assert f.tg.config.mappings.data["5"]["channel_id"] == FEET_CHANNEL
    for vals in ({}, {"reddit": "a", "telegram": "1"}):
        with pytest.raises(PanelError):
            await save(f, None, vals, [f.feet])
    with pytest.raises(PanelError):
        await save(f, None, {"reddit": "a"}, [])


async def test_new_channel_needs_category_then_is_created(f):
    with pytest.raises(PanelError, match="category"):
        await save(f, None, {"reddit": "a", "new_channel": "My Chan"})
    await set_cfg(f, interest_category_id=CAT)
    msg = await save(f, None, {"reddit": "a", "new_channel": "My Chan"})
    assert f.created[0].name == "\U0001F916・my-chan" and f.reddit.config.mappings.data["a"]["channel_ids"] == [f.created[0].id]
    assert "Created" in msg


async def test_failed_feed_rolls_back_created_channel_and_converts_error(f):
    await set_cfg(f, interest_category_id=CAT)
    f.reddit.fail = True
    with pytest.raises(PanelError, match="not NSFW"):
        await save(f, None, {"reddit": "a", "new_channel": "x"})
    assert f.created[0].deleted and not f.reddit.config.mappings.data


async def test_telegram_needs_single_channel(f):
    with pytest.raises(PanelError):
        await save(f, None, {"telegram": "1"}, [f.feet, f.guild.add_channel(900001, "b")])


async def test_edit_remove_pause_approval(f):
    await save(f, None, {"reddit": "a"}, [f.feet])
    await save(f, None, {"telegram": "9"}, [f.feet])
    other = f.guild.add_channel(900002, "o")
    await save(f, "r:a", {}, [other])
    assert f.reddit.config.mappings.data["a"]["channel_ids"] == [900002]
    await save(f, "t:9", {}, [other])
    assert f.tg.config.mappings.data["9"]["channel_id"] == 900002
    await f.cog._apply_feed_pause(f.guild, f.member("a"), "r:a")
    assert f.reddit.config.mappings.data["a"]["paused"]
    await f.cog._apply_feed_pause(f.guild, f.member("a"), "t:9")
    assert f.tg.config.mappings.data["9"]["paused"]
    await f.cog._apply_feed_approval(f.guild, f.member("a"), "r:a")
    assert f.reddit.config.mappings.data["a"]["approval"] == "auto"
    with pytest.raises(PanelError):
        await f.cog._apply_feed_approval(f.guild, f.member("a"), "t:9")
    await f.cog._apply_feed_remove(f.guild, f.member("a"), "r:a")
    await f.cog._apply_feed_remove(f.guild, f.member("a"), "t:9")
    assert not f.reddit.config.mappings.data and not f.tg.config.mappings.data


async def test_deleting_interest_pauses_only_exclusive_feeds(f):
    other = f.guild.add_channel(900003, "shared")
    await set_cfg(f, interests={
        "feet": {"key": "feet", "name": "Feet", "emoji": "x", "channel_id": FEET_CHANNEL},
        "pics": {"key": "pics", "name": "Pics", "emoji": "y", "channel_id": 900003},
    })
    await save(f, None, {"reddit": "only"}, [f.feet])
    await save(f, None, {"reddit": "kept"}, [other])
    msg = await f.cog._apply_interest_remove(f.guild, f.member("admin"), "feet")
    assert f.reddit.config.mappings.data["only"]["paused"]
    assert not f.reddit.config.mappings.data["kept"]["paused"]
    assert "Paused 1" in msg and f.feet.id in f.guild.channels
    assert "feet" not in await f.cog.config.guild(f.guild).interests()


async def test_feed_items_list_adopts_existing(f):
    f.reddit.config.mappings.data["old"] = {"channel_ids": [FEET_CHANNEL], "paused": True, "approval": "auto"}
    items = await f.cog._feed_items(f.guild)
    assert items[0][0] == "r:old" and "PAUSED" in items[0][2] and "auto approval" in items[0][2]


async def test_missing_feed_cog_is_a_clear_error(f):
    f.bot.cogs.clear()
    with pytest.raises(PanelError, match="isn't loaded"):
        await save(f, None, {"reddit": "a"}, [f.feet])
