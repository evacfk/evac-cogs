"""A won minigame tells daily quests (wonderland) once; a loss doesn't."""
import copy
from types import SimpleNamespace

import pytest

stats = pytest.importorskip("minigamehub.stats")


class _Games:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self.store

    async def __aexit__(self, *exc):
        return False


class _Config:
    def __init__(self):
        self.data = {}

    def member(self, m):
        store = self.data.setdefault(m.id, {})
        return SimpleNamespace(games=lambda: _Games(store))


async def test_a_win_dispatches_one_quest_event_and_a_loss_none():
    sent = []
    member = SimpleNamespace(id=1, _state=SimpleNamespace(dispatch=lambda *a: sent.append(a)))
    config = _Config()
    await stats.record_result(config, member, "mathdrop", good=True)
    await stats.record_result(config, member, "hunt", good=False)
    assert sent == [("wonderland_quest", member, "minigame", 1)]
    assert config.data[1]["mathdrop"]["good"] == 1 and config.data[1]["hunt"]["bad"] == 1


async def test_a_member_without_connection_state_is_fine():
    config = _Config()
    await stats.record_result(config, SimpleNamespace(id=2), "pet", good=True)
    assert copy.deepcopy(config.data[2]["pet"]["good"]) == 1
