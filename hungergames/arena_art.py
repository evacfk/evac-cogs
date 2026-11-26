"""Pillow images for hungergames: the Victor banner and the Fallen wall. Rendering only: no Config, no game rules.

Everything here is synchronous and CPU-bound, so the cog runs it in an executor. Names are stripped of emoji
(the bundled font cannot draw them) and a missing or broken avatar becomes a placeholder, so a render never fails
because of a member. If Pillow is not installed the cog skips the images and posts text only.
"""
from __future__ import annotations

import io
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONTS = Path(__file__).parent / "assets" / "fonts"
SERIF = FONTS / "DejaVuSerif-Bold.ttf"
SANS = FONTS / "DejaVuSans-Bold.ttf"

GOLD = (212, 160, 23)
RED = (178, 34, 34)
BG1, BG2 = (16, 14, 24), (38, 22, 30)
CREAM = (250, 240, 215)
MUTED = (190, 180, 160)

_EMOJI = re.compile("[\U0001F000-\U0001FAFF←-⇿⌀-⏿①-⓿■-➿⤀-⯿︎️‍⃣]")
_fonts: dict = {}


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(path), size)
    return _fonts[key]


def clean_name(name: str, limit: int = 16) -> str:
    """The name without emoji, shortened to fit under an avatar. Never empty."""
    out = " ".join(_EMOJI.sub("", name or "").split())
    if not out:
        return "Tribute"
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _background(w: int, h: int) -> Image.Image:
    """The dark vignette backdrop. Computed small and scaled up: per-pixel Python on the full size is slow."""
    sw, sh = max(1, w // 10), max(1, h // 10)
    small = Image.new("RGB", (sw, sh), BG1)
    px = small.load()
    for y in range(sh):
        t = y / sh
        for x in range(sw):
            u = abs(x / sw - 0.5) * 2
            k = max(0.0, 1 - (t * 0.9 + u * 0.6))
            px[x, y] = tuple(int(BG1[i] + (BG2[i] - BG1[i]) * k) for i in range(3))
    return small.resize((w, h), Image.BICUBIC).convert("RGBA")


def _frame(im: Image.Image) -> None:
    d = ImageDraw.Draw(im)
    w, h = im.size
    d.rectangle((10, 10, w - 11, h - 11), outline=GOLD, width=2)
    d.rectangle((18, 18, w - 19, h - 19), outline=(120, 90, 15), width=1)


def _circle(im: Image.Image, size: int) -> Image.Image:
    im = im.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    im.putalpha(mask.resize((size, size), Image.LANCZOS))
    return im


def _placeholder(name: str, size: int = 256) -> Image.Image:
    rng = random.Random(name)
    c1 = tuple(rng.randint(50, 190) for _ in range(3))
    c2 = tuple(rng.randint(50, 190) for _ in range(3))
    small = Image.new("RGB", (16, 16))
    px = small.load()
    for y in range(16):
        for x in range(16):
            k = (x + y) / 30
            px[x, y] = tuple(int(c1[i] * (1 - k) + c2[i] * k) for i in range(3))
    im = small.resize((size, size), Image.BICUBIC)
    d = ImageDraw.Draw(im)
    d.text((size / 2, size / 2), clean_name(name, 2)[0].upper(), font=_font(SANS, size // 2), fill=(240, 230, 215),
           anchor="mm")
    return im


def avatar_image(data: bytes | None, name: str, size: int) -> Image.Image:
    """A round avatar from raw image bytes, or a placeholder if there are none or they are not an image."""
    img = None
    if data:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            img = None
    return _circle(img if img is not None else _placeholder(name), size)


def _ring(im: Image.Image, xy: tuple, size: int, color: tuple, width: int = 6) -> None:
    x, y = xy
    ImageDraw.Draw(im).ellipse((x - width, y - width, x + size + width, y + size + width), outline=color, width=width)


def _grey(av: Image.Image) -> Image.Image:
    alpha = av.getchannel("A")
    g = ImageOps.grayscale(av.convert("RGB")).point(lambda v: int(v * 0.55)).convert("RGBA")
    g.putalpha(alpha)
    return g


def _text(im: Image.Image, xy: tuple, text: str, path: Path, size: int, fill: tuple, anchor: str = "mm",
          shadow: bool = False) -> None:
    d = ImageDraw.Draw(im)
    f = _font(path, size)
    if shadow:
        d.text((xy[0] + 2, xy[1] + 2), text, font=f, fill=(0, 0, 0), anchor=anchor)
    d.text(xy, text, font=f, fill=fill, anchor=anchor)


def _png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def render_victor(name: str, edition: str, stats: list, avatar: bytes | None, footer: str = "") -> bytes:
    """The Victor banner, 900x420. `stats` is [(label, value), ...] (up to four); `footer` is one small line."""
    w, h = 900, 420
    im = _background(w, h)
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((w / 2 - 230, 40, w / 2 + 230, 400), fill=(212, 160, 23, 90))
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(50)))
    _frame(im)
    rng = random.Random(name)
    d = ImageDraw.Draw(im)
    for _ in range(60):   # sparks
        x, y = rng.randint(30, w - 30), rng.randint(30, h - 30)
        d.ellipse((x, y, x + 3, y + 3), fill=(255, 220, 120, rng.randint(80, 220)))
    size, x, y = 220, 70, 120
    _ring(im, (x, y), size, GOLD, 9)
    im.alpha_composite(avatar_image(avatar, name, size), (x, y))
    _text(im, (x + size / 2, y - 40), "VICTOR", SERIF, 30, GOLD)
    tx = x + size + 70
    _text(im, (tx, 120), clean_name(name, 18), SERIF, 52, CREAM, "lm", shadow=True)
    _text(im, (tx, 175), clean_name(edition, 40), SANS, 22, GOLD, "lm")
    for i, (label, value) in enumerate(stats[:4]):
        sx = tx + i * 118
        _text(im, (sx, 255), str(value), SERIF, 42, CREAM, "lm")
        _text(im, (sx, 292), str(label).upper(), SANS, 12, MUTED, "lm")
    if footer:
        _text(im, (tx, 345), clean_name(footer, 48), SANS, 17, MUTED, "lm")
    return _png(im)


