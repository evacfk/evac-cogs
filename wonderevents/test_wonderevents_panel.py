"""Settings for photodrop: the `_apply_*` methods and the `.pp set` panel (real Red + discord.py)."""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)
try:
    from redbot.core import commands as _red_commands
    _real_red = hasattr(_red_commands.Cog, "listener")
except ImportError:
    _real_red = False
if not _real_red:
    pytest.skip("real Red not installed", allow_module_level=True)

from wonderevents import constants, panel as panel_mod
from wonderevents.settings_panel import NEW_ITEM, SettingsPanel
from wonderevents.wonderevents import WonderEvents


class Val:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return self.data
        return _get().__await__()

    async def set(self, value):
        self.data = value

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *exc):
        return False


class Group:
    def __init__(self, **values):
        self.vals = {k: Val(v) for k, v in {**constants.DEFAULT_GUILD, **values}.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


class Group:
    def __init__(self, **values):
        self.vals = {k: Val(v) for k, v in {**constants.DEFAULT_GUILD, **values}.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.chans = {10: SimpleNamespace(id=10, mention="<#10>")}

    def get_channel(self, cid):
        return self.chans.get(cid)


def make(**values):
    cog = object.__new__(WonderEvents)
    group = Group(**values)
    cog.config = SimpleNamespace(guild=lambda g: group)
    return cog, group


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


def admin():
    return SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=False, manage_guild=True))


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


async def open_it(cog):
    a = admin()
    p = SettingsPanel(cog, Guild(), a, panel_mod.build_specs(), title="Events", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


async def test_panel_lists_builtin_kinds_and_values():
    cog, g = make(channel_id=10)
    p, a = await open_it(cog)
    text = p.build_embed().description
    assert "<#10>" in text and "60 min before start" in text
    items = await panel_mod._kinds_get(cog, Guild())
    assert [i[0] for i in items] == ["movie", "game", "event"] and "3h" in items[0][2]


async def test_kinds_add_edit_remove_through_the_panel():
    cog, g = make()
    p, a = await open_it(cog)
    await p._on_setting(Click(a, ["kinds"]))
    await p._on_item(Click(a, [NEW_ITEM]))
    click = Click(a)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert set(modal.boxes) == {"key", "emoji", "hours"} and modal.boxes["hours"].default == "3"
    modal.boxes["key"]._value, modal.boxes["emoji"]._value, modal.boxes["hours"]._value = "trivia", "", "2"
    done = Click(a)
    await modal.on_submit(done)
    assert (await g.kinds())["trivia"]["duration"] == 120 and "\u2705" in note(done)
    # editing an existing kind never asks for the key again
    await p._on_item(Click(a, ["trivia"]))
    click = Click(a)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert set(modal.boxes) == {"emoji", "hours"} and modal.boxes["hours"].default == "2"
    modal.boxes["hours"]._value = "0.1"
    bad = Click(a)
    await modal.on_submit(bad)
    assert "\u26a0" in note(bad) and (await g.kinds())["trivia"]["duration"] == 120
    await p._on_item_remove(Click(a))
    assert "trivia" not in await g.kinds()


async def test_only_the_opener_with_manage_server_can_use_it():
    cog, g = make()
    p, a = await open_it(cog)
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    assert await p.interaction_check(Click(a)) is True
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False
