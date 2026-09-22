import pytest

from cardcollect import engine, import_characters
from cardcollect.constants import DEFAULT_TIER_CUTOFFS


async def instant_sleep(seconds):
    """Drop-in for asyncio.sleep in tests -- fetch_top_female_characters and
    _post_with_rate_limit_retry both space out/back off real requests with
    real delays by default; tests inject this instead so they run instantly
    rather than actually waiting out request_delay/backoff seconds."""
    return


def make_character(id_, name, favourites, gender="Female", series="Some Show", has_image=True):
    return {
        "id": id_,
        "name": {"full": name, "native": name},
        "image": {"large": f"https://example.invalid/{id_}.png"} if has_image else {"large": None},
        "favourites": favourites,
        "gender": gender,
        "media": {"nodes": [{"title": {"romaji": series}}]},
    }


def make_page_response(characters, has_next_page=False):
    return {
        "data": {
            "Page": {
                "pageInfo": {"hasNextPage": has_next_page},
                "characters": characters,
            }
        }
    }


def test_parse_characters_and_has_next_page():
    resp = make_page_response([make_character(1, "Alice", 100)], has_next_page=True)
    assert import_characters.parse_characters(resp) == resp["data"]["Page"]["characters"]
    assert import_characters.has_next_page(resp) is True


def test_filter_female_keeps_only_female_case_insensitive():
    chars = [
        make_character(1, "Alice", 100, gender="Female"),
        make_character(2, "Bob", 100, gender="Male"),
        make_character(3, "Cass", 100, gender="female"),
        make_character(4, "Dee", 100, gender=None),
    ]
    female = import_characters.filter_female(chars)
    names = {c["name"]["full"] for c in female}
    assert names == {"Alice", "Cass"}


def test_to_pool_entry_maps_fields_and_buckets_rarity():
    char = make_character(1, "Legendary Lady", DEFAULT_TIER_CUTOFFS["legendary"])
    entry = import_characters.to_pool_entry(char, DEFAULT_TIER_CUTOFFS, engine.bucket_tier)
    assert entry["name"] == "Legendary Lady"
    assert entry["series"] == "Some Show"
    assert entry["rarity"] == "legendary"
    assert entry["image_url"] == "https://example.invalid/1.png"


def test_to_pool_entry_skips_missing_image_or_name():
    no_image = make_character(1, "No Image", 100, has_image=False)
    assert import_characters.to_pool_entry(no_image, DEFAULT_TIER_CUTOFFS, engine.bucket_tier) is None

    no_name = make_character(2, "", 100)
    assert import_characters.to_pool_entry(no_name, DEFAULT_TIER_CUTOFFS, engine.bucket_tier) is None


def test_build_pool_entries_filters_and_respects_quotas():
    chars = [
        make_character(1, "Alice", 50000, gender="Female"),  # legendary
        make_character(2, "Bob", 50000, gender="Male"),  # filtered: not female
        make_character(3, "Cass", 100, gender="Female"),  # common
        make_character(4, "Dee", 5000, gender="Female"),  # rare
        make_character(5, "Eve", 60000, gender="Female"),  # legendary, quota already spent
    ]
    quotas = {"legendary": 1, "epic": 0, "rare": 1, "common": 1}
    entries = import_characters.build_pool_entries(chars, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, quotas)
    assert {e["name"] for e in entries} == {"Alice", "Cass", "Dee"}
    # quotas are decremented in place as entries are accepted, and a tier
    # with no quota left (legendary, after Alice) skips further candidates
    # (Eve) rather than overfilling it
    assert quotas == {"legendary": 0, "epic": 0, "rare": 0, "common": 0}


