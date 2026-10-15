"""Blackjack table settings (the panel's apply methods) against a fake Config."""
import copy
from types import SimpleNamespace

import pytest

from blackjacktable.blackjacktable import BlackjackTable
from blackjacktable.settings_panel import PanelError

DEFAULTS = dict(enabled=True, min_bet=10, max_bet=1000, lobby_timeout=120, bet_timeout=60, turn_timeout=60,
                deck_count=2, penetration_pct=75, show_count=False)


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


def make(**over):
    cog = object.__new__(BlackjackTable)
    group = Group(**{**DEFAULTS, **over})
    cog.config = SimpleNamespace(guild=lambda g: group)
    return cog, group


async def test_enabled_and_count_checking_toggle():
    cog, g = make()
    assert "disabled" in await cog._apply_enabled(None, None, False) and g.enabled.data is False
    assert "enabled" in await cog._apply_enabled(None, None, True) and g.enabled.data is True
    assert "enabled" in await cog._apply_show_count(None, None, True) and g.show_count.data is True


async def test_bet_limits_validate_like_the_old_commands():
    cog, g = make()
    assert "5 to 50" in await cog._apply_bets(None, None, {"min_bet": "5", "max_bet": "50"})
    assert (g.min_bet.data, g.max_bet.data) == (5, 50)
    for values, text in (({"min_bet": "0", "max_bet": "5"}, "at least 1"), ({"min_bet": "20", "max_bet": "5"}, "at least the minimum"),
                         ({"min_bet": "x", "max_bet": "5"}, "plain number"), ({"min_bet": "1"}, "plain number")):
        with pytest.raises(PanelError) as err:
            await cog._apply_bets(None, None, values)
        assert text in str(err.value)
    assert (g.min_bet.data, g.max_bet.data) == (5, 50)


async def test_shoe_bounds_are_enforced():
    cog, g = make()
    assert "4 deck(s)" in await cog._apply_shoe(None, None, {"deck_count": "4", "penetration_pct": "80"})
    assert (g.deck_count.data, g.penetration_pct.data) == (4, 80)
    for values, text in (({"deck_count": "0", "penetration_pct": "80"}, "between 1 and 8"),
                         ({"deck_count": "9", "penetration_pct": "80"}, "between 1 and 8"),
                         ({"deck_count": "2", "penetration_pct": "5"}, "between 10 and 95"),
                         ({"deck_count": "2", "penetration_pct": "99"}, "between 10 and 95"),
                         ({"deck_count": "two", "penetration_pct": "80"}, "plain number")):
        with pytest.raises(PanelError) as err:
            await cog._apply_shoe(None, None, values)
        assert text in str(err.value)
    assert (g.deck_count.data, g.penetration_pct.data) == (4, 80)


async def test_group_opens_the_panel_and_version_reports(monkeypatch):
    from blackjacktable import blackjacktable as module
    from blackjacktable.constants import COG_VERSION
    opened, sent = {}, []

    async def fake_open(ctx, cog, specs, *, title, intro=""):
        opened.update(title=title, n=len(specs))

    monkeypatch.setattr(module, "open_panel", fake_open)
    await getattr(BlackjackTable.wonderjackset, "callback")(object.__new__(BlackjackTable), "CTX")
    assert opened == {"title": "Blackjack table", "n": 4}

    class Ctx:
        async def send(self, text, **kw):
            sent.append(text)

    await getattr(BlackjackTable.wonderjackset_version, "callback")(object.__new__(BlackjackTable), Ctx())
    assert sent == [f"BlackjackTable v{COG_VERSION}"]


def test_the_old_setter_commands_are_gone():
    for name in ("wonderjackset_enabled", "wonderjackset_minbet", "wonderjackset_maxbet", "wonderjackset_decks",
                 "wonderjackset_penetration", "wonderjackset_showcount", "wonderjackset_settings"):
        assert not hasattr(BlackjackTable, name), name
