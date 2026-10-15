"""The `.tgfeed` control panel, driven through real discord.py components with fake interactions.

Needs real discord.py (skipped under the repo's stub). These prove the specs read and write the right
settings and that every control fits Discord's limits; a live click-through is still needed after deploy.
"""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from tgfeed import panel as panel_mod
from tgfeed.models import GroupInfo, TopicInfo, TopicMapping
from tgfeed.settings_panel import NEW_ITEM, SettingsPanel
from tgfeed.test_cog import FakeGuild, GroupSource, MapSource, make, overwrite_stub  # noqa: F401

TOPICS = [TopicInfo(7, "Cats"), TopicInfo(8, "Dogs"), TopicInfo(9, "Birds")]


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


class Admin:
    """A hashable stand-in for a Member (permission overwrites use members as dict keys)."""

    def __init__(self, manage=True):
        self.id = 1
        self.guild_permissions = SimpleNamespace(administrator=False, manage_guild=manage)


def admin_of(manage=True):
    return Admin(manage)


def build(tmp_path, **cfg):
    cog = make(tmp_path, mappings=cfg.pop("mappings", {}), **cfg)
    cog._source = MapSource(TOPICS)
    guild = FakeGuild()
    guild.me, guild.default_role = "BOT", "EVERYONE"
    return cog, guild


async def open_it(cog, guild, admin=None):
    admin = admin or admin_of()
    p = SettingsPanel(cog, guild, admin, panel_mod.build_specs(), title="Telegram feed", intro=panel_mod.INTRO)
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


def labels(p):
    return [getattr(c, "label", None) for c in p.children]


def test_spec_keys_are_unique_and_every_tab_fits_a_dropdown():
    specs = panel_mod.build_specs()
    assert len({s.key for s in specs}) == len(specs)
    tabs = {}
    for s in specs:
        tabs.setdefault(s.tab, []).append(s.key)
    assert tabs == {
        "Connection": ["running", "resume_skip", "group", "category", "prefix", "view_roles"],
        "Topics": ["load_topics", "topics", "map_by_name"],
        "Pace & posts": ["throttle", "x_button", "log_channel"],
    }
    for s in specs:
        assert len(s.fields) <= 5


async def test_every_setting_renders_and_fits_discords_limits(tmp_path):
    cog, guild = build(tmp_path, mappings={"7": TopicMapping(topic_id=7, title="Cats", channel_id=100).to_dict()})
    p, admin = await open_it(cog, guild)
    for tab in p.tabs:
        await p._on_tab(Click(admin, [tab]))
        for spec in p.tab_specs():
            await p._on_setting(Click(admin, [spec.key]))
            assert len(p.to_components()) <= 5
            assert p.build_embed().description


async def test_only_the_opener_with_manage_server_can_use_it(tmp_path):
    cog, guild = build(tmp_path)
    p, admin = await open_it(cog, guild)
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=admin.guild_permissions))) is False
    assert await p.interaction_check(Click(admin)) is True
    admin.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(admin)) is False


async def test_feed_toggle_pauses_and_resumes(tmp_path):
    cog, guild = build(tmp_path)
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Connection", "running")
    await p._make_toggle(False)(Click(admin))
    assert await cog.config.paused() is True
    await p._make_toggle(True)(Click(admin))
    assert await cog.config.paused() is False


async def test_resume_and_skip_asks_first(tmp_path):
    cog, guild = build(tmp_path, paused=True)
    cog._source = MapSource([], latest=900)
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Connection", "resume_skip")
    await p._on_action(Click(admin))
    assert p.mode == "confirm" and await cog.config.paused() is True
    done = Click(admin)
    await p._on_confirm(done)
    assert await cog.config.paused() is False and "Skipped" in last_note(done)
    assert (await cog.config.mappings()) == {}


async def test_group_modal_sets_a_forum_and_refuses_a_plain_chat(tmp_path):
    cog, guild = build(tmp_path, group={})
    cog._source = GroupSource(TOPICS, GroupInfo(5, "Plain", None, False))
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Connection", "group")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    modal.boxes["link"]._value = "https://t.me/+abc"
    bad = Click(admin)
    await modal.on_submit(bad)
    assert "⚠" in last_note(bad) and await cog.config.group() == {}
    cog._source.info = GroupInfo(6, "Forum", "forum", True)
    good = Click(admin)
    await modal.on_submit(good)
    assert "✅" in last_note(good) and (await cog.config.group())["id"] == 6


async def test_category_picker_only_offers_categories_and_saves_and_clears(tmp_path):
    cog, guild = build(tmp_path)
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Connection", "category")
    sel = child(p, discord.ui.ChannelSelect)
    assert sel.channel_types == [discord.ChannelType.category]
    sel._values = [SimpleNamespace(id=55, name="Telegram")]
    await p._on_pick_value(Click(admin))
    assert await cog.config.category_id() == 55
    await p._on_clear(Click(admin))
    assert await cog.config.category_id() is None


async def test_prefix_and_view_roles(tmp_path):
    cog, guild = build(tmp_path)
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Connection", "prefix")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    modal.boxes["prefix"]._value = "tg-"
    await modal.on_submit(Click(admin))
    assert await cog.config.name_prefix() == "tg-"
    modal.boxes["prefix"]._value = ""
    await modal.on_submit(Click(admin))
    assert await cog.config.name_prefix() == "🤖・"
    await go(p, admin, "Connection", "view_roles")
    sel = child(p, discord.ui.RoleSelect)
    assert sel.max_values == 25
    sel._values = [SimpleNamespace(id=9), SimpleNamespace(id=12)]
    await p._on_pick_value(Click(admin))
    assert await cog.config.view_role_ids() == [9, 12]
    await p._on_clear(Click(admin))
    assert await cog.config.view_role_ids() == []


