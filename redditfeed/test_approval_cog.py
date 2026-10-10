"""The approval queue, the X button, mode commands and channel proposals, against
fakes. Proves wiring and state handling; it does NOT prove live Discord rendering,
button clicks, or real Arctic Shift scores."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from redditfeed import constants, discovery, discovery_ui, engine, redditfeed as rf_module
from redditfeed.arctic_shift import RedditSourceError
from redditfeed.models import QueueEntry, SubredditMapping
from redditfeed.redditfeed import RedditFeed

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
    _next = 1000

    def __init__(self, channel, content=None, embeds=None, view=None):
        Msg._next += 1
        self.id, self.channel, self.content, self.embeds, self.view = Msg._next, channel, content, embeds or [], view
        self.edits, self.deleted, self.file = [], False, None

    async def edit(self, **kw):
        self.edits.append(kw)
        if "attachments" in kw:
            self.file = None
        if "embeds" in kw:
            self.embeds = kw["embeds"]

    async def delete(self):
        if self.channel.delete_raises:
            raise self.channel.delete_raises
        self.deleted = True


class Chan:
    def __init__(self, cid, nsfw=True, name="chan", everyone_can_view=False):
        self.id, self._nsfw, self.name, self.sent, self.delete_raises = cid, nsfw, name, [], None
        self.reject_files = None
        self.mention = f"<#{cid}>"
        self.messages = {}
        self.everyone_can_view = everyone_can_view
        self.guild = SimpleNamespace(default_role="everyone")

    def is_nsfw(self):
        return self._nsfw

    def permissions_for(self, role):
        return SimpleNamespace(view_channel=self.everyone_can_view)

    async def send(self, content=None, **kw):
        if kw.get("file") is not None and self.reject_files:
            raise self.reject_files
        m = Msg(self, content, kw.get("embeds") or ([kw["embed"]] if kw.get("embed") else []), kw.get("view"))
        m.file = kw.get("file")
        self.sent.append(m)
        self.messages[m.id] = m
        return m

    async def fetch_message(self, mid):
        if mid not in self.messages:
            import discord
            raise discord.NotFound("gone")
        return self.messages[mid]


class Bot:
    def __init__(self, channels, mods=()):
        self.channels = {c.id: c for c in channels}
        self.mods, self.views = set(mods), []

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def is_mod(self, user):
        return user.id in self.mods

    def add_view(self, view):
        self.views.append(view)


class Source:
    def __init__(self, posts=None, fail=False, on_fetch=None):
        self.posts, self.fail, self.fetches, self.on_fetch = posts or {}, fail, [], on_fetch

    async def fetch_new_posts(self, sub, after, limit):
        self.fetches.append((sub, after, limit))
        if self.on_fetch:
            await self.on_fetch()
        if self.fail:
            raise RedditSourceError("boom")
        return copy.deepcopy(self.posts.get(sub, []))

    async def search_subreddits(self, prefix, minsubs, limit):
        return []


class FakeRedgifs:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def fetch(self, url, max_bytes):
        self.calls.append((url, max_bytes))
        if self.error:
            raise self.error
        return "fantasticroundpuma.mp4", b"\x00\x00mp4"


QUEUE_CH, FEET_CH, LOG_CH = Chan(10, name="mod-queue"), Chan(20, name="feet"), Chan(30, name="mod-commands")


def post(pid, age_min=120, score=10, **kw):
    base = {"id": pid, "title": f"title {pid}", "score": score, "created_utc": NOW - age_min * 60,
            "url": f"https://i.redd.it/{pid}.jpg", "permalink": f"/r/feet/comments/{pid}/t/"}
    base.update(kw)
    return base


def make(posts=None, mappings=None, mods=(1,), queue_channel=True, **cfg):
    for c in (QUEUE_CH, FEET_CH, LOG_CH):
        c.sent, c.messages, c.delete_raises, c.reject_files = [], {}, None, None
    cog = object.__new__(RedditFeed)
    cog.bot = Bot([QUEUE_CH, FEET_CH, LOG_CH], mods)
    cog.source = Source(posts)
    base_map = {"feet": SubredditMapping(subreddit="feet", channel_ids=[20], added_ts=NOW - 86400,
                                         last_poll_ts=NOW - 60).to_dict()}
    values = dict(
        mappings=base_map if mappings is None else mappings, dedup_store={}, queue={}, denied_subreddits=[],
        queue_channel_id=10 if queue_channel else None, queue_min_score=3, queue_min_age_minutes=60,
        queue_max_pending=40, x_button=True, log_channel_id=30, posted_map={}, learned_topics={},
        new_channel_category_id=None, new_channel_prefix="", poll_interval_seconds=120, stagger_seconds=0,
        fetch_limit=25, dedup_ttl_days=7, redgifs_mode="upload",
    )
    values.update(cfg)
    cog.config = SimpleNamespace(**{k: Val(v) for k, v in values.items()})
    cog._queue_locks, cog._posted_buf, cog._last_stats = {}, {}, {}
    cog.redgifs, cog._redgifs_sem = FakeRedgifs(), asyncio.Semaphore(2)
    cog._channel_lock, cog._discover_lock, cog._cycle_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
    return cog


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(rf_module.asyncio, "sleep", instant)


def cb(name):
    cmd = getattr(RedditFeed, name)
    return getattr(cmd, "callback", None) or cmd.func


class Ctx:
    def __init__(self, channel=None, guild=None):
        self.channel = channel or Chan(99, name="mod-room")
        self.guild = guild
        self.author = SimpleNamespace(id=1)
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)
        return await self.channel.send(content, **kw)

    def typing(self):
        class T:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return T()


class Click:
    """An interaction on a message."""

    def __init__(self, message, uid=1, manage_guild=False):
        self.user = SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=f"user{uid}",
                                    guild_permissions=SimpleNamespace(manage_guild=manage_guild))
        self.message = message
        self.sent, self.followups, self.deferred = [], [], 0
        self.response = SimpleNamespace(defer=self._defer, send_message=self._send)
        self.followup = SimpleNamespace(send=self._follow)

    async def _defer(self):
        self.deferred += 1

    async def _send(self, content=None, ephemeral=False, **kw):
        self.sent.append(content)

    async def _follow(self, content=None, ephemeral=False):
        self.followups.append(content)


async def run_queue(cog):
    mapping = SubredditMapping.from_dict((await cog.config.mappings())["feet"])
    qcfg = await cog._queue_settings()
    ded = await cog._queue_one_subreddit(mapping, {}, NOW, qcfg)
    return mapping, ded, qcfg


# ---------------------------------------------------------------- queueing

async def test_manual_mode_queues_a_settled_post_and_posts_nothing_publicly():
    cog = make({"feet": [post("a")]})
    mapping, dedup, _ = await run_queue(cog)

    assert FEET_CH.sent == []                                   # nothing public
    assert len(QUEUE_CH.sent) == 1
    card = QUEUE_CH.sent[0]
    assert card.embeds[0].title == "title a"
    stored = (await cog.config.queue())[str(card.id)]
    assert stored["status"] == "pending" and stored["channel_ids"] == [20] and stored["subreddit"] == "feet"
    assert "20:a" in dedup                                       # never queued twice
    assert mapping.last_error is None and mapping.last_post_id == "a"


async def test_posts_that_are_too_new_or_low_score_or_old_are_not_queued():
    cog = make({"feet": [
        post("new", age_min=5), post("low", score=1),
        post("ancient", age_min=2000),                           # created before the mapping was added
        post("ok"),
    ]})
    await run_queue(cog)
    assert len(QUEUE_CH.sent) == 1 and QUEUE_CH.sent[0].embeds[0].title == "title ok"
    assert cog._last_stats == {"too_new": 1, "low_score": 1, "before_added": 1, "queued": 1}


async def test_unsettled_posts_are_retried_next_cycle_not_lost():
    cog = make({"feet": [post("late", age_min=5)]})
    await run_queue(cog)
    assert QUEUE_CH.sent == []
    cog.source.posts = {"feet": [post("late", age_min=90)]}      # an hour later
    await run_queue(cog)
    assert len(QUEUE_CH.sent) == 1


async def test_no_queue_channel_queues_nothing_and_says_why():
    cog = make({"feet": [post("a")]}, queue_channel=False)
    mapping, _, _ = await run_queue(cog)
    assert cog.source.fetches == [] and "no queue channel" in mapping.last_error


async def test_queue_cap_and_per_sub_cap():
    cog = make({"feet": [post(str(i)) for i in range(9)]}, queue_max_pending=40)
    await run_queue(cog)
    assert len(QUEUE_CH.sent) == constants.QUEUE_MAX_PER_SUB_PER_CYCLE

    cog = make({"feet": [post(str(i)) for i in range(5)]}, queue_max_pending=2)
    await run_queue(cog)
    assert len(QUEUE_CH.sent) == 2 and cog._last_stats["queue_full"] == 1


async def test_blocked_keyword_never_reaches_the_queue():
    m = SubredditMapping(subreddit="feet", channel_ids=[20], added_ts=NOW - 86400, block_keywords=["title"]).to_dict()
    cog = make({"feet": [post("a")]}, mappings={"feet": m})
    await run_queue(cog)
    assert QUEUE_CH.sent == []


async def test_already_posted_to_every_channel_is_not_queued_again():
    cog = make({"feet": [post("a")]})
    mapping = SubredditMapping.from_dict((await cog.config.mappings())["feet"])
    await cog._queue_one_subreddit(mapping, {"20:a": NOW}, NOW, await cog._queue_settings())
    assert QUEUE_CH.sent == []


async def test_fetch_failure_propagates_so_the_cycle_records_the_error():
    cog = make()
    cog.source = Source(fail=True)
    with pytest.raises(RedditSourceError):
        await run_queue(cog)


# ---------------------------------------------------------------- deciding

async def queued(cog):
    await run_queue(cog)
    return QUEUE_CH.sent[0]


async def test_approve_posts_to_the_destination_with_the_x_button_and_closes_the_card():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    click = Click(card)
    await cog.handle_queue_action(click, "approve")

    assert [m.content for m in FEET_CH.sent] == ["https://i.redd.it/a.jpg"]
    assert FEET_CH.sent[0].view is not None                      # mod-only X attached
    stored = (await cog.config.queue())[str(card.id)]
    assert stored["status"] == "approved" and stored["resolved_by"] == 1
    assert card.edits and card.edits[0]["view"] is None and "Approved by" in card.embeds[0].fields[-1].value
    assert str(FEET_CH.sent[0].id) in (await cog.config.posted_map())      # remembered for the removal log


async def test_gallery_approval_posts_all_images_in_one_message():
    gallery = post("g", is_gallery=True, url="https://reddit.com/gallery/g",
                   gallery_data={"items": [{"media_id": str(i)} for i in range(6)]},
                   media_metadata={str(i): {"s": {"u": f"https://i.redd.it/{i}.jpg"}} for i in range(6)})
    cog = make({"feet": [gallery]})
    card = await queued(cog)
    assert len(card.embeds) == constants.QUEUE_MAX_PREVIEW_IMAGES          # preview is capped...
    await cog.handle_queue_action(Click(card), "approve")
    assert len(FEET_CH.sent) == 1 and len(FEET_CH.sent[0].embeds) == 6      # ...but all six post


async def test_reject_posts_nothing():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    await cog.handle_queue_action(Click(card), "reject")
    assert FEET_CH.sent == []
    assert (await cog.config.queue())[str(card.id)]["status"] == "rejected"


async def test_pause_rejects_and_pauses_the_subreddit_and_logs():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    await cog.handle_queue_action(Click(card), "pause")
    assert (await cog.config.mappings())["feet"]["paused"] is True
    assert (await cog.config.queue())[str(card.id)]["status"] == "rejected"
    assert LOG_CH.sent and "paused r/feet" in LOG_CH.sent[0].content


async def test_non_mod_cannot_decide_and_the_item_stays_pending():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    click = Click(card, uid=99)
    await cog.handle_queue_action(click, "approve")
    assert click.sent == ["Only moderators can use these buttons."] and FEET_CH.sent == []
    assert (await cog.config.queue())[str(card.id)]["status"] == "pending"


async def test_manage_guild_member_counts_as_a_moderator():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    await cog.handle_queue_action(Click(card, uid=7, manage_guild=True), "approve")
    assert len(FEET_CH.sent) == 1


async def test_two_simultaneous_clicks_post_exactly_once():
    """REGRESSION guard: Approve and Reject at the same instant must not both act."""
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    a, b = Click(card), Click(card)
    await asyncio.gather(cog.handle_queue_action(a, "approve"), cog.handle_queue_action(b, "approve"))
    assert len(FEET_CH.sent) == 1
    assert any("already decided" in (x or "") for x in a.sent + b.sent)


async def test_unreachable_destination_leaves_the_item_pending():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    del cog.bot.channels[20]
    click = Click(card)
    await cog.handle_queue_action(click, "approve")
    assert "couldn't reach" in click.followups[0]
    assert (await cog.config.queue())[str(card.id)]["status"] == "pending"


async def test_unknown_card_is_reported_not_crashed():
    cog = make()
    click = Click(Msg(QUEUE_CH))
    await cog.handle_queue_action(click, "approve")
    assert "isn't in the queue" in click.sent[0]


# ---------------------------------------------------------------- expiry

async def test_undecided_items_expire_after_24h_and_their_cards_are_closed():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    await cog._expire_queue(NOW + 23 * 3600)
    assert (await cog.config.queue())[str(card.id)]["status"] == "pending"
    await cog._expire_queue(NOW + 25 * 3600)
    assert (await cog.config.queue())[str(card.id)]["status"] == "expired"
    assert card.edits and "Expired" in card.embeds[0].fields[-1].value


async def test_decided_records_are_pruned_after_two_days():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    await cog.handle_queue_action(Click(card), "reject")
    await cog._expire_queue(NOW + 60 * 3600)
    assert str(card.id) not in await cog.config.queue()


# ---------------------------------------------------------------- X button

async def test_mod_x_deletes_the_post_and_logs_where_it_came_from():
    cog = make({"feet": [post("a")]})
    await cog.handle_queue_action(Click(await queued(cog)), "approve")
    feed_msg = FEET_CH.sent[0]
    click = Click(feed_msg)
    await cog.handle_feed_x(click)
    assert feed_msg.deleted and click.sent == ["Removed."]
    assert "removed a feed post" in LOG_CH.sent[-1].content and "r/feet" in LOG_CH.sent[-1].content
    assert "https://redd.it/a" in LOG_CH.sent[-1].content


async def test_non_mod_x_does_nothing():
    cog = make()
    msg = Msg(FEET_CH)
    click = Click(msg, uid=99)
    await cog.handle_feed_x(click)
    assert not msg.deleted and click.sent == ["Only moderators can remove feed posts."] and LOG_CH.sent == []


async def test_x_on_an_already_deleted_message_still_works():
    import discord
    cog = make()
    FEET_CH.delete_raises = discord.NotFound("gone")
    click = Click(Msg(FEET_CH))
    await cog.handle_feed_x(click)
    assert click.sent == ["Removed."]


async def test_x_button_can_be_turned_off():
    cog = make(x_button=False)
    await cog._post_media_items(FEET_CH, {"id": "a"}, [engine.extract_media_items(post("a"))[0]], "feet")
    assert FEET_CH.sent[0].view is None


# ---------------------------------------------------------------- auto mode + cycle

async def test_auto_mode_still_posts_immediately_with_the_x_button():
    m = SubredditMapping(subreddit="feet", channel_ids=[20], approval="auto", last_poll_ts=NOW - 60).to_dict()
    cog = make({"feet": [post("a", age_min=2, score=1)]}, mappings={"feet": m})
    await cog._run_poll_cycle()
    assert [x.content for x in FEET_CH.sent] == ["https://i.redd.it/a.jpg"] and QUEUE_CH.sent == []
    assert FEET_CH.sent[0].view is not None and (await cog.config.posted_map())


async def test_full_cycle_in_manual_mode_fills_the_queue():
    cog = make({"feet": [post("a")]})
    await cog._run_poll_cycle()
    assert len(QUEUE_CH.sent) == 1 and FEET_CH.sent == []


async def test_a_mapping_removed_mid_cycle_is_not_resurrected():
    """REGRESSION guard: the cycle used to write its stale snapshot back over the
    live mappings, undoing any removal or edit made while it was fetching."""
    cog = make({"feet": [post("a")]})

    async def remove_meanwhile():
        async with cog.config.mappings() as live:
            live.clear()

    cog.source.on_fetch = remove_meanwhile
    await cog._run_poll_cycle()
    assert await cog.config.mappings() == {}


async def test_a_mode_change_mid_cycle_survives():
    cog = make({"feet": [post("a")]})

    async def flip():
        async with cog.config.mappings() as live:
            live["feet"]["approval"] = "auto"

    cog.source.on_fetch = flip
    await cog._run_poll_cycle()
    assert (await cog.config.mappings())["feet"]["approval"] == "auto"


# ---------------------------------------------------------------- commands

async def test_mode_and_modeall_commands():
    cog = make(mappings={
        "a": SubredditMapping(subreddit="a").to_dict(), "b": SubredditMapping(subreddit="b").to_dict()})
    ctx = Ctx()
    await cb("redditfeed_mode")(cog, ctx, "r/A", "auto")
    assert (await cog.config.mappings())["a"]["approval"] == "auto"
    await cb("redditfeed_mode")(cog, ctx, "a", "sometimes")
    assert "manual" in ctx.sent[-1] and "auto" in ctx.sent[-1]
    await cb("redditfeed_mode")(cog, ctx, "zzz", "auto")
    assert "isn't mapped" in ctx.sent[-1]
    await cb("redditfeed_modeall")(cog, ctx, "manual")
    assert {m["approval"] for m in (await cog.config.mappings()).values()} == {"manual"}


async def test_addmany_maps_every_subreddit_and_refuses_a_non_nsfw_channel():
    cog = make(mappings={})
    ctx = Ctx()
    await cb("redditfeed_addmany")(cog, ctx, Chan(55, nsfw=False), "a", "b")
    assert await cog.config.mappings() == {} and "age-restricted" in ctx.sent[-1]
    await cb("redditfeed_addmany")(cog, ctx, FEET_CH, "a", "R/B", "a")
    maps = await cog.config.mappings()
    assert set(maps) == {"a", "b"} and maps["a"]["channel_ids"] == [20] and maps["a"]["approval"] == "manual"


async def test_resetall_needs_confirmation_and_clears_mappings_dedup_and_pending():
    cog = make({"feet": [post("a")]})
    card = await queued(cog)
    ctx = Ctx()
    await cb("redditfeed_resetall")(cog, ctx, "")
    assert await cog.config.mappings() and "resetall yes" in ctx.sent[-1]
    await cb("redditfeed_resetall")(cog, ctx, "yes")
    assert await cog.config.mappings() == {} and await cog.config.dedup_store() == {}
    assert (await cog.config.queue())[str(card.id)]["status"] == "expired"


async def test_queue_channel_must_be_nsfw_and_hidden_from_everyone():
    cog = make()
    ctx = Ctx()
    await cb("redditfeed_queue_channel")(cog, ctx, Chan(70, nsfw=False))
    assert "age-restricted" in ctx.sent[-1] and await cog.config.queue_channel_id() == 10
    await cb("redditfeed_queue_channel")(cog, ctx, Chan(71, everyone_can_view=True))
    assert "@everyone" in ctx.sent[-1] and await cog.config.queue_channel_id() == 10
    await cb("redditfeed_queue_channel")(cog, ctx, Chan(72))
    assert await cog.config.queue_channel_id() == 72


async def test_queue_status_reports_the_numbers():
    cog = make({"feet": [post("a")]})
    await queued(cog)
    ctx = Ctx()
    await cb("redditfeed_queue")(cog, ctx)
    assert "Waiting for a decision: **1**" in ctx.sent[-1] and "1 manual" in ctx.sent[-1]


async def test_new_mappings_start_manual_and_stamp_added_ts():
    cog = make(mappings={})
    await cog._map_subreddit("newsub", 20)
    m = (await cog.config.mappings())["newsub"]
    assert m["approval"] == "manual" and m["added_ts"] is not None


# ---------------------------------------------------------------- discover: proposals

class Guild:
    def __init__(self, channels, category=None):
        self.text_channels = channels
        self.category = category
        self.created = []

    def get_channel(self, cid):
        if self.category is not None and cid == self.category.id:
            return self.category
        return next((c for c in self.text_channels if c.id == cid), None)

    async def create_text_channel(self, name, **kw):
        ch = Chan(500 + len(self.created), name=name)
        ch._nsfw = kw.get("nsfw", False)
        self.created.append((name, kw))
        self.text_channels.append(ch)
        return ch


def raw(name, subs=1000):
    return {"display_name": name, "subscribers": subs, "public_description": "", "over18": True,
            "quarantine": False, "subreddit_type": "restricted"}


async def test_discover_without_a_channel_proposes_an_existing_match():
    guild = Guild([FEET_CH, Chan(21, name="2d")])
    cog = make()
    cog.source.search_subreddits = lambda p, m, l: _async([raw("FeetInYourFace")])
    ctx = Ctx(guild=guild)
    await cb("redditfeed_discover")(cog, ctx, "feet", None)
    card = ctx.channel.sent[1]
    assert "<#20>" in card.embeds[0].fields[1].value
    assert card.view.channel_id == 20 and card.view.new_name is None


async def test_discover_proposes_a_new_channel_only_when_a_category_is_set():
    cat = SimpleNamespace(id=900, name="After Dark")
    for category_id, expect_new in ((None, False), (900, True)):
        guild = Guild([FEET_CH], category=cat)
        cog = make(new_channel_category_id=category_id, new_channel_prefix="🔞・")
        cog.source.search_subreddits = lambda p, m, l: _async([raw("bdsm")])
        ctx = Ctx(guild=guild)
        await cb("redditfeed_discover")(cog, ctx, "bdsm", None)
        view = ctx.channel.sent[1].view
        assert (view.new_name == "🔞・bdsm") is expect_new
        if not expect_new:
            assert view.channel_id is None and "None yet" in ctx.channel.sent[1].embeds[0].fields[1].value


async def test_discover_with_an_explicit_channel_forces_it_on_every_card():
    guild = Guild([FEET_CH, Chan(21, name="2d")])
    cog = make()
    cog.source.search_subreddits = lambda p, m, l: _async([raw("HentaiBeast")])
    ctx = Ctx(guild=guild)
    await cb("redditfeed_discover")(cog, ctx, "hentai", FEET_CH)
    assert ctx.channel.sent[1].view.channel_id == 20


def _async(value):
    async def inner():
        return value
    return inner()


async def test_approving_a_new_channel_proposal_creates_it_age_restricted_and_maps_it():
    cat = SimpleNamespace(id=900, name="After Dark")
    guild = Guild([FEET_CH], category=cat)
    cog = make(mappings={}, new_channel_category_id=900)
    created, cid = await cog._approve_suggestion("bdsm", None, "🔞・bdsm", guild, "BDSM")
    assert created is True and cid == 500
    name, kw = guild.created[0]
    assert name == "🔞・bdsm" and kw["nsfw"] is True and kw["category"] is cat
    assert (await cog.config.mappings())["bdsm"]["channel_ids"] == [500]


async def test_two_approvals_for_the_same_new_channel_create_it_once():
    cat = SimpleNamespace(id=900, name="After Dark")
    guild = Guild([], category=cat)
    cog = make(mappings={}, new_channel_category_id=900)
    r = await asyncio.gather(
        cog._approve_suggestion("bdsm", None, "bdsm", guild, "bdsm"),
        cog._approve_suggestion("bdsmgifs", None, "bdsm", guild, "bdsmgifs"),
    )
    assert len(guild.created) == 1 and r[0][1] == r[1][1]


async def test_creating_without_a_category_is_refused_with_instructions():
    cog = make(mappings={})
    with pytest.raises(discovery.DestinationError, match="category"):
        await cog._approve_suggestion("bdsm", None, "bdsm", Guild([]), "bdsm")
    assert await cog.config.mappings() == {}


async def test_an_existing_channel_that_isnt_age_restricted_is_never_reused():
    guild = Guild([Chan(40, nsfw=False, name="bdsm")])
    cog = make(mappings={})
    with pytest.raises(discovery.DestinationError, match="age-restricted"):
        await cog._approve_suggestion("bdsm", None, "bdsm", guild, "bdsm")


async def test_approval_teaches_the_topic_so_the_next_one_goes_to_the_same_channel():
    cog = make(mappings={})
    await cog._approve_suggestion("footfetish", 20, None, None, "FootFetish")
    assert (await cog.config.learned_topics())["feet"] == 20


# ---------------------------------------------------------------- the card's buttons

class FakeCogForView:
    def __init__(self):
        self.bot = SimpleNamespace(is_mod=lambda u: _async(u.id == 1))
        self.calls = []

    async def _approve_suggestion(self, *a):
        self.calls.append(a)
        return True, 777


def view_with(dest_channel=None, new_name=None, guild=None):
    cand = discovery.SubredditCandidate(name="bdsm", display_name="BDSM", subscribers=9)
    emb = discovery_ui.build_suggestion_embeds(
        cand, discovery.PreviewResult(), dest_channel or 0,
        destination=discovery.Destination(channel_id=dest_channel, new_name=new_name, reason="because"))
    cog = FakeCogForView()
    return cog, discovery_ui.SuggestionView(cog, "bdsm", dest_channel, emb, 1, new_name=new_name, guild=guild,
                                           display_name="BDSM"), emb


async def test_approve_is_refused_until_a_destination_exists():
    cog, view, emb = view_with()
    click = Click(None)
    click.user.id = 1
    await view._resolve(click, approve=True)
    assert cog.calls == [] and "Pick a destination" in click.sent[0]
    assert not view._resolved


async def test_a_new_channel_proposal_approves_with_the_name_and_reports_the_created_channel():
    cog, view, emb = view_with(new_name="bdsm", guild="G")
    click = Click(None)
    click.edit_original_response = _noop
    await view._resolve(click, approve=True)
    assert cog.calls == [("bdsm", None, "bdsm", "G", "BDSM")]
    assert "<#777>" in emb[0].fields[-1].value


async def _noop(**kw):
    return None


async def test_dropdown_changes_the_destination_and_the_card():
    guild = Guild([FEET_CH])
    cog, view, emb = view_with(guild=guild)
    click = Click(None)
    edits = []

    async def edit_message(**kw):
        edits.append(kw)

    click.response.edit_message = edit_message
    click.guild = guild
    await view._pick(click, SimpleNamespace(id=20))
    assert view.channel_id == 20 and view.new_name is None
    assert "<#20>" in next(f.value for f in emb[0].fields if f.name == "Would post to") and edits


async def test_dropdown_refuses_a_channel_that_isnt_age_restricted_and_non_mods():
    guild = Guild([Chan(41, nsfw=False, name="general")])
    cog, view, emb = view_with(guild=guild)
    click = Click(None)
    click.guild = guild
    await view._pick(click, SimpleNamespace(id=41))
    assert view.channel_id is None and "age-restricted" in click.sent[0]

    stranger = Click(None, uid=99)
    stranger.guild = guild
    await view._pick(stranger, SimpleNamespace(id=41))
    assert "Only moderators" in stranger.sent[0]


async def test_a_queued_post_is_not_queued_again_on_the_next_cycle():
    """REGRESSION guard: dedup is recorded at queue time, so a post still inside the
    lookback window can't fill the queue with copies of itself."""
    cog = make({"feet": [post("a")]})
    mapping = SubredditMapping.from_dict((await cog.config.mappings())["feet"])
    dedup = await cog._queue_one_subreddit(mapping, {}, NOW, await cog._queue_settings())
    await cog._queue_one_subreddit(mapping, dedup, NOW, await cog._queue_settings())
    assert len(QUEUE_CH.sent) == 1


