"""Finding a requested character: search AniList (anime/manga) and IGDB (video
games) and return one common candidate shape.

A candidate is a dict: source ("anilist" | "igdb"), source_id, name, series,
series_alt, favourites (AniList only, else 0), rarity (a suggestion), image_url
(or None), and for AniList anilist_id.

Network code takes the aiohttp session as an argument and never touches
Discord, so it is testable with a fake session. IGDB needs a Twitch developer
app: an admin stores it once with
`[p]set api igdb client_id,<id>,client_secret,<secret>` (Red's shared API
tokens); without it the game search is skipped and the review says so.
"""

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import engine, import_characters

CANDIDATES_PER_SOURCE = 5
ANILIST_SEARCH_QUERY = """
query ($search: String, $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    characters(search: $search, sort: SEARCH_MATCH) {
      id
      name { full native }
      image { large }
      favourites
      gender
      media(perPage: 1, sort: POPULARITY_DESC) { nodes { title { romaji english } } }
    }
  }
}
"""
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_CHARACTERS_URL = "https://api.igdb.com/v4/characters"
IGDB_IMAGE_URL = "https://images.igdb.com/igdb/image/upload/t_1080p/{image_id}.jpg"
IGDB_SETUP_HINT = "`[p]set api igdb client_id,<id>,client_secret,<secret>` (a free Twitch developer app)"


def search_terms(text: str) -> List[str]:
    """What to search for from a request like "Frieren, Sousou no Frieren" or
    "Kratos | God of War": the whole text, then just the name part, no repeats."""
    text = " ".join(str(text).split())
    terms = [text]
    name = re.split(r"\s*(?:[,|;]|\s-\s|\sfrom\s)\s*", text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if name and name.lower() != text.lower():
        terms.append(name)
    return [t for t in terms if len(t) >= 2]


def parse_anilist_candidates(response_json: dict, tier_cutoffs: dict) -> List[Dict[str, Any]]:
    out = []
    characters = ((((response_json or {}).get("data") or {}).get("Page") or {}).get("characters")) or []
    for character in characters:
        entry = import_characters.to_pool_entry(character, tier_cutoffs, engine.bucket_tier)
        if entry is None:
            continue
        entry.update(source="anilist", source_id=character.get("id"))
        out.append(entry)
    return out


def parse_igdb_candidates(rows: Any) -> List[Dict[str, Any]]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        games = [g.get("name") for g in (row.get("games") or []) if isinstance(g, dict) and g.get("name")]
        image_id = (row.get("mug_shot") or {}).get("image_id") if isinstance(row.get("mug_shot"), dict) else None
        out.append(
            {
                "source": "igdb",
                "source_id": row.get("id"),
                "anilist_id": None,
                "name": name,
                "series": games[0] if games else "",
                "series_alt": "",
                "favourites": 0,
                "rarity": "common",
                "image_url": IGDB_IMAGE_URL.format(image_id=image_id) if image_id else None,
            }
        )
    return out


def dedupe(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for c in candidates:
        key = (c["source"], c.get("source_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def rank(candidates: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """Closest first: an exact name match, then every word of the request found
    in the name or series, then the rest; ties keep the source's own order."""
    wanted = engine.fold_text(text)
    name_part = engine.fold_text(search_terms(text)[-1]) if search_terms(text) else wanted
    words = [w for w in wanted.split() if len(w) > 1]

    def score(c):
        name = engine.fold_text(c["name"])
        haystack = f"{name} {engine.fold_text(c.get('series', ''))}"
        hits = sum(1 for w in words if w in haystack)
        return (name == name_part or name == wanted, name in wanted, hits)

    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda p: (tuple(-int(x) for x in score(p[1])), p[0]))
    return [c for _, c in indexed]


async def search_anilist(session, text: str, tier_cutoffs: dict, sleep_fn=asyncio.sleep) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for term in search_terms(text):
        payload = {"query": ANILIST_SEARCH_QUERY, "variables": {"search": term, "perPage": CANDIDATES_PER_SOURCE}}
        data = await import_characters._post_with_rate_limit_retry(
            session, import_characters.ANILIST_URL, payload, sleep_fn=sleep_fn
        )
        found.extend(parse_anilist_candidates(data, tier_cutoffs))
    return dedupe(found)


class IGDBClient:
    """Holds a Twitch app token (valid for weeks) between searches."""

    def __init__(self):
        self._token: Optional[str] = None
        self._expires = 0.0

    async def _bearer(self, session, client_id: str, client_secret: str) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        params = {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
        async with session.post(TWITCH_TOKEN_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        self._token = data["access_token"]
        self._expires = time.time() + float(data.get("expires_in", 3600))
        return self._token

    async def search(self, session, client_id: str, client_secret: str, text: str) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for term in search_terms(text):
            token = await self._bearer(session, client_id, client_secret)
            safe = term.replace("\\", " ").replace('"', " ")
            body = f'search "{safe}"; fields name,mug_shot.image_id,games.name; limit {CANDIDATES_PER_SOURCE};'
            headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}", "Accept": "application/json"}
            async with session.post(IGDB_CHARACTERS_URL, data=body, headers=headers) as resp:
                if getattr(resp, "status", 200) == 401:
                    self._token = None  # expired or revoked: next search fetches a new one
                resp.raise_for_status()
                found.extend(parse_igdb_candidates(await resp.json()))
        return dedupe(found)


async def find_candidates(
    session, text: str, tier_cutoffs: dict, igdb: IGDBClient, igdb_keys: Optional[Tuple[str, str]], sleep_fn=asyncio.sleep
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Search both sources. Returns (candidates closest first, notes about
    anything that couldn't be searched). One source failing never loses the other's results."""
    notes: List[str] = []
    results: List[Dict[str, Any]] = []
    try:
        results.extend(await search_anilist(session, text, tier_cutoffs, sleep_fn))
    except Exception:
        notes.append("AniList couldn't be reached.")
    if igdb_keys is None:
        notes.append(f"Game search is off. To turn it on run {IGDB_SETUP_HINT}.")
    else:
        try:
            results.extend(await igdb.search(session, igdb_keys[0], igdb_keys[1], text))
        except Exception:
            notes.append("IGDB couldn't be searched (check the Twitch app keys).")
    return rank(results, text), notes
