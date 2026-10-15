"""The `.secrethunt` control panel, driven through real discord.py components with fake interactions.

Needs real discord.py (skipped under the repo's stub). These prove the specs read and write the right
settings and that every control fits Discord's limits; a live click-through is still needed after deploy.
"""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from secrethunt import panel as panel_mod
from secrethunt.puzzles import PUZZLES
from secrethunt.settings_panel import SettingsPanel
from secrethunt.test_secrethunt_cog import PID, env, mod  # noqa: F401


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


def admin_of(env, manage=True):
    m = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=False, manage_guild=manage), mention="<@1>")
    return m


async def open_it(env, admin=None):
    admin = admin or admin_of(env)
    p = SettingsPanel(env.cog, env.g, admin, panel_mod.build_specs(), title="Secret hunt", intro=panel_mod.INTRO)
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
    assert set(tabs) == {"Hunt", "Puzzles", "Roles"}
    assert all(len(v) <= 25 for v in tabs.values())
    assert {x.key for x in tabs["Hunt"]} == {"enabled", "test_mode", "pool", "pool_text", "hall", "announce", "hall_refresh"}
    assert {x.key for x in tabs["Roles"]} == {"role_medium", "role_hard", "role_expert", "create_roles", "badges"}
    assert not any(s.key == "role_easy" for s in specs)  # Easy never has a role
    for s in specs:
        assert len(s.fields) <= 5


async def test_every_setting_renders_and_fits_discords_limits(env):
    p, admin = await open_it(env)
    for tab in p.tabs:
        await p._on_tab(Click(admin, [tab]))
        for spec in p.tab_specs():
            await p._on_setting(Click(admin, [spec.key]))
            assert len(p.to_components()) <= 5
            assert p.build_embed().description


async def test_only_the_opener_with_manage_server_can_use_it(env):
    p, admin = await open_it(env)
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=admin.guild_permissions))) is False
    assert await p.interaction_check(Click(admin)) is True
    admin.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(admin)) is False


async def test_command_opens_the_panel(env):
    sent = {}

    class Ctx:
        guild, author = env.g, admin_of(env)

        async def send(self, text=None, **kw):
            sent.update(kw)
            return SimpleNamespace(id=1)

    fn = getattr(env.cog.secrethunt, "callback", None) or env.cog.secrethunt.func
    await fn(env.cog, Ctx())
    assert isinstance(sent["view"], SettingsPanel) and "Secret hunt" in sent["embed"].title


async def test_toggle_and_test_mode_save(env):
    p, admin = await open_it(env)
    await go(p, admin, "Hunt", "test_mode")
    await p._make_toggle(True)(Click(admin))
    assert await env.conf.test_mode() is True
    await go(p, admin, "Hunt", "enabled")
    await p._make_toggle(False)(Click(admin))
    assert await env.conf.enabled() is False


async def test_pool_select_asks_first_and_enforces_two_to_five(env):
    p, admin = await open_it(env)
    await go(p, admin, "Hunt", "pool")
    sel = child(p, discord.ui.ChannelSelect)
    assert sel.min_values == 2 and sel.max_values == 5
    before = await env.conf.pool()
    sel._values = [SimpleNamespace(id=env.chans[1].id), SimpleNamespace(id=env.chans[0].id)]
    await p._on_pick_value(Click(admin))
    assert p.mode == "confirm" and await env.conf.pool() == before
    await p._on_cancel(Click(admin))
    assert await env.conf.pool() == before
    child(p, discord.ui.ChannelSelect)._values = [SimpleNamespace(id=env.chans[1].id), SimpleNamespace(id=env.chans[0].id)]
    await p._on_pick_value(Click(admin))
    click = Click(admin)
    await p._on_confirm(click)
    assert await env.conf.pool() == [env.chans[1].id, env.chans[0].id]


