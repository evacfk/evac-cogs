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

import asyncio
import io
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image

from .constants import ART_MIN_SIDE, DEFAULT_DROP_WEIGHTS, SERIES_IMPORT_MAX, SERIES_SEARCH_RESULTS, TIERS
from .engine import fold_text

# AniList's public API rate-limits fairly aggressively (it's been running in
# a "degraded" reduced-limit mode for extended periods) and returns plain 429s
# with no useful body when tripped. A single `.card importpool` run can
# legitimately fire dozens of requests (max_pages=200) to fill the common/rare
# quotas, and it's trivial for an admin to fire the command more than once in
# quick succession -- both were observed to trip 429s in practice. These
# constants back off automatically instead of just failing the whole import.
REQUEST_DELAY_SECONDS = 0.75  # spacing between successful page requests
RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_DEFAULT_BACKOFF_SECONDS = 5.0  # used when AniList sends no Retry-After

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
      media(perPage: 1) { nodes { title { romaji english } } }
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


def filter_gender(characters: List[dict], gender: str = "any") -> List[dict]:
    """Keep characters of one gender ("female" / "male"), or everyone for
    "any" (which also keeps characters AniList has no gender for)."""
    gender = (gender or "any").strip().lower()
    if gender == "any":
        return list(characters)
    return [c for c in characters if (c.get("gender") or "").strip().lower() == gender]


def filter_female(characters: List[dict]) -> List[dict]:
    return filter_gender(characters, "female")


