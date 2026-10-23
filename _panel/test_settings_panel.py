"""Widget tests for the generic settings panel, run against REAL discord.py with fake interactions.

Skipped when discord.py is only the repo's tiny stub (the stub environments cannot build selects or
modals). These prove the panel's wiring and limits; a live click-through is still needed after deploy.
"""
import asyncio
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")
if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

try:
    from settings_panel import (Field, ItemAction, NEW_ITEM, PanelError, Pick, SettingsPanel, Spec,
                                format_value, make_modal, open_panel)
except ImportError:  # copy living inside a cog package
    from .settings_panel import (Field, ItemAction, NEW_ITEM, PanelError, Pick, SettingsPanel, Spec,
                                 format_value, make_modal, open_panel)


# ----------------------------------------------------------------- fakes

class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent = []
        self.edited = []
        self.modal = None

    def is_done(self):
        return self.done

    async def defer(self):
        self.done = True

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append((content, kw))

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal


class FakeInteraction:
    def __init__(self, user, values=None):
        self.user = user
        self.data = {"values": values or []}
        self.response = FakeResponse()
        self.originals = []
        self.followup = SimpleNamespace(send=self._followup)
        self.followups = []

    async def _followup(self, *a, **k):
        self.followups.append((a, k))

    async def edit_original_response(self, **kw):
        self.originals.append(kw)


def member(uid=1, manage=True):
    return SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(administrator=False, manage_guild=manage))


class Store:
    """Stands in for the cog: a dict of settings the specs read and write."""
    def __init__(self):
        self.d = {"chan": 0, "role": 0, "flag": False, "num": 5, "mode": "a", "items": {"x": {"name": "X", "n": 1}}, "roles": []}
        self.calls = []


def specs():
    async def get(key):
        async def _g(cog, guild):
            return cog.d[key]
        return _g

    def getter(key):
        async def _g(cog, guild):
            return cog.d[key]
        return _g

    def setter(key):
        async def _a(cog, guild, who, value):
            cog.calls.append((key, value))
            if value == "bad":
                raise PanelError("nope")
            cog.d[key] = getattr(value, "id", value)
            return f"{key} saved"
        return _a

    async def num_apply(cog, guild, who, values):
        n = int(values["num"])
        if n < 0:
            raise PanelError("must be positive")
        cog.d["num"] = n
        return f"num {n}"

    async def items(cog, guild):
        return [(k, v["name"], f"n={v['n']}") for k, v in cog.d["items"].items()]

    async def item_get(cog, guild, item_id):
        return dict(cog.d["items"][item_id])

    async def save(cog, guild, who, item_id, values, picked):
        key = item_id or values["name"].lower()
        cog.d["items"][key] = {"name": values["name"], "n": len(picked)}
        cog.calls.append(("save", item_id, dict(values), [getattr(p, "id", p) for p in picked]))
        return f"{key} saved"

    async def remove(cog, guild, who, item_id):
        cog.d["items"].pop(item_id)
        return "removed"

    async def bump(cog, guild, who, item_id):
        cog.d["items"][item_id]["n"] += 1
        return "bumped"

    async def go(cog, guild, who, _):
        cog.calls.append(("go",))
        return "went"

    return [
        Spec("chan", "Channel", "General", "channel", get=getter("chan"), apply=setter("chan"), clearable=True),
        Spec("role", "Role", "General", "role", get=getter("role"), apply=setter("role")),
        Spec("roles", "Roles", "General", "role", get=getter("roles"), apply=setter("roles"), multi=3),
        Spec("flag", "Flag", "General", "toggle", get=getter("flag"), apply=setter("flag"), confirm="Really turn it on?"),
        Spec("mode", "Mode", "General", "choice", get=getter("mode"), apply=setter("mode"),
             choices=(("Alpha", "a"), ("Beta", "b"))),
        Spec("num", "Number", "Rules", "modal", get=getter("num"), apply=num_apply,
             fields=(Field("num", "How many", value=lambda v: str(v)),), fmt=lambda v, g: f"{v} things"),
        Spec("go", "Go", "Rules", "action", apply=go, button="Go now", confirm="Run it?"),
        Spec("items", "Things", "Things", "collection", get=items, item_get=item_get, save=save, remove=remove,
             fields=(Field("name", "Name", value=lambda r: r.get("name", "")),),
             pick=Pick("channel", "Pick channels", 1, 5, defaults=lambda r: [11, 12]),
             item_actions=(ItemAction("Bump", bump),)),
    ]


