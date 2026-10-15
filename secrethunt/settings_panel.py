"""Generic admin settings panel for Red cogs: one embed, a few dropdowns, no typed setter commands.

This file is deliberately identical in every cog that uses it (cogs install independently, so
there is no shared library). The repo-root test `test_panel_sync.py` fails if a copy drifts; edit
`_panel/settings_panel.py` and run `python _panel/sync.py` to copy it into the cogs.

A cog describes its settings as a list of `Spec`s and calls `open_panel(...)`. Each spec belongs
to a tab and has a kind:

  channel / role    a ChannelSelect / RoleSelect (multi=N allows up to N picks)
  toggle            On / Off buttons
  choice            a dropdown of fixed choices
  modal             an "Edit" button that opens a form of one to five text fields
  action            a button that runs something once (optionally behind a confirm step)
  collection        a list of records with Add / Edit / Remove (and per-record buttons)

Every callback gets the cog first and raises `PanelError("message")` to refuse a change; the
message is shown in the panel. The panel only edits through the cog's own `_apply_*` methods, so
the validation is the same one the commands used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

import discord

log = logging.getLogger("red.evac-cogs.settings_panel")

PANEL_TIMEOUT = 900
NEW_ITEM = "__new__"
MAX_VALUE_CHARS = 180
MAX_OPTION_DESC = 100
MAX_MODAL_FIELDS = 5


class PanelError(Exception):
    """Raised by a spec callback to refuse a change. The text is shown to the admin as-is."""


@dataclass
class Field:
    """One text box in a modal."""
    key: str
    label: str
    value: Optional[Callable[[Any], str]] = None   # current value -> prefilled text
    style: str = "short"                            # "short" | "long"
    required: bool = True
    max_length: Optional[int] = None
    placeholder: str = ""
    new_default: str = ""                           # prefill when adding a new collection record
    new_only: bool = False                          # collections: only asked when adding
    edit_only: bool = False                         # collections: only asked when editing an existing record


@dataclass
class Pick:
    """A channel/role select shown after a collection modal (or instead of one)."""
    kind: str                                       # "channel" | "role"
    label: str
    min_values: int = 1
    max_values: int = 1
    defaults: Optional[Callable[[Any], Sequence[int]]] = None   # record values -> preselected ids


@dataclass
class ItemAction:
    """A button that acts on the selected record of a collection."""
    label: str
    run: Callable[..., Awaitable[str]]              # (cog, guild, member, item_id) -> message
    style: str = "secondary"
    confirm: str = ""


@dataclass
class Spec:
    key: str
    label: str
    tab: str
    kind: str
    get: Optional[Callable[..., Awaitable[Any]]] = None      # (cog, guild) -> current value
    apply: Optional[Callable[..., Awaitable[str]]] = None    # (cog, guild, member, value) -> message
    fmt: Optional[Callable[[Any, Any], str]] = None          # (value, guild) -> display text
    fields: Sequence[Field] = ()
    choices: Sequence[tuple] = ()                            # (label, value)
    multi: int = 1                                           # channel/role: how many may be picked
    min_multi: int = 1                                       # channel/role: how many must be picked
    channel_types: Sequence[str] = ()                        # channel: limit the picker to these discord.ChannelType names, e.g. ("category",)
    clearable: bool = False                                  # channel/role: allow "Clear"
    confirm: str = ""                                        # non-empty: ask before applying
    confirm_if: Optional[Callable[[Any], bool]] = None       # which values need the confirm (toggles: only On)
    help: str = ""
    button: str = ""                                         # action / modal button text
    # collection
    item_get: Optional[Callable[..., Awaitable[dict]]] = None   # (cog, guild, item_id) -> record values
    save: Optional[Callable[..., Awaitable[str]]] = None        # (cog, guild, member, item_id|None, values, picked) -> message
    remove: Optional[Callable[..., Awaitable[str]]] = None      # (cog, guild, member, item_id) -> message
    pick: Optional[Pick] = None
    item_actions: Sequence[ItemAction] = ()
    can_add: bool = True


# --------------------------------------------------------------------- display


def clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mention(kind: str, value: int) -> str:
    return f"<#{value}>" if kind == "channel" else f"<@&{value}>"


def format_value(spec: Spec, value: Any, guild) -> str:
    """Human text for a spec's current value."""
    if spec.fmt is not None:
        try:
            return spec.fmt(value, guild)
        except Exception:  # a broken formatter must never take the panel down
            log.exception("settings_panel: formatter for %s failed", spec.key)
            return "?"
    if spec.kind in ("channel", "role"):
        ids = [value] if isinstance(value, int) else list(value or [])
        ids = [i for i in ids if i]
        return ", ".join(_mention(spec.kind, i) for i in ids) if ids else "not set"
    if spec.kind == "toggle":
        return "On" if value else "Off"
    if spec.kind == "choice":
        for label, val in spec.choices:
            if val == value:
                return label
        return str(value)
    if spec.kind == "collection":
        return f"{len(value or [])} item(s)"
    if spec.kind == "action":
        return ""
    return "not set" if value in (None, "") else str(value)


