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
from .models import SubredditMapping

log = logging.getLogger("red.redditfeed")

# -- Dashboard integration (best-effort third-party page registration) ------
# UNVERIFIED against the live `dashboard` cog -- the hook name
# (`on_dashboard_cog_add`) and `add_third_party` call are the documented
# pattern other Red third-party cogs use, but the exact `dashboard_page`
# decorator signature and the GET/POST content-exchange shape have not been
# tested against this bot's installed dashboard version. If this page 404s
# or the cog fails to register, the command interface below is the fallback
# -- that's why it was built in parallel rather than dashboard-only.
try:
    from dashboard.rpc.thirdparties import dashboard_page

    DASHBOARD_INTEGRATION_AVAILABLE = True
except Exception:  # noqa: BLE001 -- dashboard cog not installed/loaded
    DASHBOARD_INTEGRATION_AVAILABLE = False

    def dashboard_page(*_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator


class RedditFeed(commands.Cog):
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

            try:
                dedup_store = await self._poll_one_subreddit(mapping, fetch_limit, dedup_store, now)
                mapping.last_error = None
            except RedditSourceError as exc:
                mapping.last_error = str(exc)
                log.warning("redditfeed: fetch failed for r/%s: %s", mapping.subreddit, exc)
            except Exception as exc:  # noqa: BLE001 -- one bad subreddit must not stop the rest
                mapping.last_error = f"unexpected error: {exc}"
                log.exception("redditfeed: unexpected error polling r/%s", mapping.subreddit)

            mapping.last_poll_ts = time.time()
            mappings_raw[key] = mapping.to_dict()

        await self.config.mappings.set(mappings_raw)
        await self.config.dedup_store.set(dedup_store)

    async def _poll_one_subreddit(
        self, mapping: SubredditMapping, fetch_limit: int, dedup_store: dict, now: float
    ) -> dict:
        posts = await self.source.fetch_new_posts(mapping.subreddit, mapping.last_poll_ts, fetch_limit)
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
        for item in media_items:
            try:
                if item.is_link_only:
                    await channel.send(embeds.build_link_message(post, item))
                else:
                    await channel.send(embed=embeds.build_image_embed(post, item))
            except discord.Forbidden:
                log.warning("redditfeed: missing permission to post in channel %s", channel.id)
                break  # no point retrying the rest of this post's items in the same channel
            except discord.HTTPException as exc:
                log.warning("redditfeed: failed to post to channel %s: %s", channel.id, exc)
            await asyncio.sleep(0.5)  # small gap between gallery images

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
        await ctx.send("redditfeed build: dashboard-integration-v1 (Arctic Shift source)")

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

    async def _set_paused(self, ctx: commands.Context, subreddit: str, paused: bool) -> None:
        name = engine.normalize_subreddit(subreddit)
        async with self.config.mappings() as mappings:
            raw = mappings.get(name)
            if not raw:
                await ctx.send(f"r/{name} isn't mapped.")
                return
            mapping = SubredditMapping.from_dict(raw)
            mapping.paused = paused
            mappings[name] = mapping.to_dict()
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

    # -- Dashboard integration (see module-level note above) ----------------------

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog) -> None:
        if DASHBOARD_INTEGRATION_AVAILABLE:
            dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(
        name="settings",
        description="Manage RedditFeed subreddit-to-channel mappings.",
        methods=("GET", "POST"),
    )
    async def dashboard_redditfeed_settings(self, user, guild: discord.Guild, **kwargs):
        """Dashboard page: lists mappings and lets a mod toggle pause or edit
        keyword filters. Parity fallback for everything this page can't do yet
        is the `.redditfeed` command tree above -- use that if this page 404s
        or a form doesn't submit, this hasn't been exercised against a live
        dashboard cog yet.
        """
        mappings_raw = await self.config.mappings()
        mappings = [SubredditMapping.from_dict(raw) for raw in mappings_raw.values()]

        if kwargs.get("method") == "POST":
            data = kwargs.get("data", {})
            name = engine.normalize_subreddit(data.get("subreddit", ""))
            if name in mappings_raw:
                async with self.config.mappings() as live_mappings:
                    mapping = SubredditMapping.from_dict(live_mappings[name])
                    mapping.paused = data.get("paused") == "on"
                    live_mappings[name] = mapping.to_dict()

        rows = "".join(
            f"<tr><td>r/{m.subreddit}</td><td>{'paused' if m.paused else 'active'}</td>"
            f"<td>{', '.join(str(c) for c in m.channel_ids)}</td></tr>"
            for m in sorted(mappings, key=lambda m: m.subreddit)
        )
        html = (
            "<h3>RedditFeed</h3>"
            "<table><tr><th>Subreddit</th><th>State</th><th>Channels</th></tr>"
            f"{rows}</table>"
        )
        return {"status": 0, "web_content": {"source": html}}