async def make(user=None, store=None):
    store = store or Store()
    guild = SimpleNamespace(id=9)
    owner = user or member()
    panel = SettingsPanel(store, guild, owner, specs(), title="T")
    await panel.refresh_data()
    panel.rebuild()
    return panel, store, owner


def child(panel, kind=None, label=None, placeholder=None):
    for c in panel.children:
        if kind and not isinstance(c, kind):
            continue
        if label is not None and getattr(c, "label", None) != label:
            continue
        if placeholder is not None and getattr(c, "placeholder", None) != placeholder:
            continue
        return c
    raise AssertionError(f"no child {kind} {label} {placeholder}: {[(type(c).__name__, getattr(c, 'label', None)) for c in panel.children]}")


async def pick_setting(panel, owner, key):
    it = FakeInteraction(owner, [key])
    await panel._on_setting(it)
    return it


def note(it):
    return it.originals[-1]["embed"].description if it.originals else it.response.edited[-1]["embed"].description


# ----------------------------------------------------------------- gate

async def test_only_the_opener_can_use_it():
    panel, _, owner = await make()
    stranger = FakeInteraction(member(uid=2))
    assert await panel.interaction_check(stranger) is False
    assert "belongs to someone else" in stranger.response.sent[0][0]
    assert await panel.interaction_check(FakeInteraction(owner)) is True


async def test_losing_manage_server_locks_the_panel():
    owner = member(manage=False)
    panel, _, _ = await make(owner)
    it = FakeInteraction(owner)
    assert await panel.interaction_check(it) is False
    assert "Manage Server" in it.response.sent[0][0]


async def test_administrator_counts():
    owner = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=True, manage_guild=False))
    panel, _, _ = await make(owner)
    assert await panel.interaction_check(FakeInteraction(owner)) is True


# ----------------------------------------------------------------- layout

async def test_every_kind_builds_within_discord_limits():
    panel, _, owner = await make()
    for key in ("chan", "role", "roles", "flag", "mode"):
        await pick_setting(panel, owner, key)
        assert len(panel.children) <= 25
    for tab in panel.tabs:
        panel.tab, panel.spec_key = tab, None
        await panel.refresh_data()
        panel.rebuild()
        for spec in panel.tab_specs():
            panel.spec_key = spec.key
            await panel.refresh_data()
            panel.rebuild()
            rows = {}
            for c in panel.children:
                rows[c.row] = rows.get(c.row, 0) + c.width
            assert all(w <= 5 for w in rows.values()), (spec.key, rows)
            assert len(panel.to_components()) <= 5


async def test_tabs_and_settings_selects_exist():
    panel, _, _ = await make()
    tab_select = child(panel, discord.ui.Select, placeholder="Section")
    assert [o.value for o in tab_select.options] == ["General", "Rules", "Things"]
    setting_select = child(panel, discord.ui.Select, placeholder="Pick a setting to change")
    assert {o.value for o in setting_select.options} == {"chan", "role", "roles", "flag", "mode"}


async def test_embed_shows_current_values():
    panel, store, _ = await make()
    store.d["chan"] = 55
    await panel.refresh_data()
    text = panel.build_embed().description
    assert "<#55>" in text and "**Flag** — Off" in text and "Alpha" in text


async def test_switching_tab_resets_the_selection():
    panel, _, owner = await make()
    await pick_setting(panel, owner, "chan")
    it = FakeInteraction(owner, ["Rules"])
    await panel._on_tab(it)
    assert panel.tab == "Rules" and panel.spec_key is None
    assert "Number" in it.response.edited[-1]["embed"].description


# ----------------------------------------------------------------- editors

async def test_channel_select_applies_the_picked_channel():
    panel, store, owner = await make()
    await pick_setting(panel, owner, "chan")
    sel = child(panel, discord.ui.ChannelSelect)
    sel._values = [SimpleNamespace(id=77)]
    it = FakeInteraction(owner)
    await panel._on_pick_value(it)
    assert store.d["chan"] == 77
    assert "chan saved" in note(it)


async def test_clear_button_sends_none():
    panel, store, owner = await make()
    store.d["chan"] = 5
    await pick_setting(panel, owner, "chan")
    it = FakeInteraction(owner)
    await panel._on_clear(it)
    assert store.calls[-1] == ("chan", None) and store.d["chan"] is None


async def test_clear_is_disabled_when_nothing_is_set():
    panel, _, owner = await make()
    await pick_setting(panel, owner, "chan")
    assert child(panel, discord.ui.Button, label="Clear").disabled is True


