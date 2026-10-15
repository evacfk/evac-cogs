"""The `.redditfeed` control panel, driven through real discord.py components with fake interactions.

Needs real discord.py (skipped under the repo's stub). These prove the specs read and write the right
settings and that every control fits Discord's limits; a live click-through is still needed after deploy.
"""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from redditfeed import panel as panel_mod
from redditfeed.models import SubredditMapping
from redditfeed.settings_panel import NEW_ITEM, SettingsPanel
from redditfeed.test_approval_cog import (FEET_CH, GUILD, LOG_CH, QUEUE_CH, Chan, make, post, queued, _no_delays)  # noqa: F401


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
    def __init__(self, manage=True):
        self.id = 1
        self.mention, self.display_name = "<@1>", "admin"
        self.guild_permissions = SimpleNamespace(administrator=False, manage_guild=manage)


def maps(*names, **kw):
    return {n: SubredditMapping(subreddit=n, channel_ids=[20], added_ts=1.0, last_poll_ts=1.0, **kw).to_dict() for n in names}


async def open_it(cog, admin=None):
    admin = admin or Admin()
    guild = SimpleNamespace(get_channel=GUILD.get_channel, id=5, name="g")
    p = SettingsPanel(cog, guild, admin, panel_mod.build_specs(), title="Reddit feed", intro=panel_mod.INTRO)
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


def test_tabs_and_specs():
    specs = panel_mod.build_specs()
    assert len({s.key for s in specs}) == len(specs)
    tabs = {}
    for s in specs:
        tabs.setdefault(s.tab, []).append(s.key)
    assert tabs == {
        "Feeds": ["feeds", "mode_all", "reset_all"],
        "Queue": ["queue_channel", "queue_gates", "queue_clear"],
        "Posting & polling": ["polling", "x_button", "redgifs", "log_channel"],
        "Discovery": ["category", "prefix", "denied"],
    }
    for s in specs:
        assert len(s.fields) <= 5


async def test_every_setting_renders_and_fits_discords_limits():
    cog = make(mappings=maps("a", "b"), denied_subreddits=["x"])
    p, admin = await open_it(cog)
    for tab in p.tabs:
        await p._on_tab(Click(admin, [tab]))
        for spec in p.tab_specs():
            await p._on_setting(Click(admin, [spec.key]))
            assert len(p.to_components()) <= 5
            assert p.build_embed().description


async def test_only_the_opener_with_manage_server_can_use_it():
    p, admin = await open_it(make())
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=admin.guild_permissions))) is False
    assert await p.interaction_check(Click(admin)) is True
    admin.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(admin)) is False


async def test_group_opens_the_panel_for_admins_and_help_for_mods(monkeypatch):
    from redditfeed import redditfeed as module
    opened, helped = {}, []

    async def fake_open(ctx, cog, specs, *, title, intro=""):
        opened.update(title=title, n=len(specs))

    monkeypatch.setattr(module, "open_panel", fake_open)
    cmd = module.RedditFeed.redditfeed
    fn = getattr(cmd, "callback", None) or cmd.func

    class Ctx:
        command = "CMD"

        def __init__(self, manage):
            self.author = Admin(manage)

        async def send_help(self, command=None):
            helped.append(command)

    await fn(make(), Ctx(True))
    assert opened == {"title": "Reddit feed", "n": len(panel_mod.build_specs())}
    await fn(make(), Ctx(False))
    assert helped == ["CMD"]


async def test_feed_list_shows_mode_pause_channels_and_filters():
    cog = make(mappings={**maps("a"), **maps("b", paused=True, approval="auto", require_keywords=["x"])})
    items = await panel_mod._feeds(cog, None)
    assert items[0] == ("a", "r/a", "manual · <#20>")
    assert items[1] == ("b", "r/b", "AUTO · PAUSED · <#20> · filtered")


