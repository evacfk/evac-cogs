"""Builds the per-day collage image used for the weekly (and `.pp pollday`)
poll, with that day's poll letter burned directly into the image.

Not stored as a permanent artifact -- built on demand at poll time from the
raw photos already saved by storage.py, which remain the canonical copy.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .constants import STATUS_NO_SHOW, STATUS_PTO

TILE_SIZE = (480, 480)
GAP = 6
BG_COLOR = (24, 24, 24)
LETTER_BADGE_RADIUS = 34
LETTER_BADGE_MARGIN = 12
LETTER_BADGE_BG = (255, 255, 255)
LETTER_BADGE_FG = (20, 20, 20)

# Month-calendar collage: many more tiles than a day/poll collage (up to one
# per day of the month), so tiles are much smaller to keep the final image
# legible-sized and under Discord's upload limit.
MONTH_TILE_SIZE = (220, 220)
MONTH_STATUS_TILE_COLORS = {
    STATUS_NO_SHOW: (110, 30, 30),
    STATUS_PTO: (30, 90, 70),
}
MONTH_STATUS_TILE_LABELS = {
    STATUS_NO_SHOW: "NO SHOW",
    STATUS_PTO: "PTO",
}


def _grid_dims(count: int) -> tuple[int, int]:
    """Near-square grid (cols, rows) for `count` tiles.

    3 photos -> a single row of 3 (matches the design doc's "1x3" example);
    anything larger grows into a proper grid rather than one long strip.
    """
    if count <= 3:
        return count, 1
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    return cols, rows


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def _fit_tile(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        return ImageOps.fit(im, TILE_SIZE, method=Image.LANCZOS)


def build_collage(photo_paths: list[Path], letter: str) -> Image.Image:
    """Build one collage image from `photo_paths`, with `letter` burned into
    the top-left corner so the image is self-identifying even if it ever
    gets separated from the poll options that reference it.
    """
    if not photo_paths:
        raise ValueError("build_collage requires at least one photo")

    cols, rows = _grid_dims(len(photo_paths))
    tile_w, tile_h = TILE_SIZE
    canvas_w = cols * tile_w + (cols - 1) * GAP
    canvas_h = rows * tile_h + (rows - 1) * GAP
    canvas = Image.new("RGB", (canvas_w, canvas_h), BG_COLOR)

    for i, path in enumerate(photo_paths):
        tile = _fit_tile(path)
        col, row = i % cols, i // cols
        x = col * (tile_w + GAP)
        y = row * (tile_h + GAP)
        canvas.paste(tile, (x, y))

    draw = ImageDraw.Draw(canvas)
    cx = LETTER_BADGE_MARGIN + LETTER_BADGE_RADIUS
    cy = LETTER_BADGE_MARGIN + LETTER_BADGE_RADIUS
    draw.ellipse(
        (cx - LETTER_BADGE_RADIUS, cy - LETTER_BADGE_RADIUS, cx + LETTER_BADGE_RADIUS, cy + LETTER_BADGE_RADIUS),
        fill=LETTER_BADGE_BG,
    )
    font = _load_font(int(LETTER_BADGE_RADIUS * 1.15))
    bbox = draw.textbbox((0, 0), letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), letter, fill=LETTER_BADGE_FG, font=font)

    return canvas


def save_collage(photo_paths: list[Path], letter: str, out_path: Path) -> Path:
    image = build_collage(photo_paths, letter)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG")
    return out_path


# ----------------------------------------------------------------------
# Month calendar collage
# ----------------------------------------------------------------------


def _grid_dims_square(count: int) -> tuple[int, int]:
    """Near-square grid for `count` tiles, no small-count special case --
    unlike `_grid_dims`, a month view with 1-3 elapsed days should still
    pack as a small square-ish block rather than force a wide single row.
    """
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    return cols, rows


def _fit_tile_sized(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        return ImageOps.fit(im, size, method=Image.LANCZOS)


def _placeholder_tile(size: tuple[int, int], bg_color: tuple[int, int, int], label: str) -> Image.Image:
    """A solid-color tile with a centered text label, standing in for a day
    with a recorded status but no photo (no-show, PTO).
    """
    tile = Image.new("RGB", size, bg_color)
    draw = ImageDraw.Draw(tile)
    font = _load_font(max(14, size[0] // 8))
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = size[0] / 2, size[1] / 2
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), label, fill=(255, 255, 255), font=font)
    return tile


def _burn_corner_badge(tile: Image.Image, text: str) -> None:
    """Burn a small day-number badge into a tile's top-left corner, scaled
    to the tile's own size (month tiles are much smaller than day/poll
    tiles, so the fixed LETTER_BADGE_* constants would be oversized here).
    """
    radius = max(14, min(tile.size) // 7)
    margin = max(4, radius // 3)
    draw = ImageDraw.Draw(tile)
    cx = margin + radius
    cy = margin + radius
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=LETTER_BADGE_BG)
    font_size = int(radius * (1.5 if len(text) == 1 else 1.05))
    font = _load_font(font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), text, fill=LETTER_BADGE_FG, font=font)


def build_month_collage(day_entries: list[tuple[int, str, Path | None]]) -> Image.Image:
    """Build a compact grid collage for `.pp calendar`.

    `day_entries` is [(day_of_month, status, photo_path_or_None), ...],
    already filtered by the caller to only the days that have *some*
    recorded status this month (full/tardy/no_show/pto) -- days with no
    entry at all (before the job started, or not yet elapsed) aren't
    passed in, so the grid is exactly as full as the month is so far, with
    no blank calendar cells.

    Each day is one tile: its first submitted photo if it has one (full/
    tardy), or a colored placeholder labeled with its status if it doesn't
    (no-show/PTO). Every tile gets its day-of-month burned into the corner
    so it's identifiable independent of its position in the grid.
    """
    if not day_entries:
        raise ValueError("build_month_collage requires at least one day entry")

    cols, rows = _grid_dims_square(len(day_entries))
    tile_w, tile_h = MONTH_TILE_SIZE
    canvas_w = cols * tile_w + (cols - 1) * GAP
    canvas_h = rows * tile_h + (rows - 1) * GAP
    canvas = Image.new("RGB", (canvas_w, canvas_h), BG_COLOR)

    for i, (day_num, status, photo_path) in enumerate(day_entries):
        if photo_path is not None:
            tile = _fit_tile_sized(photo_path, MONTH_TILE_SIZE)
        else:
            color = MONTH_STATUS_TILE_COLORS.get(status, (60, 60, 60))
            label = MONTH_STATUS_TILE_LABELS.get(status, status.upper())
            tile = _placeholder_tile(MONTH_TILE_SIZE, color, label)
        _burn_corner_badge(tile, str(day_num))

        col, row = i % cols, i // cols
        x = col * (tile_w + GAP)
        y = row * (tile_h + GAP)
        canvas.paste(tile, (x, y))

    return canvas


def save_month_collage(day_entries: list[tuple[int, str, Path | None]], out_path: Path) -> Path:
    image = build_month_collage(day_entries)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG", optimize=True)
    return out_path
