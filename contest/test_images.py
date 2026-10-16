import io

import pytest

pytest.importorskip("PIL")
from PIL import Image

from contest import images


def _img(fmt="JPEG", size=(3000, 2000), mode="RGB", exif=False):
    im = Image.new(mode, size, (200, 50, 50) if mode == "RGB" else (200, 50, 50, 128))
    buf = io.BytesIO()
    kw = {}
    if exif:
        e = Image.Exif()
        e[0x010F] = "SecretCamera"          # Make
        e[0x8825] = {1: "N", 2: (37.0, 0.0, 0.0)}   # GPS IFD
        kw["exif"] = e
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


def test_photo_is_resized_and_reencoded_as_jpeg():
    out = images.process_photo(_img())
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG" and max(im.size) <= 1600


def test_exif_and_gps_are_stripped():
    raw = _img(exif=True)
    assert b"SecretCamera" in raw
    out = images.process_photo(raw)
    assert b"SecretCamera" not in out
    assert not Image.open(io.BytesIO(out)).getexif()


def test_png_with_alpha_is_accepted():
    out = images.process_photo(_img("PNG", (400, 300), "RGBA"))
    assert Image.open(io.BytesIO(out)).mode == "RGB"


def test_garbage_and_gifs_are_refused():
    with pytest.raises(images.PhotoError):
        images.process_photo(b"not an image")
    with pytest.raises(images.PhotoError, match="jpg, png and webp"):
        images.process_photo(_img("GIF", (50, 50), "RGB"))


def test_pumpkin_icon_is_a_png():
    out = images.pumpkin_icon(0xCD7F32)
    assert out[:8] == b"\x89PNG\r\n\x1a\n"
