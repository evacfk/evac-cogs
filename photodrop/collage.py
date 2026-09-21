"""Builds the per-day collage image used for the weekly (and `.pp pollday`)
poll, with that day's poll letter burned directly into the image.

Not stored as a permanent artifact -- built on demand at poll time from the
raw photos already saved by storage.py, which remain the canonical copy.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

TILE_SIZE = (480, 480)
GAP = 6
BG_COLOR = (24, 24, 24)
LETTER_BADGE_RADIUS = 34
LETTER_BADGE_MARGIN = 12
LETTER_BADGE_BG = (255, 255, 255)
LETTER_BADGE_FG = (20, 20, 20)


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
