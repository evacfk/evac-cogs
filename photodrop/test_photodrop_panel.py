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

from photodrop import constants, panel as panel_mod
from photodrop.photodrop import PhotoDrop
from photodrop.settings_panel import PanelError, SettingsPanel


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
        self.vals = {k: Val(v) for k, v in {**constants.DEFAULT_GUILD, **values}.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.chans = {10: SimpleNamespace(id=10, mention="<#10>"), 11: SimpleNamespace(id=11, mention="<#11>")}
        self.roles = {5: SimpleNamespace(id=5, mention="<@&5>", name="Photographer")}
        self.members = {77: SimpleNamespace(id=77, display_name="Rae")}

    def get_channel(self, cid):
        return self.chans.get(cid)

    def get_role(self, rid):
        return self.roles.get(rid)

    def get_member(self, uid):
        return self.members.get(uid)


def make(**values):
    cog = object.__new__(PhotoDrop)
    group = Group(**values)
    cog.config = SimpleNamespace(guild=lambda g: group)
    return cog, group


async def test_channels_and_role():
    cog, g = make()
    guild = Guild()
    with pytest.raises(PanelError):
        await cog._apply_channel(guild, None, None)
    with pytest.raises(PanelError):
        await cog._apply_job_role(guild, None, None)
    await cog._apply_channel(guild, None, guild.chans[10])
    await cog._apply_rating_channel(guild, None, guild.chans[11])
    await cog._apply_job_role(guild, None, guild.roles[5])
    assert (g.channel_id.data, g.rating_channel_id.data, g.job_role_id.data) == (10, 11, 5)
    assert "shared channel" in await cog._apply_rating_channel(guild, None, None)
    assert g.rating_channel_id.data is None


async def test_rater_accepts_id_or_mention_and_blank_resets():
    cog, g = make()
    guild = Guild()
    for bad in ("nobody", "999", "<@999>"):
        with pytest.raises(PanelError):
            await cog._apply_rater(guild, None, {"user": bad})
    assert "Rae" in await cog._apply_rater(guild, None, {"user": "<@77>"}) and g.rater_id.data == 77
    await cog._apply_rater(guild, None, {"user": "77"})
    assert g.rater_id.data == 77
    assert "owner" in await cog._apply_rater(guild, None, {"user": " "})
    assert g.rater_id.data is None


async def test_daily_and_strikes_validation():
    cog, g = make()
    for bad in ({"quota": "0", "reminder_hours": "3"}, {"quota": "2", "reminder_hours": "0"},
                {"quota": "2", "reminder_hours": "24"}, {"quota": "x", "reminder_hours": "3"}, {}):
        with pytest.raises(PanelError):
            await cog._apply_daily(None, None, bad)
    assert (g.quota.data, g.reminder_hours_before.data) == (constants.DEFAULT_QUOTA, constants.DEFAULT_REMINDER_HOURS_BEFORE)
    await cog._apply_daily(None, None, {"quota": "2", "reminder_hours": "1"})
    assert (g.quota.data, g.reminder_hours_before.data) == (2, 1)
    for bad in ({"window_days": "0", "threshold": "3"}, {"window_days": "7", "threshold": "0"}, {"window_days": "a", "threshold": "1"}):
        with pytest.raises(PanelError):
            await cog._apply_strikes(None, None, bad)
    await cog._apply_strikes(None, None, {"window_days": "14", "threshold": "4"})
    assert (g.strike_window_days.data, g.strike_threshold.data) == (14, 4)


def test_set_command_replaces_the_old_setters():
    names = {c.name for c in PhotoDrop.pp.commands}
    assert "set" in names and "hire" in names and "version" in names
    assert not isinstance(PhotoDrop.pp_set, commands_group())


def commands_group():
    return _red_commands.Group


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


async def test_panel_shows_values_and_daily_modal_saves():
    cog, g = make(channel_id=10, quota=3)
    a = admin()
    p = SettingsPanel(cog, Guild(), a, panel_mod.build_specs(), title="Photo drop", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    text = p.build_embed().description
    assert "<#10>" in text and "the shared channel" in text and "the server owner" in text
    assert len({s.key for s in panel_mod.build_specs()}) == 6
    p2 = p
    await p2._on_tab(Click(a, ["Rules"]))
    await p2._on_setting(Click(a, ["daily"]))
    click = Click(a)
    await p2._on_edit_modal(click)
    modal = click.response.modal
    assert [modal.boxes[k].default for k in ("quota", "reminder_hours")] == ["3", str(constants.DEFAULT_REMINDER_HOURS_BEFORE)]
    modal.boxes["quota"]._value, modal.boxes["reminder_hours"]._value = "5", "2"
    done = Click(a)
    await modal.on_submit(done)
    assert "✅" in note(done) and g.quota.data == 5 and g.reminder_hours_before.data == 2
    modal.boxes["quota"]._value = "0"
    bad = Click(a)
    await modal.on_submit(bad)
    assert "⚠" in note(bad) and g.quota.data == 5
