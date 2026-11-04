import pytest

from cardcollect import engine, import_characters
from cardcollect.constants import DEFAULT_TIER_CUTOFFS


async def instant_sleep(seconds):
    """Drop-in for asyncio.sleep in tests -- the series fetch functions and
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


# ---------------------------------------------------------------------------
# gender filter, English series titles
# ---------------------------------------------------------------------------


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


# --- 1.7.1: skip / art helpers ---------------------------------------------------


def _entries(*names):
    return [{"rank": i * 2, "name": n, "image_url": f"https://x/{i}.png", "rarity": "common", "favourites": 1} for i, n in enumerate(names)]


def test_resolve_entry_takes_a_number_an_exact_name_or_a_unique_part():
    entries = _entries("Frieren", "Fern", "Stark")
    assert import_characters.resolve_entry("2", entries)[0]["name"] == "Fern"
    assert import_characters.resolve_entry("#3", entries)[0]["name"] == "Stark"
    assert import_characters.resolve_entry("frie", entries)[0]["name"] == "Frieren"
    assert import_characters.resolve_entry("fern", entries)[0]["name"] == "Fern"
    assert "no number 9" in import_characters.resolve_entry("9", entries)[1]
    assert "No new character matches" in import_characters.resolve_entry("zzz", entries)[1]
    assert "Name or number missing" in import_characters.resolve_entry("  ", entries)[1]
    assert "matches several" in import_characters.resolve_entry("r", entries)[1]


def test_parse_adjustments_reads_rarity_skip_and_unskip_lines_together():
    entries = _entries("Frieren", "Fern", "Stark")
    parsed = import_characters.parse_adjustments("Fern: epic\nskip 1, Stark\nunskip 3\nSKIP: 2\nskip 9", entries)
    assert parsed["overrides"] == {2: "epic"}
    assert parsed["skip"] == {0, 2, 4}
    assert parsed["unskip"] == {4}
    assert any("no number 9" in p for p in parsed["problems"])


def test_a_character_whose_name_starts_with_skip_is_still_a_rarity_line():
    entries = _entries("Skipper Joe", "Fern")
    parsed = import_characters.parse_adjustments("Skipper Joe: legendary", entries)
    assert parsed["overrides"] == {0: "legendary"} and parsed["skip"] == set()


def test_select_top_leaves_out_skipped_ranks_and_entry_numbers_count_new_characters_only():
    built = {"entries": _entries("A", "B", "C"), "links": [], "skipped": [{"rank": 1, "name": "X", "reason": "already in the pool"}]}
    assert [e["name"] for e in import_characters.select_top(built, 10, skip={2})["entries"]] == ["A", "C"]
    assert import_characters.entry_numbers(built) == {0: 1, 2: 2, 4: 3}


def test_art_flags_catch_placeholders_small_wide_and_broken_images():
    import io as _io
    from PIL import Image as _Image

    def png(w, h):
        buf = _io.BytesIO()
        _Image.new("RGB", (w, h)).save(buf, format="PNG")
        return buf.getvalue()

    entry = {"image_url": "https://x/char.png"}
    assert import_characters.art_flags(entry, png(230, 345)) is None
    assert "small" in import_characters.art_flags(entry, png(100, 140))
    assert "wide" in import_characters.art_flags(entry, png(400, 200))
    assert import_characters.art_flags(entry, b"junk") == "not an image"
    assert import_characters.art_flags(entry, None) == "download failed"
    assert import_characters.art_flags({"image_url": "https://s4.anilist.co/x/default.jpg"}, png(230, 345)) == "AniList placeholder"


def test_parse_adjustments_understands_number_ranges():
    entries = _entries("A", "B", "C", "D", "E")
    parsed = import_characters.parse_adjustments("skip 2-4\nunskip 3-3, 5", entries)
    assert parsed["skip"] == {2, 4, 6}  # numbers 2,3,4 -> ranks 2,4,6
    assert parsed["unskip"] == {4, 8}
    assert import_characters.parse_adjustments("skip 4-9", entries)["problems"]
