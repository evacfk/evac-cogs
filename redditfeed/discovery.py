"""Pure logic for subreddit discovery: parsing Arctic Shift subreddit-search
results, screening and filtering candidates, and picking preview posts. No
discord/redbot imports -- fully pytest-able standalone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import constants, engine
from .models import MediaItem


@dataclass
class SubredditCandidate:
    name: str                       # normalized lowercase, no "r/"
    display_name: str               # original capitalization
    subscribers: int = 0
    description: str = ""
    created_utc: Optional[float] = None


@dataclass
class FilterStats:
    found: int = 0
    already_mapped: int = 0
    denied: int = 0
    safety_filtered: int = 0
    not_usable: int = 0             # not NSFW / quarantined / private / malformed


@dataclass
class PreviewResult:
    images: list[MediaItem] = field(default_factory=list)       # one tile per post
    post_links: list[str] = field(default_factory=list)         # permalinks of the previewed posts
    video_or_link_posts: int = 0    # posts that are video/RedGIFs only (can't be tiled)
    posts_seen: int = 0
    flagged_titles: int = 0         # titles that tripped the safety screen (excluded)


# -- Parsing -----------------------------------------------------------------

def candidate_from_result(item: dict) -> Optional[SubredditCandidate]:
    """None when the result can't be used: no name, not NSFW, quarantined, private."""
    display = item.get("display_name") or ""
    name = engine.normalize_subreddit(display)
    if not name:
        return None
    if item.get("over18") is not True:
        return None
    if item.get("quarantine") is True:
        return None
    if item.get("subreddit_type") == "private":
        return None
    description = (item.get("public_description") or item.get("description") or "").strip()
    try:
        subscribers = int(item.get("subscribers") or 0)
    except (TypeError, ValueError):
        subscribers = 0
    return SubredditCandidate(
        name=name,
        display_name=display,
        subscribers=subscribers,
        description=description,
        created_utc=item.get("created_utc"),
    )


# -- Safety screen -----------------------------------------------------------

def _compact(text: str) -> str:
    return re.sub(r"[\s_\-.]+", "", text.lower())


def name_is_blocked(name: str) -> bool:
    """Subreddit names run words together ("teenfeet"), so match substrings on
    the name with separators removed."""
    squashed = _compact(name)
    return any(_compact(term) in squashed for term in constants.SAFETY_BLOCKED_TERMS)


def text_is_blocked(text: str) -> bool:
    """Descriptions and post titles are real prose, so match whole words
    (plus plural/'s' forms) to avoid hits like "kidney" or "nineteen"."""
    lowered = (text or "").lower()
    for term in constants.SAFETY_BLOCKED_TERMS:
        pattern = r"\b" + re.escape(term) + r"s?\b"
        if re.search(pattern, lowered):
            return True
    return False


def candidate_is_blocked(candidate: SubredditCandidate) -> bool:
    return name_is_blocked(candidate.name) or text_is_blocked(candidate.description)


# -- Candidate selection -------------------------------------------------------

def filter_candidates(
    raw_results: Iterable[dict],
    mapped: Iterable[str],
    denied: Iterable[str],
    limit: int = constants.DISCOVER_MAX_SUGGESTIONS,
) -> tuple[list[SubredditCandidate], FilterStats]:
    """Merge raw search results (possibly from several prefix queries) into a
    de-duplicated list of suggestions, biggest subreddit first. Anything already
    mapped, previously denied, unusable, or caught by the safety screen is dropped
    and counted so the caller can say what was hidden and why.
    """
    mapped_set = {engine.normalize_subreddit(m) for m in mapped}
    denied_set = {engine.normalize_subreddit(d) for d in denied}
    stats = FilterStats()
    seen: set[str] = set()
    kept: list[SubredditCandidate] = []

    for item in raw_results:
        candidate = candidate_from_result(item)
        if candidate is None:
            stats.not_usable += 1
            continue
        if candidate.name in seen:
            continue
        seen.add(candidate.name)
        stats.found += 1
        if candidate.name in mapped_set:
            stats.already_mapped += 1
        elif candidate.name in denied_set:
            stats.denied += 1
        elif candidate_is_blocked(candidate):
            stats.safety_filtered += 1
        else:
            kept.append(candidate)

    kept.sort(key=lambda c: c.subscribers, reverse=True)
    return kept[:limit], stats


# -- Preview selection -----------------------------------------------------------

def pick_preview(
    posts: Iterable[dict],
    max_images: int = constants.PREVIEW_MAX_IMAGES,
    max_links: int = constants.PREVIEW_MAX_POST_LINKS,
) -> PreviewResult:
    """Choose what a moderator sees before approving: the highest-scoring recent
    image posts (one tile per post, so a gallery doesn't fill every slot), plus
    permalinks to click through. Posts whose titles trip the safety screen are
    excluded from the preview and counted, so the suggestion can carry a warning.
    """
    result = PreviewResult()
    ranked = sorted(
        (p for p in posts if isinstance(p, dict)),
        key=lambda p: p.get("score") or 0,
        reverse=True,
    )
    for post in ranked:
        result.posts_seen += 1
        if text_is_blocked(post.get("title") or ""):
            result.flagged_titles += 1
            continue
        items = engine.extract_media_items(post)
        if not items:
            continue
        image_items = [i for i in items if not i.is_link_only]
        if not image_items:
            result.video_or_link_posts += 1
            continue
        if len(result.images) >= max_images:
            continue
        result.images.append(image_items[0])
        permalink = post.get("permalink")
        if permalink and len(result.post_links) < max_links:
            result.post_links.append(f"https://www.reddit.com{permalink}")
    return result


# -- Command input / output text ------------------------------------------------

_PREFIX_RE = re.compile(r"^[a-z0-9_]{2,21}$")


def parse_prefixes(text: str, max_prefixes: int = constants.DISCOVER_MAX_PREFIXES) -> list[str]:
    """`feet,foot,sole` -> ["feet", "foot", "sole"]. Normalized, de-duplicated,
    capped; anything that isn't a plausible subreddit-name prefix is dropped
    (also keeps odd characters out of the request URL)."""
    prefixes: list[str] = []
    for part in (text or "").split(","):
        name = engine.normalize_subreddit(part)
        if _PREFIX_RE.match(name) and name not in prefixes:
            prefixes.append(name)
    return prefixes[:max_prefixes]


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def format_summary(
    stats: FilterStats, shown: int, prefixes: list[str], min_subscribers: int, failures: list[str]
) -> str:
    """One message summarising what the search found and what was hidden."""
    label = ", ".join(f"`{p}*`" for p in prefixes)
    lines = [
        f"Found **{stats.found}** NSFW subreddit(s) matching {label} "
        f"with {min_subscribers:,}+ subscribers; showing **{shown}**."
    ]
    hidden = []
    if stats.already_mapped:
        hidden.append(f"{stats.already_mapped} already mapped")
    if stats.denied:
        hidden.append(f"{stats.denied} denied earlier (`.redditfeed denied`)")
    if stats.safety_filtered:
        hidden.append(f"{stats.safety_filtered} hidden by the name/description safety screen")
    if hidden:
        lines.append("Hidden: " + " \u00b7 ".join(hidden) + ".")
    if shown == 0:
        lines.append("Nothing new to suggest. Try another prefix or a lower minimum.")
    for failure in failures:
        lines.append(f"\u26a0\ufe0f Search failed for {failure}")
    return "\n".join(lines)
