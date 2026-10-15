"""The `.wonderjackset` panel through real discord.py components (fake interactions)."""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from blackjacktable import panel as panel_mod
from blackjacktable.settings_panel import SettingsPanel
from blackjacktable.test_settings import make


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
    p = SettingsPanel(cog, SimpleNamespace(id=1), a, panel_mod.build_specs(), title="Blackjack table", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


async def test_every_setting_renders_with_current_values():
    cog, g = make()
    p, a = await open_it(cog)
    text = p.build_embed().description
    assert "10 to 1,000" in text and "2 deck(s), reshuffle at 75% dealt" in text
    for spec in p.tab_specs():
        await p._on_setting(Click(a, [spec.key]))
        assert len(p.to_components()) <= 5


async def test_toggles_save():
    cog, g = make()
    p, a = await open_it(cog)
    await p._on_setting(Click(a, ["enabled"]))
    await p._make_toggle(False)(Click(a))
    assert g.enabled.data is False
    await p._on_setting(Click(a, ["show_count"]))
    await p._make_toggle(True)(Click(a))
    assert g.show_count.data is True


async def test_bets_and_shoe_modals_are_prefilled_and_validated():
    cog, g = make()
    p, a = await open_it(cog)
    await p._on_setting(Click(a, ["bets"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert (modal.boxes["min_bet"].default, modal.boxes["max_bet"].default) == ("10", "1000")
    modal.boxes["min_bet"]._value, modal.boxes["max_bet"]._value = "20", "10"
    bad = Click(a)
    await modal.on_submit(bad)
    assert "⚠" in note(bad) and g.min_bet.data == 10
    modal.boxes["max_bet"]._value = "500"
    await modal.on_submit(Click(a))
    assert (g.min_bet.data, g.max_bet.data) == (20, 500)
    await p._on_setting(Click(a, ["shoe"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    modal.boxes["deck_count"]._value, modal.boxes["penetration_pct"]._value = "6", "60"
    await modal.on_submit(Click(a))
    assert (g.deck_count.data, g.penetration_pct.data) == (6, 60)


async def test_only_the_opener_with_manage_server_can_use_it():
    cog, g = make()
    p, a = await open_it(cog)
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    assert await p.interaction_check(Click(a)) is True
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False