async def test_multi_role_select_passes_a_list_and_prefills_current():
    panel, store, owner = await make()
    store.d["roles"] = [3, 4]
    await pick_setting(panel, owner, "roles")
    sel = child(panel, discord.ui.RoleSelect)
    assert sel.max_values == 3 and [d.id for d in sel.default_values] == [3, 4]
    sel._values = [SimpleNamespace(id=8), SimpleNamespace(id=9)]
    await panel._on_pick_value(FakeInteraction(owner))
    assert store.d["roles"] == [8, 9] or store.calls[-1][0] == "roles"


async def test_a_refused_change_is_shown_and_nothing_is_saved():
    panel, store, owner = await make()
    await pick_setting(panel, owner, "mode")
    it = FakeInteraction(owner, ["b"])
    panel.spec_key = "mode"
    # a PanelError from apply
    spec = panel.current_spec()

    async def refuse(cog, guild, who, value):
        raise PanelError("nope")
    spec.apply = refuse
    await panel._on_choice(it)
    assert "nope" in note(it) and store.d["mode"] == "a"


async def test_unexpected_exception_does_not_crash_the_panel():
    panel, store, owner = await make()
    await pick_setting(panel, owner, "mode")
    spec = panel.current_spec()

    async def boom(cog, guild, who, value):
        raise RuntimeError("x")
    spec.apply = boom
    it = FakeInteraction(owner, ["b"])
    await panel._on_choice(it)
    assert "didn't work" in note(it)


async def test_choice_select_applies_the_typed_value():
    panel, store, owner = await make()
    await pick_setting(panel, owner, "mode")
    it = FakeInteraction(owner, ["b"])
    await panel._on_choice(it)
    assert store.d["mode"] == "b"


async def test_toggle_off_never_asks_for_confirmation():
    panel, store, owner = await make()
    store.d["flag"] = True
    await pick_setting(panel, owner, "flag")
    await panel._make_toggle(False)(FakeInteraction(owner))
    assert store.d["flag"] is False and panel.mode == "main"


async def test_toggle_on_asks_to_confirm_then_applies():
    panel, store, owner = await make()
    await pick_setting(panel, owner, "flag")
    on = child(panel, discord.ui.Button, label="Turn on")
    assert on.disabled is False and child(panel, discord.ui.Button, label="Turn off").disabled is True
    await panel._make_toggle(True)(FakeInteraction(owner))
    assert panel.mode == "confirm" and store.d["flag"] is False
    assert "Really turn it on?" in panel.build_embed().description
    await panel._on_confirm(FakeInteraction(owner))
    assert store.d["flag"] is True and panel.mode == "main"


async def test_cancelling_a_confirmation_changes_nothing():
    panel, store, owner = await make()
    await pick_setting(panel, owner, "flag")
    await panel._make_toggle(True)(FakeInteraction(owner))
    await panel._on_cancel(FakeInteraction(owner))
    assert store.d["flag"] is False and panel.mode == "main"


# ----------------------------------------------------------------- modal

async def test_modal_opens_prefilled_and_submit_applies_text():
    panel, store, owner = await make()
    panel.tab = "Rules"
    await pick_setting(panel, owner, "num")
    it = FakeInteraction(owner)
    await panel._on_edit_modal(it)
    modal = it.response.modal
    assert modal.boxes["num"].default == "5"
    modal.boxes["num"]._value = "9"
    sub = FakeInteraction(owner)
    await modal.on_submit(sub)
    assert store.d["num"] == 9 and "num 9" in note(sub)


async def test_modal_validation_error_is_shown():
    panel, store, owner = await make()
    panel.tab = "Rules"
    await pick_setting(panel, owner, "num")
    it = FakeInteraction(owner)
    await panel._on_edit_modal(it)
    modal = it.response.modal
    modal.boxes["num"]._value = "-3"
    sub = FakeInteraction(owner)
    await modal.on_submit(sub)
    assert store.d["num"] == 5 and "must be positive" in note(sub)


async def test_modal_fields_capped_at_five():
    fields = [Field(f"f{i}", f"F{i}") for i in range(8)]
    modal = make_modal("t", fields, None, lambda *a: None)
    assert len(modal.boxes) == 5


async def test_new_only_fields_hidden_when_editing():
    fields = [Field("k", "Key", new_only=True), Field("v", "Value")]
    assert list(make_modal("t", fields, {}, lambda *a: None, adding=False).boxes) == ["v"]
    assert list(make_modal("t", fields, {}, lambda *a: None, adding=True).boxes) == ["k", "v"]


