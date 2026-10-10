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


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<entry><author><name>/u/someone</name></author><category term="feet" label="r/feet"/>
<content type="html">&lt;table&gt;&lt;tr&gt;&lt;td&gt;&lt;a href="https://www.reddit.com/r/feet/comments/img1/pic/"&gt;&lt;img src="https://b.thumbs.redditmedia.com/t.jpg"/&gt;&lt;/a&gt; &lt;/td&gt;&lt;td&gt;&amp;#32; submitted by &amp;#32; &lt;a href="https://www.reddit.com/user/someone"&gt; /u/someone &lt;/a&gt; &lt;br/&gt; &lt;span&gt;&lt;a href="https://i.redd.it/abc123.jpg?width=1&amp;amp;s=x"&gt;[link]&lt;/a&gt;&lt;/span&gt; &lt;span&gt;&lt;a href="https://www.reddit.com/r/feet/comments/img1/pic/"&gt;[comments]&lt;/a&gt;&lt;/span&gt;&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content>
<id>t3_img1</id><media:thumbnail url="https://b.thumbs.redditmedia.com/t.jpg"/>
<link href="https://www.reddit.com/r/feet/comments/img1/pic/"/><updated>2026-10-09T20:00:00+00:00</updated><published>2026-10-09T19:59:00+00:00</published><title>A pic</title></entry>
<entry><content type="html">&lt;a href="https://www.reddit.com/gallery/gal1"&gt;[link]&lt;/a&gt;</content>
<id>t3_gal1</id><link href="https://www.reddit.com/r/feet/comments/gal1/set/"/><updated>2026-10-09T21:00:00Z</updated><title>A set</title></entry>
<entry><content type="html">&lt;a href="https://v.redd.it/vid1"&gt;[link]&lt;/a&gt;</content>
<id>t3_vid1</id><link href="https://www.reddit.com/r/feet/comments/vid1/clip/"/><updated>2026-10-09T21:05:00Z</updated><title>A clip</title></entry>
<entry><content type="html">self text</content><id>t3_txt1</id><link href="https://www.reddit.com/r/feet/comments/txt1/words/"/><updated>2026-10-09T21:10:00Z</updated><title>Words</title></entry>
<entry><content type="html">x</content><id>t3_nodate</id><link href="https://www.reddit.com/r/feet/comments/nodate/x/"/><title>No date</title></entry>
</feed>"""


class TestParseRss:
    def test_image_post_maps_to_arctic_shape(self):
        posts = {p["id"]: p for p in fb.parse_reddit_rss(ATOM)}
        img = posts["img1"]
        assert img["url"] == "https://i.redd.it/abc123.jpg?width=1&s=x"      # entity-decoded
        assert img["permalink"] == "/r/feet/comments/img1/pic/"
        assert img["title"] == "A pic"
        assert img["created_utc"] == 1791575940.0                           # published beats updated
        assert "score" not in img                                           # RSS has none: never invented

    def test_gallery_and_reddit_video_become_permalink_links(self):
        posts = {p["id"]: p for p in fb.parse_reddit_rss(ATOM)}
        assert posts["gal1"]["is_video"] is True
        assert posts["vid1"]["is_video"] is True
        assert "is_video" not in posts["img1"]

    def test_entry_without_a_date_is_skipped(self):
        assert "nodate" not in {p["id"] for p in fb.parse_reddit_rss(ATOM)}

    def test_invalid_xml_raises_source_error(self):
        with pytest.raises(RedditSourceError):
            fb.parse_reddit_rss("<html>blocked</html")

    def test_downstream_classification_works_on_rss_posts(self):
        from redditfeed import constants, engine
        posts = {p["id"]: p for p in fb.parse_reddit_rss(ATOM)}
        img_items = engine.extract_media_items(posts["img1"])
        assert [i.kind for i in img_items] == [constants.MEDIA_KIND_IMAGE]
        vid_items = engine.extract_media_items(posts["vid1"])
        assert vid_items[0].is_link_only and vid_items[0].url.endswith("/r/feet/comments/vid1/clip/")
        assert engine.extract_media_items(posts["txt1"]) == []


class TestRedditRssSource:
    async def test_filters_to_posts_after_cursor_and_builds_url(self):
        urls = []

        async def req(url):
            urls.append(url)
            return ATOM

        src = fb.RedditRssSource(request_fn=req, min_gap_seconds=0)
        posts = await src.fetch_new_posts("feet", 1791576000.0, 25)       # after 2026-10-09T20:00Z
        assert {p["id"] for p in posts} == {"gal1", "vid1", "txt1"}
        assert "/r/feet/new.rss" in urls[0] and "limit=25" in urls[0]

    async def test_error_after_retries_names_the_exception(self):
        async def req(url):
            raise asyncio.TimeoutError()

        src = fb.RedditRssSource(request_fn=req, max_retries=1, min_gap_seconds=0)
        with pytest.raises(RedditSourceError, match="TimeoutError"):
            await src.fetch_new_posts("feet", None, 10)

    async def test_search_unsupported(self):
        with pytest.raises(RedditSourceError):
            await fb.RedditRssSource(min_gap_seconds=0).search_subreddits("a", 1, 1)


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
