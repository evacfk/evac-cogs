"""AniList bulk-import pipeline for cardcollect's character pool.

Split the same way this project splits everything else: the parts that can
be pure functions (building the query, parsing AniList's response shape,
the female-only filter, rarity bucketing) have zero network/discord
dependency and are fully unit-testable with synthetic JSON that matches
AniList's real schema. The network-touching functions
(the series search and character fetch) are thin async wrappers around those
pure functions plus an injected aiohttp session, so the cog can call them
without duplicating the query/parse logic, and tests can call everything
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

from .constants import ART_MIN_SIDE, SERIES_IMPORT_MAX, SERIES_SEARCH_RESULTS, TIERS
from .engine import fold_text

# AniList's public API rate-limits fairly aggressively (it's been running in
# a "degraded" reduced-limit mode for extended periods) and returns plain 429s
# with no useful body when tripped. A series import fires several requests in a
# row, and an admin can easily start a second import before the first finishes.
# These constants back off automatically instead of just failing the whole import.
REQUEST_DELAY_SECONDS = 0.75  # spacing between successful page requests
RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_DEFAULT_BACKOFF_SECONDS = 5.0  # used when AniList sends no Retry-After

ANILIST_URL = "https://graphql.anilist.co"


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
    unexpected shape, same loud-not-silent policy as the other parsers here."""
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


_NUMBER_RANGE = re.compile(r"^\s*#?(\d+)\s*-\s*#?(\d+)\s*$")
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
                span = _NUMBER_RANGE.match(part)
                numbers = range(int(span.group(1)), int(span.group(2)) + 1) if span else [part]
                if span and int(span.group(2)) - int(span.group(1)) > len(entries):
                    problems.append(f"Range {part.strip()} is longer than the list.")
                    continue
                for item in numbers:
                    entry, problem = resolve_entry(str(item), entries)
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
