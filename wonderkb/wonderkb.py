"""WonderKB: keeps the Assistant cog's knowledge current by itself.

Reads public server channels, the live shop, the Alias cog and the Wonderland guide, writes only what
changed into the Assistant's embedding store, and routes "I'm not sure" answers to mod-chat so a mod can
teach the answer once. Everything it owns is prefixed `kb:`; answers a mod adds by hand are `kbm:` and are
never touched by a sync.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord.ext import tasks
from redbot.core import Config, commands

from . import engine, seed
from .constants import (
    ANSWER_MAX_CHARS, AUTO_PREFIX, COG_VERSION, CONFIG_ID, DEFAULT_INTERVAL_HOURS, DEFAULT_MOD_CHANNEL,
    DEFAULT_SOURCES, EMBED_COLOR, MANUAL_PREFIX, MAX_CHANNEL_MESSAGES, QUESTION_MAX_CHARS, SEEN_MAX,
    SEEN_TTL_SECONDS, SYNC_CHECK_MINUTES, UNSURE_MARKER,
)

log = logging.getLogger("red.evac-cogs.wonderkb")


class WonderKB(commands.Cog):
    """Automatic knowledge for the Assistant cog."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_ID, force_registration=True)
        self.config.register_guild(
            sources=list(DEFAULT_SOURCES),
            mod_channel=0,
            interval_hours=DEFAULT_INTERVAL_HOURS,
            last_sync=0.0,
            log_unsure=True,
            marker=UNSURE_MARKER,
            seen={},
        )
        self._locks: Dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        self.sync_loop.start()

    async def cog_unload(self):
        self.sync_loop.cancel()

    # ------------------------------------------------------------------ helpers

    def _assistant(self):
        return self.bot.get_cog("Assistant")

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def resolve_channel(guild: discord.Guild, ref) -> Optional[discord.abc.GuildChannel]:
        text = str(ref).strip()
        if text.isdigit():
            return guild.get_channel(int(text))
        found = engine.match_channel(text, {c.id: c.name for c in guild.text_channels})
        return guild.get_channel(found) if found else None

    @staticmethod
    def is_public(channel) -> bool:
        perms = channel.permissions_for(channel.guild.default_role)
        return bool(perms.view_channel and perms.read_message_history)

    async def mod_channel(self, guild: discord.Guild):
        stored = await self.config.guild(guild).mod_channel()
        return self.resolve_channel(guild, stored or DEFAULT_MOD_CHANNEL)

    # ------------------------------------------------------------------ gathering

    async def _read_channel(self, channel) -> List[Tuple[int, str]]:
        items: List[Tuple[int, str]] = []
        async for m in channel.history(limit=MAX_CHANNEL_MESSAGES):
            text = engine.message_text(m.content, [e.to_dict() for e in m.embeds])
            if text.strip():
                items.append((m.id, text))
        return items

    async def _read_shop(self, guild) -> Any:
        shop = self.bot.get_cog("Shop")
        cfg = getattr(shop, "config", None)
        if cfg is None:
            return None
        return await cfg.guild(guild).shops()

    async def _read_aliases(self, guild) -> List[Tuple[str, str]]:
        cog = self.bot.get_cog("Alias")
        store = getattr(cog, "_aliases", None)
        if store is None:
            return None
        found = list(await store.get_guild_aliases(guild))
        try:
            found += list(await store.get_global_aliases())
        except Exception:  # global aliases are optional
            log.debug("no global aliases", exc_info=True)
        return [(a.name, a.command) for a in found]

    def _read_guide_pages(self):
        wld = self.bot.get_cog("Wonderland")
        if wld is None:
            return None
        package = type(wld).__module__.rpartition(".")[0]
        return importlib.import_module(f"{package}.content").PAGES

    async def gather(self, guild: discord.Guild) -> Tuple[List[engine.Entry], Set[str], Dict[str, str]]:
        """Everything the knowledge should say right now, which sources were read, and a note per source."""
        wanted: List[engine.Entry] = []
        fetched: Set[str] = set()
        notes: Dict[str, str] = {}

        wanted += engine.seed_entries(seed.ROWS)
        fetched.add(engine.seed_prefix())
        notes["seed"] = f"{len(seed.ROWS)} built-in entries"

        try:
            pages = self._read_guide_pages()
            if pages is None:
                notes["guide"] = "Wonderland cog not loaded, skipped"
            else:
                entries = engine.guide_entries(pages)
                wanted += entries
                fetched.add(engine.guide_prefix())
                notes["guide"] = f"{len(entries)} entries"
        except Exception as e:
            log.warning("guide read failed: %s", e)
            notes["guide"] = f"failed: {e}"

        for ref in await self.config.guild(guild).sources():
            channel = self.resolve_channel(guild, ref)
            label = str(ref)
            if channel is None:
                notes[f"#{label}"] = "channel not found, skipped"
                continue
            label = engine.display_name(channel.name)
            if not self.is_public(channel):
                notes[f"#{label}"] = "not visible to @everyone, skipped (members could read it through the bot)"
                continue
            try:
                entries = engine.channel_entries(channel.id, label, await self._read_channel(channel))
            except discord.HTTPException as e:
                notes[f"#{label}"] = f"could not read: {e}"
                continue
            wanted += entries
            fetched.add(engine.channel_prefix(channel.id))
            notes[f"#{label}"] = f"{len(entries)} entries"

        try:
            shops = await self._read_shop(guild)
            if shops is None:
                notes["shop"] = "Shop cog not loaded, skipped"
            else:
                entries = engine.shop_entries(shops)
                wanted += entries
                fetched.add(engine.shop_prefix())
                notes["shop"] = f"{len(entries)} entries" if entries else "0 entries (run .kb shopdump to check the format)"
        except Exception as e:
            log.warning("shop read failed: %s", e)
            notes["shop"] = f"failed: {e}"

        try:
            pairs = await self._read_aliases(guild)
            if pairs is None:
                notes["aliases"] = "Alias cog not loaded, skipped"
            else:
                entries = engine.alias_entries(pairs)
                wanted += entries
                fetched.add(engine.alias_prefix())
                notes["aliases"] = f"{len(entries)} entries"
        except Exception as e:
            log.warning("alias read failed: %s", e)
            notes["aliases"] = f"failed: {e}"

        return wanted, fetched, notes

    # ------------------------------------------------------------------ syncing

    async def sync_guild(self, guild: discord.Guild) -> Dict[str, Any]:
        """One full pass. Returns {"ok": bool, "error": str, "added": int, "deleted": int, "same": int, "notes": {...}}."""
        async with self._lock(guild.id):
            assistant = self._assistant()
            if assistant is None:
                return {"ok": False, "error": "The Assistant cog is not loaded."}
            conf = assistant.db.get_conf(guild)
            if not await assistant.can_call_llm(conf):
                return {"ok": False, "error": "The Assistant has no API key yet (`.assistant api key`)."}

            wanted, fetched, notes = await self.gather(guild)
            store = assistant.embedding_store
            meta = await store.get_all_metadata(guild.id)
            existing = {n: m.get("text", "") for n, m in meta.items() if n.startswith(AUTO_PREFIX)}
            changed, stale = engine.plan_sync(existing, wanted, fetched)

            if changed:
                embeddings, model = await assistant.request_embeddings_batch([e.text for e in changed], conf)
                if len(embeddings) != len(changed):
                    return {"ok": False, "error": "The embedding request returned the wrong number of vectors.", "notes": notes}
                for entry, vector in zip(changed, embeddings):
                    await store.add(guild.id, entry.name, entry.text, vector, model)
            for name in stale:
                await store.delete(guild.id, name)

            await self.config.guild(guild).last_sync.set(time.time())
            return {
                "ok": True, "error": "", "added": len(changed), "deleted": len(stale),
                "same": len(wanted) - len(changed), "notes": notes,
            }

    async def _due(self, guild) -> bool:
        data = await self.config.guild(guild).all()
        return time.time() - data["last_sync"] >= data["interval_hours"] * 3600

    @tasks.loop(minutes=SYNC_CHECK_MINUTES)
    async def sync_loop(self):
        for guild in list(self.bot.guilds):
            try:
                if await self._due(guild):
                    result = await self.sync_guild(guild)
                    if not result.get("ok"):
                        log.info("sync skipped in %s: %s", guild.id, result.get("error"))
            except Exception:
                # One bad guild must never kill the loop for every other guild.
                log.exception("wonderkb sync failed in guild %s", getattr(guild, "id", "?"))

    @sync_loop.before_loop
    async def _before_sync_loop(self):
        await self.bot.wait_until_red_ready()

    # ------------------------------------------------------------------ "I'm not sure" -> mod-chat

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        guild = message.guild
        if guild is None or self.bot.user is None or message.author.id != self.bot.user.id:
            return
        if message.reference is None:
            return
        data = await self.config.guild(guild).all()
        if not data["log_unsure"] or not engine.is_unsure_reply(message.content, data["marker"]):
            return
        try:
            asked = message.reference.resolved
            if not isinstance(asked, discord.Message):
                asked = await message.channel.fetch_message(message.reference.message_id)
        except discord.HTTPException:
            return
        question = engine.trim(asked.content.replace(self.bot.user.mention, "").strip())
        if not question:
            return

        key = engine.question_key(question)
        now = time.time()
        seen = engine.prune_seen(dict(data["seen"]), now, SEEN_TTL_SECONDS, SEEN_MAX)
        if key in seen:
            return
        target = await self.mod_channel(guild)
        if target is None:
            return
        seen[key] = now
        await self.config.guild(guild).seen.set(seen)

        embed = discord.Embed(
            title="The assistant couldn't answer this",
            description=question,
            color=EMBED_COLOR,
        )
        embed.add_field(name="Asked by", value=asked.author.mention, inline=True)
        embed.add_field(name="Where", value=f"[jump]({asked.jump_url})", inline=True)
        short = question if len(question) <= 250 else question[:249] + "…"
        embed.add_field(
            name="Teach it once",
            value=f"```.kb add {short} | <your answer>```",
            inline=False,
        )
        try:
            await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("could not post to the mod channel in %s", guild.id)

    # ------------------------------------------------------------------ commands

    @commands.group(name="kb", invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def kb(self, ctx: commands.Context):
        """Keep the assistant's knowledge current."""
        await ctx.send_help()

    @kb.command(name="version")
    async def kb_version(self, ctx: commands.Context):
        """Version probe."""
        await ctx.send(f"WonderKB v{COG_VERSION}")

    @kb.command(name="add")
    async def kb_add(self, ctx: commands.Context, *, text: str):
        """Teach it an answer once: `.kb add <question> | <answer>`."""
        parsed = engine.parse_qa(text)
        if parsed is None:
            await ctx.send("Use `.kb add <question> | <answer>` (the `|` splits the two).")
            return
        question, answer = parsed
        if len(question) > QUESTION_MAX_CHARS or len(answer) > ANSWER_MAX_CHARS:
            await ctx.send(f"Keep the question under {QUESTION_MAX_CHARS} and the answer under {ANSWER_MAX_CHARS} characters.")
            return
        assistant = self._assistant()
        if assistant is None:
            await ctx.send("The Assistant cog is not loaded.")
            return
        conf = assistant.db.get_conf(ctx.guild)
        if not await assistant.can_call_llm(conf, ctx):
            return
        body = engine.manual_text(question, answer)
        vector, model = await assistant.request_embedding_with_info(body, conf)
        name = engine.manual_name(question)
        await assistant.embedding_store.add(ctx.guild.id, name, body, vector, model)
        await ctx.send(f"Learned. It will use that from now on. (Remove it with `.kb remove {name[len(MANUAL_PREFIX):]}`.)")

    @kb.command(name="remove")
    async def kb_remove(self, ctx: commands.Context, key: str):
        """Forget an answer added with `.kb add` (use the key from `.kb list`)."""
        assistant = self._assistant()
        if assistant is None:
            await ctx.send("The Assistant cog is not loaded.")
            return
        name = key if key.startswith(MANUAL_PREFIX) else f"{MANUAL_PREFIX}{key}"
        if not await assistant.embedding_store.exists(ctx.guild.id, name):
            await ctx.send("No hand-added answer has that key. Check `.kb list`.")
            return
        await assistant.embedding_store.delete(ctx.guild.id, name)
        await ctx.send("Forgotten.")

    @kb.command(name="list")
    async def kb_list(self, ctx: commands.Context):
        """Answers added by hand."""
        assistant = self._assistant()
        if assistant is None:
            await ctx.send("The Assistant cog is not loaded.")
            return
        meta = await assistant.embedding_store.get_all_metadata(ctx.guild.id)
        rows = [(n, m.get("text", "")) for n, m in meta.items() if n.startswith(MANUAL_PREFIX)]
        if not rows:
            await ctx.send("Nothing added by hand yet. Use `.kb add <question> | <answer>`.")
            return
        lines = []
        for name, text in sorted(rows)[:25]:
            first = text.split("\n", 1)[0].removeprefix("Q: ")
            lines.append(f"`{name[len(MANUAL_PREFIX):]}`  {first[:90]}")
        extra = f"\n…and {len(rows) - 25} more." if len(rows) > 25 else ""
        await ctx.send("\n".join(lines) + extra)

    @kb.command(name="sync")
    @commands.admin_or_permissions(manage_guild=True)
    async def kb_sync(self, ctx: commands.Context):
        """Refresh the knowledge now."""
        async with ctx.typing():
            result = await self.sync_guild(ctx.guild)
        if not result["ok"]:
            await ctx.send(f"Could not sync: {result['error']}")
            return
        lines = [f"{k}: {v}" for k, v in result["notes"].items()]
        await ctx.send(
            f"Synced. {result['added']} new or changed, {result['deleted']} removed, {result['same']} already up to date.\n"
            + "```\n" + "\n".join(lines) + "\n```"
        )

    @kb.command(name="status")
    async def kb_status(self, ctx: commands.Context):
        """What it reads and when it last ran."""
        data = await self.config.guild(ctx.guild).all()
        assistant = self._assistant()
        lines = [f"WonderKB v{COG_VERSION}", f"Assistant cog: {'loaded' if assistant else 'NOT loaded'}"]
        if assistant:
            meta = await assistant.embedding_store.get_all_metadata(ctx.guild.id)
            auto = sum(1 for n in meta if n.startswith(AUTO_PREFIX))
            manual = sum(1 for n in meta if n.startswith(MANUAL_PREFIX))
            lines.append(f"Entries: {auto} automatic, {manual} added by hand")
        last = data["last_sync"]
        lines.append("Last sync: " + (f"<t:{int(last)}:R>" if last else "never"))
        lines.append(f"Refreshes every {data['interval_hours']}h")
        for ref in data["sources"]:
            ch = self.resolve_channel(ctx.guild, ref)
            if ch is None:
                lines.append(f"Source {ref}: not found")
            else:
                lines.append(f"Source {ch.mention}: " + ("ok" if self.is_public(ch) else "NOT public, skipped"))
        mod = await self.mod_channel(ctx.guild)
        state = "on" if data["log_unsure"] else "off"
        lines.append(f"Unanswered questions ({state}) go to: {mod.mention if mod else 'no channel found'}")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @kb.group(name="sources", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def kb_sources(self, ctx: commands.Context):
        """Which channels it reads (public channels only)."""
        data = await self.config.guild(ctx.guild).sources()
        shown = []
        for ref in data:
            ch = self.resolve_channel(ctx.guild, ref)
            shown.append(ch.mention if ch else f"`{ref}` (not found)")
        await ctx.send("Reading: " + (", ".join(shown) or "nothing") + "\nChange with `.kb sources add #channel` / `.kb sources remove #channel`.")

    @kb_sources.command(name="add")
    async def kb_sources_add(self, ctx: commands.Context, channel: discord.TextChannel):
        """Read a channel too."""
        if not self.is_public(channel):
            await ctx.send("That channel is not visible to @everyone. Anything it reads is shared with every member through the bot, so I won't add it.")
            return
        async with self.config.guild(ctx.guild).sources() as sources:
            if any(self.resolve_channel(ctx.guild, s) == channel for s in sources):
                await ctx.send("Already reading it.")
                return
            sources.append(str(channel.id))
        await ctx.send(f"Now reading {channel.mention}. It is picked up on the next sync (`.kb sync`).")

    @kb_sources.command(name="remove")
    async def kb_sources_remove(self, ctx: commands.Context, channel: discord.TextChannel):
        """Stop reading a channel. Its entries are cleared on the next sync."""
        async with self.config.guild(ctx.guild).sources() as sources:
            keep = [s for s in sources if self.resolve_channel(ctx.guild, s) != channel]
            removed = len(keep) != len(sources)
            sources[:] = keep
        await ctx.send("Removed. Run `.kb sync` to clear what it learned from there." if removed else "It wasn't in the list.")
        if removed:
            assistant = self._assistant()
            if assistant is not None:
                meta = await assistant.embedding_store.get_all_metadata(ctx.guild.id)
                prefix = engine.channel_prefix(channel.id)
                for name in [n for n in meta if n.startswith(prefix)]:
                    await assistant.embedding_store.delete(ctx.guild.id, name)

    @kb.command(name="modchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def kb_modchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Where unanswered questions go (no channel = back to the default, #mod-chat)."""
        await self.config.guild(ctx.guild).mod_channel.set(channel.id if channel else 0)
        await ctx.send(f"Unanswered questions go to {channel.mention if channel else 'the default #mod-chat'}.")

    @kb.command(name="unsure")
    @commands.admin_or_permissions(manage_guild=True)
    async def kb_unsure(self, ctx: commands.Context, on_off: bool):
        """Turn unanswered-question posts on or off: `.kb unsure on`."""
        await self.config.guild(ctx.guild).log_unsure.set(on_off)
        await ctx.send("On." if on_off else "Off.")

    @kb.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def kb_interval(self, ctx: commands.Context, hours: int):
        """How often it refreshes by itself, in hours (1 to 168)."""
        if not 1 <= hours <= 168:
            await ctx.send("Pick between 1 and 168 hours.")
            return
        await self.config.guild(ctx.guild).interval_hours.set(hours)
        await ctx.send(f"Refreshing every {hours}h.")

    @kb.command(name="shopdump")
    @commands.admin_or_permissions(manage_guild=True)
    async def kb_shopdump(self, ctx: commands.Context):
        """Show how the shop cog stores one shop (to check it reads it right)."""
        try:
            shops = await self._read_shop(ctx.guild)
        except Exception as e:
            await ctx.send(f"Could not read the shop cog: {e}")
            return
        if not isinstance(shops, dict) or not shops:
            await ctx.send("The shop cog isn't loaded or has no shops.")
            return
        name = next(iter(shops))
        shop = shops[name]
        sample = dict(shop) if isinstance(shop, dict) else shop
        if isinstance(sample, dict):
            for key in ("stock", "Items", "items"):
                if isinstance(sample.get(key), dict):
                    sample[key] = dict(list(sample[key].items())[:2])
        text = json.dumps({name: sample}, indent=1, default=str)
        parsed = len(engine.shop_entries(shops))
        await ctx.send(f"Shops: {', '.join(map(str, shops))}\nEntries it can read: {parsed}\n```json\n{text[:1500]}\n```")