async def test_rejected_posts_do_not_come_back():
    cog = make({"feet": [post("a")]})
    await cog._run_poll_cycle()
    await cog.handle_queue_action(Click(QUEUE_CH.sent[0]), "reject")
    await cog._run_poll_cycle()
    assert len(QUEUE_CH.sent) == 1


async def test_proposals_only_consider_age_restricted_channels_and_never_the_mod_channels():
    guild = Guild([Chan(60, nsfw=False, name="feet"), QUEUE_CH, Chan(99, name="feet-mods")])
    cog = make()
    cog.source.search_subreddits = lambda p, m, l: _async([raw("FeetInYourFace")])
    ctx = Ctx(guild=guild)                      # ctx.channel is channel 99 (the room discover runs in)
    await cb("redditfeed_discover")(cog, ctx, "feet", None)
    assert ctx.channel.sent[1].view.channel_id is None


# ---------------------------------------------------------------- RedGifs

RG_POST = {"id": "r1", "url": "https://www.redgifs.com/watch/fantasticroundpuma", "permalink": "/r/feet/comments/r1/t/"}


def rg_items():
    return engine.extract_media_items(RG_POST)


async def test_redgifs_is_uploaded_as_a_video_file_with_the_x_button():
    cog = make()
    await cog._post_media_items(FEET_CH, RG_POST, rg_items(), "feet")
    msg = FEET_CH.sent[0]
    assert msg.content is None and msg.file.filename == "fantasticroundpuma.mp4" and msg.view is not None
    assert cog._last_stats["redgifs_uploaded"] == 1
    assert cog.redgifs.calls and cog.redgifs.calls[0][0] == RG_POST["url"]


