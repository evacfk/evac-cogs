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


# ---------------------------------------------------------------------------
# gender filter, English series titles
# ---------------------------------------------------------------------------


def test_filter_gender_any_keeps_everyone_including_unknown_gender():
    chars = [
        make_character(1, "Alice", 100, gender="Female"),
        make_character(2, "Bob", 100, gender="Male"),
        make_character(3, "Cass", 100, gender=None),
    ]
    assert [c["id"] for c in import_characters.filter_gender(chars, "any")] == [1, 2, 3]
    assert [c["id"] for c in import_characters.filter_gender(chars, "male")] == [2]
    assert [c["id"] for c in import_characters.filter_gender(chars, "FEMALE")] == [1]


def test_build_pool_entries_can_import_every_gender():
    chars = [make_character(1, "Alice", 100, gender="Female"), make_character(2, "Bob", 100, gender="Male")]
    quotas = {"common": 5, "rare": 5, "epic": 5, "legendary": 5}
    default = import_characters.build_pool_entries(chars, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, dict(quotas))
    everyone = import_characters.build_pool_entries(
        chars, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, dict(quotas), gender="any"
    )
    assert [e["name"] for e in default] == ["Alice"]  # the pure function's default is unchanged
    assert [e["name"] for e in everyone] == ["Alice", "Bob"]


def test_to_pool_entry_keeps_the_english_series_title_only_when_it_differs():
    char = make_character(1, "Fern", 100)
    char["media"] = {"nodes": [{"title": {"romaji": "Sousou no Frieren", "english": "Frieren: Beyond Journey's End"}}]}
    entry = import_characters.to_pool_entry(char, DEFAULT_TIER_CUTOFFS, engine.bucket_tier)
    assert entry["series"] == "Sousou no Frieren"
    assert entry["series_alt"] == "Frieren: Beyond Journey's End"

    char["media"] = {"nodes": [{"title": {"romaji": "Bleach", "english": "bleach"}}]}
    assert import_characters.to_pool_entry(char, DEFAULT_TIER_CUTOFFS, engine.bucket_tier)["series_alt"] == ""

    char["media"] = {"nodes": [{"title": {"romaji": "Bleach"}}]}  # no english key at all
    assert import_characters.to_pool_entry(char, DEFAULT_TIER_CUTOFFS, engine.bucket_tier)["series_alt"] == ""


# ---------------------------------------------------------------------------
# series search + fetch
# ---------------------------------------------------------------------------


def media_search_response(*items):
    return {"data": {"Page": {"media": list(items)}}}


def media_item(id_, romaji, english=None, format_="TV", year=2023):
    return {
        "id": id_,
        "type": "ANIME",
        "format": format_,
        "seasonYear": year,
        "startDate": {"year": year},
        "title": {"romaji": romaji, "english": english},
    }


def test_parse_media_search_maps_fields_and_skips_unusable_rows():
    resp = media_search_response(
        media_item(1, "Sousou no Frieren", "Frieren: Beyond Journey's End"),
        media_item(2, "Frieren Manga", None, format_="MANGA", year=None),
        {"id": None, "title": {"romaji": "No id"}},
        {"id": 4, "title": {"romaji": None, "english": None}},
    )
    results = import_characters.parse_media_search(resp)
    assert [r["id"] for r in results] == [1, 2]
    assert results[0]["title_english"] == "Frieren: Beyond Journey's End"
    assert results[0]["year"] == 2023
    assert results[1]["format"] == "MANGA"


def test_parse_media_search_is_loud_about_a_changed_shape():
    with pytest.raises((KeyError, TypeError)):
        import_characters.parse_media_search({"data": None})


@pytest.mark.asyncio
async def test_search_media_returns_the_parsed_pick_list():
    session = FakeSession([media_search_response(media_item(7, "Some Show"))])
    results = await import_characters.search_media(session, "  some show ", sleep_fn=instant_sleep)
    assert [r["id"] for r in results] == [7]


def media_characters_response(nodes, total=3, has_next=False, romaji="Some Show", english="Some Show EN"):
    return {
        "data": {
            "Media": {
                "id": 7,
                "title": {"romaji": romaji, "english": english},
                "characters": {"pageInfo": {"total": total, "hasNextPage": has_next}, "nodes": nodes},
            }
        }
    }


