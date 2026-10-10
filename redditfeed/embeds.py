"""Discord-facing rendering for redditfeed. Kept separate from engine.py so the
pure logic never needs discord.py imported to be tested.
"""
from __future__ import annotations

import discord

from .models import MediaItem, SubredditMapping


def build_link_message(post: dict, media_item: MediaItem) -> str:
    """A single item (direct image or a video/RedGIFs link that can't be
    re-hosted as a plain image) posted as a bare URL. Discord's own unfurl then
    renders just the media -- no embed title, no poster/author name, no
    subreddit footer, no clickable title-as-link. `post` is accepted but
    intentionally unused: image-only, no text, by design.
    """
    return media_item.url


def build_gallery_embeds(post: dict, media_items: list[MediaItem]) -> list[discord.Embed]:
    """Multiple images from the same post, batched into ONE message. Discord's
    client visually tiles multiple embeds sent together into a single gallery
    when they share the same embed `url` -- no title/description is set on any
    of them, so that shared url is never rendered as visible text or a link,
    it's purely a client-side grouping key. Caller must send these together via
    `channel.send(embeds=...)` (not one `.send()` per embed) for the grouping
    to take effect, and must not exceed constants.MAX_EMBEDS_PER_MESSAGE per call.
    """
    group_key = f"https://redd.it/{post.get('id', '')}"
    rendered = []
    for item in media_items:
        embed = discord.Embed()
        embed.url = group_key  # grouping key only -- never rendered (no title set)
        embed.set_image(url=item.url)
        rendered.append(embed)
    return rendered


def build_status_lines(mappings: list[SubredditMapping]) -> list[str]:
    """One line per subreddit mapping for `.redditfeed status`. Formatting (embed
    vs plain text, pagination) is the cog's job -- this just produces the text.
    """
    lines = []
    for mapping in sorted(mappings, key=lambda m: m.subreddit):
        state = ("paused" if mapping.paused else "active") + f", {mapping.approval}"
        channels = ", ".join(f"<#{cid}>" for cid in mapping.channel_ids) or "(no channels)"
        last_poll = _format_ts(mapping.last_poll_ts)
        last_post = _format_ts(mapping.last_post_found_ts)
        line = (
            f"**r/{mapping.subreddit}** [{state}] -> {channels}\n"
            f"  last poll: {last_poll} | last post found: {last_post}"
        )
        if mapping.last_error:
            line += f"\n  last error: {mapping.last_error}"
        lines.append(line)
    return lines


def _format_ts(ts) -> str:
    if ts is None:
        return "never"
    return f"<t:{int(ts)}:R>"


def _sub_label(mapping: SubredditMapping) -> str:
    tags = []
    if mapping.paused:
        tags.append("paused")
    if mapping.approval == "auto":
        tags.append("auto")
    return f"r/{mapping.subreddit}" + (f" ({', '.join(tags)})" if tags else "")


def build_mapping_overview(
    mappings: list[SubredditMapping],
    category_channels: dict,
    category_name: str | None,
    existing_channel_ids: set,
    telegram: dict | None = None,
) -> list[str]:
    """Lines for `.redditfeed map`: which subreddits feed which channel, and which
    channels in the category have nothing mapped. `category_channels` is
    {channel_id: name} in display order (empty when no category is known).
    Subreddits are shown plain when manual and not paused; `auto` / `paused` are
    called out because those are the exceptions. `telegram` is {channel_id: label}
    for channels fed by the tgfeed cog: they are not redditfeed mappings, but they
    are not "unmapped" either, so they are listed separately and left out of the
    unmapped list.
    """
    telegram = telegram or {}
    by_channel: dict = {}
    no_channel = []
    for mapping in sorted(mappings, key=lambda m: m.subreddit):
        if not mapping.channel_ids:
            no_channel.append(mapping)
            continue
        for channel_id in mapping.channel_ids:
            by_channel.setdefault(channel_id, []).append(mapping)

    def channel_line(channel_id) -> str:
        return f"<#{channel_id}> ← " + ", ".join(_sub_label(m) for m in by_channel[channel_id])

    have_category = bool(category_name)
    in_category = [cid for cid in category_channels if cid in by_channel]
    outside = sorted(cid for cid in by_channel if cid not in category_channels and cid in existing_channel_ids)
    gone = sorted(cid for cid in by_channel if cid not in existing_channel_ids)

    summary = f"**{len(mappings)} subreddit(s) -> {len(by_channel)} channel(s)**"
    if telegram:
        summary += f", {len(telegram)} Telegram topic channel(s)"
    lines = [summary + "  (manual approval unless marked `auto`)"]
    if have_category:
        lines += ["", f"**Mapped, in {category_name}** ({len(in_category)})"]
        lines += [channel_line(cid) for cid in in_category] or ["(none)"]
        unmapped = [cid for cid in category_channels if cid not in by_channel and cid not in telegram]
        lines += ["", f"**In {category_name} but NOT mapped** ({len(unmapped)})"]
        lines += [f"<#{cid}>" for cid in unmapped] or ["(every channel in the category is mapped)"]
        if outside:
            lines += ["", f"**Mapped, outside {category_name}** ({len(outside)})"]
            lines += [channel_line(cid) for cid in outside]
    else:
        everything = sorted(cid for cid in by_channel if cid in existing_channel_ids)
        lines += ["", f"**Mapped channels** ({len(everything)})"]
        lines += [channel_line(cid) for cid in everything] or ["(none)"]
        lines += ["", "_Add a category to also list unmapped channels:_ `.redditfeed map <category>`"]
    if telegram:
        lines += ["", f"**Fed by Telegram (tgfeed)** ({len(telegram)})"]
        for channel_id, label in telegram.items():
            note = "" if channel_id in existing_channel_ids else " (channel no longer exists)"
            clash = " \u26A0 also has a subreddit mapped" if channel_id in by_channel else ""
            lines.append(f"<#{channel_id}> \u2190 {label}{note}{clash}")
    if gone:
        lines += ["", f"**Mapped to a channel that no longer exists** ({len(gone)})"]
        lines += [f"{channel_id}: " + ", ".join(_sub_label(m) for m in by_channel[channel_id]) for channel_id in gone]
    if no_channel:
        lines += ["", f"**Subreddits with no channel** ({len(no_channel)})"]
        lines += [_sub_label(m) for m in no_channel]
    return lines


def paginate_lines(lines: list[str], limit: int = 1900) -> list[str]:
    """Join lines into messages of at most `limit` characters, splitting only
    between lines (a single over-long line is hard-cut)."""
    pages, current = [], ""
    for line in lines:
        while len(line) > limit:
            if current:
                pages.append(current)
                current = ""
            pages.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pages.append(current)
            current = line
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages
