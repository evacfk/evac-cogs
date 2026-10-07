"""Discord-facing rendering for redditfeed. Kept separate from engine.py so the
pure logic never needs discord.py imported to be tested.
"""
from __future__ import annotations

import discord

from . import constants
from .models import MediaItem, SubredditMapping


def build_image_embed(post: dict, media_item: MediaItem) -> discord.Embed:
    """One embed per image (gallery images get one each -- Discord embeds only
    render a single image, so multiple images means multiple messages/embeds).
    """
    title = (post.get("title") or "")[:256]
    permalink = post.get("permalink", "")
    embed = discord.Embed(
        title=title,
        url=f"https://www.reddit.com{permalink}" if permalink else None,
        color=discord.Color.orange(),
    )
    embed.set_image(url=media_item.url)
    subreddit = post.get("subreddit", "")
    embed.set_footer(text=f"r/{subreddit}" if subreddit else None)
    return embed


def build_link_message(post: dict, media_item: MediaItem) -> str:
    """Video/RedGIFs posts can't be re-hosted as a plain image -- post the link
    and let Discord unfurl it. Image-only per the spec, so no title/author text.
    """
    return media_item.url


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