def series_node(id_, name, favourites, has_image=True):
    return {
        "id": id_,
        "name": {"full": name, "native": name},
        "image": {"large": f"https://example.invalid/{id_}.png"} if has_image else {"large": None},
        "favourites": favourites,
        "gender": "Female",
    }


def test_parse_media_characters_reads_titles_total_and_paging():
    parsed = import_characters.parse_media_characters(
        media_characters_response([series_node(1, "A", 10)], total=38, has_next=True)
    )
    assert parsed["title"] == "Some Show"
    assert parsed["title_english"] == "Some Show EN"
    assert parsed["total"] == 38
    assert parsed["has_next"] is True
    assert len(parsed["characters"]) == 1


@pytest.mark.asyncio
async def test_fetch_series_characters_pages_until_the_end():
    session = FakeSession(
        [
            media_characters_response([series_node(1, "A", 30), series_node(2, "B", 20)], total=3, has_next=True),
            media_characters_response([series_node(3, "C", 10)], total=3, has_next=False),
        ]
    )
    info = await import_characters.fetch_series_characters(session, 7, limit=100, sleep_fn=instant_sleep)
    assert [c["id"] for c in info["characters"]] == [1, 2, 3]
    assert info["total"] == 3
    assert session.calls == 2


@pytest.mark.asyncio
async def test_fetch_series_characters_stops_at_the_limit_and_truncates():
    session = FakeSession(
        [
            media_characters_response([series_node(1, "A", 30), series_node(2, "B", 20)], total=9, has_next=True),
            media_characters_response([series_node(3, "C", 10)], total=9, has_next=False),
        ]
    )
    info = await import_characters.fetch_series_characters(session, 7, limit=2, sleep_fn=instant_sleep)
    assert [c["id"] for c in info["characters"]] == [1, 2]
    assert session.calls == 1  # the limit was already met, so no second request


# ---------------------------------------------------------------------------
# series entries: rarity, dedupe, linking hand-added cards
# ---------------------------------------------------------------------------


def build(characters, existing=(), title="Some Show", english="Some Show EN"):
    return import_characters.build_series_entries(
        characters, title, english, DEFAULT_TIER_CUTOFFS, engine.bucket_tier, list(existing)
    )


def chars_from_nodes(*nodes):
    return list(nodes)


def test_series_entries_get_their_rarity_from_favourites_and_a_rank():
    built = build(
        [
            series_node(1, "Star", 25000),
            series_node(2, "Hero", 9000),
            series_node(3, "Friend", 3000),
            series_node(4, "Extra", 50),
        ]
    )
    assert [(e["name"], e["rarity"], e["rank"]) for e in built["entries"]] == [
        ("Star", "legendary", 0),
        ("Hero", "epic", 1),
        ("Friend", "rare", 2),
        ("Extra", "common", 3),
    ]
    assert all(e["series"] == "Some Show" and e["series_alt"] == "Some Show EN" for e in built["entries"])


def test_series_entries_skip_characters_already_in_the_pool_retired_ones_included():
    existing = [
        {"card_id": 1, "anilist_id": 1, "name": "Star", "series": "Some Show", "series_alt": ""},
        # a retired card is passed in exactly like an active one -- the cog sends the WHOLE pool
        {"card_id": 2, "anilist_id": 2, "name": "Hero", "series": "Some Show", "series_alt": ""},
    ]
    built = build([series_node(1, "Star", 100), series_node(2, "Hero", 100), series_node(3, "Fresh", 100)], existing)
    assert [e["name"] for e in built["entries"]] == ["Fresh"]
    assert [(x["name"], x["reason"]) for x in built["skipped"]] == [
        ("Star", "already in the pool"),
        ("Hero", "already in the pool"),
    ]


def test_series_entries_skip_a_duplicate_within_the_same_list():
    built = build([series_node(1, "Star", 100), series_node(1, "Star", 100)])
    assert len(built["entries"]) == 1


def test_series_entries_skip_characters_without_art():
    built = build([series_node(1, "NoArt", 100, has_image=False), series_node(2, "Fine", 100)])
    assert [e["name"] for e in built["entries"]] == ["Fine"]
    assert built["skipped"][0]["reason"] == "no name or image on AniList"