async def test_redgifs_failure_falls_back_to_the_plain_link():
    from redditfeed.redgifs import RedgifsError
    cog = make()
    cog.redgifs = FakeRedgifs(RedgifsError("HTTP 403"))
    await cog._post_media_items(FEET_CH, RG_POST, rg_items(), "feet")
    assert [m.content for m in FEET_CH.sent] == [RG_POST["url"]] and FEET_CH.sent[0].file is None
    assert cog._last_stats["redgifs_link_fallback"] == 1


async def test_redgifs_too_big_for_the_server_falls_back_to_the_link():
    from redditfeed.redgifs import RedgifsTooLarge
    cog = make()
    cog.redgifs = FakeRedgifs(RedgifsTooLarge("60MB"))
    await cog._post_media_items(FEET_CH, RG_POST, rg_items(), "feet")
    assert FEET_CH.sent[0].content == RG_POST["url"]


async def test_redgifs_upload_rejected_by_discord_falls_back_to_the_link():
    import discord
    cog = make()
    FEET_CH.reject_files = discord.HTTPException("413 too large")
    await cog._post_media_items(FEET_CH, RG_POST, rg_items(), "feet")
    assert [m.content for m in FEET_CH.sent] == [RG_POST["url"]]


async def test_redgifs_missing_permission_stops_posting():
    import discord
    cog = make()
    FEET_CH.reject_files = discord.Forbidden("no")
    await cog._post_media_items(FEET_CH, RG_POST, rg_items(), "feet")
    assert FEET_CH.sent == []


