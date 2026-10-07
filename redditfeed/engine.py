"""Pure business logic for redditfeed: normalization, keyword filtering, media
extraction, dedup. Zero discord.py/redbot imports -- fully pytest-able standalone.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from . import constants
from .models import MediaItem


# -- Normalization ----------------------------------------------------------

def normalize_subreddit(name: str) -> str:
    """Lowercase, strip whitespace and a leading 'r/' or '/r/'."""
    name = name.strip().lower()
    if name.startswith("/r/"):
        name = name[3:]
    elif name.startswith("r/"):
        name = name[2:]
    return name.strip("/ ")


# -- Dedup --------------------------------------------------------------------

def build_dedup_key(channel_id: int, post_id: str) -> str:
    """Keyed by (channel, post) rather than post alone, so a crosspost landing
    under two subreddits mapped to the same channel only posts once there, but
    still posts to a *different* channel it's also mapped to.
    """
    return f"{channel_id}:{post_id}"


def prune_dedup_store(store: dict[str, float], now_ts: float, ttl_days: float) -> dict[str, float]:
    """Drop entries older than ttl_days. Keeps the store from growing unbounded."""
    ttl_seconds = ttl_days * 86400
    return {key: ts for key, ts in store.items() if (now_ts - ts) < ttl_seconds}


def filter_unseen(
    posts: list[dict],
    channel_id: int,
    seen_store: dict[str, float],
) -> list[dict]:
    """Given posts (each must have an 'id' key) and the dedup store, return only
    the posts not already recorded for this channel. Does not mutate seen_store.
    """
    unseen = []
    for post in posts:
        key = build_dedup_key(channel_id, post["id"])
        if key not in seen_store:
            unseen.append(post)
    return unseen


# -- Keyword filtering ----------------------------------------------------------

def _post_text(post: dict) -> str:
    title = post.get("title") or ""
    selftext = post.get("selftext") or ""
    return f"{title}\n{selftext}".lower()

def passes_keyword_filter(post: dict, require_keywords: list[str], block_keywords: list[str]) -> bool:
    """Block wins: if any block keyword matches, the post is rejected regardless
    of require matches. If require_keywords is non-empty, at least one must match
    (OR). Empty require_keywords means no requirement. Case-insensitive substring match.
    """
    text = _post_text(post)

    for kw in block_keywords:
        if kw and kw.lower() in text:
            return False

    if require_keywords:
        return any(kw and kw.lower() in text for kw in require_keywords)

    return True


# -- Media extraction ----------------------------------------------------------

def _is_direct_image_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.endswith(constants.IMAGE_URL_EXTENSIONS):
        return True
    return parsed.netloc.lower() in constants.IMAGE_HOST_DOMAINS and path.endswith(constants.IMAGE_URL_EXTENSIONS)


def _is_redgifs_url(url: str) -> bool:
    if not url:
        return False
    return urlparse(url).netloc.lower() in constants.REDGIFS_DOMAINS


def extract_media_items(post: dict) -> list[MediaItem]:
    """Given a Reddit post JSON object (as returned by Arctic Shift / the Reddit
    API), return the list of postable MediaItems. Text-only posts with no
    recognizable media yield an empty list (callers should skip those).

    Priority: gallery > video > redgifs link > direct image url. A post is only
    ever one of these -- Reddit posts don't mix gallery with a single url, etc.
    """
    items: list[MediaItem] = []

    if post.get("is_gallery") and post.get("gallery_data") and post.get("media_metadata"):
        gallery_items = post["gallery_data"].get("items", [])
        media_metadata = post["media_metadata"]
        for entry in gallery_items:
            media_id = entry.get("media_id")
            meta = media_metadata.get(media_id)
            if not meta:
                continue
            # 's' holds the full-resolution source; 'u' is the (HTML-escaped) URL.
            source = meta.get("s", {})
            url = source.get("u") or source.get("gif") or source.get("mp4")
            if url:
                items.append(MediaItem(kind=constants.MEDIA_KIND_GALLERY_IMAGE, url=url.replace("&amp;", "&")))
        return items

    if post.get("is_video"):
        permalink = post.get("permalink", "")
        url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")
        if url:
            items.append(MediaItem(kind=constants.MEDIA_KIND_VIDEO_LINK, url=url, is_link_only=True))
        return items

    url = post.get("url") or post.get("url_overridden_by_dest") or ""

    if _is_redgifs_url(url):
        items.append(MediaItem(kind=constants.MEDIA_KIND_REDGIFS_LINK, url=url, is_link_only=True))
        return items

    if _is_direct_image_url(url):
        items.append(MediaItem(kind=constants.MEDIA_KIND_IMAGE, url=url))
        return items

    return items


def partition_media_items(items: list[MediaItem]) -> tuple[list[MediaItem], list[MediaItem]]:
    """Split into (batchable image items, link-only items). Direct images and
    gallery images can be batched into one multi-embed gallery message;
    video/RedGIFs links can't be rendered as an embed image and always post
    individually as a bare URL.
    """
    image_items = [item for item in items if not item.is_link_only]
    link_items = [item for item in items if item.is_link_only]
    return image_items, link_items


def chunk_items(items: list, size: int) -> list[list]:
    """Split into chunks of at most `size` -- used to stay under Discord's
    10-embeds-per-message cap for large galleries.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]


def is_nsfw_over_18(post: dict) -> bool:
    return bool(post.get("over_18"))


# -- Settings validation ----------------------------------------------------------

def compute_after_ts(
    last_poll_ts: Optional[float], now_ts: float, max_lookback: float = constants.MAX_LOOKBACK_SECONDS
) -> Optional[float]:
    """Cursor to fetch from: the last successful poll, but never older than
    `max_lookback` seconds. None stays None (the source then sends no `after`)."""
    if last_poll_ts is None:
        return None
    return max(last_poll_ts, now_ts - max_lookback)


def clamp_poll_interval(seconds: int) -> int:
    return max(int(seconds), constants.MIN_POLL_INTERVAL_SECONDS)


def clamp_stagger(seconds: float) -> float:
    return min(max(float(seconds), constants.MIN_STAGGER_SECONDS), constants.MAX_STAGGER_SECONDS)


def sort_posts_oldest_first(posts: list[dict]) -> list[dict]:
    return sorted(posts, key=lambda p: p.get("created_utc", 0))
