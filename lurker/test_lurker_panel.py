"""Settings for photodrop: the `_apply_*` methods and the `.pp set` panel (real Red + discord.py)."""
from types import SimpleNamespace

import pytest

import discord
import discord.ext.commands

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)
try:
    from redbot.core import commands as _red_commands
    _real_red = hasattr(_red_commands.Cog, "listener")
except ImportError:
    _real_red = False
if not _real_red:
    pytest.skip("real Red not installed", allow_module_level=True)

import copy

from lurker import panel as panel_mod
from lurker.lurker import Lurker
from lurker.settings_panel import NEW_ITEM, PanelError, SettingsPanel

DEFAULTS = dict(
    lurker_role_id=None, lurker_channel_id=None, welcome_channel_id=912520841613418596, exempt_role_ids=[],
    threshold_days=30, enabled=False, last_sweep_ts=0, report_channel_id=None, report_interval_days=7,
    last_report_ts=0, sweep_max=250,
)


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
        self.data = copy.deepcopy(value)

    async def __aenter__(self):
        self.obj = copy.deepcopy(self.data)
        return self.obj

    async def __aexit__(self, *exc):
        if exc[0] is None:
            self.data = self.obj
        return False


class Group:
    def __init__(self):
        for k, v in DEFAULTS.items():
            setattr(self, k, Val(copy.deepcopy(v)))


class Role(SimpleNamespace):
    @property
    def mention(self):
        return f"<@&{self.id}>"


class Chan(SimpleNamespace):
    @property
    def mention(self):
        return f"<#{self.id}>"


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.roles = [Role(id=7, name="RoleA"), Role(id=8, name="RoleB"), Role(id=9, name="Mods")]
        self.chans = {10: Chan(id=10), 11: Chan(id=11)}

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_channel(self, cid):
        return self.chans.get(cid)


def make():
    cog = object.__new__(Lurker)
    group = Group()
    cog.config = SimpleNamespace(guild=lambda g: group)
    return cog, group, Guild()


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


async def open_it(cog, guild):
    a = admin()
    p = SettingsPanel(cog, guild, a, panel_mod.build_specs(), title="Lurker", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


async def test_role_channel_exempt_and_sweep_rules():
    cog, g, guild = make()
    for fn in (cog._apply_lurker_role, cog._apply_lurker_channel):
        with pytest.raises(PanelError):
            await fn(guild, None, None)
    await cog._apply_lurker_role(guild, None, guild.roles[2])
    await cog._apply_lurker_channel(guild, None, guild.chans[10])
    assert (g.lurker_role_id.data, g.lurker_channel_id.data) == (9, 10)
    await cog._apply_exempt(guild, None, [guild.roles[2], guild.roles[0]])
    assert g.exempt_role_ids.data == [9, 7]
    await cog._apply_exempt(guild, None, [])
    assert g.exempt_role_ids.data == []
    for bad in ({"days": "0", "limit": "10"}, {"days": "30", "limit": "0"}, {"days": "30", "limit": "5001"},
                {"days": "x", "limit": "10"}, {"days": "5000", "limit": "10"}, {}):
        with pytest.raises(PanelError):
            await cog._apply_sweep_rules(guild, None, bad)
    assert (g.threshold_days.data, g.sweep_max.data) == (30, 250)
    await cog._apply_sweep_rules(guild, None, {"days": "45", "limit": "100"})
    assert (g.threshold_days.data, g.sweep_max.data) == (45, 100)


async def test_the_sweep_cannot_be_turned_on_half_configured():
    cog, g, guild = make()
    with pytest.raises(PanelError, match="role and the reactivation channel"):
        await cog._apply_sweep_enabled(guild, None, True)
    await cog._apply_lurker_role(guild, None, guild.roles[2])
    with pytest.raises(PanelError):
        await cog._apply_sweep_enabled(guild, None, True)
    await cog._apply_lurker_channel(guild, None, guild.chans[10])
    msg = await cog._apply_sweep_enabled(guild, None, True)
    assert g.enabled.data is True and g.last_sweep_ts.data > 0 and "No exempt roles" in msg
    await cog._apply_sweep_enabled(guild, None, False)
    assert g.enabled.data is False


async def test_reports_and_welcome_settings():
    cog, g, guild = make()
    await cog._apply_report_channel(guild, None, guild.chans[11])
    assert g.report_channel_id.data == 11 and g.last_report_ts.data > 0
    first = g.last_report_ts.data
    await cog._apply_report_channel(guild, None, guild.chans[10])
    assert g.last_report_ts.data == first  # moving the channel doesn't restart the clock
    await cog._apply_report_channel(guild, None, None)
    assert g.report_channel_id.data is None
    for bad in ({"days": "0"}, {"days": "31"}, {"days": "x"}):
        with pytest.raises(PanelError):
            await cog._apply_report_days(guild, None, bad)
    await cog._apply_report_days(guild, None, {"days": "14"})
    assert g.report_interval_days.data == 14
    await cog._apply_welcome_channel(guild, None, None)
    assert g.welcome_channel_id.data is None


async def test_panel_tabs_and_roles_through_widgets():
    cog, g, guild = make()
    p, a = await open_it(cog, guild)
    assert set(p.tabs) == {"Lurkers", "Reports"}
    for tab in p.tabs:
        await p._on_tab(Click(a, [tab]))
        for spec in p.tab_specs():
            await p._on_setting(Click(a, [spec.key]))
            assert len(p.to_components()) <= 5 and p.build_embed().description
    await p._on_tab(Click(a, ["Lurkers"]))
    await p._on_setting(Click(a, ["exempt"]))
    sel = next(c for c in p.children if isinstance(c, discord.ui.RoleSelect))
    assert sel.max_values == 25
    sel._values = [guild.roles[2]]
    await p._on_pick_value(Click(a))
    assert g.exempt_role_ids.data == [9]
    await p._on_clear(Click(a))
    assert g.exempt_role_ids.data == []
    await p._on_setting(Click(a, ["rules"]))
    click = Click(a)
    await p._on_edit_modal(click)
    assert click.response.modal.boxes["days"].default == "30"


def test_removed_setters_are_gone_and_kept_commands_remain():
    set_names = {c.name for c in Lurker.lurkerset.commands}
    assert set_names == {"settings", "postinfo"}
    assert not hasattr(Lurker, "yearclub")  # moved to the celebrations cog
    for kept in ("lurker_manual", "lurker_version", "lurker_status", "lurker_report"):
        assert isinstance(getattr(Lurker, kept), discord.ext.commands.Command), kept