async def test_redgifs_link_mode_never_downloads():
    cog = make(redgifs_mode="link")
    await cog._post_media_items(FEET_CH, RG_POST, rg_items(), "feet")
    assert cog.redgifs.calls == [] and FEET_CH.sent[0].content == RG_POST["url"]


async def test_other_video_links_are_never_uploaded():
    cog = make()
    items = engine.extract_media_items({"id": "v", "is_video": True, "permalink": "/r/feet/comments/v/t/"})
    await cog._post_media_items(FEET_CH, {"id": "v"}, items, "feet")
    assert cog.redgifs.calls == [] and FEET_CH.sent[0].file is None


class RgCtx:
    def __init__(self):
        self.sent = []

    async def send(self, text=None, **kw):
        self.sent.append(text)


async def test_redgifs_command_shows_and_sets_the_mode():
    cog = make()
    ctx = RgCtx()
    cb = RedditFeed.redditfeed_redgifs
    cb = getattr(cb, "callback", None) or cb.func
    await cb(cog, ctx, "")
    assert "upload" in ctx.sent[-1]
    await cb(cog, ctx, "LINK")
    assert await cog.config.redgifs_mode() == "link"
    await cb(cog, ctx, "nope")
    assert await cog.config.redgifs_mode() == "link" and "upload" in ctx.sent[-1]


