from PIL import Image

from photodrop import collage


def _make_photo(path, color, size=(600, 400)):
    Image.new("RGB", size, color).save(path)
    return path


class TestGridDims:
    def test_three_photos_is_single_row(self):
        assert collage._grid_dims(3) == (3, 1)

    def test_one_photo_is_single_tile(self):
        assert collage._grid_dims(1) == (1, 1)

    def test_grows_into_a_grid_above_three(self):
        cols, rows = collage._grid_dims(7)
        assert cols * rows >= 7


class TestBuildCollage:
    def test_requires_at_least_one_photo(self):
        try:
            collage.build_collage([], "A")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_three_photo_collage_is_wide(self, tmp_path):
        paths = [_make_photo(tmp_path / f"{i}.png", c) for i, c in enumerate([(200, 0, 0), (0, 200, 0), (0, 0, 200)])]
        img = collage.build_collage(paths, "A")
        assert img.width > img.height

    def test_single_photo_collage_matches_tile_size(self, tmp_path):
        path = _make_photo(tmp_path / "0.png", (100, 100, 100))
        img = collage.build_collage([path], "A")
        assert img.size == collage.TILE_SIZE

    def test_five_photo_grid_is_taller_than_wide_ratio_grows(self, tmp_path):
        paths = [_make_photo(tmp_path / f"{i}.png", (i * 10, i * 10, i * 10)) for i in range(5)]
        img = collage.build_collage(paths, "B")
        cols, rows = collage._grid_dims(5)
        assert rows > 1
        assert img.height > collage.TILE_SIZE[1]

    def test_different_letters_produce_different_images(self, tmp_path):
        paths = [_make_photo(tmp_path / "0.png", (50, 50, 50))]
        img_a = collage.build_collage(paths, "A")
        img_b = collage.build_collage(paths, "B")
        assert img_a.tobytes() != img_b.tobytes()


class TestSaveCollage:
    def test_writes_png_to_disk(self, tmp_path):
        photo = _make_photo(tmp_path / "src.png", (10, 20, 30))
        out = tmp_path / "nested" / "out.png"
        result = collage.save_collage([photo], "C", out)
        assert result == out
        assert out.exists()
        with Image.open(out) as im:
            assert im.size == collage.TILE_SIZE


class TestGridDimsSquare:
    def test_one_day_is_single_tile(self):
        assert collage._grid_dims_square(1) == (1, 1)

    def test_three_days_packs_square_not_a_row(self):
        # unlike _grid_dims, small counts still pack square-ish for a month view
        assert collage._grid_dims_square(3) == (2, 2)

    def test_covers_every_tile(self):
        for count in [1, 5, 7, 15, 31]:
            cols, rows = collage._grid_dims_square(count)
            assert cols * rows >= count


class TestBuildMonthCollage:
    def test_requires_at_least_one_entry(self):
        try:
            collage.build_month_collage([])
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_photo_day_uses_the_photo(self, tmp_path):
        photo = _make_photo(tmp_path / "0.png", (200, 0, 0))
        img = collage.build_month_collage([(1, "full", photo)])
        assert img.size[0] >= collage.MONTH_TILE_SIZE[0]
        assert img.size[1] >= collage.MONTH_TILE_SIZE[1]

    def test_no_show_and_pto_get_placeholder_tiles_without_a_photo(self):
        # no photo_path given -- must not try to open a file
        img = collage.build_month_collage([(1, "no_show", None), (2, "pto", None)])
        assert img is not None

    def test_mixed_month_builds_without_error(self, tmp_path):
        photo = _make_photo(tmp_path / "0.png", (0, 200, 0))
        entries = [
            (1, "full", photo),
            (2, "no_show", None),
            (3, "pto", None),
            (4, "tardy", photo),
        ]
        img = collage.build_month_collage(entries)
        cols, rows = collage._grid_dims_square(4)
        assert img.size == (
            cols * collage.MONTH_TILE_SIZE[0] + (cols - 1) * collage.GAP,
            rows * collage.MONTH_TILE_SIZE[1] + (rows - 1) * collage.GAP,
        )

    def test_different_day_numbers_produce_different_images(self, tmp_path):
        photo = _make_photo(tmp_path / "0.png", (50, 50, 50))
        img_a = collage.build_month_collage([(1, "full", photo)])
        img_b = collage.build_month_collage([(21, "full", photo)])
        assert img_a.tobytes() != img_b.tobytes()


class TestSaveMonthCollage:
    def test_writes_png_to_disk(self, tmp_path):
        photo = _make_photo(tmp_path / "src.png", (10, 20, 30))
        out = tmp_path / "nested" / "cal.png"
        result = collage.save_month_collage([(1, "full", photo)], out)
        assert result == out
        assert out.exists()