def plain_value(text: str, guild=None) -> str:
    """Turn Discord mention syntax into names a dropdown description can show."""
    import re

    def name_of(getter, prefix, fallback):
        def sub(match):
            found = getter(int(match.group(1))) if getter is not None else None
            return f"{prefix}{found.name}" if found is not None and getattr(found, "name", None) else fallback
        return sub

    text = re.sub(r"<#(\d+)>", name_of(getattr(guild, "get_channel", None), "#", "#channel"), text)
    text = re.sub(r"<@&(\d+)>", name_of(getattr(guild, "get_role", None), "@", "@role"), text)
    text = re.sub(r"<@!?(\d+)>", name_of(getattr(guild, "get_member", None), "@", "@user"), text)
    return text.replace("\n", " ")


def build_option_description(spec: Spec, shown: str, guild=None) -> Optional[str]:
    shown = plain_value(shown, guild).strip()
    if not shown:
        return clip(spec.help, MAX_OPTION_DESC) if spec.help else None
    return clip(shown, MAX_OPTION_DESC)


# ----------------------------------------------------------------------- modal


def make_modal(title: str, fields: Sequence[Field], current: Any, on_submit, adding: bool = False):
    """Build a modal at call time (so importing this module needs no discord.ui.Modal)."""

    class _Modal(discord.ui.Modal):
        async def on_submit(self, interaction):
            await on_submit(interaction, {k: str(box.value) for k, box in self.boxes.items()})

        async def on_error(self, interaction, error):
            log.error("settings_panel: modal failed", exc_info=error)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Something went wrong saving that.", ephemeral=True)
                else:
                    await interaction.response.send_message("Something went wrong saving that.", ephemeral=True)
            except Exception:
                pass

    modal = _Modal(title=clip(title, 45), timeout=PANEL_TIMEOUT)
    modal.boxes = {}
    shown = [f for f in fields if (not f.edit_only if adding else not f.new_only)][:MAX_MODAL_FIELDS]
    for f in shown:
        default = ""
        if adding:
            default = f.new_default
        elif f.value is not None:
            try:
                default = f.value(current)
            except Exception:
                default = ""
        box = discord.ui.TextInput(
            label=clip(f.label, 45),
            default=clip(default, f.max_length or 4000) if default else None,
            style=discord.TextStyle.paragraph if f.style == "long" else discord.TextStyle.short,
            required=f.required,
            max_length=f.max_length,
            placeholder=clip(f.placeholder, 100) if f.placeholder else None,
        )
        modal.add_item(box)
        modal.boxes[f.key] = box
    return modal


# ----------------------------------------------------------------------- panel


