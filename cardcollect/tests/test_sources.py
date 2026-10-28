"""Searching AniList and IGDB for a requested character."""

import asyncio

import pytest

from cardcollect import sources
from cardcollect.constants import DEFAULT_TIER_CUTOFFS


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Session:
    """Answers AniList, Twitch and IGDB calls from canned data and records them."""

    def __init__(self, anilist=None, igdb=None, igdb_status=200):
        self.anilist, self.igdb, self.igdb_status = anilist or {}, igdb or [], igdb_status
        self.calls = []

    def post(self, url, json=None, data=None, headers=None, params=None):
        self.calls.append((url, json, data, headers, params))
        if "twitch" in url:
            return Resp({"access_token": "tok", "expires_in": 5000000})
        if "igdb" in url:
            return Resp(self.igdb, self.igdb_status)
        return Resp(self.anilist)


def al_char(id_, name, fav, series="Frieren", image="http://img/x.png"):
    return {"id": id_, "name": {"full": name}, "image": {"large": image}, "favourites": fav,
            "media": {"nodes": [{"title": {"romaji": series, "english": None}}]}}


def test_search_terms_use_the_whole_text_then_the_name_part():
    assert sources.search_terms("Frieren, Sousou no Frieren") == ["Frieren, Sousou no Frieren", "Frieren"]
    assert sources.search_terms("Kratos | God of War") == ["Kratos | God of War", "Kratos"]
    assert sources.search_terms("Kratos from God of War")[-1] == "Kratos"
    assert sources.search_terms("Nami") == ["Nami"]
    assert sources.search_terms(" a ") == []


def test_parse_igdb_candidates_handles_missing_pictures_games_and_junk():
    rows = [
        {"id": 1, "name": "Kratos", "games": [{"name": "God of War"}], "mug_shot": {"image_id": "abc"}},
        {"id": 2, "name": "Nobody"},
        {"id": 3, "name": " "},
    ]
    out = sources.parse_igdb_candidates(rows)
    assert [c["name"] for c in out] == ["Kratos", "Nobody"]
    assert out[0]["series"] == "God of War" and out[0]["image_url"].endswith("/abc.jpg") and out[0]["source"] == "igdb"
    assert out[1]["image_url"] is None and out[1]["series"] == ""
    assert sources.parse_igdb_candidates({"message": "bad"}) == []


def test_parse_anilist_candidates_suggest_a_rarity_from_popularity():
    resp = {"data": {"Page": {"characters": [al_char(1, "Frieren", 100000), al_char(2, "Minor", 5), {"id": 3, "name": {"full": "NoPic"}}]}}}
    out = sources.parse_anilist_candidates(resp, DEFAULT_TIER_CUTOFFS)
    assert [c["name"] for c in out] == ["Frieren", "Minor"]
    assert out[0]["rarity"] != "common" and out[1]["rarity"] == "common"
    assert out[0]["source"] == "anilist" and out[0]["anilist_id"] == 1


def test_rank_puts_an_exact_name_first_then_better_word_matches():
    cands = [
        {"source": "anilist", "source_id": 1, "name": "Frieren Fan", "series": "Other"},
        {"source": "igdb", "source_id": 2, "name": "Frieren", "series": "Sousou no Frieren"},
        {"source": "anilist", "source_id": 3, "name": "Himmel", "series": "Sousou no Frieren"},
    ]
    ranked = sources.rank(cands, "Frieren, Sousou no Frieren")
    assert ranked[0]["source_id"] == 2


@pytest.mark.asyncio
async def test_find_candidates_searches_both_sources_and_ranks():
    session = Session(
        anilist={"data": {"Page": {"characters": [al_char(1, "Kratos Clone", 10)]}}},
        igdb=[{"id": 9, "name": "Kratos", "games": [{"name": "God of War"}], "mug_shot": {"image_id": "z"}}],
    )
    out, notes = await sources.find_candidates(session, "Kratos | God of War", DEFAULT_TIER_CUTOFFS, sources.IGDBClient(), ("id", "secret"))
    assert out[0]["name"] == "Kratos" and out[0]["source"] == "igdb" and notes == []
    igdb_calls = [c for c in session.calls if "igdb.com" in c[0]]
    assert igdb_calls and igdb_calls[0][3]["Client-ID"] == "id" and igdb_calls[0][3]["Authorization"] == "Bearer tok"
    assert 'search "Kratos | God of War"' in igdb_calls[0][2]
    assert len([c for c in session.calls if "twitch" in c[0]]) == 1, "the app token is reused"


@pytest.mark.asyncio
async def test_without_igdb_keys_only_anilist_is_searched_and_the_admin_is_told_how_to_enable_games():
    session = Session(anilist={"data": {"Page": {"characters": [al_char(1, "Nami", 500, "One Piece")]}}})
    out, notes = await sources.find_candidates(session, "Nami", DEFAULT_TIER_CUTOFFS, sources.IGDBClient(), None)
    assert [c["name"] for c in out] == ["Nami"]
    assert any("set api igdb" in n for n in notes)
    assert not any("igdb" in c[0] for c in session.calls)


@pytest.mark.asyncio
async def test_one_source_failing_keeps_the_other_sources_results():
    session = Session(anilist={"data": {"Page": {"characters": [al_char(1, "Nami", 500, "One Piece")]}}}, igdb_status=500)
    out, notes = await sources.find_candidates(session, "Nami", DEFAULT_TIER_CUTOFFS, sources.IGDBClient(), ("a", "b"))
    assert [c["name"] for c in out] == ["Nami"] and any("IGDB couldn't" in n for n in notes)


@pytest.mark.asyncio
async def test_quotes_in_a_search_cannot_break_out_of_the_igdb_query():
    session = Session()
    await sources.IGDBClient().search(session, "a", "b", 'x"; fields *; "')
    body = next(c[2] for c in session.calls if "igdb.com" in c[0])
    assert body.count('"') == 2 and "fields name,mug_shot.image_id,games.name" in body
