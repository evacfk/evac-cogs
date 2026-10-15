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

from wonderland.introcleanup import panel as cleanup_panel
from wonderland.introcleanup.introcleanup import IntroCleanup
from wonderland.introform import panel as form_panel
from wonderland.introform.introform import IntroForm
from wonderland.settings_panel import PanelError, SettingsPanel

DEFAULTS = dict(intros_channel_id=None, log_channel_id=None, channel_id=None, enforce=False, panel_message_id=None)


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
        self.vals = {k: Val(v) for k, v in {**DEFAULTS, **values}.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


class Chan:
    def __init__(self, cid, fail=False):
        self.id, self.mention, self.fail = cid, f"<#{cid}>", fail


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.chans = {10: Chan(10), 20: Chan(20)}

    def get_channel(self, cid):
        return self.chans.get(cid)


def make(cls, **values):
    cog = object.__new__(cls)
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


async def test_introcleanup_channels():
    cog, g = make(IntroCleanup)
    guild = Guild()
    with pytest.raises(PanelError):
        await cog._apply_intros_channel(guild, None, None)
    await cog._apply_intros_channel(guild, None, guild.chans[10])
    await cog._apply_log_channel(guild, None, guild.chans[20])
    assert (g.intros_channel_id.data, g.log_channel_id.data) == (10, 20)
    assert "cleared" in await cog._apply_log_channel(guild, None, None)
    assert g.log_channel_id.data is None


async def test_introform_channel_reposts_the_panel_and_enforce_toggles():
    cog, g = make(IntroForm)
    guild = Guild()
    refreshed = []

    async def refresh(gd):
        refreshed.append(gd)
        return SimpleNamespace(jump_url="https://discord.com/x")
    cog.refresh_panel = refresh
    with pytest.raises(PanelError):
        await cog._apply_channel(guild, None, None)
    g.panel_message_id.data = 5
    msg = await cog._apply_channel(guild, None, guild.chans[10])
    assert g.channel_id.data == 10 and g.panel_message_id.data is None and "https://discord.com/x" in msg
    assert "on" in await cog._apply_enforce(guild, None, True) and g.enforce.data is True
    assert len(refreshed) == 2


async def test_introform_channel_saved_even_when_the_panel_cannot_be_posted():
    import discord as d
    cog, g = make(IntroForm)
    guild = Guild()

    async def refresh(gd):
        raise d.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
    cog.refresh_panel = refresh
    msg = await cog._apply_channel(guild, None, guild.chans[10])
    assert g.channel_id.data == 10 and "couldn't post the panel" in msg
    assert "couldn't refresh" in await cog._apply_enforce(guild, None, False)


async def test_both_panels_render_and_gate_on_manage_server():
    for cls, mod, title in ((IntroCleanup, cleanup_panel, "IntroCleanup"), (IntroForm, form_panel, "IntroForm")):
        cog, g = make(cls, intros_channel_id=10, channel_id=10)
        a = admin()
        p = SettingsPanel(cog, Guild(), a, mod.build_specs(), title=title, intro=mod.INTRO)
        await p.refresh_data()
        p.rebuild()
        assert "<#10>" in p.build_embed().description
        assert await p.interaction_check(Click(a)) is True
        assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
        a.guild_permissions.manage_guild = False
        assert await p.interaction_check(Click(a)) is False
