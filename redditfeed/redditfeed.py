"""RedditFeed: config-driven subreddit -> channel image feed.

Replaces the old Reddit RSS/API-based posting (both discontinued by Reddit).
Shared poll loop across all mapped subreddits, image-only posts (galleries
batched, video/RedGIFs posted as links), dedup by (channel, post id) with a
rolling TTL, per-mapping keyword filter + pause toggle, no backfill.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from . import constants, embeds, engine
from .arctic_shift import ArcticShiftSource, RedditSource, RedditSourceError
from .dashboard_integration import DashboardIntegration, dashboard_page
from .dashboard_view import PAGE_TEMPLATE
from .models import SubredditMapping

log = logging.getLogger("red.redditfeed")


class RedditFeed(DashboardIntegration, commands.Cog):
    """Subreddit -> Discord channel image feed, Arctic Shift-backed."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA12DD17F, force_registration=True)
        self.config.register_global(
            mappings={},
            poll_interval_seconds=constants.DEFAULT_POLL_INTERVAL_SECONDS,
            stagger_seconds=constants.DEFAULT_STAGGER_SECONDS,
            fetch_limit=constants.DEFAULT_FETCH_LIMIT,
            dedup_store={},
            dedup_ttl_days=constants.DEFAULT_DEDUP_TTL_DAYS,
        )
        self.source: RedditSource = ArcticShiftSource()
        self._poll_task: Optional[asyncio.Task] = None
        self._cycle_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self._poll_task = self.bot.loop.create_task(self._poll_loop())

    def cog_unload(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()

    # -- Poll loop -------------------------------------------------------------

    async def _poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                async with self._cycle_lock:
                    await self._run_poll_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- never let the loop die silently
                log.exception("redditfeed poll cycle crashed; will retry next interval")
            interval = await self.config.poll_interval_seconds()
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _run_poll_cycle(self) -> None:
        mappings_raw = await self.config.mappings()
        stagger = await self.config.stagger_seconds()
        fetch_limit = await self.config.fetch_limit()
        dedup_store = await self.config.dedup_store()
        ttl_days = await self.config.dedup_ttl_days()
        now = time.time()

        dedup_store = engine.prune_dedup_store(dedup_store, now, ttl_days)

        first = True
        for key, raw in list(mappings_raw.items()):
            mapping = SubredditMapping.from_dict(raw)
            if mapping.paused or not mapping.channel_ids:
                continue

            if not first:
                await asyncio.sleep(stagger)
            first = False

            poll_started = time.time()
            advance_cursor = True
            try:
                dedup_store = await self._poll_one_subreddit(mapping, fetch_limit, dedup_store, now)
                mapping.last_error = None
            except RedditSourceError as exc:
                mapping.last_error = str(exc)
                # Fetch failed, nothing was processed: keep the cursor where it was so
                # the posts from this window are picked up on the next successful poll
                # (bounded by MAX_LOOKBACK_SECONDS; dedup prevents any double-posting).
                advance_cursor = False
                log.warning("redditfeed: fetch failed for r/%s: %s", mapping.subreddit, exc)
            except Exception as exc:  # noqa: BLE001 -- one bad subreddit must not stop the rest
                mapping.last_error = f"unexpected error: {exc}"
                log.exception("redditfeed: unexpected error polling r/%s", mapping.subreddit)

            if advance_cursor:
                # Start-of-fetch time, not end: posts created while the fetch/post loop
                # ran are re-fetched next cycle rather than missed (dedup absorbs repeats).
                mapping.last_poll_ts = poll_started
            mappings_raw[key] = mapping.to_dict()

        await self.config.mappings.set(mappings_raw)
        await self.config.dedup_store.set(dedup_store)

    async def _poll_one_subreddit(
        self, mapping: SubredditMapping, fetch_limit: int, dedup_store: dict, now: float
    ) -> dict:
        after_ts = engine.compute_after_ts(mapping.last_poll_ts, time.time())
        posts = await self.source.fetch_new_posts(mapping.subreddit, after_ts, fetch_limit)
        posts = engine.sort_posts_oldest_first(posts)

        newest_post = None
        for post in posts:
            if not engine.passes_keyword_filter(post, mapping.require_keywords, mapping.block_keywords):
                continue

            media_items = engine.extract_media_items(post)
            if not media_items:
                continue  # text-only / unsupported post type -- nothing to post

            for channel_id in mapping.channel_ids:
                dedup_key = engine.build_dedup_key(channel_id, post["id"])
                if dedup_key in dedup_store:
                    continue
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    continue
                await self._post_media_items(channel, post, media_items)
                dedup_store[dedup_key] = now

            newest_post = post

        if newest_post is not None:
            mapping.last_post_found_ts = newest_post.get("created_utc")
            mapping.last_post_id = newest_post.get("id")

        return dedup_store

    async def _post_media_items(self, channel, post: dict, media_items: list) -> None:
        """Image-only, no text, no embed title/author/footer/link anywhere.

        A single direct image posts as a bare URL (Discord unfurls it inline).
        Multiple images from one post (a gallery) are batched into ONE message
        as multiple image-only embeds sharing a grouping url -- Discord tiles
        those into a single gallery message instead of N separate ones.
        Video/RedGIFs links can't be embedded as an image and always post
        individually as a bare URL.
        """
        image_items, link_items = engine.partition_media_items(media_items)

        if len(image_items) == 1:
            await self._send_text(channel, embeds.build_link_message(post, image_items[0]))
        elif len(image_items) > 1:
            for batch in engine.chunk_items(image_items, constants.MAX_EMBEDS_PER_MESSAGE):
                if not await self._send_embeds(channel, embeds.build_gallery_embeds(post, batch)):
                    return  # no permission -- don't bother with the rest of this post
                await asyncio.sleep(0.5)

        for item in link_items:
            if not await self._send_text(channel, embeds.build_link_message(post, item)):
                return
            await asyncio.sleep(0.5)

    async def _send_text(self, channel, content: str) -> bool:
        """Returns False on a permission failure (caller should stop posting
        further items to this channel for this post); True otherwise.
        """
        try:
            await channel.send(content)
        except discord.Forbidden:
            log.warning("redditfeed: missing permission to post in channel %s", channel.id)
            return False
        except discord.HTTPException as exc:
            log.warning("redditfeed: failed to post to channel %s: %s", channel.id, exc)
        return True

    async def _send_embeds(self, channel, embed_list: list) -> bool:
        try:
            await channel.send(embeds=embed_list)
        except discord.Forbidden:
            log.warning("redditfeed: missing permission to post in channel %s", channel.id)
            return False
        except discord.HTTPException as exc:
            log.warning("redditfeed: failed to post gallery to channel %s: %s", channel.id, exc)
        return True

    # -- Command tree -------------------------------------------------------------

    @commands.group(name="redditfeed", aliases=["rf"], invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    async def redditfeed(self, ctx: commands.Context) -> None:
        """Manage the subreddit -> channel image feed."""
        await ctx.send_help(ctx.command)

    @redditfeed.command(name="version")
    async def redditfeed_version(self, ctx: commands.Context) -> None:
        """Version-probe command -- confirms a deploy actually took."""
        await ctx.send("redditfeed build: dashboard-v4 (Arctic Shift source)")

    @redditfeed.command(name="add")
    async def redditfeed_add(
        self, ctx: commands.Context, subreddit: str, channel: discord.TextChannel
    ) -> None:
        """Map a subreddit to a channel. Safe to call again to add another channel
        to a subreddit already mapped, or to map another subreddit to the same channel.
        """
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            mapping = SubredditMapping.from_dict(raw) if raw else SubredditMapping(subreddit=name)
            if channel.id not in mapping.channel_ids:
                mapping.channel_ids.append(channel.id)
            if raw is None:
                mapping.last_poll_ts = time.time()  # no backfill: starts polling from now
            mappings[name] = mapping.to_dict()
        await ctx.send(f"r/{name} -> {channel.mention}")

    @redditfeed.command(name="remove")
    async def redditfeed_remove(self, ctx: commands.Context, subreddit: str) -> None:
        """Remove a subreddit mapping entirely (all its channels)."""
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            if name not in mappings:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            del mappings[name]
        await ctx.send(f"Removed r/{name}.")

    @redditfeed.command(name="removechannel")
    async def redditfeed_removechannel(
        self, ctx: commands.Context, subreddit: str, channel: discord.TextChannel
    ) -> None:
        """Remove one channel from a subreddit's mapping, keeping the rest."""
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            mapping = SubredditMapping.from_dict(raw)
            if channel.id in mapping.channel_ids:
                mapping.channel_ids.remove(channel.id)
            mappings[name] = mapping.to_dict()
        await ctx.send(f"Removed {channel.mention} from r/{name}.")

    @redditfeed.command(name="pause")
    async def redditfeed_pause(self, ctx: commands.Context, subreddit: str) -> None:
        """Pause polling for a subreddit without deleting its mapping."""
        await self._set_paused(ctx, subreddit, True)

    @redditfeed.command(name="resume")
    async def redditfeed_resume(self, ctx: commands.Context, subreddit: str) -> None:
        """Resume polling for a previously paused subreddit."""
        await self._set_paused(ctx, subreddit, False)

    async def _apply_paused(self, name: str, paused: bool) -> bool:
        """The one place a mapping's paused flag is written -- the commands and
        the dashboard page both go through here. False if `name` isn't mapped.
        """
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                return False
            mapping = SubredditMapping.from_dict(raw)
            mapping.paused = paused
            mappings[name] = mapping.to_dict()
        return True

    async def _set_paused(self, ctx: commands.Context, subreddit: str, paused: bool) -> None:
        name = engine.normalize_subreddit(subreddit)
        if not await self._apply_paused(name, paused):
            await ctx.send(f"r/{name} isn't mapped.")
            return
        await ctx.send(f"r/{name} is now {'paused' if paused else 'active'}.")

    @redditfeed.command(name="interval")
    async def redditfeed_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set the shared poll interval, in seconds (floor: 30s)."""
        clamped = engine.clamp_poll_interval(seconds)
        await self.config.poll_interval_seconds.set(clamped)
        note = f" (raised from {seconds} to the {clamped}s floor)" if clamped != seconds else ""
        await ctx.send(f"Poll interval set to {clamped}s{note}.")

    @redditfeed.command(name="stagger")
    async def redditfeed_stagger(self, ctx: commands.Context, seconds: float) -> None:
        """Set the delay between individual subreddit requests within a poll cycle."""
        clamped = engine.clamp_stagger(seconds)
        await self.config.stagger_seconds.set(clamped)
        await ctx.send(f"Stagger set to {clamped}s.")

    @redditfeed.command(name="list")
    async def redditfeed_list(self, ctx: commands.Context) -> None:
        """List all mapped subreddits."""
        mappings_raw = await self.config.mappings()
        if not mappings_raw:
            await ctx.send("No subreddits mapped yet. `.redditfeed add <subreddit> #channel`")
            return
        lines = [f"r/{name}" for name in sorted(mappings_raw)]
        await ctx.send("\n".join(lines))

    @redditfeed.command(name="status")
    async def redditfeed_status(self, ctx: commands.Context) -> None:
        """Per-subreddit status: channel(s), last poll, last post found, active/paused."""
        mappings_raw = await self.config.mappings()
        if not mappings_raw:
            await ctx.send("No subreddits mapped yet. `.redditfeed add <subreddit> #channel`")
            return
        mappings = [SubredditMapping.from_dict(raw) for raw in mappings_raw.values()]
        lines = embeds.build_status_lines(mappings)
        text = "\n\n".join(lines)
        for chunk_start in range(0, len(text), 1900):
            await ctx.send(text[chunk_start : chunk_start + 1900])

    @redditfeed.group(name="keyword", invoke_without_command=True)
    async def redditfeed_keyword(self, ctx: commands.Context) -> None:
        """Manage per-subreddit keyword filters."""
        await ctx.send_help(ctx.command)

    @redditfeed_keyword.command(name="show")
    async def redditfeed_keyword_show(self, ctx: commands.Context, subreddit: str) -> None:
        name = engine.normalize_subreddit(subreddit)
        mappings_raw = await self.config.mappings()
        raw = mappings_raw.get(name)
        if not raw:
            await ctx.send(f"r/{name} isn't mapped.")
            return
        mapping = SubredditMapping.from_dict(raw)
        require = ", ".join(mapping.require_keywords) or "(none)"
        block = ", ".join(mapping.block_keywords) or "(none)"
        await ctx.send(f"r/{name}\nrequire: {require}\nblock: {block}")

    @redditfeed_keyword.command(name="require")
    async def redditfeed_keyword_require(
        self, ctx: commands.Context, action: str, subreddit: str, *, word: str
    ) -> None:
        """`.redditfeed keyword require add|remove <subreddit> <word>`"""
        await self._edit_keyword_list(ctx, action, subreddit, word, constants.KEYWORD_MODE_REQUIRE)

    @redditfeed_keyword.command(name="block")
    async def redditfeed_keyword_block(
        self, ctx: commands.Context, action: str, subreddit: str, *, word: str
    ) -> None:
        """`.redditfeed keyword block add|remove <subreddit> <word>`"""
        await self._edit_keyword_list(ctx, action, subreddit, word, constants.KEYWORD_MODE_BLOCK)

    async def _edit_keyword_list(
        self, ctx: commands.Context, action: str, subreddit: str, word: str, mode: str
    ) -> None:
        action = action.lower()
        if action not in ("add", "remove"):
            await ctx.send("Action must be `add` or `remove`.")
            return
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            mapping = SubredditMapping.from_dict(raw)
            target = mapping.require_keywords if mode == constants.KEYWORD_MODE_REQUIRE else mapping.block_keywords
            word_lower = word.lower()
            if action == "add" and word_lower not in target:
                target.append(word_lower)
            elif action == "remove" and word_lower in target:
                target.remove(word_lower)
            mappings[name] = mapping.to_dict()
        await ctx.send(f"r/{name} {mode} keywords: {', '.join(target) or '(none)'}")

    # -- Dashboard page ------------------------------------------------------------
    # Registration is handled by DashboardIntegration (dashboard_integration.py).
    # Everything the page can do is also a `.redditfeed` command; the page is a
    # second front end over the same config, and writes go through _apply_paused.

    async def _on_bot_loop(self, coro):
        """Run `coro` on the bot's own event loop and await its result.

        The dashboard calls page handlers from its web thread's event loop, not
        Red's. Anything that writes Red Config (asyncio locks, the JSON driver)
        or talks to Discord must run on the bot's loop; across loops it can raise
        "bound to a different event loop" or race the poll loop. If we are
        already on the bot's loop this just awaits inline.
        """
        loop = getattr(self.bot, "loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or loop is running:
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def _dashboard_can_edit(self, user, guild: discord.Guild) -> bool:
        """Same bar as the command group: bot owner, Manage Server, or mod role."""
        if await self.bot.is_owner(user):
            return True
        member = guild.get_member(user.id)
        if member is None:
            return False
        if member.guild_permissions.manage_guild:
            return True
        return await self.bot.is_mod(member)

    async def _dashboard_apply_pause(
        self, user, guild: discord.Guild, subreddit: str, action: str
    ) -> tuple[str, str]:
        """Permission-checked pause/resume for the dashboard. Returns
        (notification category, message). Checks happen here, in the write path
        itself, not only in whether the form was shown.
        """
        paused = engine.parse_pause_action(action)
        if paused is None:
            return "error", "Unknown action."
        if not await self._dashboard_can_edit(user, guild):
            return "error", "You need the mod role or Manage Server to do that."
        name = engine.normalize_subreddit(subreddit)
        visible = engine.mappings_for_channels(
            await self.config.mappings(), {c.id for c in guild.text_channels}
        )
        if name not in {m.subreddit for m in visible}:
            return "error", f"r/{name} isn't mapped to a channel in this server."
        if not await self._on_bot_loop(self._apply_paused(name, paused)):
            return "error", f"r/{name} isn't mapped."
        return "success", f"r/{name} is now {'paused' if paused else 'active'}."

    @dashboard_page(
        name="feeds",
        description="View RedditFeed subreddit feeds and pause or resume them.",
        methods=("GET", "POST"),
    )
    async def dashboard_redditfeed(self, user: discord.User, guild: discord.Guild, **kwargs):
        can_edit = await self._dashboard_can_edit(user, guild)
        guild_channel_ids = {c.id for c in guild.text_channels}
        notifications = []
        form = None

        Form = kwargs.get("Form")
        if Form is not None and can_edit:
            import wtforms  # shipped with the dashboard; imported lazily so the cog loads without it

            class PauseForm(Form):
                def __init__(self):
                    super().__init__(prefix="redditfeed_pause_form_")

                subreddit: wtforms.SelectField = wtforms.SelectField(
                    "Subreddit", validators=[wtforms.validators.InputRequired()]
                )
                action: wtforms.SelectField = wtforms.SelectField(
                    "Action",
                    choices=[
                        (constants.DASHBOARD_ACTION_PAUSE, "Pause"),
                        (constants.DASHBOARD_ACTION_RESUME, "Resume"),
                    ],
                )
                submit: wtforms.SubmitField = wtforms.SubmitField("Apply")

            form = PauseForm()
            visible = engine.mappings_for_channels(await self.config.mappings(), guild_channel_ids)
            form.subreddit.choices = [(m.subreddit, f"r/{m.subreddit}") for m in visible]
            if form.validate_on_submit() and await form.validate_dpy_converters():
                category, message = await self._dashboard_apply_pause(
                    user, guild, form.subreddit.data, form.action.data
                )
                notifications.append({"message": message, "category": category})

        # Read after any write so the table shows the new state.
        visible = engine.mappings_for_channels(await self.config.mappings(), guild_channel_ids)
        channel_names = {c.id: f"#{c.name}" for c in guild.text_channels}
        rows = engine.build_dashboard_rows(visible, channel_names, time.time())

        result = {
            "status": 0,
            "web_content": {
                "source": PAGE_TEMPLATE,
                "rows": rows,
                "form": form,
                "can_edit": can_edit,
                "guild_name": guild.name,
            },
        }
        if notifications:
            result["notifications"] = notifications
        return result
