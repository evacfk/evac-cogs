import time

from tgfeed.store import PostedStore


def test_roundtrip_delete_prune_count(tmp_path):
    store = PostedStore(str(tmp_path / "posted.db"))
    store.add(100, 5, 7, [11, 12], ts=time.time() - 1000)
    store.add(101, 5, 7, [13], ts=time.time())
    assert store.get(100) == {"channel_id": 5, "topic_id": 7, "tg_ids": [11, 12], "ts": store.get(100)["ts"]}
    assert store.get(999) is None
    assert store.count() == 2
    assert store.prune(time.time() - 500) == 1 and store.count() == 1
    store.delete(101)
    assert store.count() == 0


def test_survives_reopen(tmp_path):
    path = str(tmp_path / "posted.db")
    PostedStore(path).add(1, 2, 3, [4])
    assert PostedStore(path).get(1)["tg_ids"] == [4]
