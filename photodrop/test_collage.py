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