async def test_paste_hiding_places_modal_validates_through_the_cog(env):
    p, admin = await open_it(env)
    await go(p, admin, "Hunt", "pool_text")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    modal.boxes["channels"]._value = f"<#{env.chans[0].id}>"
    bad = Click(admin)
    await modal.on_submit(bad)
    assert "⚠" in last_note(bad)
    modal.boxes["channels"]._value = " ".join(f"<#{c.id}>" for c in env.chans[:3])
    good = Click(admin)
    await modal.on_submit(good)
    assert "✅" in last_note(good)
    assert await env.conf.pool() == [c.id for c in env.chans[:3]]


async def test_hall_and_announce_channels_save_and_clear(env):
    p, admin = await open_it(env)
    other = env.g.add_channel(777, "chat")
    await go(p, admin, "Hunt", "announce")
    child(p, discord.ui.ChannelSelect)._values = [other]
    await p._on_pick_value(Click(admin))
    assert await env.conf.announce_channel_id() == 777
    await p._on_clear(Click(admin))
    assert await env.conf.announce_channel_id() is None
    await go(p, admin, "Hunt", "hall")
    await p._on_clear(Click(admin))
    assert await env.conf.hall_channel_id() is None


async def test_tier_roles_save_clear_and_create_missing(env):
    p, admin = await open_it(env)
    await go(p, admin, "Roles", "role_medium")
    child(p, discord.ui.RoleSelect)._values = [env.role]
    await p._on_pick_value(Click(admin))
    assert (await env.conf.roles())["medium"] == env.role.id
    await p._on_clear(Click(admin))
    assert "medium" not in await env.conf.roles()
    await go(p, admin, "Roles", "create_roles")
    click = Click(admin)
    await p._on_action(click)
    assert "Created" in last_note(click)
    assert set(await env.conf.roles()) == {"medium", "hard", "expert"}


async def test_puzzle_actions_and_badge_roles(env, monkeypatch):
    BADGE = PID
    p, admin = await open_it(env)
    items = await panel_mod._puzzles(env.cog, env.g)
    assert items[0][0] == PID and "LIVE" in items[0][2]
    badges = await panel_mod._badges(env.cog, env.g)
    assert badges == []  # both shipped puzzles are Easy, and Easy never has a badge
    monkeypatch.setitem(PUZZLES, PID, {**PUZZLES[PID], "tier": "hard"})
    badges = await panel_mod._badges(env.cog, env.g)
    assert [b[0] for b in badges] == [BADGE]
    p, admin = await open_it(env)
    await go(p, admin, "Roles", "badges")
    await p._on_item(Click(admin, [BADGE]))
    click = Click(admin)
    await p._on_item_edit(click)
    assert p.mode == "pick"
    child(p, discord.ui.RoleSelect)._values = [env.role]
    done = Click(admin)
    await p._on_pick_done(done)
    assert (await env.conf.puzzle_roles())[BADGE] == env.role.id and "✅" in last_note(done)
    await p._on_item(Click(admin, [BADGE]))
    await p._on_item_remove(Click(admin))
    assert BADGE not in await env.conf.puzzle_roles()


async def test_puzzle_item_actions_set_up_and_go_live(env):
    await env.conf.published.set([])
    await env.conf.objects.set({})
    p, admin = await open_it(env)
    await go(p, admin, "Puzzles", "puzzles")
    await p._on_item(Click(admin, [PID]))
    labels = [getattr(c, "label", None) for c in p.children]
    assert {"Set up objects", "Redo objects", "Go live", "DM me answers"} <= set(labels)
    click = Click(admin)
    await p._make_item_action(0)(click)
    assert PID in (await env.conf.objects()) and "✅" in last_note(click)
    click = Click(admin)
    await p._make_item_action(2)(click)
    assert PID in await env.conf.published() and "✅" in last_note(click)
    redo = Click(admin)
    await p._make_item_action(1)(redo)                                # asks first
    assert p.mode == "confirm"
    await p._on_cancel(Click(admin))
    assert p.mode == "main"