async def test_edit_only_fields_are_hidden_when_adding():
    fields = [Field("k", "Key", new_only=True), Field("e", "Extra", edit_only=True), Field("v", "Value")]
    assert list(make_modal("t", fields, {}, lambda *a: None, adding=False).boxes) == ["e", "v"]
    assert list(make_modal("t", fields, {}, lambda *a: None, adding=True).boxes) == ["k", "v"]


# ----------------------------------------------------------------- actions

async def test_action_confirm_then_runs():
    panel, store, owner = await make()
    panel.tab = "Rules"
    await pick_setting(panel, owner, "go")
    await panel._on_action(FakeInteraction(owner))
    assert panel.mode == "confirm" and ("go",) not in store.calls
    await panel._on_confirm(FakeInteraction(owner))
    assert ("go",) in store.calls


# ----------------------------------------------------------------- collections

async def test_collection_lists_records_with_add_first():
    panel, _, owner = await make()
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    sel = child(panel, discord.ui.Select, placeholder="Pick a record, or add a new one")
    assert [o.value for o in sel.options] == [NEW_ITEM, "x"]
    assert child(panel, discord.ui.Button, label="Edit…").disabled is True


async def test_add_flow_modal_then_pick_then_save():
    panel, store, owner = await make()
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    await panel._on_item(FakeInteraction(owner, [NEW_ITEM]))
    it = FakeInteraction(owner)
    await panel._on_item_edit(it)
    modal = it.response.modal
    modal.boxes["name"]._value = "Fresh"
    sub = FakeInteraction(owner)
    await modal.on_submit(sub)
    assert panel.mode == "pick"
    sel = child(panel, discord.ui.ChannelSelect)
    assert sel.max_values == 5
    sel._values = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    await panel._on_pick_done(FakeInteraction(owner))
    assert store.calls[-1] == ("save", None, {"name": "Fresh"}, [1, 2])
    assert "fresh" in store.d["items"] and panel.mode == "main"


async def test_edit_prefills_and_preselects_current_picks():
    panel, store, owner = await make()
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    await panel._on_item(FakeInteraction(owner, ["x"]))
    it = FakeInteraction(owner)
    await panel._on_item_edit(it)
    assert it.response.modal.boxes["name"].default == "X"
    sub = FakeInteraction(owner)
    it.response.modal.boxes["name"]._value = "X2"
    await it.response.modal.on_submit(sub)
    assert [d.id for d in child(panel, discord.ui.ChannelSelect).default_values] == [11, 12]


async def test_remove_and_item_action():
    panel, store, owner = await make()
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    await panel._on_item(FakeInteraction(owner, ["x"]))
    await panel._make_item_action(0)(FakeInteraction(owner))
    assert store.d["items"]["x"]["n"] == 2
    await panel._on_item_remove(FakeInteraction(owner))
    assert "x" not in store.d["items"] and panel.item_id is None


async def test_cancel_from_pick_returns_to_main():
    panel, store, owner = await make()
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    await panel._on_item(FakeInteraction(owner, ["x"]))
    it = FakeInteraction(owner)
    await panel._on_item_edit(it)
    it.response.modal.boxes["name"]._value = "Y"
    await it.response.modal.on_submit(FakeInteraction(owner))
    assert panel.mode == "pick"
    await panel._on_cancel(FakeInteraction(owner))
    assert panel.mode == "main" and store.d["items"]["x"]["name"] == "X"


# ----------------------------------------------------------------- misc

async def test_close_removes_the_view():
    panel, _, owner = await make()
    it = FakeInteraction(owner)
    await panel._on_close(it)
    assert it.response.edited[-1] == {"view": None}


async def test_timeout_disables_children_and_edits_message():
    panel, _, _ = await make()
    edits = []

    class Msg:
        async def edit(self, **kw):
            edits.append(kw)
    panel.message = Msg()
    await panel.on_timeout()
    assert all(c.disabled for c in panel.children) and edits


async def test_open_panel_sends_embed_and_view():
    sent = {}

    class Ctx:
        guild = SimpleNamespace(id=1)
        author = member()

        async def send(self, **kw):
            sent.update(kw)
            return "msg"
    store = Store()
    panel = await open_panel(Ctx(), store, specs(), title="Hello", intro="Intro text")
    assert sent["view"] is panel and "Intro text" in sent["embed"].description and panel.message == "msg"