async def test_add_flow_maps_subreddits_to_the_picked_channels():
    cog = make(mappings={})
    p, admin = await open_it(cog)
    await go(p, admin, "Feeds", "feeds")
    await p._on_item(Click(admin, [NEW_ITEM]))
    click = Click(admin)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert set(modal.boxes) == {"subreddits"}                              # keyword boxes are edit-only
    modal.boxes["subreddits"]._value = "feet, r/soles  footfetish"
    await modal.on_submit(Click(admin))
    assert p.mode == "pick"
    sel = child(p, discord.ui.ChannelSelect)
    assert sel.min_values == 1 and sel.max_values == 10
    sel._values = [SimpleNamespace(id=20)]
    done = Click(admin)
    await p._on_pick_done(done)
    assert set(await cog.config.mappings()) == {"feet", "soles", "footfetish"} and "✅" in last_note(done)


async def test_add_refuses_a_channel_that_is_not_age_restricted():
    cog = make(mappings={})
    p, admin = await open_it(cog)
    await go(p, admin, "Feeds", "feeds")
    await p._on_item(Click(admin, [NEW_ITEM]))
    click = Click(admin)
    await p._on_item_edit(click)
    modal = click.response.modal
    modal.boxes["subreddits"]._value = "feet"
    await modal.on_submit(Click(admin))
    child(p, discord.ui.ChannelSelect)._values = [Chan(55, nsfw=False)]
    done = Click(admin)
    await p._on_pick_done(done)
    assert "age-restricted" in last_note(done) and await cog.config.mappings() == {}


async def test_edit_a_feed_prefills_channels_and_keywords_then_saves():
    cog = make(mappings=maps("feet", require_keywords=["toes"], block_keywords=["face"]))
    p, admin = await open_it(cog)
    await go(p, admin, "Feeds", "feeds")
    await p._on_item(Click(admin, ["feet"]))
    assert {"Pause", "Resume", "Switch mode", "Edit…", "Remove"} <= set(labels(p))
    assert len(p.to_components()) <= 5
    click = Click(admin)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert set(modal.boxes) == {"require", "block"}
    assert modal.boxes["require"].default == "toes" and modal.boxes["block"].default == "face"
    modal.boxes["require"]._value, modal.boxes["block"]._value = "toes, soles", ""
    await modal.on_submit(Click(admin))
    assert [d.id for d in child(p, discord.ui.ChannelSelect).default_values] == [20]
    child(p, discord.ui.ChannelSelect)._values = [SimpleNamespace(id=20), SimpleNamespace(id=10)]
    done = Click(admin)
    await p._on_pick_done(done)
    m = (await cog.config.mappings())["feet"]
    assert m["channel_ids"] == [20, 10] and m["require_keywords"] == ["toes", "soles"] and m["block_keywords"] == []


async def test_pause_resume_switch_mode_and_remove_buttons():
    cog = make(mappings=maps("feet"))
    p, admin = await open_it(cog)
    await go(p, admin, "Feeds", "feeds")
    await p._on_item(Click(admin, ["feet"]))
    await p._make_item_action(0)(Click(admin))
    assert (await cog.config.mappings())["feet"]["paused"] is True
    await p._make_item_action(1)(Click(admin))
    assert (await cog.config.mappings())["feet"]["paused"] is False
    await p._make_item_action(2)(Click(admin))
    assert (await cog.config.mappings())["feet"]["approval"] == "auto"
    await p._on_item_remove(Click(admin))
    assert await cog.config.mappings() == {}


async def test_a_long_feed_list_is_paged_and_a_late_feed_can_be_managed():
    names = [f"sub{i:02d}" for i in range(60)]
    cog = make(mappings=maps(*names))
    p, admin = await open_it(cog)
    await go(p, admin, "Feeds", "feeds")
    assert len(p.items) == 60 and p.page_count(p.current_spec()) == 3
    await p._on_page_next(Click(admin))
    await p._on_page_next(Click(admin))
    await p._on_item(Click(admin, ["sub59"]))
    await p._make_item_action(0)(Click(admin))
    assert (await cog.config.mappings())["sub59"]["paused"] is True


