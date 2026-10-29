"""`.puzzle images` must count and list only playable (non-retired) images by default."""
from puzzle.puzzle import Puzzle


def test_split_pool_separates_playable_from_retired():
    pool = {
        "1": {"retired": True, "won_at": 1},
        "2": {"retired": False},
        "3": {},
        "4": {"retired": True},
    }
    playable, retired = Puzzle._split_pool(pool)
    assert sorted(playable) == ["2", "3"]
    assert retired == 2


def test_split_pool_all_retired_and_empty():
    playable, retired = Puzzle._split_pool({"1": {"retired": True}})
    assert playable == {} and retired == 1
    assert Puzzle._split_pool({}) == ({}, 0)
