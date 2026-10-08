"""`.redditfeed discover` and the shared mapping helpers, against fakes.
Proves wiring and filtering; it does NOT prove live Discord rendering.
"""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from redditfeed import redditfeed as redditfeed_module
from redditfeed.arctic_shift import RedditSourceError
from redditfeed.redditfeed import RedditFeed


class _Value:
    """Like a Red Value: awaitable AND an async context manager over live data."""

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


class FakeChannel:
    def __init__(self, cid=1, nsfw=True):
        self.id, self._nsfw, self.sent = cid, nsfw, []

    def is_nsfw(self):
        return self._nsfw

    async def send(self, content=None, **kwargs):
        message = SimpleNamespace(content=content, **kwargs)
        self.sent.append(message)
        return message


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCtx:
    def __init__(self, channel=None, author_id=1):
        self.channel = channel or FakeChannel()
        self.author = SimpleNamespace(id=author_id)

    async def send(self, content=None, **kwargs):
        return await self.channel.send(content, **kwargs)

    def typing(self):
        return _Typing()


class FakeSource:
    def __init__(self, results=None, posts=None, fail_search=(), fail_posts=()):
        self.results, self.posts = results or {}, posts or {}
        self.fail_search, self.fail_posts = set(fail_search), set(fail_posts)
        self.searches, self.fetches = [], []

    async def search_subreddits(self, prefix, min_subscribers, limit):
        self.searches.append((prefix, min_subscribers, limit))
        if prefix in self.fail_search:
            raise RedditSourceError("HTTP 422: Timeout")
        return self.results.get(prefix, [])

    async def fetch_new_posts(self, subreddit, after_ts, limit):
        self.fetches.append(subreddit)
        if subreddit in self.fail_posts:
            raise RedditSourceError("timeout")
        return self.posts.get(subreddit, [])


def raw(name, subs=1000, desc=""):
    return {"display_name": name, "subscribers": subs, "public_description": desc,
            "over18": True, "quarantine": False, "subreddit_type": "restricted"}


def image_post(pid, score=10):
    return {"id": pid, "score": score, "title": "nice", "url": f"https://i.redd.it/{pid}.jpg",
            "permalink": f"/r/x/comments/{pid}/t/"}


def make_cog(source=None, mapped=None, denied=None):
    cog = object.__new__(RedditFeed)
    cog.bot = SimpleNamespace()
    cog.source = source or FakeSource()
    cog.config = SimpleNamespace(
        mappings=_Value(mapped if mapped is not None else {}),
        denied_subreddits=_Value(denied if denied is not None else []),
    )
    cog._discover_lock = asyncio.Lock()
    return cog


def command_callback(name):
    cmd = getattr(RedditFeed, name)
    return getattr(cmd, "callback", None) or cmd.func


