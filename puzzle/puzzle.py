import asyncio
import io
import logging
import random
import time
from pathlib import Path
from typing import Optional

import discord
from PIL import Image, ImageDraw
from redbot.core import Config, checks, commands
from redbot.core.data_manager import cog_data_path
from redbot.core.bot import Red
from discord.ext import tasks

DEFAULT_GRID = (3, 3)
CHECK_INTERVAL_MINUTES = 5  # how often the background loop wakes up to check timers

log = logging.getLogger("red.puzzle")


class Puzzle(commands.Cog):
    """Image-reveal puzzle game.

    Admins load a pool of images. Each is sliced into a grid of pieces.
    A random piece (with repeats) is posted to a channel on a timer;
    members race to claim pieces with a reaction and build up their own
    collection. The round keeps posting pieces indefinitely until enough
    people (configurable via `[p]puzzle setwinners`) have each collected
    every distinct piece, at which point the full image is posted, the
    winners are announced, and the cog automatically starts a new puzzle
    from a random, not-yet-used image in the pool.
    """

    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x50555A5A4C45, force_registration=True)

        default_guild = {
            "channel_id": None,
            "interval_hours": 6,
            "claim_emoji": "\N{JIGSAW PUZZLE PIECE}",
            "reward_role_id": None,
            "pool": {},  # str(image_id) -> {"grid_x", "grid_y", "filename", "added_by"}
            "next_id": 1,
            "used_ids": [],
            "winners_count": 1,
            "active": None,
        }
        self.config.register_guild(**default_guild)

        self._locks: dict[int, asyncio.Lock] = {}
        self._test_tasks: dict[int, asyncio.Task] = {}
        self.background_loop.start()

    def cog_unload(self):
        self.background_loop.cancel()
        for task in self._test_tasks.values():
            task.cancel()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    # keys every valid active-round dict must have under the current schema
    _ACTIVE_SCHEMA_KEYS = frozenset(
        {
            "image_id",
            "grid_x",
            "grid_y",
            "posted_total",
            "last_post_ts",
            "open_messages",
            "inventories",
            "completions",
            "unposted_positions",
        }
    )

    async def _get_active(self, guild: discord.Guild) -> Optional[dict]:
        """Fetch the active round, automatically clearing (and treating as
        "no active round") anything left over from an older version of this
        cog whose data doesn't match the current schema — e.g. a round that
        was started before an update and never stopped. Without this, a
        stale round causes confusing KeyErrors deep in game logic instead of
        a clear, safe reset."""
        active = await self.config.guild(guild).active()
        if active is not None and not self._ACTIVE_SCHEMA_KEYS.issubset(active.keys()):
            log.warning(
                "Clearing an incompatible/stale active puzzle round for guild %s "
                "(likely left over from before a cog update). Run [p]puzzle start "
                "or [p]puzzle testrun to begin a new one.",
                guild.id,
            )
            await self.config.guild(guild).active.set(None)
            return None
        return active

    def _guild_dir(self, guild_id: int) -> Path:
        path = cog_data_path(self) / str(guild_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _image_dir(self, guild_id: int, image_id: int) -> Path:
        path = self._guild_dir(guild_id) / str(image_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _parse_grid(grid: str) -> Optional[tuple]:
        try:
            x_str, y_str = grid.lower().split("x")
            x, y = int(x_str), int(y_str)
        except (ValueError, AttributeError):
            return None
        if x < 2 or y < 2 or x > 10 or y > 10:
            return None
        return x, y

    def _slice_image(self, source: bytes, grid_x: int, grid_y: int, out_dir: Path) -> int:
        """Slice source image bytes into grid_x * grid_y pieces, saved as
        piece_0.png .. piece_N.png in out_dir. Returns the piece count."""
        img = Image.open(io.BytesIO(source)).convert("RGBA")
        width, height = img.size
        # crop to a multiple of the grid so pieces are even
        piece_w = width // grid_x
        piece_h = height // grid_y
        img = img.crop((0, 0, piece_w * grid_x, piece_h * grid_y))
        img.save(out_dir / "full.png")

        count = 0
        for row in range(grid_y):
            for col in range(grid_x):
                box = (col * piece_w, row * piece_h, (col + 1) * piece_w, (row + 1) * piece_h)
                piece = img.crop(box)
                piece.save(out_dir / f"piece_{count}.png")
                count += 1
        return count

    @staticmethod
    def _ensure_full_image(image_dir: Path, grid_x: int, grid_y: int) -> Optional[Path]:
        """Return the path to the full assembled image, stitching it back
        together from the individual pieces if it's missing (e.g. images
        added before this cog started saving full.png)."""
        full_path = image_dir / "full.png"
        if full_path.exists():
            return full_path

        total = grid_x * grid_y
        piece_paths = [image_dir / f"piece_{i}.png" for i in range(total)]
        if not all(p.exists() for p in piece_paths):
            return None

        with Image.open(piece_paths[0]) as sample:
            piece_w, piece_h = sample.size
        canvas = Image.new("RGBA", (piece_w * grid_x, piece_h * grid_y))
        for i, piece_path in enumerate(piece_paths):
            row, col = divmod(i, grid_x)
            with Image.open(piece_path) as piece_img:
                canvas.paste(piece_img, (col * piece_w, row * piece_h))
        canvas.save(full_path)
        return full_path

    @staticmethod
    def _build_progress_image(image_dir: Path, grid_x: int, grid_y: int, owned: set) -> Optional[io.BytesIO]:
        """Build a preview showing which distinct pieces a user has collected
        so far: their claimed pieces in their correct grid position, with a
        dark placeholder box for everything they don't have yet."""
        total = grid_x * grid_y
        sample_path = image_dir / "piece_0.png"
        if not sample_path.exists():
            return None
        with Image.open(sample_path) as sample:
            piece_w, piece_h = sample.size

        canvas = Image.new("RGBA", (piece_w * grid_x, piece_h * grid_y), (32, 32, 36, 255))
        draw = ImageDraw.Draw(canvas)
        for i in range(total):
            row, col = divmod(i, grid_x)
            x0, y0 = col * piece_w, row * piece_h
            if i in owned:
                piece_path = image_dir / f"piece_{i}.png"
                if piece_path.exists():
                    with Image.open(piece_path) as piece_img:
                        canvas.paste(piece_img, (x0, y0))
            else:
                draw.rectangle(
                    (x0 + 1, y0 + 1, x0 + piece_w - 2, y0 + piece_h - 2),
                    outline=(110, 110, 118, 255),
                    width=2,
                )

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        buf.seek(0)
        return buf

    async def _pick_next_image_id(self, guild: discord.Guild) -> Optional[int]:
        pool = await self.config.guild(guild).pool()
        if not pool:
            return None
        used = await self.config.guild(guild).used_ids()
        available = [int(i) for i in pool.keys() if int(i) not in used]
        if not available:
            # exhausted the pool without repeats; reshuffle the cycle
            used = []
            available = [int(i) for i in pool.keys()]
        choice = random.choice(available)
        used.append(choice)
        await self.config.guild(guild).used_ids.set(used)
        return choice

    async def _start_round(self, guild: discord.Guild, image_id: int) -> Optional[str]:
        """Sets up a fresh active round for image_id. Returns an error
        string on failure, or None on success."""
        pool = await self.config.guild(guild).pool()
        meta = pool.get(str(image_id))
        if meta is None:
            return f"Image ID {image_id} is not in the pool."

        total = meta["grid_x"] * meta["grid_y"]
        unposted = list(range(total))
        random.shuffle(unposted)

        active = {
            "image_id": image_id,
            "grid_x": meta["grid_x"],
            "grid_y": meta["grid_y"],
            "posted_total": 0,  # pieces ever posted this round, including repeats
            "last_post_ts": 0,  # 0 forces an immediate first post on the next loop tick
            "open_messages": {},  # str(message_id) -> piece_index, posted and not yet claimed
            "inventories": {},  # str(user_id) -> [piece_index, ...] (may contain duplicates)
            "completions": [],  # user_ids, in the order they completed a full set
            # every distinct position, in shuffled order, still owed a guaranteed
            # first appearance -- drained before any repeats are allowed to post
            "unposted_positions": unposted,
        }
        await self.config.guild(guild).active.set(active)
        return None

    async def _post_next_piece(self, guild: discord.Guild):
        channel_id = await self.config.guild(guild).channel_id()
        if channel_id is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        async with self._guild_lock(guild.id):
            active = await self._get_active(guild)
            if active is None:
                return
            total = active["grid_x"] * active["grid_y"]

            # every distinct position must post at least once before any repeats
            # are allowed -- only after that "first pass" is exhausted do pieces
            # start posting randomly with replacement
            first_pass = bool(active["unposted_positions"])
            piece_index = active["unposted_positions"][-1] if first_pass else random.randrange(total)

            image_dir = self._image_dir(guild.id, active["image_id"])
            piece_path = image_dir / f"piece_{piece_index}.png"
            if not piece_path.exists():
                return

            emoji = await self.config.guild(guild).claim_emoji()
            embed = discord.Embed(
                title="A new puzzle piece has appeared!",
                description=f"React with {emoji} to claim it. (Piece position {piece_index + 1} of {total}.)",
                color=discord.Color.blurple(),
            )
            file = discord.File(piece_path, filename="piece.png")
            embed.set_image(url="attachment://piece.png")

            try:
                message = await channel.send(embed=embed, file=file)
                await message.add_reaction(emoji)
            except discord.HTTPException:
                log.exception("Failed to post a puzzle piece in guild %s", guild.id)
                return

            if first_pass:
                active["unposted_positions"].pop()
            active["open_messages"][str(message.id)] = piece_index
            active["posted_total"] += 1
            active["last_post_ts"] = time.time()
            await self.config.guild(guild).active.set(active)

    async def _finish_round(self, guild: discord.Guild, winners: list):
        channel_id = await self.config.guild(guild).channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        active = await self._get_active(guild)
        image_id = active["image_id"] if active else None

        full_image_path = None
        if image_id is not None and active is not None:
            image_dir = self._image_dir(guild.id, image_id)
            full_image_path = self._ensure_full_image(image_dir, active["grid_x"], active["grid_y"])

        if channel is not None and winners:
            mentions = []
            for user_id in winners:
                member = guild.get_member(user_id)
                mentions.append(member.mention if member else f"<@{user_id}>")
            text = f"\N{PARTY POPPER} " + ", ".join(mentions) + " completed the puzzle!"
            if full_image_path is not None:
                await channel.send(text, file=discord.File(full_image_path, filename="completed.png"))
            else:
                await channel.send(text)

            role_id = await self.config.guild(guild).reward_role_id()
            if role_id:
                role = guild.get_role(role_id)
                if role is not None:
                    for user_id in winners:
                        member = guild.get_member(user_id)
                        if member is not None:
                            try:
                                await member.add_roles(role, reason="Completed the server puzzle")
                            except discord.HTTPException:
                                log.exception(
                                    "Failed to grant puzzle winner role to %s in guild %s",
                                    user_id,
                                    guild.id,
                                )

        await self.config.guild(guild).active.set(None)

        next_id = await self._pick_next_image_id(guild)
        if next_id is None:
            if channel is not None:
                await channel.send(
                    "The image pool is empty, so the puzzle game is paused. "
                    "An admin can add more with `[p]puzzle addimage`."
                )
            return

        err = await self._start_round(guild, next_id)
        if err and channel is not None:
            await channel.send(f"Couldn't start the next puzzle automatically: {err}")
        elif channel is not None:
            await channel.send(f"Starting a new puzzle with image #{next_id}!")

    # ------------------------------------------------------------------ #
    # background loop
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def background_loop(self):
        for guild in self.bot.guilds:
            try:
                if guild.id in self._test_tasks:
                    continue  # a test run is driving postings for this guild right now
                active = await self._get_active(guild)
                if active is None:
                    continue
                interval_hours = await self.config.guild(guild).interval_hours()
                elapsed = time.time() - active["last_post_ts"]
                if elapsed >= interval_hours * 3600:
                    await self._post_next_piece(guild)
            except Exception:
                # never let one guild's error kill the loop for everyone else
                log.exception("Error in puzzle background loop for guild %s", guild.id)
                continue

    @background_loop.before_loop
    async def _before_background_loop(self):
        await self.bot.wait_until_red_ready()

    # ------------------------------------------------------------------ #
    # reaction handling
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        emoji = await self.config.guild(guild).claim_emoji()
        if str(payload.emoji) != emoji:
            return

        finished_winners = None

        async with self._guild_lock(guild.id):
            active = await self._get_active(guild)
            if active is None:
                return

            msg_key = str(payload.message_id)
            piece_index = active["open_messages"].pop(msg_key, None)
            if piece_index is None:
                return  # not an open piece message (already claimed, or unrelated)

            user_key = str(payload.member.id)
            active["inventories"].setdefault(user_key, []).append(piece_index)
            await self.config.guild(guild).active.set(active)

            total = active["grid_x"] * active["grid_y"]

            # Update the message to show it's claimed. This is purely cosmetic —
            # any failure here must NEVER block the win-check below, so it gets
            # its own broad try/except rather than sharing one with real game logic.
            channel = guild.get_channel(payload.channel_id)
            if channel is not None:
                try:
                    message = await channel.fetch_message(payload.message_id)
                    new_embed = discord.Embed(
                        title="A new puzzle piece has appeared!",
                        description=(
                            f"React with {emoji} to claim it. "
                            f"(Piece position {piece_index + 1} of {total}.)"
                        ),
                        color=discord.Color.green(),
                    )
                    # reuse the ORIGINAL attachment:// reference (not a re-fetched,
                    # already-resolved CDN url) and explicitly keep the existing
                    # attachment so the image can't get detached and show up bare
                    new_embed.set_image(url="attachment://piece.png")
                    new_embed.add_field(name="Claimed by", value=payload.member.mention, inline=False)
                    await message.edit(embed=new_embed, attachments=message.attachments)
                except Exception:
                    log.exception(
                        "Failed to visually mark a puzzle piece claimed in guild %s (message %s); "
                        "the claim itself was still recorded.",
                        guild.id,
                        payload.message_id,
                    )

            distinct = set(active["inventories"][user_key])
            winners_count = await self.config.guild(guild).winners_count()

            if len(distinct) >= total and payload.member.id not in active["completions"]:
                active["completions"].append(payload.member.id)
                await self.config.guild(guild).active.set(active)
                if channel is not None:
                    await channel.send(
                        f"\N{JIGSAW PUZZLE PIECE} {payload.member.mention} collected every piece! "
                        f"({len(active['completions'])}/{winners_count} winner(s) needed to end this puzzle)"
                    )
                if len(active["completions"]) >= winners_count:
                    finished_winners = list(active["completions"])

        if finished_winners is not None:
            await self._finish_round(guild, finished_winners)

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #

    @commands.group()
    @commands.guild_only()
    async def puzzle(self, ctx: commands.Context):
        """Image-reveal puzzle game commands."""

    @puzzle.command(name="addimage")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_addimage(self, ctx: commands.Context, grid: str = "3x3"):
        """Add an image to the puzzle pool. Attach the image with this command.

        `grid` is the number of columns x rows, e.g. `4x4`. Defaults to 3x3.
        """
        if not ctx.message.attachments:
            await ctx.send("Attach an image with this command.")
            return

        parsed = self._parse_grid(grid)
        if parsed is None:
            await ctx.send("Grid must look like `3x3`, with each side between 2 and 10.")
            return
        grid_x, grid_y = parsed

        attachment = ctx.message.attachments[0]
        if not (attachment.content_type or "").startswith("image/"):
            await ctx.send("That attachment doesn't look like an image.")
            return

        data = await attachment.read()

        async with self.config.guild(ctx.guild).all() as guild_data:
            image_id = guild_data["next_id"]
            guild_data["next_id"] += 1

        try:
            out_dir = self._image_dir(ctx.guild.id, image_id)
            piece_count = self._slice_image(data, grid_x, grid_y, out_dir)
        except Exception as e:
            await ctx.send(f"Couldn't process that image: {e}")
            return

        async with self.config.guild(ctx.guild).pool() as pool:
            pool[str(image_id)] = {
                "grid_x": grid_x,
                "grid_y": grid_y,
                "added_by": ctx.author.id,
                "filename": attachment.filename,
            }

        await ctx.send(
            f"Added image **#{image_id}** to the pool ({grid_x}x{grid_y} = {piece_count} pieces)."
        )

    @puzzle.command(name="delimage")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_delimage(self, ctx: commands.Context, image_id: int):
        """Remove an image from the pool by its ID."""
        active = await self._get_active(ctx.guild)
        if active is not None and active["image_id"] == image_id:
            await ctx.send(
                "That image is the currently active puzzle. Use `[p]puzzle stop` first if you "
                "really want to delete it."
            )
            return

        async with self.config.guild(ctx.guild).pool() as pool:
            if str(image_id) not in pool:
                await ctx.send(f"No image with ID {image_id} in the pool.")
                return
            del pool[str(image_id)]

        async with self.config.guild(ctx.guild).used_ids() as used:
            if image_id in used:
                used.remove(image_id)

        image_dir = self._guild_dir(ctx.guild.id) / str(image_id)
        if image_dir.exists():
            for f in image_dir.iterdir():
                f.unlink(missing_ok=True)
            image_dir.rmdir()

        await ctx.send(f"Removed image #{image_id} from the pool.")

    @puzzle.command(name="images")
    async def puzzle_images(self, ctx: commands.Context):
        """List the images currently in the pool."""
        pool = await self.config.guild(ctx.guild).pool()
        if not pool:
            await ctx.send("The image pool is empty.")
            return

        active = await self._get_active(ctx.guild)
        active_id = active["image_id"] if active else None

        lines = []
        for image_id, meta in sorted(pool.items(), key=lambda kv: int(kv[0])):
            marker = " (active)" if active is not None and int(image_id) == active_id else ""
            lines.append(
                f"#{image_id}: {meta['grid_x']}x{meta['grid_y']} "
                f"({meta['filename']}){marker}"
            )
        await ctx.send("\n".join(lines))

    @puzzle.command(name="start")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_start(self, ctx: commands.Context):
        """Start the puzzle rotation with a random image from the pool."""
        if await self.config.guild(ctx.guild).channel_id() is None:
            await ctx.send("Set a channel first with `[p]puzzle setchannel #channel`.")
            return

        active = await self._get_active(ctx.guild)
        if active is not None:
            await ctx.send("A puzzle is already running. Use `[p]puzzle stop` first.")
            return

        image_id = await self._pick_next_image_id(ctx.guild)
        if image_id is None:
            await ctx.send("The image pool is empty. Add some with `[p]puzzle addimage` first.")
            return

        err = await self._start_round(ctx.guild, image_id)
        if err:
            await ctx.send(err)
            return

        await ctx.send(
            f"Puzzle started with image #{image_id}. The first piece will post shortly, "
            f"then every {await self.config.guild(ctx.guild).interval_hours()} hour(s) after that."
        )

    @puzzle.command(name="stop")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_stop(self, ctx: commands.Context):
        """Stop the current puzzle round and clear its state."""
        active = await self._get_active(ctx.guild)
        if active is None:
            await ctx.send("No puzzle is currently running.")
            return
        task = self._test_tasks.pop(ctx.guild.id, None)
        if task is not None:
            task.cancel()
        await self.config.guild(ctx.guild).active.set(None)
        await ctx.send("Puzzle stopped and reset. The pool and settings are untouched.")

    @puzzle.command(name="testrun")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_testrun(self, ctx: commands.Context, seconds: float = 10):
        """Fast-forward the puzzle for testing: post a new piece every
        `seconds` seconds (default 10) instead of waiting for the normal
        interval, so you can watch the whole flow — pieces, claiming, and
        the win announcement — play out quickly.

        Uses the currently active puzzle if one is running, otherwise
        starts a fresh one from the pool. Claiming still works normally.
        This runs indefinitely (auto-continuing into new puzzles, same as
        normal play) until you cancel it with `[p]puzzle teststop` — it
        does not stop on its own, since the game itself never "runs out"
        of pieces to post.
        """
        if await self.config.guild(ctx.guild).channel_id() is None:
            await ctx.send("Set a channel first with `[p]puzzle setchannel #channel`.")
            return
        if seconds < 3:
            await ctx.send("Use at least 3 seconds between pieces, to stay clear of Discord rate limits.")
            return
        if ctx.guild.id in self._test_tasks:
            await ctx.send("A test run is already in progress. Use `[p]puzzle teststop` first.")
            return

        # claim the guild for test-mode immediately (before any awaits) so the
        # normal background loop can't race in and post a piece at the same time
        self._test_tasks[ctx.guild.id] = None

        active = await self._get_active(ctx.guild)
        if active is None:
            image_id = await self._pick_next_image_id(ctx.guild)
            if image_id is None:
                self._test_tasks.pop(ctx.guild.id, None)
                await ctx.send("The image pool is empty. Add some with `[p]puzzle addimage` first.")
                return
            err = await self._start_round(ctx.guild, image_id)
            if err:
                self._test_tasks.pop(ctx.guild.id, None)
                await ctx.send(err)
                return

        await ctx.send(
            f"Test run started: a new piece every {seconds}s, continuing through new puzzles "
            "automatically, until you run `[p]puzzle teststop`."
        )

        task = asyncio.create_task(self._run_test_loop(ctx.guild, seconds))
        self._test_tasks[ctx.guild.id] = task

    async def _run_test_loop(self, guild: discord.Guild, seconds: float):
        try:
            while True:
                try:
                    active = await self._get_active(guild)
                    if active is None:
                        break
                    await self._post_next_piece(guild)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # never let one failed posting attempt silently end the whole test run
                    log.exception("Error during puzzle test run for guild %s", guild.id)
                await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            pass
        finally:
            self._test_tasks.pop(guild.id, None)

    @puzzle.command(name="teststop")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_teststop(self, ctx: commands.Context):
        """Stop an in-progress test run and go back to normal-interval posting."""
        task = self._test_tasks.get(ctx.guild.id)
        if task is None:
            await ctx.send("No test run is currently active.")
            return
        task.cancel()
        await ctx.send("Test run stopped. Any remaining pieces will post on the normal interval.")

    @puzzle.command(name="status")
    async def puzzle_status(self, ctx: commands.Context):
        """Show progress on the current puzzle."""
        active = await self._get_active(ctx.guild)
        if active is None:
            await ctx.send("No puzzle is currently running.")
            return

        total = active["grid_x"] * active["grid_y"]
        winners_count = await self.config.guild(ctx.guild).winners_count()

        lines = [
            f"Puzzle image #{active['image_id']}: {total} distinct pieces, "
            f"{active['posted_total']} posted so far (pieces repeat).",
            f"Winners so far: {len(active['completions'])}/{winners_count}",
        ]
        if active["inventories"]:
            lines.append("Standings (distinct pieces collected):")
            ranked = sorted(
                active["inventories"].items(),
                key=lambda kv: -len(set(kv[1])),
            )
            for user_id_str, pieces in ranked:
                user_id = int(user_id_str)
                member = ctx.guild.get_member(user_id)
                name = member.display_name if member else f"User {user_id}"
                marker = " ✅" if user_id in active["completions"] else ""
                lines.append(f"  {name}: {len(set(pieces))}/{total}{marker}")
        await ctx.send("\n".join(lines))

    @puzzle.command(name="mypieces", aliases=["mine", "collection"])
    async def puzzle_mypieces(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """See how many distinct pieces you (or someone else) have collected
        for the current puzzle, with an image preview of your progress."""
        member = member or ctx.author
        active = await self._get_active(ctx.guild)
        if active is None:
            await ctx.send("No puzzle is currently running.")
            return

        total = active["grid_x"] * active["grid_y"]
        owned = set(active["inventories"].get(str(member.id), []))

        if not owned:
            await ctx.send(f"{member.display_name} hasn't collected any pieces of the current puzzle yet.")
            return

        image_dir = self._image_dir(ctx.guild.id, active["image_id"])
        buf = self._build_progress_image(image_dir, active["grid_x"], active["grid_y"], owned)
        text = f"{member.display_name}: {len(owned)}/{total} distinct pieces collected."
        if buf is not None:
            await ctx.send(text, file=discord.File(buf, filename="progress.png"))
        else:
            await ctx.send(text)

    @puzzle.command(name="setchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel puzzle pieces are posted to."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"Puzzle pieces will be posted in {channel.mention}.")

    @puzzle.command(name="setinterval")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setinterval(self, ctx: commands.Context, hours: float):
        """Set how many hours between piece postings."""
        if hours <= 0:
            await ctx.send("Hours must be greater than 0.")
            return
        await self.config.guild(ctx.guild).interval_hours.set(hours)
        await ctx.send(f"New pieces will post every {hours} hour(s).")

    @puzzle.command(name="setrole")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setrole(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        """Set (or clear, if no role given) a role awarded to puzzle winners."""
        await self.config.guild(ctx.guild).reward_role_id.set(role.id if role else None)
        if role:
            await ctx.send(f"Winners will be given the {role.name} role.")
        else:
            await ctx.send("Winner role cleared.")

    @puzzle.command(name="setemoji")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setemoji(self, ctx: commands.Context, emoji: str):
        """Set the emoji used to claim pieces."""
        await self.config.guild(ctx.guild).claim_emoji.set(emoji)
        await ctx.send(f"Claim emoji set to {emoji}.")

    @puzzle.command(name="setwinners")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setwinners(self, ctx: commands.Context, count: int):
        """Set how many people need to complete the full set before the
        puzzle ends, posts the completed image, and moves on to the next
        random image in the pool. Defaults to 1.
        """
        if count < 1:
            await ctx.send("Must be at least 1.")
            return
        await self.config.guild(ctx.guild).winners_count.set(count)
        await ctx.send(
            f"The puzzle will now end (posting the full image and picking a new one) once "
            f"{count} winner(s) have collected every piece."
        )

    @puzzle.command(name="settings")
    async def puzzle_settings(self, ctx: commands.Context):
        """Show the current puzzle configuration for this server."""
        data = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(data["channel_id"]) if data["channel_id"] else None
        role = ctx.guild.get_role(data["reward_role_id"]) if data["reward_role_id"] else None
        lines = [
            f"Channel: {channel.mention if channel else 'not set'}",
            f"Interval: {data['interval_hours']} hour(s)",
            f"Claim emoji: {data['claim_emoji']}",
            f"Winners needed to end a puzzle: {data['winners_count']}",
            f"Winner role: {role.name if role else 'not set'}",
            f"Images in pool: {len(data['pool'])}",
        ]
        await ctx.send("\n".join(lines))
