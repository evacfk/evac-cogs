"""Draws the six static scene banners in assets/banners/ (900x200). Not loaded by the cog: run it by hand
(`python hungergames/assets/make_banners.py`) to regenerate them. The cog links to the committed PNGs."""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).parent / "banners"
W, H, SS = 900, 200, 2   # drawn at 2x and scaled down for smooth edges


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sky(top, bottom):
    im = Image.new("RGB", (W * SS, H * SS))
    d = ImageDraw.Draw(im)
    for y in range(H * SS):
        d.line((0, y, W * SS, y), fill=lerp(top, bottom, y / (H * SS)))
    return im.convert("RGBA")


def glow(im, xy, r, color, alpha=170, blur=40):
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    x, y = xy[0] * SS, xy[1] * SS
    ImageDraw.Draw(layer).ellipse((x - r * SS, y - r * SS, x + r * SS, y + r * SS), fill=color + (alpha,))
    im.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def disc(im, xy, r, color):
    d = ImageDraw.Draw(im)
    x, y = xy[0] * SS, xy[1] * SS
    d.ellipse((x - r * SS, y - r * SS, x + r * SS, y + r * SS), fill=color)


def pine(d, x, base, height, color):
    x, base, height = x * SS, base * SS, height * SS
    tiers = 5
    for i in range(tiers):
        top = base - height + i * height * 0.17
        bot = base - height + (i + 1) * height * 0.30 + height * 0.1
        half = height * 0.17 * (i + 1) * 0.9
        d.polygon([(x, top), (x - half, bot), (x + half, bot)], fill=color)
    d.rectangle((x - height * 0.03, base - height * 0.1, x + height * 0.03, base), fill=color)


def forest(im, color, seed, density=22, lo=60, hi=120, base=205):
    rng = random.Random(seed)
    d = ImageDraw.Draw(im)
    for _ in range(density):
        pine(d, rng.randint(-10, W + 10), base, rng.randint(lo, hi), color)


def ground(im, color, y=170):
    d = ImageDraw.Draw(im)
    pts = [(0, H * SS)]
    for x in range(0, W * SS + 1, 40):
        pts.append((x, (y + 6 * math.sin(x / 130)) * SS))
    pts.append((W * SS, H * SS))
    d.polygon(pts, fill=color)


def cornucopia(im, cx, base, scale, color):
    """The golden horn lying on its side, mouth to the right, drawn as a tapering curved tube."""
    d = ImageDraw.Draw(im)
    pts_top, pts_bot = [], []
    for i in range(41):
        t = i / 40
        x = cx + (t - 0.5) * 220 * scale
        curve = -math.sin(t * math.pi * 0.9) * 26 * scale + (1 - t) * 18 * scale
        r = (6 + 34 * t ** 1.4) * scale
        y = base - 40 * scale + curve
        pts_top.append((x * SS, (y - r) * SS))
        pts_bot.append((x * SS, (y + r) * SS))
    d.polygon(pts_top + pts_bot[::-1], fill=color)
    mx, my = pts_top[-1][0], (pts_top[-1][1] + pts_bot[-1][1]) / 2
    r = (pts_bot[-1][1] - pts_top[-1][1]) / 2
    d.ellipse((mx - r * 0.35, my - r, mx + r * 0.35, my + r), fill=color)


def stars(im, n, seed, ymax=110):
    rng = random.Random(seed)
    d = ImageDraw.Draw(im)
    for _ in range(n):
        x, y, r = rng.randint(0, W * SS), rng.randint(0, ymax * SS), rng.choice((1, 1, 2, 3))
        d.ellipse((x, y, x + r, y + r), fill=(255, 250, 230, rng.randint(120, 255)))


def finish(im, name):
    im = im.resize((W, H), Image.LANCZOS).convert("RGB")
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W - 1, H - 1), outline=(212, 160, 23), width=2)
    im.save(OUT / f"{name}.png", optimize=True)


def bloodbath():
    im = sky((60, 8, 10), (150, 30, 20))
    glow(im, (450, 150), 150, (255, 110, 40), 150, 60)
    forest(im, (14, 6, 8, 255), 1, 24, 70, 130)
    ground(im, (10, 4, 6, 255))
    cornucopia(im, 450, 175, 1.5, (255, 190, 60, 255))
    finish(im, "bloodbath")