def test_series_entries_link_a_hand_added_card_instead_of_duplicating_it():
    hand_added = {"card_id": 9, "anilist_id": None, "name": "fern", "series": "Some Show EN", "series_alt": ""}
    built = build([series_node(5, "Fern", 100)], [hand_added])
    assert built["entries"] == []
    assert built["links"] == [
        {"rank": 0, "name": "Fern", "card_id": 9, "anilist_id": 5, "series_alt": "Some Show EN"}
    ]


def test_series_entries_do_not_link_a_different_character_who_shares_a_name():
    other_series = {"card_id": 9, "anilist_id": None, "name": "Lucy", "series": "Cyberpunk Edgerunners", "series_alt": ""}
    built = build([series_node(5, "Lucy", 100)], [other_series])
    assert [e["name"] for e in built["entries"]] == ["Lucy"]
    assert built["links"] == []


def test_series_entries_link_a_hand_added_card_that_recorded_no_series():
    no_series = {"card_id": 9, "anilist_id": None, "name": "Xalatath", "series": "", "series_alt": ""}
    built = build([series_node(5, "Xalatath", 100)], [no_series])
    assert built["links"][0]["card_id"] == 9


def test_one_hand_added_card_links_to_only_one_character():
    hand_added = {"card_id": 9, "anilist_id": None, "name": "Rem", "series": "", "series_alt": ""}
    built = build([series_node(1, "Rem", 100), series_node(2, "Rem", 100)], [hand_added])
    assert len(built["links"]) == 1
    assert len(built["entries"]) == 1


def test_split_hand_added_links_bulk_entries_to_existing_cards():
    entries = [
        {"anilist_id": 1, "name": "Zero Two", "series": "Darling in the FranXX", "series_alt": "", "rarity": "epic"},
        {"anilist_id": 2, "name": "Rem", "series": "Re:Zero", "series_alt": "", "rarity": "epic"},
    ]
    existing = [{"card_id": 4, "anilist_id": None, "name": "Zero Two", "series": "Darling in the FranXX", "series_alt": ""}]
    keep, links = import_characters.split_hand_added(entries, existing)
    assert [e["name"] for e in keep] == ["Rem"]
    assert links[0]["card_id"] == 4 and links[0]["anilist_id"] == 1


# ---------------------------------------------------------------------------
# top-N selection and rarity edits
# ---------------------------------------------------------------------------


def test_select_top_limits_by_rank_and_applies_overrides_without_mutating():
    built = build([series_node(1, "A", 25000), series_node(2, "B", 9000), series_node(3, "C", 50)])
    picked = import_characters.select_top(built, 2, {1: "legendary"})
    assert [(e["name"], e["rarity"]) for e in picked["entries"]] == [("A", "legendary"), ("B", "legendary")]
    assert built["entries"][1]["rarity"] == "epic"  # the stored preview is untouched


def entry(rank, name):
    return {"rank": rank, "name": name}


def test_parse_rarity_overrides_reads_the_common_line_styles():
    entries = [entry(0, "Frieren"), entry(1, "Fern"), entry(2, "Stark")]
    overrides, problems = import_characters.parse_rarity_overrides(
        "Frieren: legendary\nFern = Epic\nStark rare\n\n", entries
    )
    assert overrides == {0: "legendary", 1: "epic", 2: "rare"}
    assert problems == []


def test_parse_rarity_overrides_accepts_a_unique_partial_name():
    overrides, problems = import_characters.parse_rarity_overrides("fri: epic", [entry(0, "Frieren"), entry(1, "Fern")])
    assert overrides == {0: "epic"} and problems == []


def test_parse_rarity_overrides_reports_ambiguous_unknown_and_bad_lines():
    entries = [entry(0, "Sword Saint"), entry(1, "Sword Maiden")]
    overrides, problems = import_characters.parse_rarity_overrides(
        "Sword: epic\nNobody: rare\nSword Saint: shiny\njustoneword", entries
    )
    assert overrides == {}
    assert len(problems) == 4
    assert "several" in problems[0]
    assert "No new character" in problems[1]
    assert "isn't a rarity" in problems[2]
    assert "Couldn't read" in problems[3]


def test_parse_rarity_overrides_handles_a_colon_inside_a_name():
    overrides, problems = import_characters.parse_rarity_overrides("Re:Zero Girl: legendary", [entry(0, "Re:Zero Girl")])
    assert overrides == {0: "legendary"} and problems == []