def render_fallen(entries: list, night: int | None, remain: int) -> bytes:
    """The Fallen wall: greyscale avatars in red rings, up to 12. `entries` is [(name, avatar bytes or None)]."""
    entries = entries[:12]
    n = max(1, len(entries))
    cols = min(n, 6)
    rows = (n + cols - 1) // cols
    size = 150 if cols <= 3 else 120 if cols <= 4 else 104
    gap = 60 if cols <= 4 else 36
    w = max(700, 80 + cols * size + (cols - 1) * gap)
    h = 150 + rows * (size + 70)
    im = _background(w, h)
    _frame(im)
    _text(im, (w / 2, 56), "THE FINAL CANNONS" if night is None else f"THE FALLEN  -  NIGHT {night}", SERIF, 32, GOLD,
          shadow=True)
    plural = "s" if len(entries) != 1 else ""
    _text(im, (w / 2, 96), f"{len(entries)} cannon{plural}. {remain} remain.", SANS, 17, MUTED)
    x0 = (w - (cols * size + (cols - 1) * gap)) // 2
    for i, (name, data) in enumerate(entries):
        r, c = divmod(i, cols)
        x, y = x0 + c * (size + gap), 130 + r * (size + 70)
        _ring(im, (x, y), size, RED, 5)
        im.alpha_composite(_grey(avatar_image(data, name, size)), (x, y))
        _text(im, (x + size / 2, y + size + 24), clean_name(name, 14), SANS, 17, (235, 225, 210))
    return _png(im)
