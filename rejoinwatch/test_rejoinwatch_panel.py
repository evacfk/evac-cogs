"""The `.rejoinwatch` panel through real discord.py components (fake interactions)."""
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

from rejoinwatch import constants, panel as panel_mod
from rejoinwatch.rejoinwatch import RejoinWatch
from rejoinwatch.settings_panel import SettingsPanel


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
        self.vals = {k: Val(v) for k, v in values.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


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


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.channels = {10: SimpleNamespace(id=10, mention="<#10>", name="mod-chat"),
                         20: SimpleNamespace(id=20, mention="<#20>", name="cuddle")}
        self.roles = {5: SimpleNamespace(id=5, mention="<@&5>", name="Mods")}

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_role(self, rid):
        return self.roles.get(rid)


def make(**values):
    cog = object.__new__(RejoinWatch)
    group = Group(**{**constants.GUILD_DEFAULTS, **values})
    cog.config = SimpleNamespace(guild=lambda g: group)
    pruned = []

    async def prune(guild):
        pruned.append(guild)
    cog._prune_guild = prune
    return cog, group, pruned


def admin(manage=True):
    return SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=False, manage_guild=manage))


async def open_it(cog, guild):
    a = admin()
    p = SettingsPanel(cog, guild, a, panel_mod.build_specs(), title="RejoinWatch", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


async def test_panel_lists_every_setting_with_current_values():
    guild = Guild()
    cog, g, _ = make(mod_channel_id=10, cuddle_channel_id=20, mod_role_id=5, retention_days=90)
    p, a = await open_it(cog, guild)
    text = p.build_embed().description
    assert "<#10>" in text and "<#20>" in text and "<@&5>" in text and "90 days" in text
    assert [s.key for s in panel_mod.build_specs()] == ["enabled", "mod_channel", "fallback", "mod_role", "retention", "warning"]


async def test_retention_modal_saves_prunes_and_rejects_bad_input():
    guild = Guild()
    cog, g, pruned = make(retention_days=180)
    p, a = await open_it(cog, guild)
    await p._on_setting(Click(a, ["retention"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert modal.boxes["days"].default == "180"
    modal.boxes["days"]._value = "3"
    bad = Click(a)
    await modal.on_submit(bad)
    assert "⚠" in note(bad) and g.retention_days.data == 180 and pruned == []
    modal.boxes["days"]._value = "30"
    done = Click(a)
    await modal.on_submit(done)
    assert "✅" in note(done) and g.retention_days.data == 30 and len(pruned) == 1


async def test_warning_modal_saves_text_and_blank_resets():
    guild = Guild()
    cog, g, _ = make()
    p, a = await open_it(cog, guild)
    await p._on_setting(Click(a, ["warning"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert modal.boxes["text"].default == constants.DEFAULT_WARNING
    modal.boxes["text"]._value = "Careful in {server}."
    done = Click(a)
    await modal.on_submit(done)
    assert g.warning_text.data == "Careful in {server}." and "✅" in note(done)
    modal.boxes["text"]._value = ""
    await modal.on_submit(Click(a))
    assert g.warning_text.data == constants.DEFAULT_WARNING


async def test_only_the_opener_with_manage_server_can_use_it():
    cog, g, _ = make()
    p, a = await open_it(cog, Guild())
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    assert await p.interaction_check(Click(a)) is True
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False
