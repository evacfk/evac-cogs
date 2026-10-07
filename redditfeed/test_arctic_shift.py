import pytest

from redditfeed import arctic_shift as arctic_shift_module
from redditfeed.arctic_shift import ArcticShiftSource, RedditSourceError, parse_arctic_shift_response


@pytest.fixture(autouse=True)
def _no_real_backoff_delay(monkeypatch):
    """Retries use asyncio.sleep for backoff; skip the real delay in tests."""
    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr(arctic_shift_module.asyncio, "sleep", instant_sleep)


class TestParseArcticShiftResponse:
    def test_returns_data_list_on_success(self):
        payload = {"data": [{"id": "a"}, {"id": "b"}]}
        assert parse_arctic_shift_response(payload) == [{"id": "a"}, {"id": "b"}]

    def test_raises_on_explicit_error_field(self):
        payload = {"data": None, "error": "Timeout. Maybe slow down a bit"}
        with pytest.raises(RedditSourceError):
            parse_arctic_shift_response(payload)

    def test_raises_when_data_missing_and_no_error(self):
        with pytest.raises(RedditSourceError):
            parse_arctic_shift_response({})


class TestArcticShiftSourceFetch:
    async def test_fetch_new_posts_returns_parsed_data_on_first_try(self):
        calls = []

        async def fake_request(url):
            calls.append(url)
            return {"data": [{"id": "p1"}]}

        source = ArcticShiftSource(request_fn=fake_request)
        posts = await source.fetch_new_posts("feet", after_ts=None, limit=10)

        assert posts == [{"id": "p1"}]
        assert len(calls) == 1
        assert "subreddit=feet" in calls[0]
        assert "limit=10" in calls[0]

    async def test_fetch_new_posts_includes_after_param_when_given(self):
        captured = {}

        async def fake_request(url):
            captured["url"] = url
            return {"data": []}

        source = ArcticShiftSource(request_fn=fake_request)
        await source.fetch_new_posts("feet", after_ts=12345, limit=10)

        assert "after=12345" in captured["url"]

    async def test_retries_on_transient_failure_then_succeeds(self):
        """REGRESSION: Arctic Shift's own free-tier timeout ({"data":null,
        "error":"Timeout. Maybe slow down a bit"}) is observed in practice and
        must be retried, not treated as a permanent failure."""
        attempts = {"n": 0}

        async def flaky_request(url):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"data": None, "error": "Timeout. Maybe slow down a bit"}
            return {"data": [{"id": "p1"}]}

        source = ArcticShiftSource(request_fn=flaky_request, max_retries=2)
        posts = await source.fetch_new_posts("feet", after_ts=None, limit=10)

        assert posts == [{"id": "p1"}]
        assert attempts["n"] == 2

    async def test_raises_reddit_source_error_after_exhausting_retries(self):
        async def always_fails(url):
            return {"data": None, "error": "still down"}

        source = ArcticShiftSource(request_fn=always_fails, max_retries=1)
        with pytest.raises(RedditSourceError):
            await source.fetch_new_posts("feet", after_ts=None, limit=10)

    async def test_raises_on_raw_exception_from_request_fn(self):
        async def boom(url):
            raise ConnectionError("network is down")

        source = ArcticShiftSource(request_fn=boom, max_retries=0)
        with pytest.raises(RedditSourceError):
            await source.fetch_new_posts("feet", after_ts=None, limit=10)