async def test_mode_all_and_remove_every_feed_ask_first():
    cog = make(mappings=maps("a", "b"))
    p, admin = await open_it(cog)
    await go(p, admin, "Feeds", "mode_all")
    await p._on_choice(Click(admin, ["auto"]))
    assert p.mode == "confirm" and {m["approval"] for m in (await cog.config.mappings()).values()} == {"manual"}
    await p._on_confirm(Click(admin))
    assert {m["approval"] for m in (await cog.config.mappings()).values()} == {"auto"}
    await go(p, admin, "Feeds", "reset_all")
    await p._on_action(Click(admin))
    assert p.mode == "confirm" and len(await cog.config.mappings()) == 2
    await p._on_cancel(Click(admin))
    assert len(await cog.config.mappings()) == 2
    await p._on_action(Click(admin))
    await p._on_confirm(Click(admin))
    assert await cog.config.mappings() == {}


async def test_queue_channel_gates_and_clear():
    cog = make({"feet": [post("a"), post("b")]})
    await cog._run_poll_cycle()
    p, admin = await open_it(cog)
    await go(p, admin, "Queue", "queue_channel")
    sel = child(p, discord.ui.ChannelSelect)
    assert sel.channel_types == [discord.ChannelType.text]
    sel._values = [Chan(70, nsfw=False)]
    click = Click(admin)
    await p._on_pick_value(click)
    assert "age-restricted" in last_note(click) and await cog.config.queue_channel_id() == 10
    await go(p, admin, "Queue", "queue_gates")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert [modal.boxes[k].default for k in ("min_score", "min_age", "max_pending")] == ["3", "60", "40"]
    modal.boxes["min_score"]._value, modal.boxes["min_age"]._value, modal.boxes["max_pending"]._value = "5", "30", "10"
    await modal.on_submit(Click(admin))
    assert (await cog.config.queue_min_score(), await cog.config.queue_min_age_minutes(), await cog.config.queue_max_pending()) == (5, 30, 10)
    await go(p, admin, "Queue", "queue_clear")
    await p._on_action(Click(admin))
    assert p.mode == "confirm"
    done = Click(admin)
    await p._on_confirm(done)
    assert "Rejected 2" in last_note(done)


async def test_posting_and_polling_settings():
    cog = make()
    p, admin = await open_it(cog)
    await go(p, admin, "Posting & polling", "polling")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert modal.boxes["interval"].default == "120"
    modal.boxes["interval"]._value, modal.boxes["stagger"]._value = "300", "2"
    await modal.on_submit(Click(admin))
    assert await cog.config.poll_interval_seconds() == 300 and await cog.config.stagger_seconds() == 2.0
    await go(p, admin, "Posting & polling", "x_button")
    await p._make_toggle(False)(Click(admin))
    assert await cog.config.x_button() is False
    await go(p, admin, "Posting & polling", "redgifs")
    await p._on_choice(Click(admin, ["link"]))
    assert await cog.config.redgifs_mode() == "link"
    await go(p, admin, "Posting & polling", "log_channel")
    child(p, discord.ui.ChannelSelect)._values = [SimpleNamespace(id=31, mention="<#31>")]
    await p._on_pick_value(Click(admin))
    assert await cog.config.log_channel_id() == 31


async def test_discovery_tab_category_prefix_and_undeny():
    cog = make(denied_subreddits=["b", "a"])
    p, admin = await open_it(cog)
    await go(p, admin, "Discovery", "category")
    sel = child(p, discord.ui.ChannelSelect)
    assert sel.channel_types == [discord.ChannelType.category]
    sel._values = [SimpleNamespace(id=55, name="Feeds")]
    await p._on_pick_value(Click(admin))
    assert await cog.config.new_channel_category_id() == 55
    await p._on_clear(Click(admin))
    assert await cog.config.new_channel_category_id() is None
    await go(p, admin, "Discovery", "prefix")
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    modal.boxes["prefix"]._value = "X"
    await modal.on_submit(Click(admin))
    assert await cog.config.new_channel_prefix() == "X"
    await go(p, admin, "Discovery", "denied")
    assert [i[0] for i in p.items] == ["a", "b"]
    await p._on_item(Click(admin, ["a"]))
    await p._on_item_remove(Click(admin))
    assert await cog.config.denied_subreddits() == ["b"]
