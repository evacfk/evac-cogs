from cardcollect import storage


def test_save_read_delete_roundtrip(tmp_path):
    assert storage.card_image_exists(tmp_path, 111, 1) is False

    storage.save_card_image(tmp_path, 111, 1, b"fake-png-bytes")
    assert storage.card_image_exists(tmp_path, 111, 1) is True
    assert storage.read_card_image(tmp_path, 111, 1) == b"fake-png-bytes"

    storage.delete_card_image(tmp_path, 111, 1)
    assert storage.card_image_exists(tmp_path, 111, 1) is False
    assert storage.read_card_image(tmp_path, 111, 1) is None


def test_list_card_ids_numeric_not_lexical_sort(tmp_path):
    # the exact bug photodrop shipped once: '10.png' sorting before '2.png'
    for card_id in (2, 10, 1, 20, 3):
        storage.save_card_image(tmp_path, 222, card_id, b"x")

    assert storage.list_card_ids(tmp_path, 222) == [1, 2, 3, 10, 20]


def test_list_card_ids_ignores_non_numeric_files(tmp_path):
    storage.save_card_image(tmp_path, 333, 1, b"x")
    junk_dir = storage.card_image_dir(tmp_path, 333)
    (junk_dir / "not_a_card.png").write_bytes(b"junk")

    assert storage.list_card_ids(tmp_path, 333) == [1]


def test_list_card_ids_separate_per_guild(tmp_path):
    storage.save_card_image(tmp_path, 1, 1, b"a")
    storage.save_card_image(tmp_path, 2, 1, b"b")
    assert storage.list_card_ids(tmp_path, 1) == [1]
    assert storage.read_card_image(tmp_path, 1, 1) != storage.read_card_image(tmp_path, 2, 1)
