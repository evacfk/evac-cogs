import io

import pytest
from PIL import Image

from cardcollect import imagegen
from cardcollect.models import Card


def fake_art(color=(120, 40, 40), size=(400, 560)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_get_emoji_image_known_emoji_from_bundled_pool():
    from cardcollect.constants import EMOJI_POOL

    img = imagegen.get_emoji_image(EMOJI_POOL[0], 32)
    assert img is not None
    assert img.size == (32, 32)


def test_get_emoji_image_unknown_emoji_returns_none():
    assert imagegen.get_emoji_image("\U0001fabf", 32) is None  # not in the bundled pool


def test_render_card_returns_correct_size_and_mode():
    card = Card(1, "Alice", "Some Anime", "legendary", "1.png")
    tile = imagegen.render_card(card, fake_art(), emoji="⭐")
    assert tile.size == (400, 560)
    assert tile.mode == "RGBA"


def test_render_drop_produces_valid_png_with_all_cards():
    cards = [
        (Card(1, "A", "S", "common", "1.png"), fake_art((200, 50, 50)), "🍉"),
        (Card(2, "B", "S", "rare", "2.png"), fake_art((50, 50, 200)), "⭐"),
    ]
    buf = imagegen.render_drop(cards, is_test=False)
    img = Image.open(buf)
    img.verify()


def test_render_drop_test_mode_is_taller_for_the_banner():
    cards = [(Card(1, "A", "S", "common", "1.png"), fake_art(), "🍉")]
    normal = Image.open(imagegen.render_drop(cards, is_test=False))
    test_marked = Image.open(imagegen.render_drop(cards, is_test=True))
    assert test_marked.height > normal.height
    assert test_marked.width == normal.width


def test_render_drop_requires_at_least_one_card():
    with pytest.raises(ValueError):
        imagegen.render_drop([], is_test=False)


def test_render_gallery_valid_png():
    entries = [
        (Card(1, "A", "S", "common", "1.png"), fake_art((200, 50, 50))),
        (Card(2, "B", "S", "rare", "2.png"), fake_art((50, 50, 200))),
        (Card(3, "C", "S", "epic", "3.png"), fake_art((50, 200, 50))),
    ]
    buf = imagegen.render_gallery(entries, favorite_card_id=2)
    img = Image.open(buf)
    img.verify()


def test_render_gallery_requires_at_least_one_card():
    with pytest.raises(ValueError):
        imagegen.render_gallery([], favorite_card_id=None)
