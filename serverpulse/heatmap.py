"""Weekday x hour heatmap rendered with Pillow (already in the stack).

Dark background so it sits well in Discord. Sequential single-ramp colour
(dark = quiet, bright = busy) with the value printed in every cell, so the
picture never relies on colour alone.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .constants import WEEKDAY_SHORT

BG = (30, 31, 34)
PANEL = (43, 45, 49)
TEXT = (219, 222, 225)
MUTED = (148, 155, 164)
EMPTY = (52, 54, 59)
# viridis-like ramp: low -> high
RAMP = [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)]

CELL_W, CELL_H = 46, 40
LEFT, TOP = 74, 92
RIGHT, BOTTOM = 24, 112

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    # the card collector ships DejaVu next to this cog when installed from the same repo
    str(Path(__file__).resolve().parent.parent / "cardcollect" / "assets" / "fonts" / "DejaVuSans-Bold.ttf"),
)


def _font(size: int):
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def ramp_color(t: float):
    t = min(1.0, max(0.0, t))
    pos = t * (len(RAMP) - 1)
    i = min(int(pos), len(RAMP) - 2)
    return _lerp(RAMP[i], RAMP[i + 1], pos - i)


def _luma(rgb) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def hour_label(h: int) -> str:
    return f"{12 if h % 12 == 0 else h % 12}{'a' if h < 12 else 'p'}"


def fmt_value(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.1f}k"
    if v >= 10:
        return f"{v:.0f}"
    return f"{v:.1f}"


def render_heatmap(
    matrix: list[list[float | None]],
    *,
    title: str,
    subtitle: str = "",
    value_label: str = "avg messages per hour",
) -> bytes:
    """7x24 matrix (Monday first, None = no data) -> PNG bytes."""
    values = [v for row in matrix for v in row if v is not None]
    vmax = max(values) if values else 0.0
    width = LEFT + CELL_W * 24 + RIGHT
    height = TOP + CELL_H * 7 + BOTTOM
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    f_title, f_sub, f_axis, f_cell = _font(26), _font(15), _font(14), _font(13)
    d.text((LEFT, 22), title, fill=TEXT, font=f_title)
    if subtitle:
        d.text((LEFT, 58), subtitle, fill=MUTED, font=f_sub)

    for h in range(24):
        x = LEFT + h * CELL_W + CELL_W / 2
        d.text((x, TOP - 8), hour_label(h), fill=MUTED, font=f_axis, anchor="ms")
    for w in range(7):
        y = TOP + w * CELL_H + CELL_H / 2
        d.text((LEFT - 12, y), WEEKDAY_SHORT[w], fill=TEXT, font=f_axis, anchor="rm")

    for w in range(7):
        for h in range(24):
            x0, y0 = LEFT + h * CELL_W, TOP + w * CELL_H
            box = (x0 + 1, y0 + 1, x0 + CELL_W - 1, y0 + CELL_H - 1)
            v = matrix[w][h]
            if v is None:
                d.rectangle(box, fill=EMPTY)
                d.text((x0 + CELL_W / 2, y0 + CELL_H / 2), "–", fill=MUTED, font=f_cell, anchor="mm")
                continue
            colour = ramp_color(v / vmax if vmax > 0 else 0.0)
            d.rectangle(box, fill=colour)
            ink = (20, 20, 20) if _luma(colour) > 140 else (240, 240, 240)
            d.text((x0 + CELL_W / 2, y0 + CELL_H / 2), fmt_value(v), fill=ink, font=f_cell, anchor="mm")

    # legend
    ly = TOP + CELL_H * 7 + 34
    lw = CELL_W * 12
    for i in range(lw):
        d.line([(LEFT + i, ly), (LEFT + i, ly + 16)], fill=ramp_color(i / (lw - 1)))
    d.text((LEFT, ly + 22), "0", fill=MUTED, font=f_axis)
    d.text((LEFT + lw, ly + 22), fmt_value(vmax), fill=MUTED, font=f_axis, anchor="ra")
    d.text((LEFT + lw + 18, ly + 8), value_label + "  ·  – = no data", fill=MUTED, font=f_axis, anchor="lm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
