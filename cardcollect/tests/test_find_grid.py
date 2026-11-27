"""2.18.0: the picture view of `.card find` -- a numbered 4x3 grid of the page's
cards above the list, a List view / Picture view button, and the counters that
say whether members use it."""

import io

import pytest
from PIL import Image

from cardcollect import imagegen, views
from cardcollect.constants import FIND_GRID_PAGE_SIZE
from cardcollect.models import Card
from test_cardcollect_smoke import (
    FakeAttachment,
    FakeChannel,
    FakeCtx,
    FakeGuild,
    FakeMember,
    _admin,
    cog,  # noqa: F401  (fixture)
    fake_art_bytes,
)
from test_packs import Inter as _Inter


class Inter(_Inter):
    """The pack tests' interaction, plus the `is_done` the find view asks before editing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.response.is_done = lambda: False


async def hunter_pool(cog, gid, count):
    """A guild with `count` cards that all match "hunter", and one member."""
    guild = FakeGuild(gid)
    admin = _admin(gid * 10, guild)
    member = FakeMember(gid * 10 + 1, guild)
    guild.members = {m.id: m for m in (admin, member)}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    for n in range(count):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((20 + n, 60, 90)))])
        await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series=f"Hunter {n:03d} | Hunter x Hunter")
    return guild, member, channel


async def open_find(cog, guild, member, channel):
    view = views.FindView(cog, member, "hunter")
    ctx = FakeCtx(member, guild, channel)
    await view.start(ctx)
    return view, ctx.sent[-1]


def tile_brightness(png: io.BytesIO, col: int, row: int):
    """Mean brightness of the middle of the art in one tile of a 4-column grid."""
    img = Image.open(png).convert("L")
    tw, th = imagegen.GALLERY_TILE_SIZE
    x, y = 16 + col * (tw + 16), 16 + row * (th + 16)
    box = img.crop((x + tw // 4, y + th // 3, x + 3 * tw // 4, y + th // 2))
    return sum(box.tobytes()) / (box.width * box.height)


def test_pool_grid_is_a_png_sized_for_the_cards_and_dims_the_ones_you_dont_own():
    cards = [(Card(i, f"C{i}", "S", "common", ""), fake_art_bytes((200, 200, 200))) for i in range(1, 13)]
    sheet = imagegen.render_pool_grid(cards, owned_ids=[1])
    size = Image.open(sheet).size
    assert size == (4 * 140 + 5 * 16, 3 * 196 + 4 * 16)
    owned = tile_brightness(imagegen.render_pool_grid(cards, owned_ids=[1]), 0, 0)
    missing = tile_brightness(imagegen.render_pool_grid(cards, owned_ids=[1]), 1, 0)
    assert owned > missing * 1.5


def test_pool_grid_survives_missing_and_broken_art_and_a_short_page():
    cards = [(Card(1, "A", "S", "rare", ""), None), (Card(2, "B", "S", "rare", ""), b"not an image")]
    img = Image.open(imagegen.render_pool_grid(cards))
    assert img.size == (2 * 140 + 3 * 16, 196 + 2 * 16)  # two tiles wide, not four
    with pytest.raises(ValueError):
        imagegen.render_pool_grid([])


@pytest.mark.asyncio
async def test_find_opens_on_a_picture_page_of_twelve(cog):
    guild, member, channel = await hunter_pool(cog, 701, 30)
    view, message = await open_find(cog, guild, member, channel)
    assert view.mode == "grid" and len(view.cards) == 30
    assert len(message.files) == 1 and message.files[0].filename == "pool.png"
    embed = message.embeds[0]
    assert embed.image.url == "attachment://pool.png"
    assert embed.description.count("\n") + 1 == FIND_GRID_PAGE_SIZE == 12
    assert view._page_count() == 3  # 30 matches, 12 a page
    assert [o.label.split(".")[0] for o in view.opener.options] == [str(n) for n in range(1, 13)]
    assert view.mode_button.label == "List view"


@pytest.mark.asyncio
async def test_list_view_button_drops_the_picture_and_goes_back_to_twenty_a_page(cog):
    guild, member, channel = await hunter_pool(cog, 702, 30)
    view, _message = await open_find(cog, guild, member, channel)
    view.page = 1  # cards 13-24 in the picture view
    press = Inter(member, channel)
    await view._on_mode(press)
    assert view.mode == "list" and view.page == 0  # card 13 is on the first list page of 20
    payload = await view.render()
    assert payload["file"] is None and payload["embed"].image.url is None
    assert payload["embed"].description.count("\n") + 1 == 20
    assert view.mode_button.label == "Picture view"
    assert view.opener.options[0].label == "Hunter 000"  # no numbers in the list view

    await view._on_mode(Inter(member, channel))  # and back to pictures, staying on the same cards
    assert view.mode == "grid" and view.page == 0
    assert (await view.render())["file"] is not None
    stats = await cog.config.guild(guild).stats()
    assert stats["find_list_switches"] == 1  # only the switch to the list counts


@pytest.mark.asyncio
async def test_paging_back_reuses_the_rendered_picture(cog, monkeypatch):
    guild, member, channel = await hunter_pool(cog, 703, 30)
    view, _message = await open_find(cog, guild, member, channel)
    calls = []
    real = imagegen.render_pool_grid
    monkeypatch.setattr(imagegen, "render_pool_grid", lambda *a, **k: calls.append(1) or real(*a, **k))
    await view._on_next_page(Inter(member, channel))
    await view._on_prev_page(Inter(member, channel))
    await view._on_next_page(Inter(member, channel))
    assert view.page == 1 and len(calls) == 1  # page 2 rendered once, not three times


@pytest.mark.asyncio
async def test_opening_a_card_and_going_back_keeps_the_picture_view(cog):
    guild, member, channel = await hunter_pool(cog, 704, 15)
    view, _message = await open_find(cog, guild, member, channel)
    view.opener._values = [str(view.cards[2].card_id)]
    await view._on_open(Inter(member, channel))
    assert view.card is not None and view.card.card_id == view.cards[2].card_id
    await view._on_back(Inter(member, channel))
    payload = await view.render()
    assert view.card is None and payload["file"] is not None
    assert (await cog.config.guild(guild).stats())["find_opens"] == 1


@pytest.mark.asyncio
async def test_a_single_match_still_lands_on_the_card_and_zero_matches_have_no_picture(cog):
    guild, member, channel = await hunter_pool(cog, 705, 1)
    view = views.FindView(cog, member, "hunter")
    await view.start(FakeCtx(member, guild, channel))
    assert view.card is not None
    empty = views.FindView(cog, member, "zzzz")
    ctx = FakeCtx(member, guild, channel)
    await empty.start(ctx)
    assert ctx.sent[-1].files == [] and not hasattr(empty, "mode_button")


@pytest.mark.asyncio
async def test_find_counts_searches_in_diagnostics(cog):
    guild, member, channel = await hunter_pool(cog, 706, 3)
    ctx = FakeCtx(member, guild, channel)
    await cog.card.commands["find"].callback(cog, ctx, query="hunter")
    assert (await cog.config.guild(guild).stats())["find_searches"] == 1
