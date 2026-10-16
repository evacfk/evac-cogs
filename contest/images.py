"""Photo handling for contest: re-encode every submission (drops EXIF/GPS and any hidden payload), and draw the role icons."""
from __future__ import annotations

import io

from .constants import PHOTO_MAX_SIDE, PHOTO_QUALITY

ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")


class PhotoError(ValueError):
    """The photo cannot be used; the text is shown to the member."""


def process_photo(data: bytes) -> bytes:
    """Validate and re-save as a resized, metadata-free JPEG."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise PhotoError("I couldn't read that file. Send a normal photo (jpg, png or webp). iPhone HEIC photos "
                         "need converting first: screenshot the photo or share it as a JPEG.")
    if fmt not in ALLOWED_FORMATS:
        raise PhotoError("Only jpg, png and webp photos are accepted (no GIFs or videos).")
    if getattr(img, "is_animated", False):
        raise PhotoError("Animated images are not accepted. Send a still photo.")
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.split()[-1])
        img = base
    else:
        img = img.convert("RGB")
    img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=PHOTO_QUALITY, optimize=True)  # no exif= argument: metadata is dropped
    return out.getvalue()


def pumpkin_icon(color: int, size: int = 128) -> bytes:
    """A flat pumpkin in `color`, used as the role icon when the server's boost level allows one."""
    from PIL import Image, ImageDraw

    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    dark = (int(r * 0.7), int(g * 0.7), int(b * 0.7), 255)
    fill = (r, g, b, 255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    d.ellipse((s * 0.08, s * 0.28, s * 0.50, s * 0.92), fill=fill, outline=dark, width=max(2, s // 40))
    d.ellipse((s * 0.50, s * 0.28, s * 0.92, s * 0.92), fill=fill, outline=dark, width=max(2, s // 40))
    d.ellipse((s * 0.30, s * 0.24, s * 0.70, s * 0.92), fill=fill, outline=dark, width=max(2, s // 40))
    d.rectangle((s * 0.46, s * 0.10, s * 0.54, s * 0.30), fill=(60, 110, 40, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
