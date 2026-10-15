"""The `.afterdark` control panel, driven through real discord.py components with fake interactions.

Needs real discord.py (skipped under the repo's stub). These prove the specs read and write the right
settings and that every control fits Discord's limits; a live click-through is still needed after deploy.
"""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from afterdark import constants as C
from afterdark import panel as panel_mod
from afterdark.settings_panel import NEW_ITEM, SettingsPanel
from afterdark.test_afterdark_cog import (FEET_CHANNEL, FEET_ROLE, FakeCtx, FakeRole, add_feet, log_text, set_cfg, w)  # noqa: F401


@pytest.fixture(autouse=True)
def mentions(monkeypatch):
    monkeypatch.setattr(FakeRole, "mention", property(lambda self: f"<@&{self.id}>"), raising=False)


class Resp:
    def __init__(self):
        self.done, self.sent, self.edited, self.modal = False, [], [], None

    def is_done(self):
        return self.done

    async def defer(self):
        self.done = True

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append(content)

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)

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


def admin_of(w, manage=True):
    m = w.member("admin")
    m.guild_permissions = SimpleNamespace(administrator=False, manage_guild=manage)
    return m


async def open_it(w, admin=None):
    admin = admin or admin_of(w)
    p = SettingsPanel(w.cog, w.guild, admin, panel_mod.build_specs(), title="Rabbit Hole", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, admin


def last_note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


async def go(p, admin, tab, key):
    await p._on_tab(Click(admin, [tab]))
    await p._on_setting(Click(admin, [key]))


def child(p, kind, **attrs):
    for c in p.children:
        if isinstance(c, kind) and all(getattr(c, k, None) == v for k, v in attrs.items()):
            return c
    raise AssertionError(f"missing {kind.__name__} {attrs}")


def test_spec_keys_are_unique_and_every_tab_fits_a_dropdown():
    specs = panel_mod.build_specs()
    assert len({s.key for s in specs}) == len(specs)
    tabs = {}
    for s in specs:
        tabs.setdefault(s.tab, []).append(s)
    assert set(tabs) == {"Roles & channels", "Access & sweep", "Invitations", "Drops", "Interests & feeds"}
    assert all(len(v) <= 25 for v in tabs.values())
    assert {x.key for x in tabs["Drops"]} == {"drops", "drop_channel_id", "drop_timing", "drop_next", "drop_now"}
    assert {x.key for x in tabs["Roles & channels"]} == {"rabbit_role_id", "adult_role_id", "verified_role_id", "log_channel_id", "clue_channel_id"}
    for s in specs:
        assert len(s.fields) <= 5


async def test_every_setting_renders_and_fits_discords_limits(w):
    p, admin = await open_it(w)
    await add_feet(w)
    for tab in p.tabs:
        await p._on_tab(Click(admin, [tab]))
        for spec in p.tab_specs():
            await p._on_setting(Click(admin, [spec.key]))
            assert len(p.to_components()) <= 5
            assert p.build_embed().description


async def test_only_the_opener_with_manage_server_can_use_it(w):
    p, admin = await open_it(w)
    assert await p.interaction_check(Click(w.member("other"))) is False
    assert await p.interaction_check(Click(admin)) is True
    admin.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(admin)) is False


async def test_command_opens_the_panel_for_admins_and_help_for_mods(w):
    sent = {}

    class Ctx(FakeCtx):
        async def send(self, text=None, **kw):
            sent.update(kw)
            return SimpleNamespace(id=1)

        async def send_help(self):
            sent["help"] = True

    fn = getattr(w.cog.afterdark, "callback", None) or w.cog.afterdark.func
    ctx = Ctx(w.guild, admin_of(w))
    await fn(w.cog, ctx)
    assert isinstance(sent["view"], SettingsPanel) and "Rabbit Hole" in sent["embed"].title
    sent.clear()
    ctx = Ctx(w.guild, admin_of(w, manage=False))
    await fn(w.cog, ctx)
    assert sent == {"help": True}


async def test_role_and_channel_selects_save_and_clear(w):
    p, admin = await open_it(w)
    role = w.guild.add_role(888, "Age Verified")
    await go(p, admin, "Roles & channels", "verified_role_id")
    child(p, discord.ui.RoleSelect)._values = [role]
    await p._on_pick_value(Click(admin))
    assert await w.cog.config.guild(w.guild).verified_role_id() == 888
    await p._on_clear(Click(admin))
    assert await w.cog.config.guild(w.guild).verified_role_id() == 0
    await go(p, admin, "Roles & channels", "log_channel_id")
    child(p, discord.ui.ChannelSelect)._values = [SimpleNamespace(id=FEET_CHANNEL, mention="#feet")]
    await p._on_pick_value(Click(admin))
    assert await w.cog.config.guild(w.guild).log_channel_id() == FEET_CHANNEL


async def test_a_required_role_cannot_be_cleared(w):
    p, admin = await open_it(w)
    await go(p, admin, "Roles & channels", "rabbit_role_id")
    assert not any(getattr(c, "label", None) == "Clear" for c in p.children)