# ---------------------------------------------------------------- RedGifs in the queue

def rg_post(pid="r1", **kw):
    return post(pid, url=f"https://www.redgifs.com/watch/clip{pid}", **kw)


async def test_queue_card_for_a_redgifs_post_carries_the_clip():
    cog = make({"feet": [rg_post()]})
    await cog._run_poll_cycle()
    card = QUEUE_CH.sent[0]
    assert card.file is not None and card.file.filename == "fantasticroundpuma.mp4"
    assert card.content is None and card.view is not None
    assert cog._last_stats["redgifs_preview"] == 1


async def test_queue_card_falls_back_to_the_link_when_the_download_fails():
    from redditfeed.redgifs import RedgifsError
    cog = make({"feet": [rg_post()]})
    cog.redgifs = FakeRedgifs(RedgifsError("HTTP 403"))
    await cog._run_poll_cycle()
    card = QUEUE_CH.sent[0]
    assert card.file is None and card.content == "https://www.redgifs.com/watch/clipr1"
    assert cog._last_stats["redgifs_preview_failed"] == 1 and cog._last_stats["queued"] == 1


async def test_queue_card_falls_back_when_discord_rejects_the_upload():
    import discord
    cog = make({"feet": [rg_post()]})
    QUEUE_CH.reject_files = discord.HTTPException("413")
    await cog._run_poll_cycle()
    card = QUEUE_CH.sent[0]
    assert card.file is None and card.content == "https://www.redgifs.com/watch/clipr1"


