"""Pillow-based image compositing for cardcollect: burns the claim emoji into
each card, builds the multi-card drop composite, and builds the collection
gallery (favorite-first, larger tile).

Emoji are drawn from a locally bundled 64x64 PNG set (assets/emoji_cache/,
sourced once at build time from the emoji-datasource-twitter npm package --
see that directory's NOTICE.md) rather than fetched from any CDN at
drop-time. Fonts are the bundled DejaVu Sans / DejaVu Sans Bold
(assets/fonts/) rather than relying on fonts being installed on the actual
bot host. Both follow the same "download/bundle once, never depend on a live
third-party host at runtime" rule the project settled on for AniList art
(see storage.py).

No discord/redbot imports -- this module only knows about bytes in, bytes
(PNG) out, so it's usable standalone and unit-testable with plain pytest
(modulo needing Pillow installed, same as any other imagegen-style module
in this project).
"""

import io
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from .constants import (
    CARD_IMAGE_SIZE,
    GALLERY_COLUMNS,
    GALLERY_FAVORITE_TILE_SIZE,
    GALLERY_TILE_SIZE,
)
from .models import Card

ASSETS_DIR = Path(__file__).parent / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
EMOJI_CACHE_DIR = ASSETS_DIR / "emoji_cache"

FONT_BOLD_PATH = FONTS_DIR / "DejaVuSans-Bold.ttf"
FONT_REGULAR_PATH = FONTS_DIR / "DejaVuSans.ttf"

RARITY_COLORS = {
    "common": (149, 165, 166),
    "rare": (52, 152, 219),
    "epic": (155, 89, 182),
    "legendary": (241, 196, 15),
}

CARD_BG = (30, 30, 34)
PLATE_BG = (0, 0, 0, 175)
TEXT_WHITE = (240, 240, 240)
BORDER_WIDTH = 8
CORNER_RADIUS = 18
EMOJI_BADGE_SIZE = 56
NAME_PLATE_HEIGHT = 64
DROP_PADDING = 24
TEST_BANNER_HEIGHT = 40
TEST_BANNER_COLOR = (231, 76, 60)


_font_cache: dict = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        path = FONT_BOLD_PATH if bold else FONT_REGULAR_PATH
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def _codepoints(emoji: str) -> str:
    return "-".join(f"{ord(c):x}" for c in emoji)


def get_emoji_image(emoji: str, size: int) -> Optional[Image.Image]:
    """Load a bundled emoji PNG and resize it, or None if this emoji isn't
    in the bundled cache (shouldn't happen for anything drawn from
    constants.EMOJI_POOL -- see emoji_cache/NOTICE.md)."""
    path = EMOJI_CACHE_DIR / f"{_codepoints(emoji)}.png"
    if not path.exists():
        # strip a trailing variation selector and retry once
        stripped = emoji.replace("️", "")
        path = EMOJI_CACHE_DIR / f"{_codepoints(stripped)}.png"
    if not path.exists():
        return None
    img = Image.open(path).convert("RGBA")
    return img.resize((size, size), Image.LANCZOS)


def _load_card_art(image_bytes: bytes, size: Tuple[int, int]) -> Image.Image:
    art = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    # cover-crop to the target aspect ratio, then resize
    target_w, target_h = size
    src_w, src_h = art.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        x0 = (src_w - new_w) // 2
        art = art.crop((x0, 0, x0 + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        y0 = (src_h - new_h) // 2
        art = art.crop((0, y0, src_w, y0 + new_h))
    return art.resize(size, Image.LANCZOS)


def _rounded_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), (size[0] - 1, size[1] - 1)], radius=radius, fill=255)
    return mask


