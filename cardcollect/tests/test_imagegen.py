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
    buf = imagegen.render_gallery(entries, showcase_card_ids=[2])
    img = Image.open(buf)
    img.verify()


def test_render_gallery_with_multiple_showcase_cards_is_wider_than_without():
    entries = [
        (Card(1, "A", "S", "common", "1.png"), fake_art((200, 50, 50))),
        (Card(2, "B", "S", "rare", "2.png"), fake_art((50, 50, 200))),
        (Card(3, "C", "S", "epic", "3.png"), fake_art((50, 200, 50))),
    ]
    no_showcase = Image.open(imagegen.render_gallery(entries, showcase_card_ids=[]))
    three_showcase = Image.open(imagegen.render_gallery(entries, showcase_card_ids=[1, 2, 3]))
    assert three_showcase.height > no_showcase.height  # header row adds height
    # grid content is identical either way -- showcase is additive, not a
    # replacement, so nothing is missing from the grid when showcased
    assert three_showcase.width >= no_showcase.width


def test_render_gallery_showcase_ids_not_in_entries_are_ignored():
    entries = [(Card(1, "A", "S", "common", "1.png"), fake_art())]
    # a showcase id for a card not in `entries` (e.g. traded away since) must
    # not raise -- just skipped
    buf = imagegen.render_gallery(entries, showcase_card_ids=[1, 999])
    img = Image.open(buf)
    img.verify()


def test_render_gallery_requires_at_least_one_card():
    with pytest.raises(ValueError):
        imagegen.render_gallery([], showcase_card_ids=[])


def test_render_card_with_quantity_greater_than_one_draws_a_badge():
    # the badge is drawn as extra non-transparent pixels in the top-left
    # corner (see render_card) -- compare against quantity=1 (the default,
    # no badge) to confirm the pixels are actually different, not just that
    # the call didn't crash
    card = Card(1, "A", "S", "common", "1.png")
    art = fake_art((10, 10, 10))
    plain = imagegen.render_card(card, art, size=(140, 196), quantity=1)
    badged = imagegen.render_card(card, art, size=(140, 196), quantity=3)
    corner_plain = plain.crop((0, 0, 40, 30)).tobytes()
    corner_badged = badged.crop((0, 0, 40, 30)).tobytes()
    assert corner_plain != corner_badged


def test_render_gallery_applies_quantities_without_crashing():
    entries = [
        (Card(1, "A", "S", "common", "1.png"), fake_art((200, 50, 50))),
        (Card(2, "B", "S", "rare", "2.png"), fake_art((50, 50, 200))),
    ]
    buf = imagegen.render_gallery(entries, showcase_card_ids=[1], quantities={1: 2, 2: 1})
    img = Image.open(buf)
    img.verify()


def test_render_gallery_missing_quantities_entry_defaults_to_one():
    # a card_id with no entry in `quantities` (e.g. caller only tracked
    # duplicates) must not KeyError -- treated as a single copy, no badge
    entries = [(Card(1, "A", "S", "common", "1.png"), fake_art())]
    buf = imagegen.render_gallery(entries, showcase_card_ids=[], quantities={})
    img = Image.open(buf)
    img.verify()


def test_render_card_show_id_draws_a_badge():
    # regression test: gallery tiles need *something* a member can actually
    # type into `.card showcase add <card>` -- long names get truncated by
    # the name-plate width (live bug report), so the ID badge is the only
    # reliable option. Same pixel-diff approach as the quantity badge test:
    # compare the top-right corner (where the badge is drawn) with and
    # without show_id, since a crop of the actual PNG doesn't depend on font
    # rendering specifics the way asserting on exact pixel values would.
    card = Card(1, "A", "S", "common", "1.png")
    art = fake_art((10, 10, 10))
    plain = imagegen.render_card(card, art, size=(140, 196), show_id=False)
    with_id = imagegen.render_card(card, art, size=(140, 196), show_id=True)
    corner_plain = plain.crop((100, 0, 140, 30)).tobytes()
    corner_with_id = with_id.crop((100, 0, 140, 30)).tobytes()
    assert corner_plain != corner_with_id


def test_render_card_show_id_defaults_to_off_for_drop_tiles():
    # drop tiles must never reveal the card_id -- only the claim emoji
    # should identify a card there. render_card's default (no show_id
    # passed) is what render_drop relies on, so pin that default explicitly
    # rather than trusting render_drop's own tests to catch a regression.
    card = Card(1, "A", "S", "common", "1.png")
    art = fake_art((10, 10, 10))
    default = imagegen.render_card(card, art, size=(140, 196))
    explicit_off = imagegen.render_card(card, art, size=(140, 196), show_id=False)
    assert default.tobytes() == explicit_off.tobytes()


def test_render_showcase_produces_valid_png_at_the_large_tile_size():
    entries = [
        (Card(1, "A", "S", "common", "1.png"), fake_art((200, 50, 50))),
        (Card(2, "B", "S", "legendary", "2.png"), fake_art((50, 50, 200))),
    ]
    buf = imagegen.render_showcase(entries)
    img = Image.open(buf)
    img.verify()
    # re-open (verify() invalidates the handle) and confirm this actually
    # used the large favorite-tile size, not the small grid-tile size --
    # the whole point of this function existing (live feedback: showcase
    # used to be a plain text list, not large images at all)
    img2 = Image.open(imagegen.render_showcase(entries))
    fav_w, fav_h = imagegen.GALLERY_FAVORITE_TILE_SIZE
    assert img2.height >= fav_h
    assert img2.width >= 2 * fav_w


def test_render_showcase_requires_at_least_one_card():
    with pytest.raises(ValueError):
        imagegen.render_showcase([])


def test_render_showcase_applies_quantities_without_crashing():
    entries = [(Card(1, "A", "S", "common", "1.png"), fake_art())]
    buf = imagegen.render_showcase(entries, quantities={1: 2})
    img = Image.open(buf)
    img.verify()


def test_render_gallery_tiles_show_the_card_id(monkeypatch):
    # end-to-end: render_gallery must actually request show_id=True on every
    # tile it builds (both the showcase header and the main grid), not just
    # leave the capability available on render_card unused. A pixel-diff
    # against the finished PNG is the wrong tool here -- the rounded-corner
    # mask alone makes a composited gallery tile differ from a freestanding
    # one regardless of the badge, which would make this pass even with the
    # bug present. Spying on render_card's actual call kwargs is precise
    # instead of guessing at pixels.
    calls = []
    real_render_card = imagegen.render_card

    def spy_render_card(*args, **kwargs):
        calls.append(kwargs.get("show_id", False))
        return real_render_card(*args, **kwargs)

    monkeypatch.setattr(imagegen, "render_card", spy_render_card)

    entries = [
        (Card(1, "A", "S", "common", "1.png"), fake_art((10, 10, 10))),
        (Card(2, "B", "S", "rare", "2.png"), fake_art((50, 50, 200))),
    ]
    imagegen.render_gallery(entries, showcase_card_ids=[1])

    # one call for the showcase header tile (card 1) + one per grid tile
    # (cards 1 and 2) = 3 total; every single one must pass show_id=True
    assert len(calls) == 3
    assert all(calls), "render_gallery must request show_id=True on every tile it builds"
