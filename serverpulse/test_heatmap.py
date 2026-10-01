import io

from PIL import Image

from serverpulse import heatmap


def matrix(fill=None):
    return [[fill for _ in range(24)] for _ in range(7)]


def test_renders_a_valid_png_of_stable_size():
    m = matrix(1.0)
    m[2][21] = 120.0
    png = heatmap.render_heatmap(m, title="t", subtitle="s")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.size == (heatmap.LEFT + heatmap.CELL_W * 24 + heatmap.RIGHT, heatmap.TOP + heatmap.CELL_H * 7 + heatmap.BOTTOM)


def test_all_none_and_all_zero_do_not_crash():
    assert heatmap.render_heatmap(matrix(None), title="empty")[:4] == b"\x89PNG"
    assert heatmap.render_heatmap(matrix(0.0), title="zeros")[:4] == b"\x89PNG"


def test_busiest_cell_is_brighter_than_quiet_cell():
    m = matrix(1.0)
    m[0][0] = 100.0
    img = Image.open(io.BytesIO(heatmap.render_heatmap(m, title="t"))).convert("RGB")

    def px(w, h):
        return img.getpixel((heatmap.LEFT + h * heatmap.CELL_W + 3, heatmap.TOP + w * heatmap.CELL_H + 3))

    assert sum(px(0, 0)) > sum(px(3, 12))


def test_no_data_cells_get_the_empty_colour():
    m = matrix(5.0)
    m[1][1] = None
    img = Image.open(io.BytesIO(heatmap.render_heatmap(m, title="t"))).convert("RGB")
    assert img.getpixel((heatmap.LEFT + heatmap.CELL_W + 3, heatmap.TOP + heatmap.CELL_H + 3)) == heatmap.EMPTY


def test_ramp_endpoints_and_clamping():
    assert heatmap.ramp_color(0) == heatmap.RAMP[0]
    assert heatmap.ramp_color(1) == heatmap.RAMP[-1]
    assert heatmap.ramp_color(-5) == heatmap.RAMP[0] and heatmap.ramp_color(9) == heatmap.RAMP[-1]


def test_labels_and_value_format():
    assert [heatmap.hour_label(h) for h in (0, 1, 11, 12, 13, 23)] == ["12a", "1a", "11a", "12p", "1p", "11p"]
    assert heatmap.fmt_value(0.34) == "0.3" and heatmap.fmt_value(12.4) == "12" and heatmap.fmt_value(1500) == "1.5k"