def _alt_title(romaji: str, english: Optional[str]) -> str:
    """The English series title, or "" when AniList has none or it is the same
    text as the romaji one (so search doesn't index the same words twice)."""
    english = (english or "").strip()
    if not english or fold_text(english) == fold_text(romaji):
        return ""
    return english


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
    title = (media_nodes[0].get("title") or {}) if media_nodes else {}
    series = title.get("romaji") or ""
    series_alt = _alt_title(series, title.get("english"))

    return {
        "anilist_id": character.get("id"),
        "name": name,
        "series": series,
        "series_alt": series_alt,
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
    gender: str = "female",
) -> List[Dict[str, Any]]:
    """Filter to `gender` (default female; "any" keeps everyone), convert to pool-entry shape, and keep
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
    wanted = filter_gender(characters, gender)
    entries = []
    for c in wanted:
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


async def _post_with_rate_limit_retry(session, url, payload, sleep_fn=asyncio.sleep, max_retries=RATE_LIMIT_MAX_RETRIES):
    """POST to `url` and return the parsed JSON body, retrying with backoff
    on a 429 (rate limited) response instead of failing the whole import
    outright. Honors AniList's `Retry-After` response header (seconds) when
    present, falling back to `RATE_LIMIT_DEFAULT_BACKOFF_SECONDS` otherwise.
    Still raises (via `raise_for_status`) for a 429 that persists past
    `max_retries`, or for any other HTTP error status.

    `sleep_fn` is injected (defaults to `asyncio.sleep`) so tests can swap in
    a fast no-op instead of actually waiting out a real backoff.

    A fake session/response in tests that has no `.status` (only the
    lightweight `raise_for_status()`/`.json()` surface older tests use) is
    treated as never rate-limited -- `getattr(..., "status", 200)` defaults
    to a non-429 value, so existing fakes keep working unchanged."""
    attempt = 0
    while True:
        async with session.post(url, json=payload) as resp:
            status = getattr(resp, "status", 200)
            if status == 429:
                attempt += 1
                if attempt > max_retries:
                    resp.raise_for_status()
                headers = getattr(resp, "headers", None) or {}
                retry_after = headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else RATE_LIMIT_DEFAULT_BACKOFF_SECONDS
                except (TypeError, ValueError):
                    delay = RATE_LIMIT_DEFAULT_BACKOFF_SECONDS
                await sleep_fn(delay)
                continue
            resp.raise_for_status()
            return await resp.json()


async def fetch_top_characters(
    session,
    tier_cutoffs: dict,
    bucket_tier_fn,
    target_count: int,
    weights: Optional[dict] = None,
    per_page: int = 50,
    max_pages: int = 200,
    exclude_ids: Optional[set] = None,
    request_delay: float = REQUEST_DELAY_SECONDS,
    sleep_fn=asyncio.sleep,
    gender: str = "any",
) -> List[Dict[str, Any]]:
    """Page through AniList's characters-by-favourites list, filtering to
    `gender` ("any", "female" or "male") as pages come in, and stop once every tier's quota
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

    Each request is spaced `request_delay` seconds apart, and a 429
    (rate-limited) response is retried with backoff rather than aborting the
    whole import -- see `_post_with_rate_limit_retry`. AniList's public API
    rate-limits aggressively enough that a single deep import (paging to
    fill common/rare quotas) or two `.card importpool` runs started close
    together can trip it; both were observed doing exactly that in practice.
    """
    quotas = tier_quotas(target_count, weights)
    seen_ids = set(exclude_ids) if exclude_ids else set()
    collected: List[Dict[str, Any]] = []
    page = 1
    while any(q > 0 for q in quotas.values()) and page <= max_pages:
        payload = {"query": CHARACTERS_QUERY, "variables": {"page": page, "perPage": per_page}}
        data = await _post_with_rate_limit_retry(session, ANILIST_URL, payload, sleep_fn=sleep_fn)

        characters = parse_characters(data)
        collected.extend(build_pool_entries(characters, tier_cutoffs, bucket_tier_fn, quotas, seen_ids, gender))

        if not has_next_page(data):
            break
        page += 1
        if request_delay:
            await sleep_fn(request_delay)

    return collected


async def fetch_top_female_characters(*args, **kwargs) -> List[Dict[str, Any]]:
    """`fetch_top_characters` restricted to female characters (the original
    behaviour, kept so older callers and tests keep working)."""
    kwargs["gender"] = "female"
    return await fetch_top_characters(*args, **kwargs)


# ---------------------------------------------------------------------------
# series import: look a series up on AniList, then pull its characters
# ---------------------------------------------------------------------------

MEDIA_SEARCH_QUERY = """
query ($search: String, $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(search: $search, sort: SEARCH_MATCH, isAdult: false) {
      id
      type
      format
      seasonYear
      startDate { year }
      title { romaji english }
    }
  }
}
"""

MEDIA_CHARACTERS_QUERY = """
query ($id: Int, $page: Int, $perPage: Int) {
  Media(id: $id) {
    id
    title { romaji english }
    characters(sort: FAVOURITES_DESC, page: $page, perPage: $perPage) {
      pageInfo { total hasNextPage }
      nodes {
        id
        name { full native }
        image { large }
        favourites
        gender
      }
    }
  }
}
"""

SERIES_PAGE_SIZE = 50  # AniList's maximum per page


def parse_media_search(response_json: dict) -> List[dict]:
    """The pick-list for `.card importseries`: one dict per AniList match
    (id, title, title_english, format, year). Raises KeyError/TypeError on an
    unexpected shape, same loud-not-silent policy as parse_characters."""
    results = []
    for media in response_json["data"]["Page"]["media"] or []:
        title = media.get("title") or {}
        romaji = title.get("romaji") or title.get("english")
        if media.get("id") is None or not romaji:
            continue
        year = media.get("seasonYear") or (media.get("startDate") or {}).get("year")
        results.append(
            {
                "id": media["id"],
                "title": romaji,
                "title_english": _alt_title(romaji, title.get("english")),
                "format": media.get("format") or media.get("type") or "",
                "year": year,
            }
        )
    return results


async def search_media(
    session, query: str, limit: int = SERIES_SEARCH_RESULTS, sleep_fn=asyncio.sleep
) -> List[dict]:
    payload = {"query": MEDIA_SEARCH_QUERY, "variables": {"search": query.strip(), "perPage": limit}}
    data = await _post_with_rate_limit_retry(session, ANILIST_URL, payload, sleep_fn=sleep_fn)
    return parse_media_search(data)


def parse_media_characters(response_json: dict) -> dict:
    """One page of a series' characters: its titles, AniList's total
    character count, whether another page exists, and the raw nodes."""
    media = response_json["data"]["Media"]
    title = media.get("title") or {}
    romaji = title.get("romaji") or title.get("english") or ""
    connection = media["characters"]
    page_info = connection.get("pageInfo") or {}
    return {
        "title": romaji,
        "title_english": _alt_title(romaji, title.get("english")),
        "total": page_info.get("total") or 0,
        "has_next": bool(page_info.get("hasNextPage")),
        "characters": connection.get("nodes") or [],
    }


async def fetch_series_characters(
    session,
    media_id: int,
    limit: int = SERIES_IMPORT_MAX,
    request_delay: float = REQUEST_DELAY_SECONDS,
    sleep_fn=asyncio.sleep,
) -> dict:
    """A series' characters, most-favourited first, up to `limit`. Returns
    the parse_media_characters dict with `characters` holding at most `limit`
    entries (a long-running series can list well over a thousand, and nobody
    wants all of them as cards)."""
    info: Optional[dict] = None
    collected: List[dict] = []
    page = 1
    while len(collected) < limit:
        payload = {
            "query": MEDIA_CHARACTERS_QUERY,
            "variables": {"id": media_id, "page": page, "perPage": SERIES_PAGE_SIZE},
        }
        data = await _post_with_rate_limit_retry(session, ANILIST_URL, payload, sleep_fn=sleep_fn)
        parsed = parse_media_characters(data)
        info = info or parsed
        collected.extend(parsed["characters"])
        if not parsed["has_next"]:
            break
        page += 1
        if request_delay:
            await sleep_fn(request_delay)
    if info is None:  # unreachable (the loop always runs once) but keeps the type honest
        raise KeyError("no data")
    info = dict(info)
    info["characters"] = collected[:limit]
    return info


def _match_hand_added(unlinked: List[dict], name: str, series_keys: set) -> Optional[dict]:
    """A hand-added card (no AniList id) that is the same character: same
    folded name AND the same series -- or no series recorded on it. Two
    different characters who share a first name stay separate."""
    folded = fold_text(name)
    for card in unlinked:
        if fold_text(card.get("name", "")) != folded:
            continue
        existing_series = fold_text(card.get("series", ""))
        existing_alt = fold_text(card.get("series_alt", ""))
        if not existing_series or existing_series in series_keys or (existing_alt and existing_alt in series_keys):
            return card
    return None


def split_hand_added(entries: List[dict], existing_cards: List[dict]) -> Tuple[List[dict], List[dict]]:
    """For a bulk import: pull out the freshly fetched entries that are really
    a hand-added card already in the pool. Returns (entries_to_import, links);
    each link says which existing card gets which AniList id instead of a
    second copy of the character being created."""
    unlinked = [c for c in existing_cards if c.get("anilist_id") is None]
    keep: List[dict] = []
    links: List[dict] = []
    for entry in entries:
        keys = {fold_text(entry.get("series", "")), fold_text(entry.get("series_alt", ""))} - {""}
        match = _match_hand_added(unlinked, entry["name"], keys)
        if match is None:
            keep.append(entry)
            continue
        unlinked.remove(match)
        links.append(
            {
                "name": entry["name"],
                "card_id": match["card_id"],
                "anilist_id": entry.get("anilist_id"),
                "series_alt": entry.get("series_alt", ""),
            }
        )
    return keep, links


def build_series_entries(
    characters: List[dict],
    series_title: str,
    series_title_english: str,
    tier_cutoffs: dict,
    bucket_tier_fn,
    existing_cards: List[dict],
) -> Dict[str, list]:
    """Sort a series' characters into what an import would do with each.
    `characters` is most-favourited first, and every result carries its `rank`
    (position in that list) so "top 15" is just rank < 15.

    `existing_cards` is every pool card INCLUDING retired ones, as dicts with
    card_id / anilist_id / name / series / series_alt -- retired cards must
    count, or a character you pruned would come straight back.

    Returns:
      entries  new characters to import (pool-entry shape, plus `rank`)
      links    hand-added cards (no AniList id) that are the same character:
               not imported again, but given the AniList id so the next
               import recognises them. Same folded name AND the same series
               (or no series recorded) -- two different "Lucy"s stay separate.
      skipped  characters left out, each with a `reason`
    """
    existing_ids = {c["anilist_id"] for c in existing_cards if c.get("anilist_id") is not None}
    unlinked = [c for c in existing_cards if c.get("anilist_id") is None]
    series_keys = {fold_text(series_title), fold_text(series_title_english)} - {""}
    alt = _alt_title(series_title, series_title_english)

    entries: List[dict] = []
    links: List[dict] = []
    skipped: List[dict] = []
    seen_ids = set()
    for rank, character in enumerate(characters):
        name = (character.get("name") or {}).get("full")
        image_url = (character.get("image") or {}).get("large")
        label = name or "(unnamed character)"
        if not name or not image_url:
            skipped.append({"rank": rank, "name": label, "reason": "no name or image on AniList"})
            continue
        anilist_id = character.get("id")
        if anilist_id is not None and (anilist_id in existing_ids or anilist_id in seen_ids):
            skipped.append({"rank": rank, "name": name, "reason": "already in the pool"})
            continue
        if anilist_id is not None:
            seen_ids.add(anilist_id)

        match = _match_hand_added(unlinked, name, series_keys)
        if match is not None:
            unlinked.remove(match)
            links.append(
                {
                    "rank": rank,
                    "name": name,
                    "card_id": match["card_id"],
                    "anilist_id": anilist_id,
                    "series_alt": alt,
                }
            )
            continue

        favourites = character.get("favourites", 0) or 0
        entries.append(
            {
                "anilist_id": anilist_id,
                "name": name,
                "series": series_title,
                "series_alt": alt,
                "favourites": favourites,
                "rarity": bucket_tier_fn(favourites, tier_cutoffs),
                "image_url": image_url,
                "rank": rank,
            }
        )
    return {"entries": entries, "links": links, "skipped": skipped}


def select_top(
    built: Dict[str, list],
    limit: int,
    overrides: Optional[Dict[int, str]] = None,
    skip: Optional[Iterable[int]] = None,
) -> Dict[str, list]:
    """The slice of build_series_entries' result an import of the top `limit`
    characters would act on, with any rarity overrides (rank -> tier) applied
    and any ranks in `skip` (characters the admin left out) removed."""
    overrides = overrides or {}
    skip = set(skip or ())
    entries = []
    for entry in built["entries"]:
        if entry["rank"] < limit and entry["rank"] not in skip:
            entry = dict(entry)
            entry["rarity"] = overrides.get(entry["rank"], entry["rarity"])
            entries.append(entry)
    return {
        "entries": entries,
        "links": [x for x in built["links"] if x["rank"] < limit],
        "skipped": [x for x in built["skipped"] if x["rank"] < limit],
    }


def parse_rarity_overrides(text: str, entries: List[dict]) -> Tuple[Dict[int, str], List[str]]:
    """Read the admin's rarity edits, one per line ("Name: epic", "Name = rare",
    or "Name epic"), against the series' new characters. Returns
    ({rank: tier}, [problems]); a line that can't be applied is reported, never
    guessed at. A name matches exactly first, else as a unique partial."""
    overrides: Dict[int, str] = {}
    problems: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        for sep in (":", "="):
            if sep in line:
                name_part, tier_part = line.rsplit(sep, 1)
                break
        else:
            parts = line.rsplit(None, 1)
            if len(parts) < 2:
                problems.append(f"Couldn't read: {line}")
                continue
            name_part, tier_part = parts
        tier = tier_part.strip().lower()
        if tier not in TIERS:
            problems.append(f"{tier_part.strip() or '(blank)'} isn't a rarity (common, rare, epic, legendary): {line}")
            continue
        wanted = fold_text(name_part)
        exact = [e for e in entries if fold_text(e["name"]) == wanted]
        matches = exact or [e for e in entries if wanted and wanted in fold_text(e["name"])]
        if not matches:
            problems.append(f"No new character matches \"{name_part.strip()}\".")
        elif len(matches) > 1 and not exact:
            names = ", ".join(m["name"] for m in matches[:4])
            problems.append(f"\"{name_part.strip()}\" matches several ({names}); be more specific.")
        else:
            overrides[matches[0]["rank"]] = tier
    return overrides, problems


def entry_numbers(built: Dict[str, list]) -> Dict[int, int]:
    """rank -> the 1-based number an admin sees next to each new character in
    the preview (its position among the NEW characters, most-favourited first)."""
    return {e["rank"]: i + 1 for i, e in enumerate(built["entries"])}


def resolve_entry(text: str, entries: List[dict]) -> Tuple[Optional[dict], Optional[str]]:
    """Find one new character from what the admin typed: its preview number, an
    exact name, or a unique part of a name. Returns (entry, None) or
    (None, problem)."""
    wanted = (text or "").strip().lstrip("#").strip()
    if not wanted:
        return None, "Name or number missing."
    if wanted.isdigit():
        n = int(wanted)
        if 1 <= n <= len(entries):
            return entries[n - 1], None
        return None, f"There is no number {n} (the new characters are 1 to {len(entries)})."
    folded = fold_text(wanted)
    exact = [e for e in entries if fold_text(e["name"]) == folded]
    matches = exact or [e for e in entries if folded in fold_text(e["name"])]
    if not matches:
        return None, f"No new character matches \"{wanted}\"."
    if len(matches) > 1 and not exact:
        names = ", ".join(m["name"] for m in matches[:4])
        return None, f"\"{wanted}\" matches several ({names}); use its number."
    return matches[0], None


_SKIP_LINE = re.compile(r"^\s*(un)?skip\b[\s:,-]*(.*)$", re.IGNORECASE)


def parse_adjustments(text: str, entries: List[dict]) -> Dict[str, object]:
    """Read everything the Adjust pop-up can say: rarity lines ("Name: epic"),
    `skip 3, 7` / `skip Name` to leave characters out, and `unskip 3` to put
    them back. Returns {"overrides": {rank: tier}, "skip": set, "unskip": set,
    "problems": [str]}. A line that can't be applied is reported, never guessed at."""
    rarity_lines: List[str] = []
    skip: set = set()
    unskip: set = set()
    problems: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        match = _SKIP_LINE.match(line)
        if match and match.group(2) and line.rsplit(None, 1)[-1].strip(":=").lower() not in TIERS:
            target = unskip if match.group(1) else skip
            for part in match.group(2).split(","):
                if not part.strip():
                    continue
                entry, problem = resolve_entry(part, entries)
                if entry is None:
                    problems.append(problem)
                else:
                    target.add(entry["rank"])
        else:
            rarity_lines.append(raw)
    overrides, rarity_problems = parse_rarity_overrides("\n".join(rarity_lines), entries)
    return {"overrides": overrides, "skip": skip, "unskip": unskip, "problems": problems + rarity_problems}


def art_flags(entry: dict, image_bytes: Optional[bytes]) -> Optional[str]:
    """Why a character's AniList art probably isn't worth using, or None if it
    looks fine: AniList's "no image" placeholder, a tiny or very wide picture,
    or a download that isn't an image at all."""
    name = str(entry.get("image_url") or "").rsplit("/", 1)[-1].lower()
    if name.startswith("default"):
        return "AniList placeholder"
    if image_bytes is None:
        return "download failed"
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.size
    except Exception:
        return "not an image"
    if min(width, height) < ART_MIN_SIDE:
        return f"small ({width}x{height})"
    if width > height * 1.1:
        return f"wide ({width}x{height})"
    return None
