from pathlib import Path

from photodrop import storage


class TestSavePhotoBytes:
    def test_saves_file_with_correct_extension(self, tmp_path):
        path = storage.save_photo_bytes(tmp_path, 123, "2026-09-21", 0, "sunset.jpg", b"fake-bytes")
        assert path.exists()
        assert path.suffix == ".jpg"
        assert path.read_bytes() == b"fake-bytes"

    def test_falls_back_to_default_extension_for_unsafe_names(self, tmp_path):
        path = storage.save_photo_bytes(tmp_path, 123, "2026-09-21", 0, "no_extension_at_all", b"x")
        assert path.suffix == f".{storage.DEFAULT_EXTENSION}"

    def test_keyed_by_member_and_date(self, tmp_path):
        path = storage.save_photo_bytes(tmp_path, 123, "2026-09-21", 0, "a.png", b"x")
        assert "123" in path.parts
        assert "2026-09-21" in path.parts

    def test_multiple_photos_same_day_dont_collide(self, tmp_path):
        p0 = storage.save_photo_bytes(tmp_path, 1, "2026-09-21", 0, "a.png", b"first")
        p1 = storage.save_photo_bytes(tmp_path, 1, "2026-09-21", 1, "b.png", b"second")
        assert p0 != p1
        assert p0.read_bytes() == b"first"
        assert p1.read_bytes() == b"second"


class TestListPhotos:
    def test_empty_when_nothing_saved(self, tmp_path):
        assert storage.list_photos(tmp_path, 1, "2026-09-21") == []

    def test_returns_sorted_saved_photos(self, tmp_path):
        storage.save_photo_bytes(tmp_path, 1, "2026-09-21", 2, "c.png", b"c")
        storage.save_photo_bytes(tmp_path, 1, "2026-09-21", 0, "a.png", b"a")
        storage.save_photo_bytes(tmp_path, 1, "2026-09-21", 1, "b.png", b"b")
        photos = storage.list_photos(tmp_path, 1, "2026-09-21")
        assert [p.name for p in photos] == ["0.png", "1.png", "2.png"]

    def test_different_days_dont_mix(self, tmp_path):
        storage.save_photo_bytes(tmp_path, 1, "2026-09-21", 0, "a.png", b"a")
        storage.save_photo_bytes(tmp_path, 1, "2026-09-22", 0, "b.png", b"b")
        assert len(storage.list_photos(tmp_path, 1, "2026-09-21")) == 1
        assert len(storage.list_photos(tmp_path, 1, "2026-09-22")) == 1

    def test_sorts_numerically_past_nine_photos(self, tmp_path):
        # lexical sort would put "10.png"/"11.png" before "2.png"
        for i in [0, 1, 10, 11, 2, 3, 4, 5, 6, 7, 8, 9]:
            storage.save_photo_bytes(tmp_path, 1, "2026-09-21", i, f"{i}.png", str(i).encode())
        photos = storage.list_photos(tmp_path, 1, "2026-09-21")
        assert [p.name for p in photos] == [f"{i}.png" for i in range(12)]