async def test_queue_card_in_link_mode_and_for_images_never_downloads():
    cog = make({"feet": [rg_post(), post("img")]}, redgifs_mode="link")
    await cog._run_poll_cycle()
    assert cog.redgifs.calls == [] and all(m.file is None for m in QUEUE_CH.sent)
    cog = make({"feet": [post("img2")]})
    await cog._run_poll_cycle()
    assert cog.redgifs.calls == []


async def test_deciding_a_card_removes_the_clip_from_the_queue_channel():
    cog = make({"feet": [rg_post()]})
    await cog._run_poll_cycle()
    card = QUEUE_CH.sent[0]
    click = Click(card)
    await cog.handle_queue_action(click, "approve")
    assert card.file is None and card.edits[-1]["attachments"] == []
    assert FEET_CH.sent and FEET_CH.sent[0].file is not None          # and the feed got its own copy


# ---------------------------------------------------------------- queue clear

async def test_queue_clear_needs_confirmation_and_changes_nothing_without_it():
    cog = make({"feet": [post("a"), post("b")]})
    await cog._run_poll_cycle()
    ctx = RgCtx()
    ctx.author = SimpleNamespace(id=7, mention="<@7>", display_name="mod")
    cb = RedditFeed.redditfeed_queue_clear
    cb = getattr(cb, "callback", None) or cb.func
    await cb(cog, ctx, "")
    assert "2" in ctx.sent[-1] and "confirm" in ctx.sent[-1]
    assert len(engine.pending_ids(await cog.config.queue())) == 2