async def test_going_live_asks_first_and_stays_dry_if_cancelled(w):
    p, admin = await open_it(w)
    await go(p, admin, "Access & sweep", "sweep_mode")
    await p._on_choice(Click(admin, ["live"]))
    assert p.mode == "confirm" and await w.cog.config.guild(w.guild).dry_run() is True
    await p._on_cancel(Click(admin))
    assert await w.cog.config.guild(w.guild).dry_run() is True
    await p._on_choice(Click(admin, ["live"]))
    await p._on_confirm(Click(admin))
    assert await w.cog.config.guild(w.guild).dry_run() is False and "LIVE" in log_text(w)
    await p._on_choice(Click(admin, ["dry"]))                      # going back to safe never asks
    assert p.mode == "main" and await w.cog.config.guild(w.guild).dry_run() is True


async def test_modal_settings_validate_through_the_cog(w):
    p, admin = await open_it(w)
    await go(p, admin, "Access & sweep", "inactivity")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert modal.boxes["warn"].default == "7" and modal.boxes["remove"].default == "14"
    modal.boxes["warn"]._value, modal.boxes["remove"]._value = "20", "10"
    bad = Click(admin)
    await modal.on_submit(bad)
    assert "⚠" in last_note(bad) and await w.cog.config.guild(w.guild).warn_days() == 7
    modal.boxes["warn"]._value, modal.boxes["remove"]._value = "3", "10"
    good = Click(admin)
    await modal.on_submit(good)
    assert await w.cog.config.guild(w.guild).warn_days() == 3 and "✅" in last_note(good)


async def test_drops_toggle_confirms_then_schedules(w):
    w.guild.add_channel(C.DEFAULT_CLUE_CHANNEL_ID, "pillow-talk")
    p, admin = await open_it(w)
    await go(p, admin, "Drops", "drops")
    await p._make_toggle(True)(Click(admin))
    assert p.mode == "confirm" and await w.cog.config.guild(w.guild).drop_enabled() is False
    await p._on_confirm(Click(admin))
    assert await w.cog.config.guild(w.guild).drop_enabled() is True
    await p._make_toggle(False)(Click(admin))
    assert await w.cog.config.guild(w.guild).drop_enabled() is False


async def test_action_buttons_run_the_cog(w):
    p, admin = await open_it(w)
    await set_cfg(w, invites_enabled=True)
    w.member("candidate", level=30)
    await go(p, admin, "Invitations", "invite_now")
    click = Click(admin)
    await p._on_action(click)
    assert "Review round started" in last_note(click)
    assert (await w.cog.config.guild(w.guild).invite_round())["prompt"]
    await go(p, admin, "Invitations", "invite_stop")
    await p._on_action(Click(admin))                                    # asks first
    assert p.mode == "confirm" and not (await w.cog.config.guild(w.guild).invite_round())["done"]
    stop = Click(admin)
    await p._on_confirm(stop)
    assert (await w.cog.config.guild(w.guild).invite_round())["done"] and "stopped" in last_note(stop)


async def test_interest_collection_add_edit_remove(w):
    c2 = w.guild.add_channel(900001, "user-feet")
    p, admin = await open_it(w)
    await go(p, admin, "Interests & feeds", "interests")
    await p._on_item(Click(admin, [NEW_ITEM]))
    click = Click(admin)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert set(modal.boxes) == {"name", "emoji", "new_channel", "join_message"}  # no role: access is per member
    modal.boxes["name"]._value, modal.boxes["emoji"]._value = "Feet", "\U0001F9B6"
    modal.boxes["new_channel"]._value, modal.boxes["join_message"]._value = "", "hello"
    await modal.on_submit(Click(admin))
    assert p.mode == "pick"
    sel = child(p, discord.ui.ChannelSelect)
    assert sel.min_values == 0 and sel.max_values == 25
    sel._values = [SimpleNamespace(id=FEET_CHANNEL), SimpleNamespace(id=900001)]
    done = Click(admin)
    await p._on_pick_done(done)
    saved = (await w.cog.config.guild(w.guild).interests())["feet"]
    assert saved["emoji"] == "\U0001F9B6" and saved["join_message"] == "hello"
    assert [saved["channel_id"]] + saved["extra_channel_ids"] == [FEET_CHANNEL, 900001]
    assert "✅" in last_note(done)
    # edit: the form is prefilled and the channel dropdown preselects the current channels
    await p._on_item(Click(admin, ["feet"]))
    click = Click(admin)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert modal.boxes["name"].default == "Feet" and modal.boxes["join_message"].default == "hello"
    await modal.on_submit(Click(admin))
    assert [d.id for d in child(p, discord.ui.ChannelSelect).default_values] == [FEET_CHANNEL, 900001]
    await p._on_cancel(Click(admin))
    await p._on_item_remove(Click(admin))
    assert not await w.cog.config.guild(w.guild).interests()


async def test_interest_records_show_channels_and_respect_the_25_cap(w):
    await add_feet(w)
    items = await panel_mod._get_interests(w.cog, w.guild)
    assert items[0][0] == "feet" and f"<#{FEET_CHANNEL}>" in items[0][2]
