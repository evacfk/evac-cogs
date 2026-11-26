"""Canvas images: grid size, labels, colours, the zoom window."""
import io

from PIL import Image

from gamble import canvas_render as cr
from gamble.canvas_store import BASE_PRICE, GRID_H, GRID_W, parse_color


def open_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return Image.open(io.BytesIO(data)).convert("RGB")


def cell_center(x, y):
    return cr.LEFT + x * cr.CELL + cr.CELL // 2, cr.TOP + y * cr.CELL + cr.CELL // 2


def test_canvas_image_has_the_whole_grid_and_margins():
    img = open_png(cr.render_canvas({}))
    assert img.size == (cr.LEFT + GRID_W * cr.CELL + cr.RIGHT, cr.TOP + GRID_H * cr.CELL + cr.BOTTOM)


def test_blank_pixels_are_near_white_and_painted_ones_show_their_colour():
    img = open_png(cr.render_canvas({(3, 4): "#ff0000", (99, 49): "#00ff00"}))
    assert img.getpixel(cell_center(3, 4)) == (255, 0, 0)
    assert img.getpixel(cell_center(99, 49)) == (0, 255, 0)
    assert img.getpixel(cell_center(50, 25)) == cr.BLANK


def test_gridlines_every_10_are_drawn_and_labelled():
    img = open_png(cr.render_canvas({}))
    for n in range(0, GRID_W + 1, 10):
        assert img.getpixel((cr.LEFT + n * cr.CELL, cr.TOP + 3)) == cr.MAJOR_LINE
    # the label strip above the grid holds ink (the numbers) and so does the one to the left
    top_strip = img.crop((cr.LEFT, 0, cr.LEFT + GRID_W * cr.CELL, cr.TOP - 6))
    left_strip = img.crop((0, cr.TOP, cr.LEFT - 6, cr.TOP + GRID_H * cr.CELL))
    assert top_strip.getcolors(10_000) and len(top_strip.getcolors(10_000)) > 1
    assert len(left_strip.getcolors(10_000)) > 1


def test_zoom_window_stays_inside_the_grid_with_the_centre_near_the_middle():
    assert cr.zoom_window(50, 25) == (45, 20)
    assert cr.zoom_window(0, 0) == (0, 0)
    assert cr.zoom_window(99, 49) == (90, 40)
    assert cr.zoom_window(2, 48) == (0, 40)


def test_zoom_image_shows_prices_and_outlines_the_centre():
    found = {(50, 25): ("#ff0000", 4200)}
    data = cr.render_zoom(50, 25, lambda x, y: found.get((x, y)))
    img = open_png(data)
    assert img.size == (34 + 10 * cr.ZOOM_CELL + 2, 24 + 10 * cr.ZOOM_CELL + 2)
    px, py = 34 + 5 * cr.ZOOM_CELL, 24 + 5 * cr.ZOOM_CELL          # the centre cell
    assert img.getpixel((px + 1, py + 1)) == (220, 40, 80)          # red outline
    assert img.getpixel((px + 6, py + 6)) == (255, 0, 0)            # the painted colour inside it
    inked = [c for _, c in (img.crop((px + 4, py + 4, px + cr.ZOOM_CELL - 4, py + cr.ZOOM_CELL - 4)).getcolors(10_000))]
    assert (250, 250, 250) in inked                                # the price text is drawn in white on red


def test_zoom_of_a_blank_cell_prints_the_base_price_text_colour():
    assert cr.text_color_for((250, 250, 250)) == (20, 20, 28)
    assert cr.text_color_for((10, 10, 10)) == (250, 250, 250)
    img = open_png(cr.render_zoom(5, 5, lambda x, y: None))
    assert img.size[0] > 600 and BASE_PRICE == 1000


def test_the_pastel_palette_fits_a_dropdown_and_every_entry_is_valid_hex():
    assert 1 <= len(cr.PASTELS) <= 25
    assert len({h for _, h in cr.PASTELS}) == len(cr.PASTELS)
    for name, hex_ in cr.PASTELS:
        assert parse_color(hex_) == hex_.lower() and len(name) <= 100
