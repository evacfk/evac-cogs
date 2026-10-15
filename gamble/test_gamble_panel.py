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

import copy

from gamble import panel as panel_mod
from gamble.gamblethreads import COG_VERSION, GambleThreads
from gamble.settings_panel import PanelError, SettingsPanel


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


class Chan(SimpleNamespace):
    @property
    def mention(self):
        return f"<#{self.id}>"


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.chans = {10: Chan(id=10), 11: Chan(id=11)}

    def get_channel(self, cid):
        return self.chans.get(cid)


def make():
    cog = object.__new__(GambleThreads)
    group = SimpleNamespace(bot_channel_id=Val(None), timeout_minutes=Val(10), shared_timeout_minutes=Val(15))
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


async def test_channel_and_timeouts_are_validated():
    cog, g, guild = make()
    with pytest.raises(PanelError):
        await cog._apply_channel(guild, None, None)
    await cog._apply_channel(guild, None, guild.chans[10])
    assert g.bot_channel_id.data == 10
    for bad in ({"personal": "0", "shared": "5"}, {"personal": "5", "shared": "0"}, {"personal": "x", "shared": "5"},
                {"personal": "5"}, {"personal": "2.5", "shared": "5"}):
        with pytest.raises(PanelError):
            await cog._apply_timeouts(guild, None, bad)
    assert (g.timeout_minutes.data, g.shared_timeout_minutes.data) == (10, 15)
    await cog._apply_timeouts(guild, None, {"personal": "20", "shared": "30"})
    assert (g.timeout_minutes.data, g.shared_timeout_minutes.data) == (20, 30)


async def test_post_hub_relays_the_cogs_error_or_success():
    cog, g, guild = make()
    results = ["No hub channel is set", None]

    async def refresh(gd):
        return results.pop(0)
    cog._refresh_hub_message = refresh
    with pytest.raises(PanelError, match="No hub channel"):
        await cog._apply_post_hub(guild, None)
    assert "posted" in await cog._apply_post_hub(guild, None)


async def test_panel_widgets_save_the_timeouts_and_gate_by_permission():
    cog, g, guild = make()
    a = admin()
    p = SettingsPanel(cog, guild, a, panel_mod.build_specs(), title="Wondercasino", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    for spec in p.tab_specs():
        await p._on_setting(Click(a, [spec.key]))
        assert len(p.to_components()) <= 5 and p.build_embed().description
    await p._on_setting(Click(a, ["timeouts"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert (modal.boxes["personal"].default, modal.boxes["shared"].default) == ("10", "15")
    modal.boxes["personal"]._value = "12"
    done = Click(a)
    await modal.on_submit(done)
    assert "\u2705" in note(done) and g.timeout_minutes.data == 12
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False


async def test_group_opens_the_panel_for_admins_and_help_for_everyone_else():
    cog, g, guild = make()
    sent = {}

    class Ctx:
        def __init__(self, member):
            self.guild, self.author, self.command = guild, member, GambleThreads.gambleset

        async def send(self, content=None, **kw):
            sent.update(kw)
            return SimpleNamespace(id=1)

        async def send_help(self, command=None):
            sent["help"] = True
    fn = GambleThreads.gambleset.callback
    await fn(cog, Ctx(admin()))
    assert isinstance(sent["view"], SettingsPanel)
    sent.clear()
    plain = SimpleNamespace(id=3, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False))
    await fn(cog, Ctx(plain))
    assert sent == {"help": True}


def test_removed_setters_are_gone_and_the_probe_exists():
    names = {c.name for c in GambleThreads.gambleset.commands}
    assert names == {"version", "settings"}
    assert COG_VERSION == "1.1.0"
    assert {"setup", "listgames", "lockdown"} <= {c.name for c in GambleThreads.gamblehub.commands}
