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

from .constants import DEFAULT_DROP_WEIGHTS, TIERS

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


def tier_quotas(target_count: int, weights: Optional[dict] = None) -> Dict[str, int]:
    """Split `target_count` across rarity tiers proportionally to `weights`
    (defaults to the same DEFAULT_DROP_WEIGHTS drops are rolled against).

    This exists because AniList's characters list is sorted purely by
    favourites, descending. Naively taking the first `target_count` results
    (the old behavior) only ever grabs the *most*-favourited characters --
    which, against the default tier cutoffs, are almost all epic or
    legendary. The result was a pool with no common/rare cards at all, so
    every drop rolled epic/legendary regardless of drop_weights, since
    build_drop's tier-fallback had nothing else to fall back to. Quotas fix
    this at the source: the import keeps paging until each tier has its
    proportional share filled, not just until a raw count is hit."""
    weights = weights or DEFAULT_DROP_WEIGHTS
    tiers = [t for t in TIERS if weights.get(t, 0) > 0]
    if not tiers:
        tiers = list(TIERS)
        weights = DEFAULT_DROP_WEIGHTS
    total_weight = sum(weights.get(t, 0) for t in tiers) or 1
    quotas: Dict[str, int] = {}
    assigned = 0
    for i, tier in enumerate(tiers):
        if i == len(tiers) - 1:
            quotas[tier] = max(target_count - assigned, 0)
        else:
            n = round(target_count * weights.get(tier, 0) / total_weight)
            quotas[tier] = n
            assigned += n
    return quotas


def build_pool_entries(
    characters: List[dict],
    tier_cutoffs: dict,
    bucket_tier_fn,
    quotas: Dict[str, int],
    exclude_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Filter to female characters, convert to pool-entry shape, and keep
    only entries whose tier still has quota remaining -- decrementing
    `quotas` in place as entries are accepted, so the caller can page
    across multiple calls and know when every tier is filled. A character
    whose tier's quota is already spent is skipped, not appended, so a page
    stacked with (say) legendary characters doesn't blow past that tier's
    share just because they showed up first.

    `exclude_ids`, if given, is a set of AniList character ids to skip
    outright (already in the pool from an earlier import) -- and every
    *newly accepted* character's id is added to it in place, so a second
    call sharing the same set (as fetch_top_female_characters does, once
    per page) also can't re-add the same character twice within one import
    run, not just across separate runs."""
    exclude_ids = set() if exclude_ids is None else exclude_ids
    female = filter_female(characters)
    entries = []
    for c in female:
        entry = to_pool_entry(c, tier_cutoffs, bucket_tier_fn)
        if entry is None:
            continue
        anilist_id = entry.get("anilist_id")
        if anilist_id is not None and anilist_id in exclude_ids:
            continue
        tier = entry["rarity"]
        if quotas.get(tier, 0) <= 0:
            continue
        entries.append(entry)
        quotas[tier] = quotas.get(tier, 0) - 1
        if anilist_id is not None:
            exclude_ids.add(anilist_id)
    return entries


async def fetch_top_female_characters(
    session,
    tier_cutoffs: dict,
    bucket_tier_fn,
    target_count: int,
    weights: Optional[dict] = None,
    per_page: int = 50,
    max_pages: int = 200,
    exclude_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Page through AniList's characters-by-favourites list, filtering to
    female characters as pages come in, and stop once every tier's quota
    (see `tier_quotas`) is filled or `max_pages` is hit (whichever first --
    max_pages is a safety cap so a quota nothing can satisfy, e.g. more
    common characters than AniList actually has favourites data for, can't
    page forever).

    Sorted-by-favourites-descending means the early pages fill the
    legendary/epic quotas almost immediately and then get skipped for the
    rest of the run; rare and especially common quotas may need many pages
    before enough low-favourite characters show up. `max_pages` defaults
    much higher than a naive "just get target_count" import would need, to
    give the common quota a real chance to fill instead of silently coming
    back empty.

    `exclude_ids` -- AniList character ids already present in the pool from
    an earlier import -- are skipped so a repeat `.card importpool` doesn't
    add the same character twice under a new local card_id; pass the set of
    ids already on disk. See build_pool_entries for how this also prevents
    intra-run duplicates.

    `session` is an aiohttp.ClientSession (or anything with a compatible
    `.post(url, json=...)` async context manager) -- injected rather than
    created here so this function is testable with a fake session and so
    the cog can reuse its own long-lived session instead of opening a new
    one per import.
    """
    quotas = tier_quotas(target_count, weights)
    seen_ids = set(exclude_ids) if exclude_ids else set()
    collected: List[Dict[str, Any]] = []
    page = 1
    while any(q > 0 for q in quotas.values()) and page <= max_pages:
        payload = {"query": CHARACTERS_QUERY, "variables": {"page": page, "perPage": per_page}}
        async with session.post(ANILIST_URL, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

        characters = parse_characters(data)
        collected.extend(build_pool_entries(characters, tier_cutoffs, bucket_tier_fn, quotas, seen_ids))

        if not has_next_page(data):
            break
        page += 1

    return collected
