"""The `.duelset` panel through real discord.py components (fake interactions)."""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from duel import panel as panel_mod
from duel.settings_panel import SettingsPanel
from duel.test_duel import make


class Resp:
    def __init__(self):
        self.done, self.edited, self.modal = False, [], None

    def is_done(self):
        return self.done

    async def defer(self):
        self.done = True

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)

    async def send_message(self, *a, **k):
        self.done = True

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal


class Click:
    def __init__(self, user, values=None):
        self.user, self.data, self.response = user, {"values": values or []}, Resp()
        self.originals = []
        self.followup = SimpleNamespace(send=self._noop)

    async def _noop(self, *a, **k):
        pass

    async def edit_original_response(self, **kw):
        self.originals.append(kw)


def admin(manage=True):
    return SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=False, manage_guild=manage))


async def open_it(cog):
    a = admin()
    p = SettingsPanel(cog, SimpleNamespace(id=1), a, panel_mod.build_specs(), title="Duels", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


async def test_panel_shows_current_values_and_saves_through_the_modal():
    cog, g = make(min_bet=10, max_bet=0, cooldown=60)
    p, a = await open_it(cog)
    assert "wager 10 to no cap, cooldown 60s" in p.build_embed().description
    await p._on_setting(Click(a, ["limits"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert [modal.boxes[k].default for k in ("min_bet", "max_bet", "cooldown")] == ["10", "0", "60"]
    modal.boxes["min_bet"]._value, modal.boxes["max_bet"]._value, modal.boxes["cooldown"]._value = "5", "100", "0"
    done = Click(a)
    await modal.on_submit(done)
    assert (g.min_bet.data, g.max_bet.data, g.cooldown.data) == (5, 100, 0) and "✅" in note(done)
    modal.boxes["max_bet"]._value = "2"
    bad = Click(a)
    await modal.on_submit(bad)
    assert "⚠" in note(bad) and g.max_bet.data == 100


async def test_only_the_opener_with_manage_server_can_use_it():
    cog, g = make()
    p, a = await open_it(cog)
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    assert await p.interaction_check(Click(a)) is True
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False