def test_tier_quotas_splits_proportionally_and_covers_the_full_count():
    quotas = import_characters.tier_quotas(100, weights={"common": 60, "rare": 27, "epic": 10, "legendary": 3})
    assert sum(quotas.values()) == 100
    # common should dominate, legendary should be the smallest slice
    assert quotas["common"] > quotas["rare"] > quotas["epic"] >= quotas["legendary"]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class FakeSession:
    """Replays a fixed sequence of page responses, one per call to .post()."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def post(self, url, json=None):
        payload = self._pages[self.calls]
        self.calls += 1
        return FakeResponse(payload)


@pytest.mark.asyncio
async def test_fetch_top_female_characters_pages_until_every_tier_quota_is_filled():
    # target_count=7 against DEFAULT_DROP_WEIGHTS works out to quotas of
    # common=4, rare=2, epic=1, legendary=0 (see test_tier_quotas_* above).
    # Spread favourites across tiers so the quotas -- not a raw character
    # count -- are what stops the paging.
    page1 = make_page_response(
        [
            make_character(0, "Fem0", 100, gender="Female"),  # common
            make_character(1, "Fem1", 200, gender="Female"),  # common
            make_character(2, "Fem2", 300, gender="Female"),  # common
            make_character(3, "Fem3", 3000, gender="Female"),  # rare
            make_character(4, "Fem4", 9000, gender="Female"),  # epic
        ],
        has_next_page=True,
    )
    page2 = make_page_response(
        [
            make_character(100, "Fem2_0", 150, gender="Female"),  # common (fills the 4th slot)
            make_character(101, "Fem2_1", 2500, gender="Female"),  # rare (fills the 2nd slot)
        ],
        has_next_page=True,  # irrelevant: quotas are full, so a 3rd page is never fetched
    )
    session = FakeSession([page1, page2])

    result = await import_characters.fetch_top_female_characters(
        session, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, target_count=7, per_page=5, sleep_fn=instant_sleep
    )
    assert len(result) == 7
    assert session.calls == 2
    by_tier = {"common": 0, "rare": 0, "epic": 0, "legendary": 0}
    for entry in result:
        by_tier[entry["rarity"]] += 1
    assert by_tier == {"common": 4, "rare": 2, "epic": 1, "legendary": 0}


@pytest.mark.asyncio
async def test_fetch_top_female_characters_stops_when_no_next_page():
    page1 = make_page_response(
        [make_character(i, f"Fem{i}", 1000, gender="Female") for i in range(3)], has_next_page=False
    )
    session = FakeSession([page1])

    result = await import_characters.fetch_top_female_characters(
        session, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, target_count=50, per_page=3, sleep_fn=instant_sleep
    )
    assert len(result) == 3
    assert session.calls == 1


def test_build_pool_entries_skips_excluded_anilist_ids_and_decrements_exclude_set_in_place():
    chars = [
        make_character(1, "Already imported", 100, gender="Female"),  # id 1 excluded
        make_character(2, "New character", 150, gender="Female"),
    ]
    quotas = {"common": 5, "rare": 0, "epic": 0, "legendary": 0}
    exclude_ids = {1}
    entries = import_characters.build_pool_entries(chars, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, quotas, exclude_ids)
    assert {e["name"] for e in entries} == {"New character"}
    # the newly-accepted character's id is folded into exclude_ids too, so a
    # second page (or a second run sharing the same set) can't re-add it
    assert exclude_ids == {1, 2}


@pytest.mark.asyncio
async def test_fetch_top_female_characters_does_not_reimport_existing_pool_characters():
    """Regression test for the 'importpool adds duplicate characters on a
    second run' bug: AniList ids already in the pool must be skipped, and
    paging must continue past them to find fresh characters instead of
    stopping early because the page 'looked' full of already-seen ids."""
    # id 1 is "already in the pool" (common favourites so it'd otherwise be
    # accepted); id 2 is genuinely new
    page1 = make_page_response(
        [
            make_character(1, "Already have this one", 100, gender="Female"),
            make_character(2, "Brand new", 150, gender="Female"),
        ],
        has_next_page=False,
    )
    session = FakeSession([page1])

    result = await import_characters.fetch_top_female_characters(
        session,
        DEFAULT_TIER_CUTOFFS,
        engine.bucket_tier,
        target_count=5,
        per_page=2,
        exclude_ids={1},
        sleep_fn=instant_sleep,
    )
    assert [e["name"] for e in result] == ["Brand new"]


# ---------------------------------------------------------------------------
# rate-limit (429) retry behavior
# ---------------------------------------------------------------------------


class FakeRateLimitedResponse:
    """Unlike FakeResponse above, this carries a real .status and .headers
    so _post_with_rate_limit_retry's 429-detection path is actually
    exercised (FakeResponse's getattr(..., default) fallback deliberately
    skips that path for the older, lighter-weight fakes)."""

    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._payload


class FakeRateLimitedSession:
    """Replays a fixed sequence of responses (a mix of 429s and normal page
    responses), one per call to .post() -- used to simulate AniList tripping
    its rate limit mid-import and then recovering."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


