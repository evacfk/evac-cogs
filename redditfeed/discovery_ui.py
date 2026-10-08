"""Discord-facing rendering for subreddit discovery: the suggestion embed with
an image preview, and the Approve/Deny buttons. Kept separate from discovery.py
so the pure logic never needs discord.py imported to be tested.
"""
from __future__ import annotations

import asyncio
import logging

import discord

from . import constants
from .discovery import PreviewResult, SubredditCandidate, truncate

log = logging.getLogger("red.redditfeed.discovery")


def subreddit_url(display_name: str) -> str:
    return f"https://www.reddit.com/r/{display_name}"


def build_suggestion_embeds(
    candidate: SubredditCandidate,
    preview: PreviewResult,
    channel_id: int,
    preview_failed: bool = False,
) -> list[discord.Embed]:
    """The first embed carries the details. Preview images ride along as extra
    embeds sharing the same url: Discord's client folds those into one card with
    an image grid under the details (up to 4 images).
    """
    url = subreddit_url(candidate.display_name or candidate.name)
    embed = discord.Embed(
        title=f"r/{candidate.display_name or candidate.name}",
        description=truncate(candidate.description, 300) or "(no description)",
        color=discord.Color.gold(),
    )
    embed.url = url
    embed.add_field(name="Subscribers", value=f"{candidate.subscribers:,}", inline=True)
    embed.add_field(name="Would post to", value=f"<#{channel_id}>", inline=True)

    if preview.post_links:
        links = " · ".join(f"[post {i}]({link})" for i, link in enumerate(preview.post_links, 1))
        embed.add_field(name="Top recent posts", value=links, inline=False)

    if preview_failed:
        embed.add_field(
            name="Preview",
            value="Arctic Shift timed out fetching recent posts. Open the subreddit link above to look before deciding.",
            inline=False,
        )
    elif not preview.images:
        embed.add_field(
            name="Preview",
            value="No recent image posts in the last 3 days. Open the subreddit link above to look before deciding.",
            inline=False,
        )

    if preview.video_or_link_posts:
        embed.add_field(
            name="Also posts",
            value=f"{preview.video_or_link_posts} recent video/RedGIFs-only post(s) (not shown here)",
            inline=False,
        )
    if preview.flagged_titles:
        embed.add_field(
            name="⚠️ Safety screen",
            value=(
                f"{preview.flagged_titles} recent post title(s) matched the safety screen and were "
                "left out of the preview. Look closely before approving."
            ),
            inline=False,
        )
    embed.set_footer(text="Mods only: Approve to start feeding this subreddit, Deny to never suggest it again.")

    embeds = [embed]
    if preview.images:
        embed.set_image(url=preview.images[0].url)
        for item in preview.images[1:]:
            tile = discord.Embed()
            tile.url = url
            tile.set_image(url=item.url)
            embeds.append(tile)
    return embeds


class SuggestionView(discord.ui.View):
    """Approve / Deny for one suggestion. Not persistent: a bot restart drops the
    buttons, and re-running `.redditfeed discover` re-suggests anything undecided
    (mapped and denied subreddits are never re-suggested).
    """

    def __init__(self, cog, subreddit: str, channel_id: int, embeds: list, owner_id: int,
                 timeout: float = constants.DISCOVER_VIEW_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.subreddit = subreddit
        self.channel_id = channel_id
        self.embeds = embeds
        self.owner_id = owner_id
        self.message = None
        self._resolved = False
        self._lock = asyncio.Lock()

    async def _allowed(self, user) -> bool:
        if user.id == self.owner_id:
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and perms.manage_guild:
            return True
        try:
            return bool(await self.cog.bot.is_mod(user))
        except Exception:  # noqa: BLE001 -- unknown means not allowed
            return False

    def _finish(self, result: str, footer: str) -> None:
        for child in self.children:
            child.disabled = True
        self.embeds[0].add_field(name="Result", value=result, inline=False)
        self.embeds[0].set_footer(text=footer)

    async def _resolve(self, interaction: discord.Interaction, approve: bool) -> None:
        if not await self._allowed(interaction.user):
            await interaction.response.send_message("Only moderators can approve or deny suggestions.", ephemeral=True)
            return

        async with self._lock:
            if self._resolved:
                await interaction.response.send_message("Someone already decided this one.", ephemeral=True)
                return
            self._resolved = True  # claimed before any further await

            await interaction.response.defer()
            try:
                if approve:
                    created = await self.cog._approve_suggestion(self.subreddit, self.channel_id)
                else:
                    await self.cog._deny_suggestion(self.subreddit)
            except Exception:  # noqa: BLE001 -- release the claim so a mod can retry
                log.exception("redditfeed: failed to %s r/%s", "approve" if approve else "deny", self.subreddit)
                self._resolved = False
                await interaction.followup.send("Something went wrong saving that. Check the bot logs and try again.", ephemeral=True)
                return

            who = interaction.user.mention
            if approve:
                note = "already mapped; channel added" if not created else "new mapping, posts from now on (no backfill)"
                self._finish(
                    f"✅ Approved by {who}: r/{self.subreddit} → <#{self.channel_id}> ({note})",
                    "Approved",
                )
            else:
                self._finish(f"❌ Denied by {who}. It won't be suggested again.", "Denied")
            try:
                await interaction.edit_original_response(embeds=self.embeds, view=self)
            except discord.HTTPException as exc:
                log.warning("redditfeed: could not update suggestion message for r/%s: %s", self.subreddit, exc)
            self.stop()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, False)

    async def on_timeout(self) -> None:
        async with self._lock:
            if self._resolved:
                return
            self._resolved = True
            for child in self.children:
                child.disabled = True
            self.embeds[0].set_footer(text="Expired. Run .redditfeed discover again for a fresh suggestion.")
            if self.message is not None:
                try:
                    await self.message.edit(embeds=self.embeds, view=self)
                except discord.HTTPException:
                    pass
