"""AniList bulk-import pipeline for cardcollect's character pool.

Split the same way this project splits everything else: the parts that can
be pure functions (building the query, parsing AniList's response shape,
the female-only filter, rarity bucketing) have zero network/discord
dependency and are fully unit-testable with synthetic JSON that matches
AniList's real schema. The one network-touching function
(`fetch_top_female_characters`) is a thin async wrapper around those pure
functions plus an injected aiohttp session, so the cog can call it without
duplicating the query/parse/filter logic, and tests can call everything
else without a session at all.

AniList itself is unreachable from this build sandbox (outbound access is
allowlisted to package registries only), so the network path is built
correctly against AniList's documented public schema and the actual query
that was validated by hand against their docs during design, but has not
been exercised against the live API from here -- flagged clearly rather
than silently assumed to work.
"""

from typing import Any, Dict, List, Optional

CHARACTERS_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    characters(sort: FAVOURITES_DESC) {
      id
      name { full native }
      image { large }
      favourites
      gender
      media(perPage: 1) { nodes { title { romaji } } }
    }
  }
}
"""

ANILIST_URL = "https://graphql.anilist.co"


def parse_characters(response_json: dict) -> List[dict]:
    """Pull the raw character list out of one page of AniList's response.
    Raises KeyError/TypeError on an unexpected shape rather than silently
    returning an empty list -- a schema change should be loud, not produce
    a pool import that silently does nothing."""
    return response_json["data"]["Page"]["characters"]


def has_next_page(response_json: dict) -> bool:
    return bool(response_json["data"]["Page"]["pageInfo"]["hasNextPage"])


def filter_female(characters: List[dict]) -> List[dict]:
    return [c for c in characters if (c.get("gender") or "").strip().lower() == "female"]


def to_pool_entry(character: dict, tier_cutoffs: dict, bucket_tier_fn) -> Optional[Dict[str, Any]]:
    """Convert one AniList character dict into the shape cardcollect's pool
    stores (minus image_path, added_by -- the caller fills those in once it
    has actually downloaded and saved the art locally). Returns None for a
    character missing the fields we need (no image, or an empty name) so
    the caller can skip it rather than importing a broken entry.

    `bucket_tier_fn` is passed in (engine.bucket_tier) instead of imported
    directly, so this module stays free of any cardcollect-package
    dependency beyond what's handed to it -- keeps it trivially testable
    standalone."""
    name = (character.get("name") or {}).get("full")
    image_url = (character.get("image") or {}).get("large")
    if not name or not image_url:
        return None

    favourites = character.get("favourites", 0) or 0
    media_nodes = ((character.get("media") or {}).get("nodes")) or []
    series = (media_nodes[0].get("title", {}).get("romaji") if media_nodes else "") or ""

    return {
        "anilist_id": character.get("id"),
        "name": name,
        "series": series,
        "favourites": favourites,
        "rarity": bucket_tier_fn(favourites, tier_cutoffs),
        "image_url": image_url,
    }


def build_pool_entries(
    characters: List[dict],
    tier_cutoffs: dict,
    bucket_tier_fn,
    target_count: int,
) -> List[Dict[str, Any]]:
    """Filter to female characters, convert to pool-entry shape, and cap at
    `target_count`. Characters are already favourites-sorted by the AniList
    query itself, so taking the first `target_count` after filtering keeps
    the highest-favourites female characters."""
    female = filter_female(characters)
    entries = []
    for c in female:
        entry = to_pool_entry(c, tier_cutoffs, bucket_tier_fn)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= target_count:
            break
    return entries


async def fetch_top_female_characters(
    session,
    tier_cutoffs: dict,
    bucket_tier_fn,
    target_count: int,
    per_page: int = 50,
    max_pages: int = 10,
) -> List[Dict[str, Any]]:
    """Page through AniList's characters-by-favourites list, filtering to
    female characters as pages come in, and stop once `target_count` is
    reached or `max_pages` is hit (whichever first -- max_pages is a safety
    cap so a target_count nothing can satisfy, e.g. more than actually
    exist, can't page forever).

    `session` is an aiohttp.ClientSession (or anything with a compatible
    `.post(url, json=...)` async context manager) -- injected rather than
    created here so this function is testable with a fake session and so
    the cog can reuse its own long-lived session instead of opening a new
    one per import.
    """
    collected: List[Dict[str, Any]] = []
    page = 1
    while len(collected) < target_count and page <= max_pages:
        payload = {"query": CHARACTERS_QUERY, "variables": {"page": page, "perPage": per_page}}
        async with session.post(ANILIST_URL, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

        characters = parse_characters(data)
        remaining = target_count - len(collected)
        collected.extend(build_pool_entries(characters, tier_cutoffs, bucket_tier_fn, remaining))

        if not has_next_page(data):
            break
        page += 1

    return collected[:target_count]
