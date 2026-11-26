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


class _Player:
    def __init__(self, uid):
        self.id, self.display_name, self.mention = uid, f"p{uid}", f"<@{uid}>"
        self.display_avatar = SimpleNamespace(url="https://x/a.png")


class _Msg:
    async def edit(self, **kw):
        pass


class _Channel:
    def __init__(self):
        self.guild = SimpleNamespace(id=1)

    async def send(self, *a, **k):
        return _Msg()


async def _fight(monkeypatch, pay_fails=False):
    """Run a whole duel against fakes; returns (cog, dispatched quest hits, challenger, opponent)."""
    from duel import duel as module

    async def no_sleep(_):
        pass

    async def currency(_):
        return "wc"

    async def can_spend(*_):
        return True

    async def withdraw(*_):
        if pay_fails:
            raise ValueError

    async def deposit(*_):
        return 0

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(module.bank, "get_currency_name", currency)
    monkeypatch.setattr(module.bank, "can_spend", can_spend)
    monkeypatch.setattr(module.bank, "withdraw_credits", withdraw)
    monkeypatch.setattr(module.bank, "deposit_credits", deposit)
    cog, _g = make()
    hits = []
    cog.bot = SimpleNamespace(dispatch=lambda event, *a: hits.append((event, a[0].id, a[1], a[2])))

    class Member:
        def __init__(self):
            self.vals = {"wins": 0, "losses": 0, "coins_won": 0, "coins_lost": 0, "last_duel": 0}

        async def __aenter__(self):
            return self.vals

        async def __aexit__(self, *exc):
            return False

        def all(self):
            return self

        async def _noop(self, *_):
            pass

        @property
        def last_duel(self):
            return SimpleNamespace(set=self._noop)

    members = {}
    cog.config = SimpleNamespace(guild=cog.config.guild, member=lambda m: members.setdefault(m.id, Member()))
    a, b = _Player(1), _Player(2)
    await cog.run_duel(_Channel(), a, b, 100)
    return cog, hits, a, b


async def test_a_finished_duel_counts_for_both_players_and_a_cancelled_one_for_nobody(monkeypatch):
    pytest.importorskip("redbot.core.bank")
    cog, hits, a, b = await _fight(monkeypatch)
    assert sorted(hits) == [("wonderland_quest", 1, "duel", 1), ("wonderland_quest", 2, "duel", 1)]
    assert not cog._active
    cog, hits, a, b = await _fight(monkeypatch, pay_fails=True)
    assert hits == []
