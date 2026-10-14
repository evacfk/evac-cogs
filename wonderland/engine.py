"""Pure guide logic: which entries are live, search, channel matching, text layout.

No discord/redbot imports, so it is fully testable on its own.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .constants import SEARCH_MAX_RESULTS, SEARCH_MIN_CHARS
from .content import MAP, PAGES, Channel, Entry, Page

Exists = Callable[[str], bool]


def resolve_entry(entry: Entry, exists: Exists) -> Optional[Tuple[str, str]]:
    """(usage, desc) to show, or None when the entry's commands are not on the live bot."""
    if entry.requires and not all(exists(name) for name in entry.requires):
        return None
    usage = entry.usage
    if entry.expand:
        live = [name for name in entry.expand if exists(name)]
        if not live:
            return None
        usage = usage.replace("{cmds}", "  ".join(f".{name}" for name in live))
    return usage, entry.desc


def page_lines(page: Page, exists: Exists) -> List[str]:
    lines = []
    for entry in page.entries:
        resolved = resolve_entry(entry, exists)
        if resolved:
            usage, desc = resolved
            lines.append(f"`{usage}`: {desc}")
    return lines


def visible_pages(is_mod: bool) -> List[Page]:
    return [p for p in PAGES if is_mod or not p.mod_only]


def get_page(key: str, is_mod: bool) -> Optional[Page]:
    for page in visible_pages(is_mod):
        if page.key == key:
            return page
    return None


def search(query: str, exists: Exists, is_mod: bool) -> List[Tuple[Page, str]]:
    """Case-insensitive match on command text and description, curated pages only."""
    q = (query or "").strip().lower().lstrip(".")
    if len(q) < SEARCH_MIN_CHARS:
        return []
    hits: List[Tuple[Page, str]] = []
    for page in visible_pages(is_mod):
        for entry in page.entries:
            resolved = resolve_entry(entry, exists)
            if not resolved:
                continue
            usage, desc = resolved
            if q in usage.lower() or q in desc.lower():
                hits.append((page, f"`{usage}`: {desc}"))
                if len(hits) >= SEARCH_MAX_RESULTS:
                    return hits
    return hits


def match_channel(slug: str, names: Dict[int, str]) -> Optional[int]:
    """Find the live channel for a slug. Names carry decoration ("🧸・cuddle"), so match the tail,
    and require a non-word character before it so "shop" never matches "wondershop"."""
    target = slug.lower()
    for channel_id, name in names.items():
        lowered = name.lower().strip()
        if not lowered.endswith(target):
            continue
        head = lowered[: len(lowered) - len(target)]
        if head == "" or not (head[-1].isalnum() or head[-1] == "-"):
            return channel_id
    return None


def channel_line(ch: Channel, mention: Optional[str]) -> str:
    label = mention or f"**{ch.slug}**"
    return f"{ch.emoji} {label}: {ch.desc}"


def map_blocks(resolve: Callable[[str], Optional[str]]) -> List[Tuple[str, List[str]]]:
    """[(section title, [lines])]. `resolve(slug)` returns a clickable mention or None."""
    return [
        (section.title, [channel_line(ch, resolve(ch.slug)) for ch in section.channels])
        for section in MAP
    ]


def clamp(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
