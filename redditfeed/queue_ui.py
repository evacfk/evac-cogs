"""Discord-facing pieces of the approval queue: the queue card, its persistent
Approve / Reject / Pause buttons, and the mod-only X button under feed posts.
All logic lives in the cog; callbacks only delegate. Custom ids are fixed and
the views are registered with bot.add_view(), so buttons survive restarts.
"""
from __future__ import annotations

import discord

from . import constants
from .models import QueueEntry

REDDIT = "https://www.reddit.com"


def build_queue_message(entry: QueueEntry, ttl_hours: float = constants.QUEUE_TTL_HOURS):
    """(content, embeds). Images ride as extra embeds sharing the card's url so
    Discord folds them into one grid; a video / RedGIFs link goes in `content`
    so Discord unfurls it (only when there are no images to show)."""
    post = entry.post
    link = f"{REDDIT}{post.get('permalink')}" if post.get("permalink") else f"{REDDIT}/r/{entry.subreddit}"
    items = entry.media_items()
    images = [i for i in items if not i.is_link_only]
    links = [i for i in items if i.is_link_only]

    embed = discord.Embed(title=(post.get("title") or "(untitled)")[:250], color=discord.Color.gold())
    embed.url = link
    embed.add_field(name="Subreddit", value=f"[r/{entry.subreddit}]({REDDIT}/r/{entry.subreddit})", inline=True)
    embed.add_field(name="Score", value=(str(post["score"]) if post.get("score") is not None else "n/a"), inline=True)
    embed.add_field(name="Posts to", value=" ".join(f"<#{c}>" for c in entry.channel_ids) or "(no channel)", inline=True)
    if post.get("created_utc"):
        embed.add_field(name="Posted", value=f"<t:{int(post['created_utc'])}:R>", inline=True)
    embed.add_field(name="Expires", value=f"<t:{int(entry.created_ts + ttl_hours * 3600)}:R>", inline=True)
    if len(images) > constants.QUEUE_MAX_PREVIEW_IMAGES:
        embed.add_field(name="Gallery", value=f"{len(images)} images (first {constants.QUEUE_MAX_PREVIEW_IMAGES} shown; all post if approved)", inline=False)
    if links:
        embed.add_field(name="Video / GIF", value="\n".join(f"[open]({i.url})" for i in links[:3]), inline=False)
    embed.set_footer(text="Mods only: Approve posts it, Reject discards it, Pause stops this subreddit.")

    embeds = [embed]
    shown = images[: constants.QUEUE_MAX_PREVIEW_IMAGES]
    if shown:
        embed.set_image(url=shown[0].url)
        for item in shown[1:]:
            tile = discord.Embed()
            tile.url = link
            tile.set_image(url=item.url)
            embeds.append(tile)
    content = links[0].url if (links and not shown) else None
    return content, embeds


def resolved_embeds(embeds: list, result: str) -> list:
    """A decided card: keep the details, drop the images (nothing explicit left
    sitting in the queue), record who decided what."""
    if not embeds:
        embed = discord.Embed(title="(queue item)")
    else:
        embed = embeds[0]
        embed.set_image(url=None)
    embed.add_field(name="Result", value=result, inline=False)
    embed.set_footer(text="Decided")
    return [embed]


class QueueView(discord.ui.View):
    """Approve / Reject / Pause under every queue card (one persistent view)."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success, custom_id=constants.QUEUE_APPROVE_ID)
    async def approve(self, interaction, button):
        await self.cog.handle_queue_action(interaction, "approve")

    @discord.ui.button(label="Reject", emoji="✖️", style=discord.ButtonStyle.danger, custom_id=constants.QUEUE_REJECT_ID)
    async def reject(self, interaction, button):
        await self.cog.handle_queue_action(interaction, "reject")

    @discord.ui.button(label="Pause sub", emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id=constants.QUEUE_PAUSE_ID)
    async def pause(self, interaction, button):
        await self.cog.handle_queue_action(interaction, "pause")


class FeedPostView(discord.ui.View):
    """The small mod-only X under a feed post. Everyone can see it; only
    moderators can use it (others get a private refusal)."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, custom_id=constants.FEED_X_ID)
    async def remove(self, interaction, button):
        await self.cog.handle_feed_x(interaction)
