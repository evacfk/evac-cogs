"""Pillow-based image compositing for cardcollect: builds the numbered
multi-card drop composite, the collection gallery (favorite-first, larger
tile), single-card renders, and the importseries contact sheet.

Fonts are the bundled DejaVu Sans / DejaVu Sans Bold (assets/fonts/) rather
than relying on fonts being installed on the actual bot host -- the same
"bundle once, never depend on a live third-party host at runtime" rule the
project settled on for AniList art (see storage.py).

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


def _draw_number_badge(canvas: Image.Image, number: int) -> None:
    """Draw the drop's card number (the number on that card's button) in a dark
    circle at the top-left corner of `canvas`."""
    size = 56
    badge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.ellipse([(0, 0), (size - 1, size - 1)], fill=(0, 0, 0, 215), outline=TEXT_WHITE, width=3)
    bd.text((size // 2, size // 2), str(number), font=_font(34, bold=True), fill=TEXT_WHITE, anchor="mm")
    canvas.alpha_composite(badge, (14, 14))


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
    number: Optional[int] = None,
    size: Tuple[int, int] = CARD_IMAGE_SIZE,
    quantity: int = 1,
    show_id: bool = False,
) -> Image.Image:
    """Build one card tile: art, rarity-colored border, name plate, and
    (if given) the drop's card `number` in a circle at the top-left, matching the
    number on that card's button. `number` is omitted for gallery tiles.

    `quantity` > 1 draws a small "×N" badge in the top-left corner -- used by
    the gallery to show a member holds a tradeable spare copy (see
    constants.MAX_COPIES_KEPT) without rendering a second, redundant tile
    for the same character. Drop tiles never pass a quantity, so this never
    shows up outside the gallery.

    `show_id` draws a small "#<card_id>" badge in the top-right corner --
    used by the gallery so a member has something to actually type into
    `.card showcase add <card>` (live bug report: card names get truncated
    by the name-plate width, e.g. "Isuzu Sento..." for "Isuzu Sento(mi)ya",
    and the gallery previously showed neither the full name nor the ID, so
    there was nothing usable to type). Drop tiles never pass this -- the
    drop number is the only thing that should identify a card there."""
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

    if number:
        _draw_number_badge(canvas, number)

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

    if show_id:
        id_font = _font(16, bold=True)
        id_text = f"#{card.card_id}"
        id_draw = ImageDraw.Draw(canvas)
        bbox = id_draw.textbbox((0, 0), id_text, font=id_font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 6
        badge_w, badge_h = text_w + pad * 2, text_h + pad * 2
        id_badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
        id_bd = ImageDraw.Draw(id_badge)
        id_bd.rounded_rectangle([(0, 0), (badge_w - 1, badge_h - 1)], radius=8, fill=(0, 0, 0, 190))
        id_bd.text((pad - bbox[0], pad - bbox[1]), id_text, font=id_font, fill=TEXT_WHITE)
        canvas.alpha_composite(id_badge, (w - badge_w - 10, 10))

    return canvas


def render_drop(
    entries: Sequence[Tuple[Card, bytes]],
    is_test: bool = False,
) -> io.BytesIO:
    """Build the composite image posted for a drop: the cards side by side,
    numbered 1, 2, 3... to match their buttons. A test drop gets a visible red
    banner so it's never mistaken for a real one."""
    tiles = [render_card(card, image_bytes, number=i + 1) for i, (card, image_bytes) in enumerate(entries)]
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
        text = "TEST DROP — picks here award nothing"
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
    showcase_pool: Optional[Sequence[Tuple[Card, bytes]]] = None,
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
    instead of a duplicate tile.

    `showcase_pool` is where showcased cards are looked up for the header
    row; it defaults to `entries`. Callers paginating the grid (one page's
    worth of `entries` per image) must pass the member's *full* collection
    here -- otherwise a showcased card that isn't on the current page would
    silently vanish from the header instead of just not appearing in that
    page's grid."""
    if not entries:
        raise ValueError("render_gallery needs at least one card")

    quantities = quantities or {}
    showcase_pool = entries if showcase_pool is None else showcase_pool
    by_id = {card.card_id: (card, image_bytes) for card, image_bytes in showcase_pool}
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
        for i, (card, image_bytes) in enumerate(showcase_entries):
            tile = render_card(
                card, image_bytes, size=(fav_w, fav_h), quantity=quantities.get(card.card_id, 1), show_id=True
            )
            _draw_showcase_star(tile, fav_h)
            x = padding + i * (fav_w + padding)
            canvas.alpha_composite(tile, (x, padding))

    for i, (card, image_bytes) in enumerate(entries):
        col = i % columns
        row = i // columns
        tile = render_card(
            card, image_bytes, size=(tile_w, tile_h), quantity=quantities.get(card.card_id, 1), show_id=True
        )
        x = padding + col * (tile_w + padding)
        y = header_h + padding + row * (tile_h + padding)
        canvas.alpha_composite(tile, (x, y))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def _draw_showcase_star(tile: Image.Image, tile_h: int) -> None:
    """Stamp the "★ SHOWCASE" label onto an already-rendered card tile.
    Shared by render_gallery's header row and render_showcase, below."""
    star_font = _font(18, bold=True)
    star_draw = ImageDraw.Draw(tile)
    star_draw.text((14, tile_h - NAME_PLATE_HEIGHT - 30), "★ SHOWCASE", font=star_font, fill=(241, 196, 15))


