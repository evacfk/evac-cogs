"""Wonderland pixel canvas: image rendering with Pillow. No discord/redbot imports.

`render_canvas` draws the whole 100x50 grid with labelled gridlines every 10. `render_zoom` draws a
10x10 close-up with each pixel's price printed in its cell. Both return PNG bytes. Pixel text is never
drawn: it only appears when someone inspects a pixel.
"""
from __future__ import annotations

import io
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from .canvas_store import BASE_PRICE, GRID_H, GRID_W, short_price

BLANK = (250, 250, 250)
MINOR_LINE = (236, 236, 236)
MAJOR_LINE = (170, 170, 178)
LABEL = (90, 90, 100)
MARGIN_BG = (255, 255, 255)

CELL = 10            # pixels per canvas cell in the full render
LEFT, TOP = 30, 22   # room for the axis labels
RIGHT, BOTTOM = 14, 8  # so the closing '100' and '50' labels aren't clipped
ZOOM_CELL = 64
ZOOM_SIDE = 10

# The pastel palette offered in the Paint dropdown (hex is always allowed too). 16 entries.
PASTELS = (
    ("Blush", "#FFD1DC"), ("Rose", "#FFB7C5"), ("Coral", "#FFB3A7"), ("Peach", "#FFDAB9"),
    ("Apricot", "#FFE5B4"), ("Butter", "#FFF3B0"), ("Lemon", "#FDFD96"), ("Sage", "#D4E6C3"),
    ("Mint", "#C1F0D0"), ("Seafoam", "#B5EAD7"), ("Sky", "#BDE0FE"), ("Periwinkle", "#C7CEEA"),
    ("Lavender", "#E0BBE4"), ("Lilac", "#D5AAFF"), ("Cloud", "#E8EAF0"), ("Mocha", "#DCC9B6"),
)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:   # very old Pillow: fixed-size bitmap font
        return ImageFont.load_default()


def hex_to_rgb(color: str) -> tuple:
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def text_color_for(rgb: tuple) -> tuple:
    """Black or white, whichever reads better on this background."""
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (20, 20, 28) if luminance > 150 else (250, 250, 250)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def render_canvas(colors: dict) -> bytes:
    """`colors` is {(x, y): '#rrggbb'}. Blank pixels are near-white."""
    width, height = LEFT + GRID_W * CELL + RIGHT, TOP + GRID_H * CELL + BOTTOM
    img = Image.new("RGB", (width, height), MARGIN_BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([LEFT, TOP, LEFT + GRID_W * CELL, TOP + GRID_H * CELL], fill=BLANK)
    for (x, y), color in colors.items():
        px, py = LEFT + x * CELL, TOP + y * CELL
        draw.rectangle([px, py, px + CELL - 1, py + CELL - 1], fill=hex_to_rgb(color))
    for x in range(GRID_W + 1):
        if x % 10:
            draw.line([LEFT + x * CELL, TOP, LEFT + x * CELL, TOP + GRID_H * CELL], fill=MINOR_LINE)
    for y in range(GRID_H + 1):
        if y % 10:
            draw.line([LEFT, TOP + y * CELL, LEFT + GRID_W * CELL, TOP + y * CELL], fill=MINOR_LINE)
    font = _font(12)
    for x in range(0, GRID_W + 1, 10):
        draw.line([LEFT + x * CELL, TOP - 5, LEFT + x * CELL, TOP + GRID_H * CELL], fill=MAJOR_LINE)
        draw.text((LEFT + x * CELL, 2), str(x), fill=LABEL, font=font, anchor="mt")
    for y in range(0, GRID_H + 1, 10):
        draw.line([LEFT - 5, TOP + y * CELL, LEFT + GRID_W * CELL, TOP + y * CELL], fill=MAJOR_LINE)
        draw.text((LEFT - 8, TOP + y * CELL), str(y), fill=LABEL, font=font, anchor="rm")
    return _png(img)


def zoom_window(cx: int, cy: int) -> tuple:
    """Top-left of the 10x10 window around a centre point, shifted to stay inside the grid."""
    x0 = min(max(cx - ZOOM_SIDE // 2, 0), GRID_W - ZOOM_SIDE)
    y0 = min(max(cy - ZOOM_SIDE // 2, 0), GRID_H - ZOOM_SIDE)
    return x0, y0


def render_zoom(cx: int, cy: int, lookup: Callable[[int, int], Optional[tuple]]) -> bytes:
    """10x10 close-up. `lookup(x, y)` returns (color, price) for a painted pixel or None for a blank one
    (blank cells show BASE_PRICE). The requested centre is outlined."""
    x0, y0 = zoom_window(cx, cy)
    left, top = 34, 24
    img = Image.new("RGB", (left + ZOOM_SIDE * ZOOM_CELL + 2, top + ZOOM_SIDE * ZOOM_CELL + 2), MARGIN_BG)
    draw = ImageDraw.Draw(img)
    small, price_font = _font(12), _font(15)
    for dy in range(ZOOM_SIDE):
        for dx in range(ZOOM_SIDE):
            x, y = x0 + dx, y0 + dy
            found = lookup(x, y)
            rgb = hex_to_rgb(found[0]) if found else BLANK
            price = found[1] if found else BASE_PRICE
            px, py = left + dx * ZOOM_CELL, top + dy * ZOOM_CELL
            draw.rectangle([px, py, px + ZOOM_CELL - 1, py + ZOOM_CELL - 1], fill=rgb, outline=MAJOR_LINE)
            draw.text((px + ZOOM_CELL // 2, py + ZOOM_CELL // 2), short_price(price),
                      fill=text_color_for(rgb), font=price_font, anchor="mm")
    for dx in range(ZOOM_SIDE):
        draw.text((left + dx * ZOOM_CELL + ZOOM_CELL // 2, 4), str(x0 + dx), fill=LABEL, font=small, anchor="mt")
    for dy in range(ZOOM_SIDE):
        draw.text((left - 6, top + dy * ZOOM_CELL + ZOOM_CELL // 2), str(y0 + dy), fill=LABEL, font=small, anchor="rm")
    if x0 <= cx < x0 + ZOOM_SIDE and y0 <= cy < y0 + ZOOM_SIDE:
        px, py = left + (cx - x0) * ZOOM_CELL, top + (cy - y0) * ZOOM_CELL
        draw.rectangle([px, py, px + ZOOM_CELL - 1, py + ZOOM_CELL - 1], outline=(220, 40, 80), width=3)
    return _png(img)