async def test_queue_clear_rejects_everything_waiting_and_nothing_posts():
    cog = make({"feet": [post("a"), post("b")]})
    await cog._run_poll_cycle()
    cards = list(QUEUE_CH.sent)
    ctx = RgCtx()
    ctx.author = SimpleNamespace(id=7, mention="<@7>", display_name="mod")
    cb = RedditFeed.redditfeed_queue_clear
    cb = getattr(cb, "callback", None) or cb.func
    await cb(cog, ctx, "YES")
    stored = await cog.config.queue()
    assert engine.pending_ids(stored) == [] and all(r["status"] == "rejected" and r["resolved_by"] == 7 for r in stored.values())
    assert FEET_CH.sent == []
    assert all(c.edits and c.edits[-1]["view"] is None and c.edits[-1]["attachments"] == [] for c in cards)
    assert "Rejected 2" in ctx.sent[-1] and any("cleared the redditfeed queue" in (m.content or "") for m in LOG_CH.sent)


async def test_cleared_posts_do_not_come_back_and_new_ones_can_queue():
    cog = make({"feet": [post("a")]})
    await cog._run_poll_cycle()
    ctx = RgCtx()
    ctx.author = SimpleNamespace(id=7, mention="<@7>", display_name="mod")
    cb = RedditFeed.redditfeed_queue_clear
    cb = getattr(cb, "callback", None) or cb.func
    await cb(cog, ctx, "yes")
    n = len(QUEUE_CH.sent)
    await cog._run_poll_cycle()
    assert len(QUEUE_CH.sent) == n                       # "a" is deduped, not re-queued
    cog.source.posts["feet"] = [post("a"), post("c")]
    await cog._run_poll_cycle()
    assert len(QUEUE_CH.sent) == n + 1


