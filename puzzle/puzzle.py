import asyncio
import hashlib
import io
import logging
import random
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

import discord
from PIL import Image, ImageDraw
from redbot.core import Config, checks, commands
from redbot.core.data_manager import cog_data_path
from redbot.core.bot import Red
from discord.ext import tasks

MIN_PIECE_COUNT = 2
MAX_PIECE_COUNT = 25
DEFAULT_PIECE_COUNT = 9  # used when no per-server default and no per-image size is set

CHECK_INTERVAL_MINUTES = 5  # how often the background loop wakes up to check posting timers
SHARED_SWEEP_INTERVAL_SECONDS = 30  # how often open shared-mode pieces are checked for an expired claim window

ACTIVE_SCHEMA_VERSION = 2

_GRID_RE = re.compile(r"^(\d+)x(\d+)$")

log = logging.getLogger("red.puzzle")


class Puzzle(commands.Cog):
    """Image-reveal puzzle game.

    Admins load a pool of images. Each is sliced into a set of pieces
    (either an explicit grid like `4x4`, or a plain piece count like `7`,
    auto-arranged into rows). A random piece (with repeats) is posted to
    a channel on a timer.

    Two claim modes: normal (first reaction wins the piece) or shared
    mode (everyone who reacts within a configurable window gets credit,
    so a single piece can have multiple claimants). Either way, a round
    ends once enough people (`[p]puzzle setwinners`) have each collected
    every distinct piece, at which point the full image is posted, the
    winners are announced, and the cog automatically starts a new puzzle
    from a random, not-yet-used image in the pool (never immediately
    repeating the image that just finished, pool size permitting).

    An optional announcement channel gets a copy of every completion
    post, an optional live status message tracks standings in real time,
    and an all-time leaderboard tracks puzzles won and pieces collected
    per person across every round.
    """

    __version__ = "1.2.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x50555A5A4C45, force_registration=True)

        default_guild = {
            "channel_id": None,
            "interval_min_hours": 4,
            "interval_max_hours": 8,
            "claim_emoji": "\N{JIGSAW PUZZLE PIECE}",
            "reward_role_id": None,
            # str(image_id) -> {"piece_rows", "img_w", "img_h", "filename", "added_by", "image_hash"}
            "pool": {},
            "next_id": 1,
            "used_ids": [],
            "last_image_id": None,  # never picked twice in a row, pool size permitting
            "winners_count": 1,
            "active": None,
            # default piece count for images added WITHOUT an explicit size.
            # None means "use DEFAULT_PIECE_COUNT". Set via [p]puzzle setpieces;
            # only takes effect for images added (and puzzles started) after
            # it's set.
            "next_piece_count": None,
            # shared-credit mode: when on, a piece posted after this was
            # turned on stays claimable by everyone for shared_window_minutes
            # instead of locking to the first reactor.
            "shared_mode": False,
            "shared_window_minutes": 1,
            "announce_channel_id": None,
            "live_status_channel_id": None,
            "live_status_message_id": None,
            # str(user_id) -> {"pieces_collected": int, "puzzles_won": int}, all-time
            "lifetime_stats": {},
        }
        self.config.register_guild(**default_guild)

        self._locks: dict[int, asyncio.Lock] = {}
        self._test_tasks: dict[int, asyncio.Task] = {}
        self.background_loop.start()
        self.shared_sweep_loop.start()

    def cog_unload(self):
        self.background_loop.cancel()
        self.shared_sweep_loop.cancel()
        for task in self._test_tasks.values():
            task.cancel()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    # keys every valid active-round dict must have under the current schema.
    # "schema_version" is checked separately against ACTIVE_SCHEMA_VERSION so
    # that a structural change to what's INSIDE an existing key (e.g.
    # open_messages entries changing from a bare int to a dict, as happened
    # going into schema version 2) also triggers the safe-reset below, not
    # just an added/removed top-level key.
    _ACTIVE_SCHEMA_KEYS = frozenset(
        {
            "schema_version",
            "image_id",
            "piece_rows",
            "img_w",
            "img_h",
            "posted_total",
            "last_post_ts",
            "open_messages",
            "inventories",
            "completions",
            "unposted_positions",
            "next_interval_hours",
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
        if active is not None and (
            not self._ACTIVE_SCHEMA_KEYS.issubset(active.keys())
            or active.get("schema_version") != ACTIVE_SCHEMA_VERSION
        ):
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
    def _compute_layout(count: int) -> List[int]:
        """Given a target piece count, spread it across
        round(sqrt(count)) rows as evenly as possible, returning the
        per-row piece counts (summing to `count`).

        Examples: 9 -> [3, 3, 3], 6 -> [3, 3], 7 -> [3, 2, 2], 2 -> [2].
        Perfect squares/rectangles come out as clean grids; other counts
        (including primes like 7, 11, 13) come out as an uneven "brick"
        layout — still exactly `count` pieces, just not a perfect grid.
        """
        if count < 1:
            raise ValueError("count must be >= 1")
        rows = max(1, round(count**0.5))
        base, extra = divmod(count, rows)
        return [base + 1 if r < extra else base for r in range(rows)]

    @staticmethod
    def _piece_boxes(img_w: int, img_h: int, rows: List[int]) -> List[Tuple[int, int, int, int]]:
        """Row-major (x0, y0, x1, y1) pixel boxes for a brick layout of
        `rows` (per-row piece counts) over an img_w x img_h canvas.
        Piece index order matches `_slice_image`'s piece_N.png numbering."""
        n_rows = len(rows)
        piece_h = img_h // n_rows
        boxes = []
        for row_idx, cols in enumerate(rows):
            piece_w = img_w // cols
            y0, y1 = row_idx * piece_h, (row_idx + 1) * piece_h
            for col_idx in range(cols):
                x0, x1 = col_idx * piece_w, (col_idx + 1) * piece_w
                boxes.append((x0, y0, x1, y1))
        return boxes

    @staticmethod
    def _parse_size(size: str) -> Optional[List[int]]:
        """Parse a `[p]puzzle addimage` size argument into a per-row piece
        layout. Accepts either an explicit grid like `4x4` (each side
        2-10, producing a clean rectangle of that exact shape) or a plain
        piece count like `7` (2-{MAX_PIECE_COUNT}, auto-arranged into rows
        via `_compute_layout`). Returns None if the string is invalid."""
        size = size.strip().lower()

        grid_match = _GRID_RE.match(size)
        if grid_match:
            x, y = int(grid_match.group(1)), int(grid_match.group(2))
            if x < 2 or y < 2 or x > 10 or y > 10:
                return None
            return [x] * y  # y rows of x pieces each -- an exact rectangle

        if size.isdigit():
            count = int(size)
            if count < MIN_PIECE_COUNT or count > MAX_PIECE_COUNT:
                return None
            return Puzzle._compute_layout(count)

        return None

    def _slice_image(self, source: bytes, rows: List[int], out_dir: Path) -> Tuple[int, int, int]:
        """Slice source image bytes into pieces according to `rows` (a
        per-row piece-count layout from `_compute_layout` or an explicit
        grid), saved as piece_0.png .. piece_N.png in out_dir in row-major
        order. Returns (piece_count, img_w, img_h) of the cropped canvas."""
        max_cols = max(rows)
        n_rows = len(rows)

        img = Image.open(io.BytesIO(source)).convert("RGBA")
        width, height = img.size
        # crop so the tallest row-count and widest row's column count both
        # divide the canvas evenly
        piece_h = height // n_rows
        piece_w = width // max_cols
        img = img.crop((0, 0, piece_w * max_cols, piece_h * n_rows))
        img.save(out_dir / "full.png")

        boxes = self._piece_boxes(img.width, img.height, rows)
        for i, box in enumerate(boxes):
            img.crop(box).save(out_dir / f"piece_{i}.png")
        return len(boxes), img.width, img.height

    @staticmethod
    def _ensure_full_image(image_dir: Path, img_w: int, img_h: int, rows: List[int]) -> Optional[Path]:
        """Return the path to the full assembled image, stitching it back
        together from the individual pieces if it's missing (e.g. images
        added before this cog started saving full.png)."""
        full_path = image_dir / "full.png"
        if full_path.exists():
            return full_path

        boxes = Puzzle._piece_boxes(img_w, img_h, rows)
        piece_paths = [image_dir / f"piece_{i}.png" for i in range(len(boxes))]
        if not all(p.exists() for p in piece_paths):
            return None

        canvas = Image.new("RGBA", (img_w, img_h))
        for (x0, y0, _x1, _y1), piece_path in zip(boxes, piece_paths):
            with Image.open(piece_path) as piece_img:
                canvas.paste(piece_img, (x0, y0))
        canvas.save(full_path)
        return full_path

    def _build_pool_preview_image(
        self, guild_id: int, pool: dict, active_id: Optional[int]
    ) -> Optional[io.BytesIO]:
        """Build a labeled thumbnail grid of every image in the pool, so
        admins can actually tell images apart instead of relying on generic
        filenames like 'image.png'."""
        entries = sorted(pool.items(), key=lambda kv: int(kv[0]))
        if not entries:
            return None

        thumb = 140
        pad = 10
        label_h = 20
        cols = min(4, len(entries))
        rows_ct = (len(entries) + cols - 1) // cols

        cell_w = thumb + pad
        cell_h = thumb + label_h + pad
        canvas = Image.new("RGBA", (cols * cell_w + pad, rows_ct * cell_h + pad), (32, 32, 36, 255))
        draw = ImageDraw.Draw(canvas)

        for idx, (image_id_str, meta) in enumerate(entries):
            image_id = int(image_id_str)
            col, row = idx % cols, idx // cols
            x = pad + col * cell_w
            y = pad + row * cell_h

            image_dir = self._image_dir(guild_id, image_id)
            full_path = self._ensure_full_image(image_dir, meta["img_w"], meta["img_h"], meta["piece_rows"])
            if full_path is not None:
                with Image.open(full_path) as img:
                    img = img.convert("RGBA")
                    img.thumbnail((thumb, thumb))
                    offset = ((thumb - img.width) // 2, (thumb - img.height) // 2)
                    canvas.paste(img, (x + offset[0], y + offset[1]), img)
            else:
                draw.rectangle((x, y, x + thumb, y + thumb), outline=(110, 110, 118, 255), width=2)

            piece_count = sum(meta["piece_rows"])
            label = f"#{image_id} {piece_count}pc"
            if active_id is not None and image_id == active_id:
                label += " (active)"
            draw.text((x, y + thumb + 3), label, fill=(230, 230, 230, 255))

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        buf.seek(0)
        return buf

    @staticmethod
    def _build_progress_image(
        image_dir: Path, img_w: int, img_h: int, rows: List[int], owned: set
    ) -> Optional[io.BytesIO]:
        """Build a preview showing which distinct pieces a user has collected
        so far: their claimed pieces in their correct position, with a
        dark placeholder box (numbered with that piece's position, so you
        know exactly which ones you're still missing) for everything they
        don't have yet."""
        boxes = Puzzle._piece_boxes(img_w, img_h, rows)
        if not (image_dir / "piece_0.png").exists():
            return None

        canvas = Image.new("RGBA", (img_w, img_h), (32, 32, 36, 255))
        draw = ImageDraw.Draw(canvas)
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            if i in owned:
                piece_path = image_dir / f"piece_{i}.png"
                if piece_path.exists():
                    with Image.open(piece_path) as piece_img:
                        canvas.paste(piece_img, (x0, y0))
            else:
                draw.rectangle(
                    (x0 + 1, y0 + 1, x1 - 2, y1 - 2),
                    outline=(110, 110, 118, 255),
                    width=2,
                )
                label = str(i + 1)
                bbox = draw.textbbox((0, 0), label)
                text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                cx = x0 + (x1 - x0 - text_w) // 2 - bbox[0]
                cy = y0 + (y1 - y0 - text_h) // 2 - bbox[1]
                draw.text((cx, cy), label, fill=(160, 160, 170, 255))

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        buf.seek(0)
        return buf

    async def _render_status_embed(self, guild: discord.Guild, active: Optional[dict]) -> discord.Embed:
        """Build the standings embed used by the live status message. Kept
        separate from `[p]puzzle status` (which stays plain text) since the
        live version needs to be cheap to regenerate on every claim."""
        if active is None:
            return discord.Embed(
                title="Puzzle status",
                description="No puzzle is currently running.",
                color=discord.Color.greyple(),
            )

        total = sum(active["piece_rows"])
        winners_count = await self.config.guild(guild).winners_count()

        embed = discord.Embed(
            title=f"Puzzle status \N{EM DASH} image #{active['image_id']}",
            description=f"{total} distinct pieces, {active['posted_total']} posted so far (pieces repeat).",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Winners so far", value=f"{len(active['completions'])}/{winners_count}", inline=False)

        if active["inventories"]:
            ranked = sorted(active["inventories"].items(), key=lambda kv: -len(set(kv[1])))
            lines = []
            for user_id_str, pieces in ranked[:15]:
                user_id = int(user_id_str)
                member = guild.get_member(user_id)
                name = member.display_name if member else f"User {user_id}"
                marker = " \N{WHITE HEAVY CHECK MARK}" if user_id in active["completions"] else ""
                lines.append(f"{name}: {len(set(pieces))}/{total}{marker}")
            embed.add_field(name="Standings", value="\n".join(lines), inline=False)

        embed.set_footer(text="Updates automatically as pieces are claimed.")
        return embed

    async def _update_live_status(self, guild: discord.Guild):
        """Refresh the tracked live-status message, if one is set up for
        this guild. Silently gives up (and turns tracking off) if the
        message has been deleted; any other failure is logged but never
        allowed to interrupt real game logic, since this is purely
        cosmetic."""
        channel_id = await self.config.guild(guild).live_status_channel_id()
        message_id = await self.config.guild(guild).live_status_message_id()
        if channel_id is None or message_id is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        try:
            active = await self._get_active(guild)
            embed = await self._render_status_embed(guild, active)
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
        except discord.NotFound:
            await self.config.guild(guild).live_status_message_id.set(None)
        except Exception:
            log.exception("Failed to update the live puzzle status message in guild %s", guild.id)

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

        # never immediately repeat the image that just finished, as long as
        # there's another option -- this matters most right when a cycle
        # resets, since the just-finished image is back in the running pool
        last_image_id = await self.config.guild(guild).last_image_id()
        if last_image_id is not None and len(available) > 1 and last_image_id in available:
            available = [i for i in available if i != last_image_id]

        choice = random.choice(available)
        used.append(choice)
        await self.config.guild(guild).used_ids.set(used)
        await self.config.guild(guild).last_image_id.set(choice)
        return choice

    async def _roll_interval_hours(self, guild: discord.Guild) -> float:
        """Pick a fresh random wait (in hours) within the configured range,
        so the posting schedule can't be predicted/camped."""
        low = await self.config.guild(guild).interval_min_hours()
        high = await self.config.guild(guild).interval_max_hours()
        if high < low:
            low, high = high, low
        return random.uniform(low, high)

    async def _start_round(self, guild: discord.Guild, image_id: int) -> Optional[str]:
        """Sets up a fresh active round for image_id. Returns an error
        string on failure, or None on success.

        The piece layout is whatever was stored on the pool image when it
        was added (either its own explicit size, or the server's
        `next_piece_count` default at that time) — it's locked in here and
        won't change even if `[p]puzzle setpieces` is run again later while
        this round is in progress."""
        pool = await self.config.guild(guild).pool()
        meta = pool.get(str(image_id))
        if meta is None:
            return f"Image ID {image_id} is not in the pool."

        rows = meta["piece_rows"]
        total = sum(rows)
        unposted = list(range(total))
        random.shuffle(unposted)

        active = {
            "schema_version": ACTIVE_SCHEMA_VERSION,
            "image_id": image_id,
            "piece_rows": rows,
            "img_w": meta["img_w"],
            "img_h": meta["img_h"],
            "posted_total": 0,  # pieces ever posted this round, including repeats
            "last_post_ts": 0,  # 0 forces an immediate first post on the next loop tick
            # str(message_id) -> {"piece_index", "shared", "opened_ts",
            # "window_minutes", "claimants"}. In non-shared mode a piece is
            # removed from here the instant it's claimed; in shared mode it
            # stays until its claim window closes (see shared_sweep_loop).
            "open_messages": {},
            "inventories": {},  # str(user_id) -> [piece_index, ...] (may contain duplicates)
            "completions": [],  # user_ids, in the order they completed a full set
            # every distinct position, in shuffled order, still owed a guaranteed
            # first appearance -- drained before any repeats are allowed to post
            "unposted_positions": unposted,
            # re-rolled after every post, within [interval_min_hours, interval_max_hours],
            # so the schedule can't be predicted and camped
            "next_interval_hours": await self._roll_interval_hours(guild),
        }
        await self.config.guild(guild).active.set(active)
        await self._update_live_status(guild)
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
            total = sum(active["piece_rows"])

            # every distinct position must post at least once before any repeats
            # are allowed -- only after that "first pass" is exhausted do pieces
            # start posting randomly with replacement
            first_pass = bool(active["unposted_positions"])
            piece_index = active["unposted_positions"][-1] if first_pass else random.randrange(total)

            image_dir = self._image_dir(guild.id, active["image_id"])
            piece_path = image_dir / f"piece_{piece_index}.png"
            if not piece_path.exists():
                return

            shared_mode = await self.config.guild(guild).shared_mode()
            window_minutes = await self.config.guild(guild).shared_window_minutes() if shared_mode else 0

            emoji = await self.config.guild(guild).claim_emoji()
            if shared_mode:
                description = (
                    f"React with {emoji} within {window_minutes} minute(s) to claim it \N{EM DASH} "
                    f"everyone who reacts in time gets credit! (Piece position {piece_index + 1} of {total}.)"
                )
            else:
                description = f"React with {emoji} to claim it. (Piece position {piece_index + 1} of {total}.)"

            embed = discord.Embed(
                title="A new puzzle piece has appeared!",
                description=description,
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
            active["open_messages"][str(message.id)] = {
                "piece_index": piece_index,
                "shared": shared_mode,
                "opened_ts": time.time(),
                "window_minutes": window_minutes,
                "claimants": [],
            }
            active["posted_total"] += 1
            active["last_post_ts"] = time.time()
            active["next_interval_hours"] = await self._roll_interval_hours(guild)
            await self.config.guild(guild).active.set(active)

    async def _close_shared_piece_message(self, guild: discord.Guild, message_id: int, entry: dict):
        """Cosmetically close out a shared-mode piece message once its claim
        window has elapsed: show who claimed it and strip the reaction so
        late reactors don't think they can still claim it. Purely cosmetic
        -- credit was already awarded as each reaction came in, so any
        failure here is logged and otherwise ignored."""
        channel_id = await self.config.guild(guild).channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            message = await channel.fetch_message(message_id)
            claimants = entry.get("claimants", [])
            names = []
            for user_id in claimants[:15]:
                member = guild.get_member(user_id)
                names.append(member.mention if member else f"<@{user_id}>")
            if not names:
                description = "This piece's claim window closed \N{EM DASH} nobody claimed it."
            else:
                extra = len(claimants) - len(names)
                suffix = f" (+{extra} more)" if extra > 0 else ""
                description = "Claim window closed. Claimed by: " + ", ".join(names) + suffix

            new_embed = discord.Embed(
                title="Puzzle piece \N{EM DASH} claim window closed",
                description=description,
                color=discord.Color.dark_grey(),
            )
            new_embed.set_image(url="attachment://piece.png")
            await message.edit(embed=new_embed, attachments=message.attachments)
            try:
                await message.clear_reactions()
            except discord.HTTPException:
                pass
        except Exception:
            log.exception(
                "Failed to close a shared puzzle piece message in guild %s (message %s)", guild.id, message_id
            )

    async def _finish_round(self, guild: discord.Guild, winners: list):
        channel_id = await self.config.guild(guild).channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        active = await self._get_active(guild)
        image_id = active["image_id"] if active else None

        full_image_path = None
        if image_id is not None and active is not None:
            image_dir = self._image_dir(guild.id, image_id)
            full_image_path = self._ensure_full_image(
                image_dir, active["img_w"], active["img_h"], active["piece_rows"]
            )

        if winners:
            mentions = []
            for user_id in winners:
                member = guild.get_member(user_id)
                mentions.append(member.mention if member else f"<@{user_id}>")
            text = f"\N{PARTY POPPER} " + ", ".join(mentions) + " completed the puzzle!"

            if channel is not None:
                if full_image_path is not None:
                    await channel.send(text, file=discord.File(full_image_path, filename="completed.png"))
                else:
                    await channel.send(text)

            announce_channel_id = await self.config.guild(guild).announce_channel_id()
            if announce_channel_id and announce_channel_id != channel_id:
                announce_channel = guild.get_channel(announce_channel_id)
                if announce_channel is not None:
                    try:
                        if full_image_path is not None:
                            await announce_channel.send(
                                text, file=discord.File(full_image_path, filename="completed.png")
                            )
                        else:
                            await announce_channel.send(text)
                    except discord.HTTPException:
                        log.exception(
                            "Failed to post the puzzle-completed announcement in guild %s", guild.id
                        )

            role_id = await self.config.guild(guild).reward_role_id()
            if role_id:
                role = guild.get_role(role_id)
                if role is not None:
                    new_winner_ids = set(winners)

                    # this is a rotating "current champion" role: strip it from
                    # anyone who held it from a previous puzzle before handing
                    # it to this puzzle's winner(s)
                    for member in list(role.members):
                        if member.id not in new_winner_ids:
                            try:
                                await member.remove_roles(role, reason="No longer the current puzzle champion")
                            except discord.HTTPException:
                                log.exception(
                                    "Failed to remove puzzle winner role from %s in guild %s",
                                    member.id,
                                    guild.id,
                                )

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

            async with self.config.guild(guild).lifetime_stats() as stats:
                for user_id in winners:
                    entry = stats.setdefault(str(user_id), {"pieces_collected": 0, "puzzles_won": 0})
                    entry["puzzles_won"] += 1

        await self.config.guild(guild).active.set(None)
        await self._update_live_status(guild)

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
    # background loops
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
                elapsed = time.time() - active["last_post_ts"]
                if elapsed >= active["next_interval_hours"] * 3600:
                    await self._post_next_piece(guild)
            except Exception:
                # never let one guild's error kill the loop for everyone else
                log.exception("Error in puzzle background loop for guild %s", guild.id)
                continue

    @background_loop.before_loop
    async def _before_background_loop(self):
        await self.bot.wait_until_red_ready()

    @tasks.loop(seconds=SHARED_SWEEP_INTERVAL_SECONDS)
    async def shared_sweep_loop(self):
        """Closes out shared-mode pieces once their claim window has
        elapsed. Runs on its own short interval, separate from the (much
        coarser) posting-schedule loop, since claim windows are typically
        just a minute or two. Polls stored state rather than scheduling a
        per-piece timer, so it recovers correctly even if the bot restarts
        mid-window."""
        for guild in self.bot.guilds:
            to_close = []
            try:
                async with self._guild_lock(guild.id):
                    active = await self._get_active(guild)
                    if active is None:
                        continue
                    now = time.time()
                    changed = False
                    for msg_key, entry in list(active["open_messages"].items()):
                        if entry.get("shared") and entry.get("window_minutes", 0) > 0:
                            if now - entry["opened_ts"] >= entry["window_minutes"] * 60:
                                to_close.append((msg_key, entry))
                    for msg_key, _entry in to_close:
                        del active["open_messages"][msg_key]
                        changed = True
                    if changed:
                        await self.config.guild(guild).active.set(active)

                # do the Discord API calls outside the lock so it's held as
                # briefly as possible
                for msg_key, entry in to_close:
                    await self._close_shared_piece_message(guild, int(msg_key), entry)
            except Exception:
                log.exception("Error in puzzle shared-piece sweep for guild %s", guild.id)
                continue

    @shared_sweep_loop.before_loop
    async def _before_shared_sweep_loop(self):
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
            entry = active["open_messages"].get(msg_key)
            if entry is None:
                return  # not an open piece message (already claimed/closed, or unrelated)

            piece_index = entry["piece_index"]
            shared = entry["shared"]
            user_id = payload.member.id
            user_key = str(user_id)

            if shared:
                if user_id in entry["claimants"]:
                    return  # already credited for this piece; ignore repeat reacts
                entry["claimants"].append(user_id)
            else:
                active["open_messages"].pop(msg_key, None)

            is_new_distinct = piece_index not in active["inventories"].get(user_key, [])
            active["inventories"].setdefault(user_key, []).append(piece_index)
            await self.config.guild(guild).active.set(active)

            if is_new_distinct:
                async with self.config.guild(guild).lifetime_stats() as stats:
                    lstats = stats.setdefault(user_key, {"pieces_collected": 0, "puzzles_won": 0})
                    lstats["pieces_collected"] += 1

            total = sum(active["piece_rows"])

            # Update the message to show it's claimed. This is purely cosmetic —
            # any failure here must NEVER block the win-check below, so it gets
            # its own broad try/except rather than sharing one with real game logic.
            channel = guild.get_channel(payload.channel_id)
            if channel is not None:
                try:
                    message = await channel.fetch_message(payload.message_id)
                    if shared:
                        names = []
                        for cid in entry["claimants"][:15]:
                            member = guild.get_member(cid)
                            names.append(member.mention if member else f"<@{cid}>")
                        extra = len(entry["claimants"]) - len(names)
                        suffix = f" (+{extra} more)" if extra > 0 else ""
                        window_minutes = entry.get("window_minutes", 0)
                        new_embed = discord.Embed(
                            title="A new puzzle piece has appeared!",
                            description=(
                                f"React with {emoji} within {window_minutes} minute(s) to claim it \N{EM DASH} "
                                f"everyone who reacts in time gets credit! "
                                f"(Piece position {piece_index + 1} of {total}.)"
                            ),
                            color=discord.Color.green(),
                        )
                        new_embed.set_image(url="attachment://piece.png")
                        new_embed.add_field(
                            name=f"Claimed so far ({len(entry['claimants'])})",
                            value=", ".join(names) + suffix,
                            inline=False,
                        )
                    else:
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

            if len(distinct) >= total and user_id not in active["completions"]:
                active["completions"].append(user_id)
                await self.config.guild(guild).active.set(active)
                if channel is not None:
                    await channel.send(
                        f"\N{JIGSAW PUZZLE PIECE} {payload.member.mention} collected every piece! "
                        f"({len(active['completions'])}/{winners_count} winner(s) needed to end this puzzle)"
                    )
                if len(active["completions"]) >= winners_count:
                    finished_winners = list(active["completions"])

        await self._update_live_status(guild)

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
    async def puzzle_addimage(self, ctx: commands.Context, size: Optional[str] = None):
        """Add one or more images to the puzzle pool. Attach one or more
        images with this command; they'll all use the same size.

        `size` is optional and can be either:
        - a piece count, e.g. `7` (2-25 pieces, auto-arranged into rows)
        - an explicit grid, e.g. `4x4` (each side 2-10, an exact rectangle)

        If omitted, uses the server's default from `[p]puzzle setpieces`
        (or 9 pieces if that's never been set).

        Images that are byte-for-byte identical to one already in the pool
        are skipped automatically, so attaching the same file twice by
        accident won't create a duplicate entry.
        """
        if not ctx.message.attachments:
            await ctx.send("Attach one or more images with this command.")
            return

        if size is None:
            next_piece_count = await self.config.guild(ctx.guild).next_piece_count()
            rows = self._compute_layout(next_piece_count or DEFAULT_PIECE_COUNT)
        else:
            rows = self._parse_size(size)
            if rows is None:
                await ctx.send(
                    f"`size` must be either a piece count between {MIN_PIECE_COUNT} and "
                    f"{MAX_PIECE_COUNT} (e.g. `7`), or a grid like `3x3` with each side between 2 and 10."
                )
                return

        added, skipped_dupe, failed = [], [], []

        for attachment in ctx.message.attachments:
            if not (attachment.content_type or "").startswith("image/"):
                failed.append(f"{attachment.filename} (not an image)")
                continue

            data = await attachment.read()
            image_hash = hashlib.sha256(data).hexdigest()

            pool = await self.config.guild(ctx.guild).pool()
            dupe_id = next((eid for eid, meta in pool.items() if meta.get("image_hash") == image_hash), None)
            if dupe_id is not None:
                skipped_dupe.append(f"{attachment.filename} (matches existing #{dupe_id})")
                continue

            image_id = await self.config.guild(ctx.guild).next_id()
            await self.config.guild(ctx.guild).next_id.set(image_id + 1)

            try:
                out_dir = self._image_dir(ctx.guild.id, image_id)
                piece_count, img_w, img_h = self._slice_image(data, rows, out_dir)
            except Exception as e:
                failed.append(f"{attachment.filename} ({e})")
                continue

            async with self.config.guild(ctx.guild).pool() as pool:
                pool[str(image_id)] = {
                    "piece_rows": rows,
                    "img_w": img_w,
                    "img_h": img_h,
                    "added_by": ctx.author.id,
                    "filename": attachment.filename,
                    "image_hash": image_hash,
                }
            added.append(f"#{image_id} ({piece_count} pieces)")

        lines = []
        if added:
            lines.append("Added: " + ", ".join(added))
        if skipped_dupe:
            lines.append("Skipped as duplicates: " + ", ".join(skipped_dupe))
        if failed:
            lines.append("Failed: " + ", ".join(failed))
        if not lines:
            lines.append("Nothing was added.")
        await ctx.send("\n".join(lines))

    @puzzle.command(name="setpieces")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setpieces(self, ctx: commands.Context, count: int):
        """Set the default piece count for images added to the pool
        WITHOUT an explicit size.

        This only affects images added after this command runs, and (via
        those images) whichever puzzle starts next — it never changes an
        image or puzzle that already exists. To size one image
        differently, give `[p]puzzle addimage` its own size instead.
        """
        if count < MIN_PIECE_COUNT or count > MAX_PIECE_COUNT:
            await ctx.send(f"Piece count must be between {MIN_PIECE_COUNT} and {MAX_PIECE_COUNT}.")
            return
        await self.config.guild(ctx.guild).next_piece_count.set(count)
        await ctx.send(
            f"Images added from now on (without their own size) will use {count} pieces. "
            "Existing pool images and the current puzzle, if any, are unaffected."
        )

    @puzzle.command(name="setsharedmode")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setsharedmode(self, ctx: commands.Context, on_off: bool):
        """Turn shared-credit mode on or off.

        When on, everyone who reacts to a piece within the claim window
        (see `[p]puzzle setsharedwindow`) gets credit for it, not just the
        first person -- so a round can end with multiple simultaneous
        winners more easily. When off (the default), it's back to
        first-come-first-claimed.

        Only affects pieces posted AFTER this changes -- a piece already
        posted keeps whatever mode was active when it went up.
        """
        await self.config.guild(ctx.guild).shared_mode.set(on_off)
        if on_off:
            window = await self.config.guild(ctx.guild).shared_window_minutes()
            await ctx.send(
                f"Shared mode is now ON. New pieces will stay claimable by everyone for {window} minute(s)."
            )
        else:
            await ctx.send("Shared mode is now OFF. New pieces go back to first-come-first-claimed.")

    @puzzle.command(name="setsharedwindow")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setsharedwindow(self, ctx: commands.Context, minutes: float):
        """Set how many minutes a piece stays open for shared credit.

        Only matters when shared mode is on (`[p]puzzle setsharedmode`),
        and only affects pieces posted after this changes.
        """
        if minutes <= 0 or minutes > 60:
            await ctx.send("Must be greater than 0 and at most 60 minutes.")
            return
        await self.config.guild(ctx.guild).shared_window_minutes.set(minutes)
        await ctx.send(f"New shared-mode pieces will stay claimable for {minutes} minute(s).")

    @puzzle.command(name="setannouncechannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_setannouncechannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Set (or clear, if no channel given) a channel where puzzle
        completions are ALSO posted, in addition to the channel pieces are
        collected in. Useful when the collection channel is busy enough
        that a completion announcement gets buried quickly.
        """
        await self.config.guild(ctx.guild).announce_channel_id.set(channel.id if channel else None)
        if channel:
            await ctx.send(f"Puzzle completions will also be announced in {channel.mention}.")
        else:
            await ctx.send("Announcement channel cleared \N{EM DASH} completions will only post in the collection channel.")

    @puzzle.group(name="livestatus")
    async def puzzle_livestatus(self, ctx: commands.Context):
        """Manage the auto-updating live puzzle status message."""

    @puzzle_livestatus.command(name="enable")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_livestatus_enable(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Post a status message in `channel` (or the current channel) that
        automatically updates with live standings every time a piece is
        claimed, so people don't have to keep running `[p]puzzle status`.
        """
        channel = channel or ctx.channel
        active = await self._get_active(ctx.guild)
        embed = await self._render_status_embed(ctx.guild, active)
        try:
            message = await channel.send(embed=embed)
        except discord.HTTPException as e:
            await ctx.send(f"Couldn't post the status message: {e}")
            return
        await self.config.guild(ctx.guild).live_status_channel_id.set(channel.id)
        await self.config.guild(ctx.guild).live_status_message_id.set(message.id)
        await ctx.send(f"Live status enabled in {channel.mention}. It'll update automatically as pieces are claimed.")

    @puzzle_livestatus.command(name="disable")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_livestatus_disable(self, ctx: commands.Context):
        """Stop auto-updating the live status message. The last posted
        message is left in place as-is, just no longer refreshed."""
        await self.config.guild(ctx.guild).live_status_channel_id.set(None)
        await self.config.guild(ctx.guild).live_status_message_id.set(None)
        await ctx.send("Live status tracking disabled. The last posted message is left as-is.")

    @puzzle.command(name="leaderboard", aliases=["lb"])
    async def puzzle_leaderboard(self, ctx: commands.Context):
        """Show the all-time puzzle leaderboard: most puzzles won, then
        most pieces collected, across every round ever played here."""
        stats = await self.config.guild(ctx.guild).lifetime_stats()
        if not stats:
            await ctx.send("No puzzle history yet.")
            return
        ranked = sorted(
            stats.items(),
            key=lambda kv: (-kv[1].get("puzzles_won", 0), -kv[1].get("pieces_collected", 0)),
        )
        lines = ["**Puzzle leaderboard**"]
        for i, (user_id_str, s) in enumerate(ranked[:10], start=1):
            user_id = int(user_id_str)
            member = ctx.guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"
            lines.append(
                f"{i}. {name} \N{EM DASH} {s.get('puzzles_won', 0)} puzzle(s) won, "
                f"{s.get('pieces_collected', 0)} pieces collected"
            )
        await ctx.send("\n".join(lines))

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

        if any("piece_rows" not in meta for meta in pool.values()):
            await ctx.send(
                "Some images in the pool are still in the old format. Run "
                "`[p]puzzle migratepool` once to convert them, then try this again."
            )
            return

        active = await self._get_active(ctx.guild)
        active_id = active["image_id"] if active else None

        lines = []
        for image_id, meta in sorted(pool.items(), key=lambda kv: int(kv[0])):
            marker = " (active)" if active is not None and int(image_id) == active_id else ""
            piece_count = sum(meta["piece_rows"])
            lines.append(f"#{image_id}: {piece_count} pieces ({meta['filename']}){marker}")

        buf = self._build_pool_preview_image(ctx.guild.id, pool, active_id)
        if buf is not None:
            await ctx.send("\n".join(lines), file=discord.File(buf, filename="pool.png"))
        else:
            await ctx.send("\n".join(lines))

    @puzzle.command(name="migratepool")
    @checks.admin_or_permissions(manage_guild=True)
    async def puzzle_migratepool(self, ctx: commands.Context):
        """One-time cleanup: convert any pool images still using the old
        `grid_x`/`grid_y` format to the current format, and backfill
        duplicate-detection hashes for images added before that feature
        existed. Safe to run any time, including if there's nothing to do
        -- it only touches entries that actually need updating.

        Format conversion does NOT re-slice or re-crop any images; it just
        relabels their existing grid as an equivalent row layout, so
        existing piece images and any claimed pieces are untouched. Hash
        backfilling is best-effort: it's computed from the stored image
        rather than the original upload (which isn't kept around), so it
        may not catch every duplicate a freshly-added image would.
        """
        converted = 0
        skipped = []

        async with self.config.guild(ctx.guild).pool() as pool:
            for image_id_str, meta in pool.items():
                image_dir = self._image_dir(ctx.guild.id, int(image_id_str))
                changed = False

                if "piece_rows" not in meta:
                    grid_x = meta.pop("grid_x", None)
                    grid_y = meta.pop("grid_y", None)
                    if grid_x is None or grid_y is None:
                        skipped.append(image_id_str)
                        continue
                    rows = [grid_x] * grid_y

                    full_path = image_dir / "full.png"
                    if not full_path.exists():
                        piece_paths = [image_dir / f"piece_{i}.png" for i in range(grid_x * grid_y)]
                        if not all(p.exists() for p in piece_paths):
                            skipped.append(image_id_str)
                            continue
                        with Image.open(piece_paths[0]) as sample:
                            piece_w, piece_h = sample.size
                        canvas = Image.new("RGBA", (piece_w * grid_x, piece_h * grid_y))
                        for i, piece_path in enumerate(piece_paths):
                            row, col = divmod(i, grid_x)
                            with Image.open(piece_path) as piece_img:
                                canvas.paste(piece_img, (col * piece_w, row * piece_h))
                        canvas.save(full_path)

                    with Image.open(full_path) as full_img:
                        img_w, img_h = full_img.size

                    meta["piece_rows"] = rows
                    meta["img_w"] = img_w
                    meta["img_h"] = img_h
                    changed = True

                if "image_hash" not in meta:
                    full_path = image_dir / "full.png"
                    if full_path.exists():
                        meta["image_hash"] = hashlib.sha256(full_path.read_bytes()).hexdigest()
                        changed = True

                if changed:
                    converted += 1

        msg = f"Converted/updated {converted} image(s)."
        if skipped:
            msg += (
                f" Couldn't convert {len(skipped)} image(s) (missing files): "
                f"{', '.join(skipped)}. You may need to re-add those with `[p]puzzle addimage`."
            )
        await ctx.send(msg)

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

        low = await self.config.guild(ctx.guild).interval_min_hours()
        high = await self.config.guild(ctx.guild).interval_max_hours()
        await ctx.send(
            f"Puzzle started with image #{image_id}. The first piece will post shortly, "
            f"then pieces will keep posting at a random interval between {low} and {high} hour(s) apart."
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
        await self._update_live_status(ctx.guild)
        await ctx.send("Puzzle stopped and reset. The pool and settings are untouched.")

    @puzzle.command(name="skip")
    @commands.is_owner()
    async def puzzle_skip(self, ctx: commands.Context):
        """Bot owner only: immediately skip the current puzzle (no winners
        declared, no full image posted) and move on to a new random image
        from the pool. If a test run is active, it keeps going against the
        new puzzle without needing to be restarted."""
        active = await self._get_active(ctx.guild)
        if active is None:
            await ctx.send("No puzzle is currently running.")
            return
        skipped_image_id = active["image_id"]
        await self._finish_round(ctx.guild, [])
        await ctx.send(f"Skipped puzzle image #{skipped_image_id}.")

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

        total = sum(active["piece_rows"])
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
        for the current puzzle, with an image preview of your progress.
        Pieces you're still missing are numbered in the preview so you
        know exactly which ones to watch for."""
        member = member or ctx.author
        active = await self._get_active(ctx.guild)
        if active is None:
            await ctx.send("No puzzle is currently running.")
            return

        total = sum(active["piece_rows"])
        owned = set(active["inventories"].get(str(member.id), []))

        if not owned:
            await ctx.send(f"{member.display_name} hasn't collected any pieces of the current puzzle yet.")
            return

        image_dir = self._image_dir(ctx.guild.id, active["image_id"])
        buf = self._build_progress_image(image_dir, active["img_w"], active["img_h"], active["piece_rows"], owned)
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
    async def puzzle_setinterval(self, ctx: commands.Context, min_hours: float, max_hours: float):
        """Set the random range (in hours) between piece postings.

        Each piece picks a fresh random wait within this range rather than a
        fixed interval, so the schedule can't be predicted and camped.
        Example: `[p]puzzle setinterval 4 8` posts every 4-8 hours at random.
        A currently running puzzle picks up the new range on its next post.
        """
        if min_hours <= 0 or max_hours <= 0:
            await ctx.send("Both values must be greater than 0.")
            return
        if max_hours < min_hours:
            min_hours, max_hours = max_hours, min_hours
        await self.config.guild(ctx.guild).interval_min_hours.set(min_hours)
        await self.config.guild(ctx.guild).interval_max_hours.set(max_hours)
        await ctx.send(f"New pieces will post at a random interval between {min_hours} and {max_hours} hour(s).")

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
        guild_conf = self.config.guild(ctx.guild)
        channel_id = await guild_conf.channel_id()
        reward_role_id = await guild_conf.reward_role_id()
        interval_min_hours = await guild_conf.interval_min_hours()
        interval_max_hours = await guild_conf.interval_max_hours()
        claim_emoji = await guild_conf.claim_emoji()
        winners_count = await guild_conf.winners_count()
        next_piece_count = await guild_conf.next_piece_count()
        shared_mode = await guild_conf.shared_mode()
        shared_window_minutes = await guild_conf.shared_window_minutes()
        announce_channel_id = await guild_conf.announce_channel_id()
        live_status_channel_id = await guild_conf.live_status_channel_id()
        pool = await guild_conf.pool()

        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        role = ctx.guild.get_role(reward_role_id) if reward_role_id else None
        announce_channel = ctx.guild.get_channel(announce_channel_id) if announce_channel_id else None
        live_status_channel = ctx.guild.get_channel(live_status_channel_id) if live_status_channel_id else None

        lines = [
            f"Channel: {channel.mention if channel else 'not set'}",
            f"Interval: random between {interval_min_hours} and {interval_max_hours} hour(s)",
            f"Claim emoji: {claim_emoji}",
            f"Winners needed to end a puzzle: {winners_count}",
            f"Winner role: {role.name if role else 'not set'}",
            f"Default pieces for new images (no explicit size): {next_piece_count or DEFAULT_PIECE_COUNT}",
            f"Shared mode: {'ON, ' + str(shared_window_minutes) + ' minute(s) per piece' if shared_mode else 'off'}",
            f"Announcement channel: {announce_channel.mention if announce_channel else 'not set'}",
            f"Live status: {('enabled in ' + live_status_channel.mention) if live_status_channel else 'off'}",
            f"Images in pool: {len(pool)}",
        ]
        await ctx.send("\n".join(lines))
