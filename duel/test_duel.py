"""Duel settings and the pieces around them, against fakes (real Red is installed here, but Config is faked)."""
import copy
from types import SimpleNamespace

import pytest

from duel.duel import Duel
from duel.settings_panel import PanelError


class Val:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.data)
        return _get().__await__()

    async def set(self, value):
        self.data = value


class Group:
    def __init__(self, **values):
        self.vals = {k: Val(v) for k, v in values.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


def make(**guild_values):
    cog = object.__new__(Duel)
    cog._active = set()
    group = Group(**{"min_bet": 0, "max_bet": 0, "cooldown": 0, **guild_values})
    cog.config = SimpleNamespace(guild=lambda g: group)
    return cog, group


async def test_limits_save_and_report():
    cog, g = make()
    msg = await cog._apply_limits(None, None, {"min_bet": "10", "max_bet": "500", "cooldown": "30"})
    assert (g.min_bet.data, g.max_bet.data, g.cooldown.data) == (10, 500, 30)
    assert "10 to 500" in msg and "30s" in msg
    assert "no cap" in await cog._apply_limits(None, None, {"min_bet": "0", "max_bet": "0", "cooldown": "0"})


async def test_limits_refuse_junk_negatives_and_a_max_below_the_min():
    cog, g = make(min_bet=5, max_bet=50, cooldown=9)
    for values, text in (
        ({"min_bet": "x", "max_bet": "1", "cooldown": "1"}, "plain number"),
        ({"min_bet": "-1", "max_bet": "1", "cooldown": "1"}, "0 or higher"),
        ({"min_bet": "100", "max_bet": "50", "cooldown": "1"}, "below the minimum"),
        ({"min_bet": "1", "max_bet": "1"}, "plain number"),
    ):
        with pytest.raises(PanelError) as err:
            await cog._apply_limits(None, None, values)
        assert text in str(err.value)
    assert (g.min_bet.data, g.max_bet.data, g.cooldown.data) == (5, 50, 9)


async def test_duelset_opens_the_panel_and_version_reports(monkeypatch):
    from duel import duel as module
    opened, sent = {}, []

    async def fake_open(ctx, cog, specs, *, title, intro=""):
        opened.update(title=title, n=len(specs))

    monkeypatch.setattr(module, "open_panel", fake_open)
    cmd = Duel.duelset
    fn = getattr(cmd, "callback", None)
    await fn(object.__new__(Duel), "CTX")
    assert opened == {"title": "Duels", "n": 1}

    class Ctx:
        async def send(self, text, **kw):
            sent.append(text)

    ver = Duel.duelset_version
    await getattr(ver, "callback", None)(SimpleNamespace(__version__=Duel.__version__), Ctx())
    assert sent == [f"Duel v{Duel.__version__}"]


def test_the_old_setter_commands_are_gone():
    for name in ("duelset_mincost", "duelset_maxcost", "duelset_cooldown", "duelset_settings"):
        assert not hasattr(Duel, name), name