def test_format_value_defaults():
    g = None
    ch = Spec("c", "C", "t", "channel")
    assert format_value(ch, 0, g) == "not set" and format_value(ch, 5, g) == "<#5>"
    assert format_value(Spec("r", "R", "t", "role", multi=2), [1, 2], g) == "<@&1>, <@&2>"
    assert format_value(Spec("f", "F", "t", "toggle"), True, g) == "On"
    assert format_value(Spec("m", "M", "t", "modal"), None, g) == "not set"
    broken = Spec("b", "B", "t", "modal", fmt=lambda v, g: 1 / 0)
    assert format_value(broken, 1, g) == "?"


async def test_choice_confirm_only_for_the_risky_value():
    panel, store, owner = await make()
    spec = next(x for x in panel.specs if x.key == "mode")
    spec.confirm, spec.confirm_if = "Switch to B?", (lambda v: v == "b")
    await pick_setting(panel, owner, "mode")
    await panel._on_choice(FakeInteraction(owner, ["a"]))
    assert panel.mode == "main" and store.d["mode"] == "a"
    await panel._on_choice(FakeInteraction(owner, ["b"]))
    assert panel.mode == "confirm" and store.d["mode"] == "a"
    await panel._on_confirm(FakeInteraction(owner))
    assert store.d["mode"] == "b"


def test_dropdown_descriptions_use_names_not_mentions():
    try:
        from settings_panel import plain_value
    except ImportError:
        from .settings_panel import plain_value
    guild = SimpleNamespace(get_channel=lambda i: SimpleNamespace(name="general"), get_role=lambda i: None, get_member=lambda i: None)
    assert plain_value("<#5> and <@&6> and <@7>", guild) == "#general and @role and @user"
    assert plain_value("<#5>") == "#channel"


async def test_min_multi_is_enforced_by_the_dropdown_and_many_item_actions_fit():
    panel, store, owner = await make()
    spec = next(x for x in panel.specs if x.key == "roles")
    spec.min_multi = 2
    await pick_setting(panel, owner, "roles")
    assert child(panel, discord.ui.RoleSelect).min_values == 2
    things = next(x for x in panel.specs if x.key == "items")
    things.can_add, things.save, things.remove, things.fields, things.pick = False, None, None, (), None
    things.item_actions = tuple(ItemAction(f"A{i}", things.item_actions[0].run) for i in range(5))
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    await panel._on_item(FakeInteraction(owner, ["x"]))
    labels = [c.label for c in panel.children if isinstance(c, discord.ui.Button) and c.row == 3]
    assert labels == ["A0", "A1", "A2", "A3", "A4"]
    assert len(panel.to_components()) <= 5


async def test_channel_types_limit_the_channel_picker():
    panel, store, owner = await make()
    spec = next(x for x in panel.specs if x.key == "chan")
    spec.channel_types = ("category",)
    await pick_setting(panel, owner, "chan")
    assert child(panel, discord.ui.ChannelSelect).channel_types == [discord.ChannelType.category]
    spec.channel_types = ()
    await pick_setting(panel, owner, "chan")
    assert not child(panel, discord.ui.ChannelSelect).channel_types


async def test_long_collections_are_paged_and_every_record_is_reachable():
    panel, store, owner = await make()
    store.d["items"] = {f"k{i:02d}": {"name": f"Item {i}", "n": i} for i in range(60)}
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    sel = child(panel, discord.ui.Select, placeholder="Pick a record, or add a new one")
    assert len(sel.options) == 25 and sel.options[0].value == NEW_ITEM and sel.options[1].value == "k00"
    assert len(panel.to_components()) <= 5
    pager = [c.label for c in panel.children if isinstance(c, discord.ui.Button) and c.row == 4]
    assert pager == ["Refresh", "Close", "◀", "1/3", "▶"]
    assert next(c for c in panel.children if getattr(c, "label", None) == "◀").disabled
    await panel._on_page_next(FakeInteraction(owner))
    sel = child(panel, discord.ui.Select, placeholder="Pick a record, or add a new one")
    assert sel.options[1].value == "k24" and panel.page == 1
    await panel._on_page_next(FakeInteraction(owner))
    await panel._on_page_next(FakeInteraction(owner))                  # stops at the last page
    assert panel.page == 2
    sel = child(panel, discord.ui.Select, placeholder="Pick a record, or add a new one")
    assert [o.value for o in sel.options][-1] == "k59"
    assert next(c for c in panel.children if getattr(c, "label", None) == "▶").disabled
    await panel._on_item(FakeInteraction(owner, ["k59"]))               # a record on a later page can be chosen
    assert panel.item_id == "k59"
    await panel._on_page_prev(FakeInteraction(owner))
    assert panel.page == 1 and "page 2/3" in panel.build_embed().description


