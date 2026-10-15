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

from bumpreward import panel as panel_mod
from bumpreward.bumpreward import BumpReward, DEFAULT_GUILD
from bumpreward.settings_panel import SettingsPanel


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


class Group:
    def __init__(self, **values):
        self.vals = {k: Val(v) for k, v in {**DEFAULT_GUILD, **values}.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


class Group:
    def __init__(self, **values):
        self.vals = {k: Val(v) for k, v in {**DEFAULT_GUILD, **values}.items()}
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

    def get_role(self, rid):
        return None


def make(**values):
    cog = object.__new__(BumpReward)
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
    p = SettingsPanel(cog, Guild(), a, panel_mod.build_specs(), title="Bump rewards", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


def test_specs_fit_discord_limits():
    specs = panel_mod.build_specs()
    keys = [s.key for s in specs]
    assert len(keys) == len(set(keys)) == 8
    assert all(len(s.fields) <= 5 for s in specs)
    assert {s.tab for s in specs} == {"Basics", "Rewards", "Reminders"}


async def test_panel_shows_values_and_reward_modal_saves_and_rejects():
    cog, g = make(channel_id=10)
    p, a = await open_it(cog)
    text = p.build_embed().description
    assert "<#10>" in text and "none (no pings)" in text or "MISSING" in text
    await p._on_tab(Click(a, ["Rewards"]))
    await p._on_setting(Click(a, ["reward"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert [modal.boxes[k].default for k in ("min", "max")] == ["100", "250"]
    modal.boxes["min"]._value, modal.boxes["max"]._value = "10", "20"
    done = Click(a)
    await modal.on_submit(done)
    assert "\u2705" in note(done) and (g.min_reward.data, g.max_reward.data) == (10, 20)
    modal.boxes["min"]._value = "-5"
    bad = Click(a)
    await modal.on_submit(bad)
    assert "\u26a0" in note(bad) and g.min_reward.data == 10


async def test_mode_choice_saves():
    cog, g = make()
    p, a = await open_it(cog)
    await p._on_setting(Click(a, ["mode"]))
    pick = Click(a, ["pro"])
    await p._on_choice(pick)
    assert g.mode.data == "pro" and "\u2705" in note(pick)


async def test_only_the_opener_with_manage_server_can_use_it():
    cog, g = make()
    p, a = await open_it(cog)
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    assert await p.interaction_check(Click(a)) is True
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False
