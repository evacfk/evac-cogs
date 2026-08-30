import asyncio
import io
import random
import time
from pathlib import Path
from typing import Optional

import discord
from PIL import Image
from redbot.core import Config, checks, commands
from redbot.core.data_manager import cog_data_path
from redbot.core.bot import Red
from discord.ext import tasks

DEFAULT_GRID = (3, 3)
CHECK_INTERVAL_MINUTES = 5  # how often the background loop wakes up to check timers


class Puzzle(commands.Cog):
    """Image-reveal puzzle game.

    Admins load a pool of images. Each is sliced into a grid of pieces.
    One piece is posted to a channel on a timer; members race to claim
    pieces with a reaction. Whoever claims every piece of the current
    image wins, and the cog automatically starts a new puzzle from a
    random, not-yet-used image in the pool.
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
            "active": None,
        }
        self.config.register_guild(**default_guild)

        self._locks: dict[int, asyncio.Lock] = {}
        self.background_loop.start()

    def cog_unload(self):
        self.background_loop.cancel()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

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

        count = 0
        for row in range(grid_y):
            for col in range(grid_x):
                box = (col * piece_w, row * piece_h, (col + 1) * piece_w, (row + 1) * piece_h)
                piece = img.crop(box)
                piece.save(out_dir / f"piece_{count}.png")
                count += 1
        return count

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
        order = list(range(total))
        random.shuffle(order)

        active = {
            "image_id": image_id,
            "grid_x": meta["grid_x"],
            "grid_y": meta["grid_y"],
            "order": order,
            "posted_count": 0,
            "last_post_ts": 0,  # 0 forces an immediate first post on the next loop tick
            "claims": {},
            "messages": {},
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
            active = await self.config.guild(guild).active()
            if active is None:
                return
            total = active["grid_x"] * active["grid_y"]
            if active["posted_count"] >= total:
                return

            piece_index = active["order"][active["posted_count"]]
            image_dir = self._image_dir(guild.id, active["image_id"])
            piece_path = image_dir / f"piece_{piece_index}.png"
            if not piece_path.exists():
                return

            emoji = await self.config.guild(guild).claim_emoji()
            remaining_after = total - active["posted_count"] - 1
            embed = discord.Embed(
                title="A new puzzle piece has appeared!",
                description=(
                    f"React with {emoji} to claim it.\n"
                    f"Piece {active['posted_count'] + 1} of {total} "
                    f"({remaining_after} left to reveal after this one)."
                ),
                color=discord.Color.blurple(),
            )
            file = discord.File(piece_path, filename="piece.png")
            embed.set_image(url="attachment://piece.png")

            try:
                message = await channel.send(embed=embed, file=file)
                await message.add_reaction(emoji)
            except discord.HTTPException:
                return

            active["messages"][str(piece_index)] = message.id
            active["posted_count"] += 1
            active["last_post_ts"] = time.time()
            await self.config.guild(guild).active.set(active)

    async def _finish_round(
        self, guild: discord.Guild, winner_id: Optional[int]
    ):
        channel_id = await self.config.guild(guild).channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        active = await self.config.guild(guild).active()
        image_id = active["image_id"] if active else None

        if channel is not None:
            if winner_id is not None:
                member = guild.get_member(winner_id)
                name = member.mention if member else f"<@{winner_id}>"
                await channel.send(f"\N{PARTY POPPER} {name} collected every piece and completed the puzzle!")
                role_id = await self.config.guild(guild).reward_role_id()
                if role_id and member is not None:
                    role = guild.get_role(role_id)
                    if role is not None:
                        try:
                            await member.add_roles(role, reason="Completed the server puzzle")
                        except discord.HTTPException:
                            pass
            else:
                await channel.send(
                    "All pieces of that puzzle were claimed, but no single person collected the "
                    "full set. Starting a new puzzle!"
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

    def _check_winner(self, active: dict) -> Optional[int]:
        total = active["grid_x"] * active["grid_y"]
        counts: dict[int, int] = {}
        for user_id in active["claims"].values():
            counts[user_id] = counts.get(user_id, 0) + 1
        for user_id, count in counts.items():
            if count >= total:
                return user_id
        return None

    # ------------------------------------------------------------------ #
    # background loop
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def background_loop(self):
        for guild in self.bot.guilds:
            try:
                active = await self.config.guild(guild).active()
                if active is None:
                    continue
                interval_hours = await self.config.guild(guild).interval_hours()
                elapsed = time.time() - active["last_post_ts"]
                if elapsed >= interval_hours * 3600:
                    await self._post_next_piece(guild)
            except Exception:
                # never let one guild's error kill the loop for everyone else
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

        async with self._guild_lock(guild.id):
            active = await self.config.guild(guild).active()
            if active is None:
                return

            piece_index = None
            for idx_str, message_id in active["messages"].items():
                if message_id == payload.message_id:
                    piece_index = idx_str
                    break
            if piece_index is None:
                return

            if piece_index in active["claims"]:
                return  # already claimed, first reactor wins

            active["claims"][piece_index] = payload.member.id
            await self.config.guild(guild).active.set(active)

            channel = guild.get_channel(payload.channel_id)
            if channel is not None:
                try:
                    message = await channel.fetch_message(payload.message_id)
                    embed = message.embeds[0]
                    embed.color = discord.Color.green()
                    embed.add_field(name="Claimed by", value=payload.member.mention, inline=False)
                    await message.edit(embed=embed)
                except discord.HTTPException:
                    pass

            total = active["grid_x"] * active["grid_y"]
            all_claimed = len(active["claims"]) >= total
            winner_id = self._check_winner(active)

        if winner_id is not None or all_claimed:
            await self._finish_round(guild, winner_id)

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
        active = await self.config.guild(ctx.guild).active()
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

        active = await self.config.guild(ctx.guild).active()
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

        active = await self.config.guild(ctx.guild).active()
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
        active = await self.config.guild(ctx.guild).active()
        if active is None:
            await ctx.send("No puzzle is currently running.")
            return
        await self.config.guild(ctx.guild).active.set(None)
        await ctx.send("Puzzle stopped and reset. The pool and settings are untouched.")

    @puzzle.command(name="status")
    async def puzzle_status(self, ctx: commands.Context):
        """Show progress on the current puzzle."""
        active = await self.config.guild(ctx.guild).active()
        if active is None:
            await ctx.send("No puzzle is currently running.")
            return

        total = active["grid_x"] * active["grid_y"]
        claimed = len(active["claims"])
        counts: dict[int, int] = {}
        for user_id in active["claims"].values():
            counts[user_id] = counts.get(user_id, 0) + 1

        lines = [
            f"Puzzle image #{active['image_id']}: {active['posted_count']}/{total} pieces posted, "
            f"{claimed}/{total} claimed."
        ]
        if counts:
            lines.append("Standings:")
            for user_id, count in sorted(counts.items(), key=lambda kv: -kv[1]):
                member = ctx.guild.get_member(user_id)
                name = member.display_name if member else f"User {user_id}"
                lines.append(f"  {name}: {count}/{total}")
        await ctx.send("\n".join(lines))

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
            f"Winner role: {role.name if role else 'not set'}",
            f"Images in pool: {len(data['pool'])}",
        ]
        await ctx.send("\n".join(lines))