async def test_topics_tab_lists_mapped_first_then_loaded_ones(tmp_path):
    cog, guild = build(tmp_path, group={"id": 777, "title": "G", "username": None, "is_forum": True},
                       mappings={"7": TopicMapping(topic_id=7, title="Cats", channel_id=100, posted_total=3).to_dict()})
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Topics", "topics")
    assert [i[0] for i in p.items] == ["7"]
    await go(p, admin, "Topics", "load_topics")
    click = Click(admin)
    await p._on_action(click)
    assert "Loaded 3" in last_note(click)
    await go(p, admin, "Topics", "topics")
    assert [(i[0], i[2]) for i in p.items][1:] == [("8", "not mapped"), ("9", "not mapped")]
    assert "<#100>" in p.items[0][2] and "3 files" in p.items[0][2]


async def test_map_new_channel_edit_to_existing_pause_resume_and_remove(tmp_path, overwrite_stub):
    cog, guild = build(tmp_path, group={"id": 777, "title": "G", "username": None, "is_forum": True}, category_id=55)
    await cog._apply_load_topics(guild, "ME")
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Topics", "topics")
    await p._on_item(Click(admin, ["8"]))
    assert {"Map (new channel)", "Pause", "Resume", "Edit…", "Remove"} <= set(labels(p))
    assert len(p.to_components()) <= 5
    click = Click(admin)
    await p._make_item_action(0)(click)
    assert "✅" in last_note(click) and len(guild.created) == 1
    saved = (await cog.config.mappings())["8"]
    assert saved["channel_id"] == 700 and saved["cursor"] == 500
    # point it at an existing channel instead
    click = Click(admin)
    await p._on_item_edit(click)
    assert p.mode == "pick"
    assert [d.id for d in child(p, discord.ui.ChannelSelect).default_values] == [700]
    child(p, discord.ui.ChannelSelect)._values = [SimpleNamespace(id=321, mention="<#321>")]
    done = Click(admin)
    await p._on_pick_done(done)
    assert (await cog.config.mappings())["8"]["channel_id"] == 321 and "now goes to" in last_note(done)
    # pause / resume
    await p._on_item(Click(admin, ["8"]))
    await p._make_item_action(1)(Click(admin))
    assert (await cog.config.mappings())["8"]["paused"] is True
    await p._make_item_action(2)(Click(admin))
    assert (await cog.config.mappings())["8"]["paused"] is False
    # unmap
    await p._on_item_remove(Click(admin))
    assert "8" not in await cog.config.mappings()


async def test_a_topic_cannot_share_a_channel(tmp_path):
    cog, guild = build(tmp_path, group={"id": 777, "title": "G", "username": None, "is_forum": True},
                       mappings={"7": TopicMapping(topic_id=7, title="Cats", channel_id=100).to_dict()})
    await cog._apply_load_topics(guild, "ME")
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Topics", "topics")
    await p._on_item(Click(admin, ["8"]))
    await p._on_item_edit(Click(admin))
    child(p, discord.ui.ChannelSelect)._values = [SimpleNamespace(id=100, mention="<#100>")]
    done = Click(admin)
    await p._on_pick_done(done)
    assert "already receives" in last_note(done) and "8" not in await cog.config.mappings()


async def test_map_by_name_modal_creates_a_channel(tmp_path, overwrite_stub):
    cog, guild = build(tmp_path, group={"id": 777, "title": "G", "username": None, "is_forum": True}, category_id=55)
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Topics", "map_by_name")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    modal.boxes["topic"]._value = "Birds"
    done = Click(admin)
    await modal.on_submit(done)
    assert "✅" in last_note(done) and (await cog.config.mappings())["9"]["channel_id"] == 700
    modal.boxes["topic"]._value = "Nope"
    bad = Click(admin)
    await modal.on_submit(bad)
    assert "⚠" in last_note(bad)


async def test_pacing_modal_is_prefilled_clamps_and_rejects_junk(tmp_path):
    cog, guild = build(tmp_path)
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Pace & posts", "throttle")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert [modal.boxes[k].default for k in ("interval", "gap_min", "gap_max", "per_hour", "per_day")] == ["300", "5", "12", "60", "500"]
    modal.boxes["interval"]._value, modal.boxes["per_hour"]._value = "20", "4"
    done = Click(admin)
    await modal.on_submit(done)
    assert await cog.config.poll_interval_seconds() == 120 and await cog.config.max_per_hour() == 10
    modal.boxes["interval"]._value = "soon"
    bad = Click(admin)
    await modal.on_submit(bad)
    assert "plain number" in last_note(bad) and await cog.config.poll_interval_seconds() == 120


async def test_x_button_toggle_and_log_channel(tmp_path):
    cog, guild = build(tmp_path)
    p, admin = await open_it(cog, guild)
    await go(p, admin, "Pace & posts", "x_button")
    await p._make_toggle(False)(Click(admin))
    assert await cog.config.x_button() is False
    await go(p, admin, "Pace & posts", "log_channel")
    sel = child(p, discord.ui.ChannelSelect)
    assert sel.channel_types == [discord.ChannelType.text]
    sel._values = [SimpleNamespace(id=31, mention="<#31>")]
    await p._on_pick_value(Click(admin))
    assert await cog.config.log_channel_id() == 31