@pytest.fixture(autouse=True)
def _no_real_delays(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(redditfeed_module.asyncio, "sleep", instant)


TARGET = FakeChannel(cid=777)


# -- shared mapping helper ---------------------------------------------------------------------

async def test_map_subreddit_creates_then_only_adds_channels():
    cog = make_cog()
    assert await cog._map_subreddit("feet", 10) is True
    created = cog.config.mappings.data["feet"]
    assert created["channel_ids"] == [10] and created["last_poll_ts"] is not None   # no backfill

    assert await cog._map_subreddit("feet", 20) is False
    assert cog.config.mappings.data["feet"]["channel_ids"] == [10, 20]
    assert cog.config.mappings.data["feet"]["last_poll_ts"] == created["last_poll_ts"]


async def test_approve_maps_and_clears_an_old_denial():
    cog = make_cog(denied=["feet", "other"])
    assert await cog._approve_suggestion("feet", 10) is True
    assert "feet" in cog.config.mappings.data
    assert cog.config.denied_subreddits.data == ["other"]


async def test_deny_is_idempotent():
    cog = make_cog()
    await cog._deny_suggestion("feet")
    await cog._deny_suggestion("feet")
    assert cog.config.denied_subreddits.data == ["feet"]


# -- command guards --------------------------------------------------------------------------------

async def test_refuses_when_invoked_in_a_non_nsfw_channel():
    cog = make_cog()
    ctx = FakeCtx(channel=FakeChannel(nsfw=False))
    await command_callback("redditfeed_discover")(cog, ctx, "feet", TARGET)
    assert cog.source.searches == []
    assert "age-restricted" in ctx.channel.sent[0].content


async def test_refuses_when_the_target_channel_is_not_nsfw():
    cog = make_cog()
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(cog, ctx, "feet", FakeChannel(cid=2, nsfw=False))
    assert cog.source.searches == []
    assert "age-restricted" in ctx.channel.sent[0].content


async def test_refuses_garbage_prefixes():
    cog = make_cog()
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(cog, ctx, "!!,??", TARGET)
    assert cog.source.searches == []
    assert "name prefixes" in ctx.channel.sent[0].content


async def test_refuses_a_second_run_while_one_is_in_progress():
    cog = make_cog()
    await cog._discover_lock.acquire()
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(cog, ctx, "feet", TARGET)
    assert cog.source.searches == []
    assert "already in progress" in ctx.channel.sent[0].content


# -- the run itself ----------------------------------------------------------------------------------

async def test_run_posts_a_summary_then_one_card_per_new_candidate():
    source = FakeSource(
        results={"feet": [raw("FeetInYourFace", 400), raw("Mapped", 300), raw("DeniedOne", 200), raw("TeenFeet", 100)]},
        posts={"feetinyourface": [image_post("a"), image_post("b", 5)]},
    )
    cog = make_cog(source, mapped={"mapped": {}}, denied=["deniedone"])
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(cog, ctx, "feet", TARGET, 2500)

    assert source.searches == [("feet", 2500, 50)]
    summary, card = ctx.channel.sent
    assert "1 already mapped" in summary.content and "1 denied earlier" in summary.content
    assert "1 hidden by the name/description safety screen" in summary.content
    assert source.fetches == ["feetinyourface"]             # only the surviving candidate is previewed
    assert card.embeds[0].title == "r/FeetInYourFace"
    assert len(card.embeds) == 2                            # details + one extra image tile
    assert card.view.subreddit == "feetinyourface" and card.view.channel_id == 777
    assert card.view.message is card                        # so the timeout can edit it


async def test_preview_failure_still_posts_the_card_with_a_note():
    source = FakeSource(results={"feet": [raw("FeetInYourFace", 400)]}, fail_posts={"feetinyourface"})
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(make_cog(source), ctx, "feet", TARGET)

    card = ctx.channel.sent[1]
    assert any("timed out" in f.value for f in card.embeds[0].fields)


async def test_one_failing_prefix_does_not_sink_the_others():
    source = FakeSource(results={"foot": [raw("FootFetish", 500)]}, fail_search={"feet"})
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(make_cog(source), ctx, "feet,foot", TARGET)

    assert "Search failed for `feet`" in ctx.channel.sent[0].content
    assert ctx.channel.sent[1].embeds[0].title == "r/FootFetish"


async def test_same_subreddit_from_two_prefixes_is_suggested_once():
    source = FakeSource(results={"feet": [raw("FeetFoot", 500)], "foot": [raw("FeetFoot", 500)]})
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(make_cog(source), ctx, "feet,foot", TARGET)
    assert len(ctx.channel.sent) == 2        # summary + a single card


async def test_nothing_to_suggest_just_posts_the_summary():
    ctx = FakeCtx()
    await command_callback("redditfeed_discover")(make_cog(FakeSource()), ctx, "feet", TARGET)
    assert len(ctx.channel.sent) == 1 and "Nothing new to suggest" in ctx.channel.sent[0].content


# -- denied list commands ----------------------------------------------------------------------------

async def test_denied_and_undeny_commands():
    cog = make_cog(denied=["b", "a"])
    ctx = FakeCtx()
    await command_callback("redditfeed_denied")(cog, ctx)
    assert "r/a, r/b" in ctx.channel.sent[0].content

    await command_callback("redditfeed_undeny")(cog, ctx, "r/A")
    assert cog.config.denied_subreddits.data == ["b"]
    await command_callback("redditfeed_undeny")(cog, ctx, "zzz")
    assert "isn't on the denied list" in ctx.channel.sent[-1].content