def render_card(
    card: Card,
    image_bytes: bytes,
    emoji: Optional[str] = None,
    size: Tuple[int, int] = CARD_IMAGE_SIZE,
) -> Image.Image:
    """Build one card tile: art, rarity-colored border, name plate, and
    (if given) the claim emoji burned into the bottom-right corner. `emoji`
    is omitted for gallery tiles, which don't need a claim badge."""
    w, h = size
    color = RARITY_COLORS.get(card.rarity, RARITY_COLORS["common"])

    canvas = Image.new("RGBA", size, CARD_BG + (255,))
    art = _load_card_art(image_bytes, (w - BORDER_WIDTH * 2, h - BORDER_WIDTH * 2))
    canvas.paste(art, (BORDER_WIDTH, BORDER_WIDTH))

    mask = _rounded_mask(size, CORNER_RADIUS)
    rounded = Image.new("RGBA", size, (0, 0, 0, 0))
    rounded.paste(canvas, (0, 0), mask)
    canvas = rounded

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        [(BORDER_WIDTH // 2, BORDER_WIDTH // 2), (w - BORDER_WIDTH // 2, h - BORDER_WIDTH // 2)],
        radius=CORNER_RADIUS,
        outline=color,
        width=BORDER_WIDTH,
    )

    # name plate along the bottom: semi-transparent bar + name + rarity label
    plate = Image.new("RGBA", (w, NAME_PLATE_HEIGHT), (0, 0, 0, 0))
    plate_draw = ImageDraw.Draw(plate)
    plate_draw.rectangle([(0, 0), (w, NAME_PLATE_HEIGHT)], fill=PLATE_BG)
    name_font = _font(20, bold=True)
    rarity_font = _font(14, bold=True)
    plate_draw.text((14, 8), card.name, font=name_font, fill=TEXT_WHITE)
    plate_draw.text((14, 32), card.rarity.upper(), font=rarity_font, fill=color)
    canvas.alpha_composite(plate, (0, h - NAME_PLATE_HEIGHT))

    if emoji:
        badge = get_emoji_image(emoji, EMOJI_BADGE_SIZE)
        if badge is not None:
            bx = w - EMOJI_BADGE_SIZE - 14
            by = h - NAME_PLATE_HEIGHT - EMOJI_BADGE_SIZE - 10
            # small circular backing so the emoji reads clearly against any art
            backing = Image.new("RGBA", (EMOJI_BADGE_SIZE + 10, EMOJI_BADGE_SIZE + 10), (0, 0, 0, 0))
            ImageDraw.Draw(backing).ellipse(
                [(0, 0), (EMOJI_BADGE_SIZE + 10, EMOJI_BADGE_SIZE + 10)], fill=(0, 0, 0, 190)
            )
            canvas.alpha_composite(backing, (bx - 5, by - 5))
            canvas.alpha_composite(badge, (bx, by))

    return canvas


def render_drop(
    entries: Sequence[Tuple[Card, bytes, str]],
    is_test: bool = False,
) -> io.BytesIO:
    """Build the composite image posted for a drop: each (card, image_bytes,
    emoji) side by side. A test drop gets a visible red banner so it's never
    mistaken for a real one."""
    tiles = [render_card(card, image_bytes, emoji=emoji) for card, image_bytes, emoji in entries]
    if not tiles:
        raise ValueError("render_drop needs at least one card")

    tile_w, tile_h = tiles[0].size
    banner_h = TEST_BANNER_HEIGHT if is_test else 0
    total_w = tile_w * len(tiles) + DROP_PADDING * (len(tiles) + 1)
    total_h = tile_h + DROP_PADDING * 2 + banner_h

    composite = Image.new("RGBA", (total_w, total_h), (20, 20, 24, 255))
    draw = ImageDraw.Draw(composite)

    if is_test:
        draw.rectangle([(0, 0), (total_w, banner_h)], fill=TEST_BANNER_COLOR)
        banner_font = _font(22, bold=True)
        text = "TEST DROP — claims here award nothing"
        bbox = draw.textbbox((0, 0), text, font=banner_font)
        text_w = bbox[2] - bbox[0]
        draw.text(((total_w - text_w) / 2, (banner_h - (bbox[3] - bbox[1])) / 2 - bbox[1]), text, font=banner_font, fill=(255, 255, 255))

    x = DROP_PADDING
    y = banner_h + DROP_PADDING
    for tile in tiles:
        composite.alpha_composite(tile, (x, y))
        x += tile_w + DROP_PADDING

    buf = io.BytesIO()
    composite.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_gallery(
    entries: Sequence[Tuple[Card, bytes]],
    favorite_card_id: Optional[int] = None,
    columns: int = GALLERY_COLUMNS,
) -> io.BytesIO:
    """Build a member's collection gallery. The favorite card (if any and if
    present in `entries`) is rendered larger and placed first; everything
    else follows in a uniform grid, in the order given."""
    if not entries:
        raise ValueError("render_gallery needs at least one card")

    ordered: List[Tuple[Card, bytes]] = list(entries)
    favorite_entry = None
    if favorite_card_id is not None:
        for i, (card, image_bytes) in enumerate(ordered):
            if card.card_id == favorite_card_id:
                favorite_entry = ordered.pop(i)
                break

    tile_w, tile_h = GALLERY_TILE_SIZE
    fav_w, fav_h = GALLERY_FAVORITE_TILE_SIZE
    padding = 16

    rows = -(-len(ordered) // columns) if ordered else 0
    grid_w = columns * tile_w + (columns + 1) * padding
    grid_h = rows * tile_h + (rows + 1) * padding if rows else padding

    header_h = 0
    if favorite_entry is not None:
        header_h = fav_h + padding * 2

    total_w = max(grid_w, fav_w + padding * 2)
    total_h = header_h + grid_h

    canvas = Image.new("RGBA", (total_w, total_h), (20, 20, 24, 255))

    if favorite_entry is not None:
        card, image_bytes = favorite_entry
        fav_tile = render_card(card, image_bytes, size=(fav_w, fav_h))
        star_font = _font(18, bold=True)
        star_draw = ImageDraw.Draw(fav_tile)
        star_draw.text((14, fav_h - NAME_PLATE_HEIGHT - 30), "★ FAVORITE", font=star_font, fill=(241, 196, 15))
        canvas.alpha_composite(fav_tile, (padding, padding))

    for i, (card, image_bytes) in enumerate(ordered):
        col = i % columns
        row = i // columns
        tile = render_card(card, image_bytes, size=(tile_w, tile_h))
        x = padding + col * (tile_w + padding)
        y = header_h + padding + row * (tile_h + padding)
        canvas.alpha_composite(tile, (x, y))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
