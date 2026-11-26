"""Tests for the Pillow images (Victor banner, Fallen wall) and the committed scene banners."""
import io
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from hungergames import arena_art as art  # noqa: E402
from hungergames import embeds  # noqa: E402


def _size(png: bytes) -> tuple:
    return Image.open(io.BytesIO(png)).size


def test_names_lose_emoji_and_are_never_empty_or_too_long():
    assert art.clean_name("Aramoonstaz ☕👻") == "Aramoonstaz"
    assert art.clean_name("👻☕") == "Tribute" and art.clean_name("") == "Tribute"
    assert len(art.clean_name("A" * 60)) == 16 and art.clean_name("A" * 60).endswith("…")


def test_victor_banner_renders_with_and_without_an_avatar():
    avatar = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(avatar, "PNG")
    stats = [("score", 11), ("kills", 4), ("parachutes", 9), ("sponsors", 6)]
    assert _size(art.render_victor("Katniss", "The 1st Hunger Games", stats, avatar.getvalue(), "Prize 1 coins")) == (900, 420)
    assert _size(art.render_victor("👻", "The 1st Hunger Games", stats, None)) == (900, 420)
    assert _size(art.render_victor("X", "E", stats, b"this is not an image")) == (900, 420)   # a broken avatar is fine


@pytest.mark.parametrize("n", [1, 2, 3, 5, 6, 7, 12, 15])
def test_fallen_wall_fits_any_number_of_cannons(n):
    entries = [(f"Tribute {i} 🔥", None) for i in range(n)]
    w, h = _size(art.render_fallen(entries, 2, 5))
    assert 700 <= w <= 1100 and 200 <= h <= 600


def test_the_final_cannons_wall_has_its_own_title_and_does_not_crash_on_zero():
    assert _size(art.render_fallen([("A", None)], None, 1))[0] >= 700
    assert _size(art.render_fallen([], 3, 4))[0] >= 700


def test_every_scene_has_a_committed_banner_and_cards_link_to_it():
    folder = Path(__file__).parent / "assets" / "banners"
    for scene in embeds.SCENE_COLOR:
        f = folder / f"{scene}.png"
        assert f.exists() and Image.open(f).size == (900, 200), scene
        e = embeds.scene_embed({"title": "t", "scene": scene}, [])
        assert e.image.url.endswith(f"/hungergames/assets/banners/{scene}.png")
    assert embeds.scene_embed({"title": "t", "scene": ""}, []).image.url is None   # old queues: no banner, no crash