async def test_queue_clear_with_nothing_waiting_and_with_a_deleted_card():
    cog = make()
    ctx = RgCtx()
    ctx.author = SimpleNamespace(id=7, mention="<@7>", display_name="mod")
    cb = RedditFeed.redditfeed_queue_clear
    cb = getattr(cb, "callback", None) or cb.func
    await cb(cog, ctx, "yes")
    assert ctx.sent[-1] == "Nothing is waiting."
    cog = make({"feet": [post("a")]})
    await cog._run_poll_cycle()
    QUEUE_CH.messages.clear()                            # the card was deleted by hand
    await cb(cog, ctx, "yes")
    assert "1 card(s) couldn't be edited" in ctx.sent[-1]
    assert engine.pending_ids(await cog.config.queue()) == []


async def test_least_recently_polled_subreddit_is_fetched_first():
    """A slow or rate-limited source cuts cycles short; without this the subreddits at the end
    of the list were starved every time (two sat 13 hours stale)."""
    def m(name, last):
        return SubredditMapping(subreddit=name, channel_ids=[20], added_ts=NOW - 86400, last_poll_ts=last).to_dict()

    mappings = {"fresh": m("fresh", NOW - 60), "stale": m("stale", NOW - 40000), "middle": m("middle", NOW - 3000)}
    cog = make({}, mappings=mappings)
    await cog._run_poll_cycle()
    assert [f[0] for f in cog.source.fetches] == ["stale", "middle", "fresh"]