class SettingsPanel(discord.ui.View):
    """The panel message. Rows: 0 tab dropdown, 1 setting dropdown, 2-3 the editor for the selected setting."""

    def __init__(self, cog, guild, owner, specs: Sequence[Spec], *, title: str, intro: str = "",
                 timeout: float = PANEL_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog, self.guild, self.owner = cog, guild, owner
        self.specs = list(specs)
        self.title, self.intro = title, intro
        self.tabs = list(dict.fromkeys(s.tab for s in self.specs))
        self.tab = self.tabs[0]
        self.spec_key: Optional[str] = None
        self.item_id: Optional[str] = None
        self.mode = "main"            # "main" | "confirm" | "pick"
        self.pending: Optional[dict] = None
        self.note = ""
        self.message = None
        self.values: dict[str, Any] = {}
        self.items: list = []
        self.page = 0                 # collections longer than one dropdown are paged
        self._value_select = None
        self._pick_select = None
        self._pager = None

    # ---- lookups

    def tab_specs(self) -> list:
        return [s for s in self.specs if s.tab == self.tab]

    def current_spec(self) -> Optional[Spec]:
        for s in self.specs:
            if s.key == self.spec_key and s.tab == self.tab:
                return s
        return None

    def page_capacity(self, spec: Spec) -> int:
        return 25 - (1 if spec.can_add else 0)

    def page_count(self, spec: Spec) -> int:
        return max(1, -(-len(self.items) // self.page_capacity(spec)))

    def page_items(self, spec: Spec) -> list:
        cap = self.page_capacity(spec)
        self.page = max(0, min(self.page, self.page_count(spec) - 1))
        return self.items[self.page * cap:(self.page + 1) * cap]

    # ---- permission gate

    async def interaction_check(self, interaction) -> bool:
        user = interaction.user
        if getattr(user, "id", None) != getattr(self.owner, "id", None):
            await self._deny(interaction, "This panel belongs to someone else. Run the command yourself to open your own.")
            return False
        perms = getattr(user, "guild_permissions", None)
        if perms is None or not (perms.administrator or perms.manage_guild):
            await self._deny(interaction, "You need Manage Server to change these settings.")
            return False
        return True

    @staticmethod
    async def _deny(interaction, text: str) -> None:
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass

    async def on_error(self, interaction, error, item) -> None:
        log.error("settings_panel: callback failed", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong. Try again.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong. Try again.", ephemeral=True)
        except Exception:
            pass

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    # ---- rendering

    async def refresh_data(self) -> None:
        self.values = {}
        for spec in self.tab_specs():
            if spec.get is None:
                self.values[spec.key] = None
                continue
            try:
                self.values[spec.key] = await spec.get(self.cog, self.guild)
            except PanelError as exc:
                self.values[spec.key] = None
                log.warning("settings_panel: %s unavailable: %s", spec.key, exc)
            except Exception:
                log.exception("settings_panel: reading %s failed", spec.key)
                self.values[spec.key] = None
        spec = self.current_spec()
        self.items = []
        if spec is not None and spec.kind == "collection":
            self.items = list(self.values.get(spec.key) or [])

    def build_embed(self) -> discord.Embed:
        lines = []
        for spec in self.tab_specs():
            if spec.kind == "action":
                lines.append(f"▸ **{spec.label}**" + (f" — {spec.help}" if spec.help else ""))
                continue
            if spec.kind == "collection":
                items = self.values.get(spec.key) or []
                paged = spec is self.current_spec() and self.page_count(spec) > 1
                shown_items = self.page_items(spec) if paged else items
                lines.append(f"**{spec.label}** — {len(items)}" + (f" (page {self.page + 1}/{self.page_count(spec)})" if paged else ""))
                for item in shown_items[:12]:
                    lines.append(f" • {clip(item[1], 80)}" + (f" — {clip(item[2], 90)}" if len(item) > 2 and item[2] else ""))
                if len(shown_items) > 12:
                    lines.append(f" … and {len(shown_items) - 12} more{' on this page' if paged else ''}")
                continue
            shown = format_value(spec, self.values.get(spec.key), self.guild)
            lines.append(f"**{spec.label}** — {clip(shown, MAX_VALUE_CHARS)}")
        body = "\n".join(lines) or "Nothing here yet."
        if self.mode == "confirm" and self.pending:
            body = f"⚠️ **{self.pending['text']}**\n\nPress **Confirm** to go ahead or **Cancel** to leave it as it is."
        elif self.mode == "pick" and self.pending:
            body = f"**{self.pending['label']}**\nChoose below, then it saves."
        body = clip(body, 3800)
        parts = []
        if self.intro and self.mode == "main":
            parts.append(self.intro)
        parts.append(body)
        if self.note:
            parts.append(self.note)
        embed = discord.Embed(title=clip(f"{self.title} — {self.tab}", 250), description=clip("\n\n".join(parts), 4000))
        spec = self.current_spec()
        if spec is not None and spec.help and self.mode == "main":
            embed.set_footer(text=clip(spec.help, 200))
        return embed

    def rebuild(self) -> None:
        self.clear_items()
        self._value_select = self._pick_select = None
        self._pager = None
        if self.mode == "confirm":
            self._add_button("Confirm", "success", self._on_confirm, row=0)
            self._add_button("Cancel", "secondary", self._on_cancel, row=0)
            return
        if self.mode == "pick":
            self._add_pick_select()
            self._add_button("Cancel", "secondary", self._on_cancel, row=1)
            return
        self._add_tab_select()
        self._add_setting_select()
        spec = self.current_spec()
        if spec is not None:
            self._add_editor(spec)
        self._add_button("Refresh", "secondary", self._on_refresh, row=4)
        self._add_button("Close", "danger", self._on_close, row=4)
        if self._pager is not None:
            page, pages = self._pager
            self._add_button("\u25c0", "secondary", self._on_page_prev, row=4, disabled=page <= 0)
            self._add_button(f"{page + 1}/{pages}", "secondary", self._on_refresh, row=4, disabled=True)
            self._add_button("\u25b6", "secondary", self._on_page_next, row=4, disabled=page >= pages - 1)

    # ---- item builders

    def _add_button(self, label, style, callback, *, row, disabled=False) -> None:
        button = discord.ui.Button(label=label, style=getattr(discord.ButtonStyle, style), row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def _add_tab_select(self) -> None:
        if len(self.tabs) < 2:
            return
        options = [discord.SelectOption(label=clip(t, 100), value=t, default=(t == self.tab)) for t in self.tabs[:25]]
        select = discord.ui.Select(placeholder="Section", options=options, row=0)
        select.callback = self._on_tab
        self.add_item(select)

    def _add_setting_select(self) -> None:
        specs = self.tab_specs()[:25]
        if not specs:
            return
        options = []
        for spec in specs:
            shown = format_value(spec, self.values.get(spec.key), self.guild) if spec.kind != "action" else ""
            options.append(discord.SelectOption(
                label=clip(spec.label, 100), value=spec.key,
                description=build_option_description(spec, shown, self.guild),
                default=(spec.key == self.spec_key),
            ))
        select = discord.ui.Select(placeholder="Pick a setting to change", options=options, row=1)
        select.callback = self._on_setting
        self.add_item(select)

    def _add_editor(self, spec: Spec) -> None:
        kind = spec.kind
        if kind in ("channel", "role"):
            cls = discord.ui.ChannelSelect if kind == "channel" else discord.ui.RoleSelect
            current = self.values.get(spec.key)
            ids = [current] if isinstance(current, int) else list(current or [])
            ids = [i for i in ids if i][: spec.multi]
            kwargs = dict(placeholder=f"Choose {spec.label.lower()}", min_values=max(1, min(spec.min_multi, spec.multi)),
                          max_values=max(1, spec.multi), row=2)
            if kind == "channel" and spec.channel_types:
                kwargs["channel_types"] = [getattr(discord.ChannelType, name) for name in spec.channel_types]
            if ids:
                dtype = discord.SelectDefaultValueType.channel if kind == "channel" else discord.SelectDefaultValueType.role
                kwargs["default_values"] = [discord.SelectDefaultValue(id=i, type=dtype) for i in ids]
            select = cls(**kwargs)
            select.callback = self._on_pick_value
            self._value_select = select
            self.add_item(select)
            if spec.clearable:
                self._add_button("Clear", "secondary", self._on_clear, row=3, disabled=not ids)
        elif kind == "toggle":
            on = bool(self.values.get(spec.key))
            self._add_button("Turn on", "success", self._make_toggle(True), row=2, disabled=on)
            self._add_button("Turn off", "danger", self._make_toggle(False), row=2, disabled=not on)
        elif kind == "choice":
            current = self.values.get(spec.key)
            options = [discord.SelectOption(label=clip(lab, 100), value=str(val), default=(val == current))
                       for lab, val in spec.choices[:25]]
            select = discord.ui.Select(placeholder=f"Choose {spec.label.lower()}", options=options, row=2)
            select.callback = self._on_choice
            self.add_item(select)
        elif kind == "modal":
            self._add_button(spec.button or "Edit…", "primary", self._on_edit_modal, row=2)
        elif kind == "action":
            self._add_button(spec.button or spec.label, "primary", self._on_action, row=2)
        elif kind == "collection":
            self._add_collection_editor(spec)

    def _add_collection_editor(self, spec: Spec) -> None:
        options = []
        if spec.can_add:
            options.append(discord.SelectOption(label="➕ Add new", value=NEW_ITEM, default=(self.item_id == NEW_ITEM)))
        for item in self.page_items(spec):
            options.append(discord.SelectOption(
                label=clip(item[1], 100), value=str(item[0]),
                description=clip(plain_value(item[2], self.guild), MAX_OPTION_DESC) if len(item) > 2 and item[2] else None,
                default=(str(item[0]) == self.item_id)))
        if not options:
            return
        select = discord.ui.Select(placeholder="Pick a record, or add a new one", options=options, row=2)
        select.callback = self._on_item
        self.add_item(select)
        chosen = self.item_id is not None
        adding = self.item_id == NEW_ITEM
        has_form = bool(spec.fields) or spec.pick is not None
        used = 0
        if has_form and spec.save is not None:
            self._add_button("Add…" if adding else "Edit…", "primary", self._on_item_edit, row=3, disabled=not chosen)
            used += 1
        if spec.remove is not None:
            self._add_button("Remove", "danger", self._on_item_remove, row=3, disabled=not chosen or adding)
            used += 1
        for index, action in enumerate(list(spec.item_actions)[: 5 - used]):
            self._add_button(action.label, action.style, self._make_item_action(index), row=3, disabled=not chosen or adding)
        if self.page_count(spec) > 1:
            self._pager = (self.page, self.page_count(spec))

    def _add_pick_select(self) -> None:
        pick = self.pending["pick"]
        cls = discord.ui.ChannelSelect if pick.kind == "channel" else discord.ui.RoleSelect
        kwargs = dict(placeholder=clip(pick.label, 100), min_values=pick.min_values, max_values=pick.max_values, row=0)
        ids = self.pending.get("defaults") or []
        if ids:
            dtype = discord.SelectDefaultValueType.channel if pick.kind == "channel" else discord.SelectDefaultValueType.role
            kwargs["default_values"] = [discord.SelectDefaultValue(id=i, type=dtype) for i in ids[: pick.max_values]]
        select = cls(**kwargs)
        select.callback = self._on_pick_done
        self._pick_select = select
        self.add_item(select)

    # ---- core: run a change and redraw

    async def redraw(self, interaction, *, note: Optional[str] = None) -> None:
        if note is not None:
            self.note = note
        await self.refresh_data()
        self.rebuild()
        embed = self.build_embed()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            log.exception("settings_panel: redraw failed")

    async def _defer(self, interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def _run(self, interaction, coro_factory) -> None:
        """Run an apply callback, turning PanelError into a visible note, then redraw."""
        await self._defer(interaction)
        try:
            message = await coro_factory()
            note = f"✅ {message}" if message else ""
        except PanelError as exc:
            note = f"⚠️ {exc}"
        except Exception:
            log.exception("settings_panel: change failed")
            note = "⚠️ That didn't work. Check the log channel."
        await self.redraw(interaction, note=note)

    # ---- callbacks: navigation

    async def _on_tab(self, interaction) -> None:
        self.tab = interaction.data["values"][0]
        self.spec_key, self.item_id, self.note, self.page = None, None, "", 0
        await self.redraw(interaction)

    async def _on_setting(self, interaction) -> None:
        self.spec_key = interaction.data["values"][0]
        self.item_id, self.note, self.page = None, "", 0
        await self.redraw(interaction)

    async def _on_item(self, interaction) -> None:
        self.item_id = interaction.data["values"][0]
        self.note = ""
        await self.redraw(interaction)

    async def _on_page_prev(self, interaction) -> None:
        self.page = max(0, self.page - 1)
        await self.redraw(interaction)

    async def _on_page_next(self, interaction) -> None:
        self.page += 1
        await self.redraw(interaction)

    async def _on_refresh(self, interaction) -> None:
        await self.redraw(interaction, note="")

    async def _on_close(self, interaction) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            pass

    async def _on_cancel(self, interaction) -> None:
        self.mode, self.pending = "main", None
        await self.redraw(interaction, note="Cancelled.")

    # ---- callbacks: value editors

    async def _on_pick_value(self, interaction) -> None:
        spec = self.current_spec()
        picked = list(self._value_select.values) if self._value_select is not None else []
        value = picked if spec.multi > 1 else (picked[0] if picked else None)
        await self._apply(interaction, spec, value)

    async def _on_clear(self, interaction) -> None:
        spec = self.current_spec()
        await self._apply(interaction, spec, [] if spec.multi > 1 else None)

    def _make_toggle(self, state: bool):
        async def callback(interaction) -> None:
            await self._apply(interaction, self.current_spec(), state)
        return callback

    async def _on_choice(self, interaction) -> None:
        spec = self.current_spec()
        raw = interaction.data["values"][0]
        value = next((val for _label, val in spec.choices if str(val) == raw), raw)
        await self._apply(interaction, spec, value)

    async def _apply(self, interaction, spec: Spec, value) -> None:
        if spec.confirm and self._needs_confirm(spec, value):
            self.mode = "confirm"
            self.pending = {"text": spec.confirm, "run": ("apply", spec.key, value)}
            await self.redraw(interaction)
            return
        await self._run(interaction, lambda: spec.apply(self.cog, self.guild, interaction.user, value))

    @staticmethod
    def _needs_confirm(spec: Spec, value) -> bool:
        # Confirm guards the risky direction only: turning a toggle On, or whatever confirm_if picks.
        if spec.confirm_if is not None:
            return bool(spec.confirm_if(value))
        if spec.kind == "toggle":
            return bool(value)
        return True

    async def _on_edit_modal(self, interaction) -> None:
        spec = self.current_spec()

        async def submit(modal_interaction, values: dict) -> None:
            await self._run(modal_interaction, lambda: spec.apply(self.cog, self.guild, modal_interaction.user, values))

        modal = make_modal(spec.label, spec.fields, self.values.get(spec.key), submit)
        await interaction.response.send_modal(modal)

    async def _on_action(self, interaction) -> None:
        spec = self.current_spec()
        if spec.confirm:
            self.mode = "confirm"
            self.pending = {"text": spec.confirm, "run": ("action", spec.key, None)}
            await self.redraw(interaction)
            return
        await self._run(interaction, lambda: spec.apply(self.cog, self.guild, interaction.user, None))

    # ---- callbacks: collections

    async def _record(self, spec: Spec) -> dict:
        if self.item_id in (None, NEW_ITEM) or spec.item_get is None:
            return {}
        return await spec.item_get(self.cog, self.guild, self.item_id) or {}

    async def _on_item_edit(self, interaction) -> None:
        spec = self.current_spec()
        adding = self.item_id == NEW_ITEM
        try:
            record = await self._record(spec)
        except PanelError as exc:
            await self.redraw(interaction, note=f"⚠️ {exc}")
            return
        if spec.fields:
            async def submit(modal_interaction, values: dict) -> None:
                if spec.pick is not None:
                    await self._defer(modal_interaction)
                    self.mode = "pick"
                    self.pending = {"pick": spec.pick, "label": spec.pick.label, "values": values,
                                    "item": None if adding else self.item_id, "spec": spec.key,
                                    "defaults": list(spec.pick.defaults(record)) if spec.pick.defaults and record else []}
                    await self.redraw(modal_interaction)
                    return
                await self._run(modal_interaction, lambda: spec.save(
                    self.cog, self.guild, modal_interaction.user, None if adding else self.item_id, values, []))

            modal = make_modal(spec.label, spec.fields, record, submit, adding=adding)
            await interaction.response.send_modal(modal)
            return
        # pick-only record (no text fields)
        await self._defer(interaction)
        self.mode = "pick"
        self.pending = {"pick": spec.pick, "label": spec.pick.label, "values": {}, "item": None if adding else self.item_id,
                        "spec": spec.key, "defaults": list(spec.pick.defaults(record)) if spec.pick.defaults and record else []}
        await self.redraw(interaction)

    async def _on_pick_done(self, interaction) -> None:
        pending, spec = self.pending, self.current_spec()
        picked = list(self._pick_select.values) if self._pick_select is not None else []
        self.mode, self.pending = "main", None
        await self._run(interaction, lambda: spec.save(
            self.cog, self.guild, interaction.user, pending["item"], pending["values"], picked))

    async def _on_item_remove(self, interaction) -> None:
        spec = self.current_spec()
        item = self.item_id
        self.item_id = None
        await self._run(interaction, lambda: spec.remove(self.cog, self.guild, interaction.user, item))

    def _make_item_action(self, index: int):
        async def callback(interaction) -> None:
            spec = self.current_spec()
            action = spec.item_actions[index]
            if action.confirm:
                self.mode = "confirm"
                self.pending = {"text": action.confirm, "run": ("item_action", spec.key, index)}
                await self.redraw(interaction)
                return
            item = self.item_id
            await self._run(interaction, lambda: action.run(self.cog, self.guild, interaction.user, item))
        return callback

    # ---- confirm step

    async def _on_confirm(self, interaction) -> None:
        pending, self.mode, self.pending = self.pending, "main", None
        kind, key, arg = pending["run"]
        spec = next(s for s in self.specs if s.key == key)
        if kind == "apply":
            await self._run(interaction, lambda: spec.apply(self.cog, self.guild, interaction.user, arg))
        elif kind == "action":
            await self._run(interaction, lambda: spec.apply(self.cog, self.guild, interaction.user, None))
        else:
            action, item = spec.item_actions[arg], self.item_id
            await self._run(interaction, lambda: action.run(self.cog, self.guild, interaction.user, item))


# ------------------------------------------------------------------- entry point


async def open_panel(ctx, cog, specs: Sequence[Spec], *, title: str, intro: str = "") -> SettingsPanel:
    """Send the panel as a reply to `ctx`. Only the invoker can use it."""
    panel = SettingsPanel(cog, ctx.guild, ctx.author, specs, title=title, intro=intro)
    await panel.refresh_data()
    panel.rebuild()
    panel.message = await ctx.send(embed=panel.build_embed(), view=panel)
    return panel