def render_showcase(
    entries: Sequence[Tuple[Card, bytes]],
    quantities: Optional[dict] = None,
) -> io.BytesIO:
    """Build a standalone showcase image: just the member's pinned cards,
    at the same large size render_gallery uses for its header row, side by
    side -- no grid of the rest of the collection underneath.

    `.card showcase` used to answer with a plain text list ("`303` — Zero
    Two"), which was the whole point of the feature ("shown larger" per the
    design doc) in name only -- live feedback: "this should show large
    versions of the character. what kind of showcase is this". This is
    what `.card showcase` (no subcommand) now renders instead; the grid
    view of the *entire* collection, showcase included, stays `.card`'s
    job (decision #16 -- the grid never excludes showcased cards, so
    nothing is lost by not repeating them here).

    `entries` must be given in the caller's intended display order (i.e.
    `MemberState.showcase_card_ids` order) -- unlike render_gallery, there's
    no separate showcase_card_ids parameter to reorder by, since every
    entry here *is* a showcase entry."""
    if not entries:
        raise ValueError("render_showcase needs at least one card")

    quantities = quantities or {}
    fav_w, fav_h = GALLERY_FAVORITE_TILE_SIZE
    padding = 16

    total_w = len(entries) * fav_w + (len(entries) + 1) * padding
    total_h = fav_h + padding * 2

    canvas = Image.new("RGBA", (total_w, total_h), (20, 20, 24, 255))

    for i, (card, image_bytes) in enumerate(entries):
        tile = render_card(
            card, image_bytes, size=(fav_w, fav_h), quantity=quantities.get(card.card_id, 1), show_id=True
        )
        _draw_showcase_star(tile, fav_h)
        x = padding + i * (fav_w + padding)
        canvas.alpha_composite(tile, (x, padding))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_single(card: Card, image_bytes: bytes, quantity: int = 1) -> io.BytesIO:
    """One card at full size with its "#<id>" badge, for `.card show`.
    Never carries a drop number -- that only exists on drop tiles."""
    tile = render_card(card, image_bytes, quantity=quantity, show_id=True)
    buf = io.BytesIO()
    tile.convert("RGBA").save(buf, format="PNG")
    buf.seek(0)
    return buf


def valid_art(image_bytes: bytes) -> bool:
    """True if Pillow can fully decode these bytes as an image. Gate for any
    art an admin uploads: a file that isn't really an image would otherwise be
    saved and then break every drop and gallery that tries to draw the card."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
        return True
    except Exception:
        return False


SHEET_TILE = (150, 210)
SHEET_CAPTION_HEIGHT = 20
SHEET_GAP = 10
SHEET_BG = (32, 34, 37)
SHEET_FLAG = (231, 76, 60)
SHEET_NEW = (46, 204, 113)


def render_contact_sheet(tiles: Sequence[dict], columns: int = 5) -> io.BytesIO:
    """A numbered grid of candidate card art, cropped exactly the way the real
    card will crop it, so an admin can spot bad images before importing. Each
    tile is {"number", "name", "image" (bytes or None), "flag" (str or None),
    "skipped" (bool), "replaced" (bool)}. Flagged art gets a red border, art
    the admin uploaded a green one, skipped characters are dimmed with an X."""
    tw, th = SHEET_TILE
    cell_h = th + SHEET_CAPTION_HEIGHT
    rows = max(1, -(-len(tiles) // columns))
    width = columns * tw + (columns + 1) * SHEET_GAP
    height = rows * cell_h + (rows + 1) * SHEET_GAP
    sheet = Image.new("RGBA", (width, height), SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    label_font = _font(14, bold=True)
    caption_font = _font(12)
    for i, tile in enumerate(tiles):
        x = SHEET_GAP + (i % columns) * (tw + SHEET_GAP)
        y = SHEET_GAP + (i // columns) * (cell_h + SHEET_GAP)
        art = None
        if tile.get("image"):
            try:
                art = _load_card_art(tile["image"], SHEET_TILE)
            except Exception:
                art = None
        if art is None:
            art = Image.new("RGBA", SHEET_TILE, (60, 63, 68, 255))
            ImageDraw.Draw(art).text((tw // 2, th // 2), "no image", fill=TEXT_WHITE, font=caption_font, anchor="mm")
        if tile.get("skipped"):
            art = Image.blend(art, Image.new("RGBA", SHEET_TILE, (0, 0, 0, 255)), 0.7)
            ImageDraw.Draw(art).text((tw // 2, th // 2), "X", fill=TEXT_WHITE, font=_font(64, bold=True), anchor="mm")
        sheet.alpha_composite(art, (x, y))
        border = SHEET_NEW if tile.get("replaced") else SHEET_FLAG if tile.get("flag") else None
        if border is not None and not tile.get("skipped"):
            draw.rectangle((x, y, x + tw - 1, y + th - 1), outline=border, width=4)
        draw.rectangle((x, y, x + 30, y + 24), fill=(0, 0, 0))
        draw.text((x + 15, y + 12), str(tile["number"]), fill=TEXT_WHITE, font=label_font, anchor="mm")
        name = str(tile.get("name", ""))
        while name and draw.textlength(name, font=caption_font) > tw and len(name) > 1:
            name = name[:-2].rstrip() + "…"
        draw.text((x, y + th + 3), name, fill=TEXT_WHITE, font=caption_font)
    out = io.BytesIO()
    sheet.convert("RGB").save(out, format="PNG")
    out.seek(0)
    return out