def day():
    im = sky((120, 175, 215), (250, 220, 150))
    glow(im, (700, 60), 90, (255, 235, 160), 200, 50)
    disc(im, (700, 60), 30, (255, 244, 200, 255))
    forest(im, (40, 70, 55, 255), 2, 26, 50, 105)
    forest(im, (22, 48, 38, 255), 3, 20, 70, 130)
    ground(im, (15, 35, 28, 255), 178)
    finish(im, "day")


def night():
    im = sky((6, 8, 28), (30, 36, 80))
    stars(im, 140, 4)
    glow(im, (210, 60), 80, (190, 205, 255), 160, 50)
    disc(im, (210, 60), 26, (240, 244, 255, 255))
    forest(im, (8, 12, 22, 255), 5, 24, 60, 125)
    ground(im, (4, 6, 14, 255), 178)
    finish(im, "night")


def feast():
    im = sky((70, 30, 40), (210, 120, 60))
    glow(im, (450, 120), 170, (255, 170, 70), 150, 70)
    forest(im, (24, 12, 14, 255), 6, 18, 60, 110)
    ground(im, (20, 10, 10, 255), 172)
    d = ImageDraw.Draw(im)
    d.rectangle((180 * SS, 150 * SS, 720 * SS, 160 * SS), fill=(60, 30, 20, 255))      # the long table
    d.rectangle((200 * SS, 158 * SS, 210 * SS, 190 * SS), fill=(60, 30, 20, 255))
    d.rectangle((690 * SS, 158 * SS, 700 * SS, 190 * SS), fill=(60, 30, 20, 255))
    rng = random.Random(7)
    for i in range(9):                                                                   # backpacks on the table
        x = 215 + i * 56
        h = rng.randint(22, 34)
        d.rounded_rectangle((x * SS, (150 - h) * SS, (x + 34) * SS, 150 * SS), radius=8 * SS, fill=(30, 16, 14, 255))
        d.rectangle(((x + 8) * SS, (150 - h * 0.55) * SS, (x + 26) * SS, (150 - h * 0.3) * SS), fill=(212, 160, 23, 255))
    cornucopia(im, 450, 138, 1.15, (255, 190, 60, 255))
    for x in (250, 450, 650):                                                            # candles
        glow(im, (x, 138), 14, (255, 200, 90), 230, 10)
    finish(im, "feast")


def arena():
    im = sky((28, 8, 54), (80, 30, 110))
    d = ImageDraw.Draw(im)
    for x in range(-100, W + 100, 46):                                                   # the force-field lattice
        d.line((x * SS, 0, (x + 90) * SS, H * SS), fill=(190, 140, 255, 70), width=2)
        d.line(((x + 90) * SS, 0, x * SS, H * SS), fill=(190, 140, 255, 70), width=2)
    rng = random.Random(8)
    for _ in range(3):                                                                   # lightning
        x, y = rng.randint(120, 780), 0
        pts = [(x, y)]
        while y < H:
            x += rng.randint(-26, 26)
            y += rng.randint(18, 34)
            pts.append((x, y))
        glow(im, pts[len(pts) // 2], 40, (200, 160, 255), 120, 30)
        d.line([(px * SS, py * SS) for px, py in pts], fill=(250, 245, 255, 255), width=4)
    forest(im, (10, 4, 20, 255), 9, 22, 40, 80)
    ground(im, (8, 3, 16, 255), 184)
    finish(im, "arena")


def finale():
    im = sky((12, 10, 24), (70, 50, 20))
    for x in (120, 300, 600, 780):                                                       # searchlights on the horn
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon([(x * SS, H * SS), ((x - 14) * SS, 0), ((x + 14) * SS, 0)],
                                      fill=(255, 225, 140, 60))
        im.alpha_composite(layer.filter(ImageFilter.GaussianBlur(6)))
    glow(im, (450, 140), 150, (255, 200, 70), 190, 60)
    forest(im, (10, 8, 12, 255), 10, 20, 60, 110)
    ground(im, (8, 6, 8, 255), 176)
    cornucopia(im, 450, 175, 1.7, (255, 205, 80, 255))
    finish(im, "finale")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for fn in (bloodbath, day, night, feast, arena, finale):
        fn()
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))
