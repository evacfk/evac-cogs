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


def test_pop_queued_takes_in_order():
    pool = {"5": {}, "7": {}, "9": {}}
    chosen, rest = Puzzle._pop_queued([7, 5, 9], pool)
    assert chosen == 7 and rest == [5, 9]
    chosen, rest = Puzzle._pop_queued(rest, pool)
    assert chosen == 5 and rest == [9]


def test_pop_queued_skips_retired_and_deleted():
    pool = {"5": {"retired": True}, "9": {}}
    chosen, rest = Puzzle._pop_queued([4, 5, 9], pool)
    assert chosen == 9 and rest == []


def test_pop_queued_empty_or_all_dead_returns_none():
    assert Puzzle._pop_queued([], {"1": {}}) == (None, [])
    assert Puzzle._pop_queued([5], {"5": {"retired": True}}) == (None, [])
