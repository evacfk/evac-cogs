"""The cog's glue against fakes: one full poll cycle, the flood / auth safety paths,
the X button, trace/takedown, and an import smoke test. Proves wiring and state
handling; it does NOT prove real Telegram, real Discord rendering or button clicks."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from tgfeed import tgfeed as tg_module
from tgfeed.models import MediaItem, TopicMapping
from tgfeed.source import SourceAuthError, SourceFlood
from tgfeed.store import PostedStore
from tgfeed.tgfeed import TGFeed

NOW = time.time()


class Val:
    """A Red Value stand-in: awaitable, async-context-manager and settable."""

    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.data)
        return _get().__await__()

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *exc):
        return False

    async def set(self, value):
        self.data = value


class Msg:
    _next = 9000

    def __init__(self, channel, **kw):
        Msg._next += 1
        self.id, self.channel, self.kw, self.deleted = Msg._next, channel, kw, False

    async def delete(self):
        self.deleted = True


class Chan:
    def __init__(self, cid, limit=10 * 1024 * 1024):
        self.id, self.sent = cid, []
        self.guild = SimpleNamespace(filesize_limit=limit)

    async def send(self, content=None, **kw):
        m = Msg(self, content=content, **kw)
        self.sent.append(m)
        return m


class Bot:
    def __init__(self, channels, mods=()):
        self.channels, self.mods = {c.id: c for c in channels}, set(mods)

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def is_mod(self, user):
        return user.id in self.mods


class FakeSource:
    def __init__(self, items=None, scan_max=0, raise_on_fetch=None):
        self.items, self.scan_max, self.raise_on_fetch = items or [], scan_max, raise_on_fetch
        self.fetches, self.connected_calls, self.disconnected = [], 0, False

    async def connect(self):
        self.connected_calls += 1

    async def disconnect(self):
        self.disconnected = True

    async def fetch_new(self, group, min_id, ceiling=0):
        self.fetches.append(min_id)
        if self.raise_on_fetch:
            raise self.raise_on_fetch
        return list(self.items), self.scan_max

    async def download_bytes(self, item):
        return b"jpeg-bytes"

    async def download_file(self, item, path):
        open(path, "wb").write(b"mp4-bytes")
        return path


CH, LOG = Chan(100), Chan(30)
GROUP = {"id": 777, "title": "Test Group", "username": None, "is_forum": True}


def make(tmp_path, items=None, scan_max=0, mapping_cursor=5, paused=False, source=None, **cfg):
    CH.sent, LOG.sent = [], []
    (tmp_path / "tmp").mkdir(exist_ok=True)
    cog = object.__new__(TGFeed)
    cog.bot = Bot([CH, LOG], mods={1})
    cog._data_dir = str(tmp_path)
    cog._store = PostedStore(str(tmp_path / "posted.db"))
    cog._source = source or FakeSource(items or [], scan_max)
    cog._source_factory = None
    cog._cycle_lock = asyncio.Lock()
    cog._fails, cog._last_cycle, cog._last_cycle_ts, cog._notified = {}, {}, 0.0, set()
    cog._poll_task = None
    values = dict(
        group=GROUP,
        mappings={"7": TopicMapping(topic_id=7, title="Cats", channel_id=100, cursor=mapping_cursor, added_ts=NOW).to_dict()},
        poll_interval_seconds=300, file_gap_min=5.0, file_gap_max=12.0, max_per_hour=60, max_per_day=500,
        paused=paused, pause_reason=None, x_button=True, log_channel_id=30, category_id=None, name_prefix="",
        view_role_ids=[], rate_log=[], flood_events=[], cooldown_until=0.0,
    )
    values.update(cfg)
    cog.config = SimpleNamespace(**{k: Val(v) for k, v in values.items()})
    return cog


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(tg_module.asyncio, "sleep", instant)


def photo(mid, topic=7, gid=None, date=NOW - 1000):
    return MediaItem(msg_id=mid, topic_id=topic, kind="photo", grouped_id=gid, date=date, ext=".jpg")


async def stored_mapping(cog, tid=7):
    return (await cog.config.mappings())[str(tid)]


# -- a full cycle ---------------------------------------------------------------------------

async def test_cycle_posts_media_only_with_x_button_and_records_origin(tmp_path):
    cog = make(tmp_path, items=[photo(11)], scan_max=15)
    await cog._run_cycle()
    assert len(CH.sent) == 1
    sent = CH.sent[0]
    assert sent.kw["content"] is None                 # no text, ever
    assert set(sent.kw) <= {"content", "files", "view", "allowed_mentions"}
    assert len(sent.kw["files"]) == 1 and sent.kw["files"][0].filename == "file-1.jpg"
    assert sent.kw["view"] is not None
    assert cog._store.get(sent.id)["tg_ids"] == [11]
    m = await stored_mapping(cog)
    assert m["cursor"] == 15 and m["posted_total"] == 1 and m["last_post_ts"] > 0
    assert len(await cog.config.rate_log()) == 1
    assert cog._last_cycle["posted_files"] == 1


async def test_cycle_scans_from_the_lowest_active_cursor_once(tmp_path):
    cog = make(tmp_path, items=[], scan_max=50)
    maps = await cog.config.mappings()
    maps["8"] = TopicMapping(topic_id=8, title="Dogs", channel_id=100, cursor=3).to_dict()
    await cog.config.mappings.set(maps)
    await cog._run_cycle()
    assert cog._source.fetches == [3]                  # one scan for the whole group
    assert (await stored_mapping(cog))["cursor"] == 50 and (await stored_mapping(cog, 8))["cursor"] == 50


async def test_x_button_can_be_off(tmp_path):
    cog = make(tmp_path, items=[photo(11)], scan_max=11, x_button=False)
    await cog._run_cycle()
    assert CH.sent[0].kw["view"] is None


async def test_items_for_unmapped_topics_are_ignored(tmp_path):
    cog = make(tmp_path, items=[photo(11, topic=99)], scan_max=11)
    await cog._run_cycle()
    assert CH.sent == [] and (await stored_mapping(cog))["cursor"] == 11


async def test_global_pause_and_cooldown_and_no_group_do_nothing(tmp_path):
    for cfg in ({"paused": True}, {"cooldown_until": time.time() + 500}, {"group": {}}):
        cog = make(tmp_path, items=[photo(11)], scan_max=11, **cfg)
        await cog._run_cycle()
        assert cog._source.fetches == [] and CH.sent == []


async def test_paused_topic_is_skipped(tmp_path):
    cog = make(tmp_path, items=[photo(11)], scan_max=11)
    maps = await cog.config.mappings()
    maps["7"]["paused"] = True
    await cog.config.mappings.set(maps)
    await cog._run_cycle()
    assert cog._source.fetches == [] and CH.sent == []


async def test_missing_destination_channel_is_reported_not_crashed(tmp_path):
    cog = make(tmp_path, items=[photo(11)], scan_max=11)
    maps = await cog.config.mappings()
    maps["7"]["channel_id"] = 424242
    await cog.config.mappings.set(maps)
    await cog._run_cycle()
    assert (await stored_mapping(cog))["last_error"] == "destination channel not found"
    assert (await stored_mapping(cog))["cursor"] == 5      # nothing lost: retried once the channel is back


async def test_pause_made_mid_cycle_is_not_undone_by_the_cycle_write(tmp_path):
    cog = make(tmp_path, items=[photo(11)], scan_max=11)

    real_send = CH.send

    async def pausing_send(content=None, **kw):
        stored = cog.config.mappings.data
        stored["7"]["paused"] = True         # a mod runs `pausetopic` while the cycle is posting
        return await real_send(content, **kw)

    CH.send = pausing_send
    try:
        await cog._run_cycle()
    finally:
        CH.send = real_send
    m = await stored_mapping(cog)
    assert m["paused"] is True and m["cursor"] == 11


async def test_topic_unmapped_mid_cycle_does_not_come_back(tmp_path):
    cog = make(tmp_path, items=[photo(11)], scan_max=11)
    real_send = CH.send

    async def unmapping_send(content=None, **kw):
        cog.config.mappings.data.pop("7")
        return await real_send(content, **kw)

    CH.send = unmapping_send
    try:
        await cog._run_cycle()
    finally:
        CH.send = real_send
    assert await cog.config.mappings() == {}


async def test_hourly_cap_limits_a_cycle_and_defers_the_rest(tmp_path):
    cog = make(tmp_path, items=[photo(11), photo(12), photo(13)], scan_max=13, max_per_hour=10)
    cog.config.rate_log = Val([time.time() - 5] * 9)       # 9 of 10 already used this hour
    await cog._run_cycle()
    assert len(CH.sent) == 1                                # one slot left
    assert (await stored_mapping(cog))["cursor"] == 11      # the rest wait for the next cycle


# -- Telegram trouble -------------------------------------------------------------------------------

async def test_flood_sets_a_cooldown_and_posts_nothing_more(tmp_path):
    cog = make(tmp_path, source=FakeSource(raise_on_fetch=SourceFlood(120)))
    await cog._run_cycle()
    until = await cog.config.cooldown_until()
    assert until >= time.time() + 120
    assert await cog.config.paused() is False and len(await cog.config.flood_events()) == 1
    await cog._run_cycle()
    assert len(cog._source.fetches) == 1                    # second cycle respected the cooldown


async def test_third_flood_in_a_day_pauses_the_feed_and_says_so(tmp_path):
    cog = make(tmp_path, source=FakeSource(raise_on_fetch=SourceFlood(30)),
               flood_events=[time.time() - 200, time.time() - 100])
    await cog._run_cycle()
    assert await cog.config.paused() is True
    assert len(LOG.sent) == 1 and "paused" in LOG.sent[0].kw["content"]


async def test_long_flood_is_announced_in_the_log_channel(tmp_path):
    cog = make(tmp_path, source=FakeSource(raise_on_fetch=SourceFlood(900)))
    await cog._run_cycle()
    assert len(LOG.sent) == 1 and LOG.sent[0].kw["content"] is not None


async def test_auth_failure_pauses_disconnects_and_announces_once(tmp_path):
    source = FakeSource(raise_on_fetch=SourceAuthError("Telegram rejected the session. Run the one-time login again."))
    cog = make(tmp_path, source=source)
    await cog._run_cycle()
    assert await cog.config.paused() is True and source.disconnected and cog._source is None
    assert len(LOG.sent) == 1
    cog._source = FakeSource(raise_on_fetch=SourceAuthError("Telegram rejected the session. Run the one-time login again."))
    await cog.config.paused.set(False)
    await cog._run_cycle()
    assert len(LOG.sent) == 1                                # same reason: no second announcement


async def test_missing_credentials_pause_with_a_clear_reason(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    cog = make(tmp_path)
    cog._source = None
    await cog._run_cycle()
    assert await cog.config.paused() is True and "TG_API_ID" in (await cog.config.pause_reason())


# -- X button, trace, takedown ------------------------------------------------------------------------

class Interaction:
    def __init__(self, message, user):
        self.message, self.user, self.said = message, user, []
        self.response = SimpleNamespace(send_message=self._say)

    async def _say(self, text, ephemeral=False):
        self.said.append((text, ephemeral))


def user(uid, manage=False):
    return SimpleNamespace(id=uid, display_name=f"user{uid}", guild_permissions=SimpleNamespace(manage_guild=manage))


async def posted_message(cog):
    cog._source = FakeSource([photo(11)], 11)
    await cog._run_cycle()
    return CH.sent[0]


async def test_x_button_refuses_non_mods_and_keeps_the_post(tmp_path):
    cog = make(tmp_path)
    msg = await posted_message(cog)
    it = Interaction(msg, user(2))
    await cog.handle_feed_x(it)
    assert not msg.deleted and "Only moderators" in it.said[0][0]


async def test_x_button_deletes_and_logs_the_telegram_origin_privately(tmp_path):
    cog = make(tmp_path)
    msg = await posted_message(cog)
    LOG.sent.clear()
    await cog.handle_feed_x(Interaction(msg, user(1)))
    assert msg.deleted
    text = LOG.sent[0].kw["content"]
    assert "https://t.me/c/777/7/11" in text and "<#100>" in text


async def test_trace_and_takedown_use_the_stored_map(tmp_path):
    cog = make(tmp_path)
    msg = await posted_message(cog)

    class Ctx:
        def __init__(self): self.sent, self.author = [], user(1)
        async def send(self, content=None, **kw): self.sent.append(content)

    def cb(name):
        cmd = getattr(TGFeed, name)
        return getattr(cmd, "callback", None) or cmd.func

    ctx = Ctx()
    await cb("tgfeed_trace")(cog, ctx, msg)
    assert "https://t.me/c/777/7/11" in ctx.sent[0]
    other = Msg(CH)
    ctx2 = Ctx()
    await cb("tgfeed_takedown")(cog, ctx2, other)
    assert not other.deleted and "isn't in the takedown map" in ctx2.sent[0]
    ctx3 = Ctx()
    await cb("tgfeed_takedown")(cog, ctx3, msg)
    assert msg.deleted and cog._store.get(msg.id) is None and ctx3.sent == ["Removed."]


# -- import smoke ------------------------------------------------------------------------------------------

def test_module_imports_and_defines_the_cog_and_commands():
    from tgfeed import tgfeed

    assert hasattr(tgfeed, "TGFeed")
    for name in ("tgfeed_version", "tgfeed_group", "tgfeed_topics", "tgfeed_map", "tgfeed_unmap",
                 "tgfeed_pausetopic", "tgfeed_resumetopic", "tgfeed_pause", "tgfeed_resume", "tgfeed_list",
                 "tgfeed_status", "tgfeed_interval", "tgfeed_gap", "tgfeed_limits", "tgfeed_xbutton",
                 "tgfeed_logchannel", "tgfeed_category", "tgfeed_prefix", "tgfeed_viewrole", "tgfeed_trace",
                 "tgfeed_takedown"):
        assert hasattr(TGFeed, name), name


def test_mapall_is_gone():
    assert not hasattr(TGFeed, "tgfeed_mapall")


# -- mapping one topic at a time ------------------------------------------------------------------------

class MapSource(FakeSource):
    def __init__(self, topics, latest=500):
        super().__init__()
        self.topics, self.latest = topics, latest

    async def list_topics(self, group):
        return self.topics

    async def latest_id(self, group):
        return self.latest


class FakeGuild:
    def __init__(self):
        self.default_role, self.me = "EVERYONE", "BOT"
        self.created, self.categories, self.roles = [], {55: SimpleNamespace(id=55, name="Telegram")}, {}

    def get_channel(self, cid):
        return self.categories.get(cid)

    def get_role(self, rid):
        return self.roles.get(rid)

    async def create_text_channel(self, name, category=None, overwrites=None, reason=None):
        chan = SimpleNamespace(id=700 + len(self.created), mention=f"<#{700 + len(self.created)}>", name=name)
        self.created.append(SimpleNamespace(name=name, category=category, overwrites=overwrites, chan=chan))
        return chan


class MapCtx:
    def __init__(self, guild):
        self.guild, self.sent, self.author = guild, [], "ME"

    async def send(self, content=None, **kw):
        self.sent.append(content)


def cbk(name):
    cmd = getattr(TGFeed, name)
    return getattr(cmd, "callback", None) or cmd.func


@pytest.fixture
def overwrite_stub(monkeypatch):
    monkeypatch.setattr(tg_module.discord, "PermissionOverwrite", lambda **kw: dict(kw), raising=False)


def map_cog(tmp_path, topics, **cfg):
    cog = make(tmp_path, mappings={}, **cfg)
    cog._source = MapSource(topics)
    return cog


from tgfeed.models import TopicInfo  # noqa: E402

TOPICS = [TopicInfo(7, "Cats"), TopicInfo(8, "Dogs")]


async def test_map_one_topic_creates_a_channel_only_the_creator_can_see(tmp_path, overwrite_stub):
    cog = map_cog(tmp_path, TOPICS, category_id=55, name_prefix="tg-")
    guild = FakeGuild()
    ctx = MapCtx(guild)
    await cbk("tgfeed_map")(cog, ctx, "Cats")
    assert len(guild.created) == 1 and guild.created[0].name == "tg-cats" and guild.created[0].category.id == 55
    ow = guild.created[0].overwrites
    assert ow["EVERYONE"] == {"view_channel": False}
    assert ow["ME"]["view_channel"] is True and ow["BOT"]["send_messages"] is True
    assert set(ow) == {"EVERYONE", "BOT", "ME"}            # no mod role, no other role
    m = (await cog.config.mappings())["7"]
    assert m["channel_id"] == 700 and m["cursor"] == 500 and "Dogs" not in str(await cog.config.mappings())


async def test_extra_view_roles_are_added_only_when_configured(tmp_path, overwrite_stub):
    cog = map_cog(tmp_path, TOPICS, category_id=55, view_role_ids=[9])
    guild = FakeGuild()
    role = type('Role', (), {'id': 9})()
    guild.roles[9] = role
    await cbk("tgfeed_map")(cog, MapCtx(guild), "7")
    assert role in guild.created[0].overwrites


async def test_default_config_grants_no_roles_access():
    import inspect
    src = inspect.getsource(TGFeed.__init__)
    assert "view_role_ids=[]" in src


async def test_map_to_an_existing_channel_creates_nothing(tmp_path, overwrite_stub):
    cog = map_cog(tmp_path, TOPICS)
    guild = FakeGuild()
    await cbk("tgfeed_map")(cog, MapCtx(guild), "Dogs", SimpleNamespace(id=321, mention="<#321>"))
    assert guild.created == [] and (await cog.config.mappings())["8"]["channel_id"] == 321


async def test_map_without_a_category_or_channel_asks_for_one(tmp_path, overwrite_stub):
    cog = map_cog(tmp_path, TOPICS)
    ctx = MapCtx(FakeGuild())
    await cbk("tgfeed_map")(cog, ctx, "Cats")
    assert "category" in ctx.sent[0] and await cog.config.mappings() == {}


async def test_map_refuses_a_topic_that_is_already_mapped_and_a_shared_channel(tmp_path, overwrite_stub):
    cog = map_cog(tmp_path, TOPICS, category_id=55)
    guild = FakeGuild()
    await cbk("tgfeed_map")(cog, MapCtx(guild), "Cats")
    ctx = MapCtx(guild)
    await cbk("tgfeed_map")(cog, ctx, "Cats")
    assert "already mapped" in ctx.sent[0] and len(guild.created) == 1
    ctx2 = MapCtx(guild)
    await cbk("tgfeed_map")(cog, ctx2, "Dogs", SimpleNamespace(id=700, mention="<#700>"))
    assert "already receives" in ctx2.sent[0] and "8" not in await cog.config.mappings()


async def test_map_unknown_topic_says_so(tmp_path, overwrite_stub):
    cog = map_cog(tmp_path, TOPICS, category_id=55)
    guild = FakeGuild()
    ctx = MapCtx(guild)
    await cbk("tgfeed_map")(cog, ctx, "Nope")
    assert "No topic titled" in ctx.sent[0] and guild.created == []


async def test_version_probe_text():
    class Ctx:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kw):
            self.sent.append(text)

    ctx = Ctx()
    cmd = TGFeed.tgfeed_version
    await (getattr(cmd, "callback", None) or cmd.func)(object.__new__(TGFeed), ctx)
    assert ctx.sent == ["tgfeed build: tg-v1 (public forum media mirror)"]
