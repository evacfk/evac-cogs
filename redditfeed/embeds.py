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
        state = "paused" if mapping.paused else "active"
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
