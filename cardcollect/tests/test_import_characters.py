import pytest

from cardcollect import engine, import_characters
from cardcollect.constants import DEFAULT_TIER_CUTOFFS


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


def test_build_pool_entries_filters_and_caps():
    chars = [
        make_character(1, "Alice", 50000, gender="Female"),
        make_character(2, "Bob", 50000, gender="Male"),
        make_character(3, "Cass", 100, gender="Female"),
        make_character(4, "Dee", 5000, gender="Female"),
    ]
    entries = import_characters.build_pool_entries(chars, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, target_count=2)
    assert len(entries) == 2
    assert {e["name"] for e in entries} == {"Alice", "Cass"}


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
async def test_fetch_top_female_characters_pages_until_target_reached():
    page1 = make_page_response(
        [make_character(i, f"Fem{i}", 1000, gender="Female") for i in range(5)], has_next_page=True
    )
    page2 = make_page_response(
        [make_character(100 + i, f"Fem2_{i}", 900, gender="Female") for i in range(5)], has_next_page=True
    )
    session = FakeSession([page1, page2])

    result = await import_characters.fetch_top_female_characters(
        session, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, target_count=7, per_page=5
    )
    assert len(result) == 7
    assert session.calls == 2


@pytest.mark.asyncio
async def test_fetch_top_female_characters_stops_when_no_next_page():
    page1 = make_page_response(
        [make_character(i, f"Fem{i}", 1000, gender="Female") for i in range(3)], has_next_page=False
    )
    session = FakeSession([page1])

    result = await import_characters.fetch_top_female_characters(
        session, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, target_count=50, per_page=3
    )
    assert len(result) == 3
    assert session.calls == 1