@pytest.mark.asyncio
async def test_post_with_rate_limit_retry_retries_a_429_and_then_succeeds():
    sleeps = []

    async def recording_sleep(seconds):
        sleeps.append(seconds)

    page = make_page_response([], has_next_page=False)
    session = FakeRateLimitedSession(
        [
            FakeRateLimitedResponse(429, headers={"Retry-After": "2"}),
            FakeRateLimitedResponse(200, payload=page),
        ]
    )

    data = await import_characters._post_with_rate_limit_retry(
        session, "https://example.invalid", {"query": "x"}, sleep_fn=recording_sleep
    )
    assert data == page
    assert session.calls == 2
    assert sleeps == [2.0]  # honored the Retry-After header exactly, no default backoff used


@pytest.mark.asyncio
async def test_post_with_rate_limit_retry_uses_default_backoff_without_retry_after_header():
    sleeps = []

    async def recording_sleep(seconds):
        sleeps.append(seconds)

    page = make_page_response([], has_next_page=False)
    session = FakeRateLimitedSession(
        [FakeRateLimitedResponse(429), FakeRateLimitedResponse(200, payload=page)]
    )

    await import_characters._post_with_rate_limit_retry(
        session, "https://example.invalid", {"query": "x"}, sleep_fn=recording_sleep
    )
    assert sleeps == [import_characters.RATE_LIMIT_DEFAULT_BACKOFF_SECONDS]


@pytest.mark.asyncio
async def test_post_with_rate_limit_retry_gives_up_after_max_retries():
    async def instant(seconds):
        return

    # always 429, never recovers -- must eventually raise rather than loop forever
    session = FakeRateLimitedSession(
        [FakeRateLimitedResponse(429) for _ in range(import_characters.RATE_LIMIT_MAX_RETRIES + 2)]
    )

    with pytest.raises(Exception):
        await import_characters._post_with_rate_limit_retry(
            session, "https://example.invalid", {"query": "x"}, sleep_fn=instant, max_retries=2
        )
    # gave up after max_retries+1 attempts (the original try plus max_retries retries),
    # not before, and not indefinitely
    assert session.calls == 3


@pytest.mark.asyncio
async def test_fetch_top_female_characters_recovers_from_a_429_mid_import():
    """End-to-end: a 429 on the first page must not abort the whole import
    -- fetch_top_female_characters should recover via the retry and still
    return the page's characters."""
    page = make_page_response(
        [make_character(1, "Survivor", 100, gender="Female")], has_next_page=False
    )
    session = FakeRateLimitedSession(
        [FakeRateLimitedResponse(429, headers={"Retry-After": "0"}), FakeRateLimitedResponse(200, payload=page)]
    )

    result = await import_characters.fetch_top_female_characters(
        session, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, target_count=5, per_page=5, sleep_fn=instant_sleep
    )
    assert [e["name"] for e in result] == ["Survivor"]
