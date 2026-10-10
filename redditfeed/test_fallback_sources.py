import asyncio

import pytest

from redditfeed import arctic_shift as arctic_shift_module
from redditfeed import fallback_sources as fb
from redditfeed.arctic_shift import RedditSourceError, describe_error


@pytest.fixture(autouse=True)
def _no_real_delays(monkeypatch):
    async def instant_sleep(_s):
        return None
    monkeypatch.setattr(arctic_shift_module.asyncio, "sleep", instant_sleep)
    monkeypatch.setattr(fb.asyncio, "sleep", instant_sleep)


class FakeSource:
    def __init__(self, name, posts=None, fail=False):
        self.name, self.posts, self.fail, self.calls = name, posts or [], fail, 0

    async def fetch_new_posts(self, subreddit, after_ts, limit):
        self.calls += 1
        if self.fail:
            raise RedditSourceError(f"{self.name} down")
        return self.posts

    async def search_subreddits(self, prefix, min_subscribers, limit):
        if self.fail:
            raise RedditSourceError("down")
        return [{"display_name": prefix}]


class TestDescribeError:
    def test_timeout_is_not_blank(self):
        assert "TimeoutError" in describe_error(asyncio.TimeoutError())
        assert describe_error(asyncio.TimeoutError()).strip() != ""

    def test_message_is_kept(self):
        assert describe_error(ConnectionError("boom")) == "ConnectionError: boom"

    def test_source_error_text_is_not_double_prefixed(self):
        assert describe_error(RedditSourceError("HTTP 522")) == "HTTP 522"


class TestParsing:
    def test_reddit_listing(self):
        payload = {"data": {"children": [{"data": {"id": "a"}}, {"data": {"id": "b"}}]}}
        assert fb.parse_reddit_listing(payload) == [{"id": "a"}, {"id": "b"}]

    def test_reddit_listing_bad_shape_raises(self):
        with pytest.raises(RedditSourceError):
            fb.parse_reddit_listing({"error": 403})


class TestRedditJsonSource:
    async def test_filters_to_posts_after_cursor(self):
        payload = {"data": {"children": [{"data": {"id": "old", "created_utc": 100}},
                                         {"data": {"id": "new", "created_utc": 300}}]}}
        urls = []

        async def req(url):
            urls.append(url)
            return payload

        src = fb.RedditJsonSource(request_fn=req, min_gap_seconds=0)
        posts = await src.fetch_new_posts("feet", 200, 25)
        assert [p["id"] for p in posts] == ["new"]
        assert "/r/feet/new.json" in urls[0] and "limit=25" in urls[0] and "raw_json=1" in urls[0]

    async def test_error_after_retries(self):
        async def req(url):
            raise asyncio.TimeoutError()

        src = fb.RedditJsonSource(request_fn=req, max_retries=1, min_gap_seconds=0)
        with pytest.raises(RedditSourceError, match="TimeoutError"):
            await src.fetch_new_posts("feet", None, 10)

    async def test_search_unsupported(self):
        with pytest.raises(RedditSourceError):
            await fb.RedditJsonSource(request_fn=None, min_gap_seconds=0).search_subreddits("a", 1, 1)


class TestPullPushSource:
    async def test_builds_url_and_filters(self):
        urls = []

        async def req(url):
            urls.append(url)
            return {"data": [{"id": "x", "created_utc": 500}, {"id": "y", "created_utc": 50}]}

        posts = await fb.PullPushSource(request_fn=req).fetch_new_posts("feet", 100, 10)
        assert [p["id"] for p in posts] == ["x"]
        assert "subreddit=feet" in urls[0] and "after=100" in urls[0] and "size=10" in urls[0]

    async def test_missing_data_raises(self):
        async def req(url):
            return {}

        with pytest.raises(RedditSourceError):
            await fb.PullPushSource(request_fn=req, max_retries=0).fetch_new_posts("feet", None, 10)


class TestFallbackSource:
    async def test_primary_used_when_healthy(self):
        a, b = FakeSource("a", [{"id": 1}]), FakeSource("b", [{"id": 2}])
        fs = fb.FallbackSource([a, b], names=["a", "b"])
        assert await fs.fetch_new_posts("x", None, 5) == [{"id": 1}]
        assert fs.active_name == "a" and b.calls == 0

    async def test_fails_over_to_backup(self):
        a, b = FakeSource("a", fail=True), FakeSource("b", [{"id": 2}])
        fs = fb.FallbackSource([a, b], names=["a", "b"])
        assert await fs.fetch_new_posts("x", None, 5) == [{"id": 2}]
        assert fs.active_name == "b" and "a" in fs.last_errors

    async def test_all_fail_raises_with_every_reason(self):
        fs = fb.FallbackSource([FakeSource("a", fail=True), FakeSource("b", fail=True)], names=["a", "b"])
        with pytest.raises(RedditSourceError) as exc:
            await fs.fetch_new_posts("x", None, 5)
        assert "a down" in str(exc.value) and "b down" in str(exc.value)

    async def test_primary_skipped_after_repeated_failures_then_retried(self):
        now = {"t": 0.0}
        a, b = FakeSource("a", fail=True), FakeSource("b", [{"id": 2}])
        fs = fb.FallbackSource([a, b], names=["a", "b"], primary_failures_to_skip=2,
                               primary_cooldown=300, clock=lambda: now["t"])
        await fs.fetch_new_posts("x", None, 5)
        await fs.fetch_new_posts("x", None, 5)
        assert fs.primary_skipped
        calls = a.calls
        await fs.fetch_new_posts("x", None, 5)
        assert a.calls == calls                      # skipped during cooldown
        now["t"] = 301.0
        a.fail, a.posts = False, [{"id": 1}]
        assert await fs.fetch_new_posts("x", None, 5) == [{"id": 1}]
        assert fs.active_name == "a" and not fs.primary_skipped

    async def test_single_failure_does_not_trip_cooldown(self):
        a, b = FakeSource("a", fail=True), FakeSource("b", [{"id": 2}])
        fs = fb.FallbackSource([a, b], names=["a", "b"], primary_failures_to_skip=2)
        await fs.fetch_new_posts("x", None, 5)
        a.fail, a.posts = False, [{"id": 1}]
        await fs.fetch_new_posts("x", None, 5)       # success resets the count
        a.fail = True
        await fs.fetch_new_posts("x", None, 5)
        assert not fs.primary_skipped

    async def test_search_is_primary_only(self):
        fs = fb.FallbackSource([FakeSource("a", fail=True), FakeSource("b")], names=["a", "b"])
        with pytest.raises(RedditSourceError):
            await fs.search_subreddits("fe", 1, 5)
