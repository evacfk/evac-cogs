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
from typing import Optional, Sequence, Tuple

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
    quantity: int = 1,
) -> Image.Image:
    """Build one card tile: art, rarity-colored border, name plate, and
    (if given) the claim emoji burned into the bottom-right corner. `emoji`
    is omitted for gallery tiles, which don't need a claim badge.

    `quantity` > 1 draws a small "×N" badge in the top-left corner -- used by
    the gallery to show a member holds a tradeable spare copy (see
    constants.MAX_COPIES_KEPT) without rendering a second, redundant tile
    for the same character. Drop tiles never pass a quantity, so this never
    shows up outside the gallery."""
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

    if quantity > 1:
        qty_font = _font(16, bold=True)
        qty_text = f"×{quantity}"
        qty_draw = ImageDraw.Draw(canvas)
        bbox = qty_draw.textbbox((0, 0), qty_text, font=qty_font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 6
        badge_w, badge_h = text_w + pad * 2, text_h + pad * 2
        qty_badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
        qty_bd = ImageDraw.Draw(qty_badge)
        qty_bd.rounded_rectangle([(0, 0), (badge_w - 1, badge_h - 1)], radius=8, fill=(0, 0, 0, 190))
        qty_bd.text((pad - bbox[0], pad - bbox[1]), qty_text, font=qty_font, fill=TEXT_WHITE)
        canvas.alpha_composite(qty_badge, (10, 10))

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
    showcase_card_ids: Sequence[int] = (),
    columns: int = GALLERY_COLUMNS,
    quantities: Optional[dict] = None,
) -> io.BytesIO:
    """Build a member's collection gallery. Showcase cards (up to a few, in
    showcase order) render larger in a header row; the full grid below still
    includes every owned card -- showcased ones too -- so the grid alone
    stays a complete view of the collection rather than "everything except
    what's pinned up top".

    `entries` should have exactly one (Card, image_bytes) pair per *unique*
    owned card_id -- a member holding a tradeable spare (see
    constants.MAX_COPIES_KEPT) still gets one tile, not two identical ones.
    `quantities`, if given, maps card_id -> how many copies the member
    holds; any id with a count > 1 gets a small "xN" badge on its tile
    instead of a duplicate tile."""
    if not entries:
        raise ValueError("render_gallery needs at least one card")

    quantities = quantities or {}
    by_id = {card.card_id: (card, image_bytes) for card, image_bytes in entries}
    showcase_entries = [by_id[cid] for cid in showcase_card_ids if cid in by_id]

    tile_w, tile_h = GALLERY_TILE_SIZE
    fav_w, fav_h = GALLERY_FAVORITE_TILE_SIZE
    padding = 16

    rows = -(-len(entries) // columns)
    grid_w = columns * tile_w + (columns + 1) * padding
    grid_h = rows * tile_h + (rows + 1) * padding

    header_h = 0
    header_w = 0
    if showcase_entries:
        header_h = fav_h + padding * 2
        header_w = len(showcase_entries) * fav_w + (len(showcase_entries) + 1) * padding

    total_w = max(grid_w, header_w)
    total_h = header_h + grid_h

    canvas = Image.new("RGBA", (total_w, total_h), (20, 20, 24, 255))

    if showcase_entries:
        star_font = _font(18, bold=True)
        for i, (card, image_bytes) in enumerate(showcase_entries):
            tile = render_card(card, image_bytes, size=(fav_w, fav_h), quantity=quantities.get(card.card_id, 1))
            star_draw = ImageDraw.Draw(tile)
            star_draw.text((14, fav_h - NAME_PLATE_HEIGHT - 30), "★ SHOWCASE", font=star_font, fill=(241, 196, 15))
            x = padding + i * (fav_w + padding)
            canvas.alpha_composite(tile, (x, padding))

    for i, (card, image_bytes) in enumerate(entries):
        col = i % columns
        row = i // columns
        tile = render_card(card, image_bytes, size=(tile_w, tile_h), quantity=quantities.get(card.card_id, 1))
        x = padding + col * (tile_w + padding)
        y = header_h + padding + row * (tile_h + padding)
        canvas.alpha_composite(tile, (x, y))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