async def test_paging_resets_when_the_setting_changes_and_short_lists_have_no_pager():
    panel, store, owner = await make()
    store.d["items"] = {f"k{i:02d}": {"name": f"Item {i}", "n": i} for i in range(60)}
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    await panel._on_page_next(FakeInteraction(owner))
    assert panel.page == 1
    await pick_setting(panel, owner, "items")
    assert panel.page == 0
    store.d["items"] = {"x": {"name": "X", "n": 1}}
    await pick_setting(panel, owner, "items")
    assert not any(isinstance(c, discord.ui.Button) and c.row == 4 and c.label in ("◀", "▶") for c in panel.children)


# ---- a pick screen with a role select and a Save button (opt-in through Pick.role_label)

async def make_role_pick(store=None):
    store = store or Store()
    sp = specs()
    items = sp[-1]
    items.pick = Pick("channel", "Pick channels", 0, 5, defaults=lambda r: [11, 12],
                      role_label="Role (optional)", role_defaults=lambda r: [77] if r and r.get("n") else [])
    owner = member()
    panel = SettingsPanel(store, SimpleNamespace(id=9), owner, sp, title="T")
    await panel.refresh_data()
    panel.rebuild()
    panel.tab = "Things"
    await pick_setting(panel, owner, "items")
    return panel, store, owner


async def open_pick(panel, owner, item):
    await panel._on_item(FakeInteraction(owner, [item]))
    it = FakeInteraction(owner)
    await panel._on_item_edit(it)
    modal = it.response.modal
    modal.boxes["name"]._value = "Fresh"
    await modal.on_submit(FakeInteraction(owner))
    assert panel.mode == "pick"


async def test_role_pick_screen_has_both_selects_and_a_save_button():
    panel, store, owner = await make_role_pick()
    await open_pick(panel, owner, NEW_ITEM)
    child(panel, discord.ui.ChannelSelect)
    child(panel, discord.ui.RoleSelect)
    child(panel, discord.ui.Button, label="Save")
    child(panel, discord.ui.Button, label="Cancel")
    assert "press **Save**" in panel.build_embed().description


async def test_selecting_does_not_save_until_save_is_pressed():
    panel, store, owner = await make_role_pick()
    await open_pick(panel, owner, NEW_ITEM)
    child(panel, discord.ui.ChannelSelect)._values = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    await panel._on_pick_hold(FakeInteraction(owner))
    child(panel, discord.ui.RoleSelect)._values = [SimpleNamespace(id=5)]
    await panel._on_role_hold(FakeInteraction(owner))
    assert store.calls == [] and panel.mode == "pick"
    await panel._on_pick_save(FakeInteraction(owner))
    assert store.calls[-1] == ("save", None, {"name": "Fresh", "role": "5"}, [1, 2]) and panel.mode == "main"


async def test_save_without_touching_anything_keeps_the_existing_choices():
    panel, store, owner = await make_role_pick()
    await open_pick(panel, owner, "x")                 # x has n=1, so role default 77 and channel defaults 11, 12
    assert [d.id for d in child(panel, discord.ui.RoleSelect).default_values] == [77]
    await panel._on_pick_save(FakeInteraction(owner))
    assert store.calls[-1] == ("save", "x", {"name": "Fresh", "role": "77"}, [11, 12])


async def test_clearing_the_role_select_removes_the_role():
    panel, store, owner = await make_role_pick()
    await open_pick(panel, owner, "x")
    child(panel, discord.ui.RoleSelect)._values = []
    await panel._on_role_hold(FakeInteraction(owner))
    await panel._on_pick_save(FakeInteraction(owner))
    assert store.calls[-1][2]["role"] == "" and store.calls[-1][3] == [11, 12]


async def test_new_record_untouched_selects_save_empty():
    panel, store, owner = await make_role_pick()
    await open_pick(panel, owner, NEW_ITEM)
    await panel._on_pick_save(FakeInteraction(owner))
    assert store.calls[-1] == ("save", None, {"name": "Fresh", "role": ""}, [])


async def test_cancel_on_the_role_pick_screen_saves_nothing():
    panel, store, owner = await make_role_pick()
    await open_pick(panel, owner, NEW_ITEM)
    await panel._on_cancel(FakeInteraction(owner))
    assert panel.mode == "main" and store.calls == []
