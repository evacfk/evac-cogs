"""Dropdown-driven settings UI for `.minigamehub settings`, styled after the
heist cog's "Select a parameter... -> Set Value" flow (the screenshot that
prompted this). Covers the common scalar/range fields per game type;
lootdrop/boss's scenario pools and boss's difficulty tiers stay on their
existing dedicated commands (`scenario`/`bossscenario` CRUD, `game boss
tier`) since those are larger free-form JSON blobs, not a good fit for a
short modal. Hunt's animal pool (`.minigamehub huntanimals`, below) gets its
own list-select + Add/Edit/Remove-modal view instead -- unlike the scenario
pools it's just small per-entry records (key/emoji/text), which a modal
handles fine once there's a way to pick *which* entry you're editing.

PARAM_SCHEMA maps game_key -> list of param specs:
  key:     unique id within the game, used as the Select option value
  label:   shown in the dropdown and the embed
  kind:    "range_int" | "int" | "float" | "str" | "choice"
  path:    single Config key (str) for scalar kinds, or a (min_key, max_key)
           tuple for "range_int"
  choices: allowed values, only for kind == "choice"
  unit:    optional, "minutes" -- the field is stored in seconds (Config,
           game logic) but shown/edited in minutes here, same convention as
           `.minigamehub game <key> frequency` and `.minigamehub
           activitywindow`. Reserved for "how often"/"how long the whole
           event runs" fields (spawn frequency, boss fight duration); short
           per-action timeouts (response_timeout, attack_cooldown, etc.) stay
           in seconds since minutes would just be awkward decimals there.
"""
import re
from typing import Optional

import discord

GAME_LABELS = {
    "pet": "pet",
    "mathdrop": "mathdrop",
    "hunt": "hunt",
    "lootdrop": "lootdrop",
    "reacttowin": "reacttowin",
    "boss": "boss",
}

PARAM_SCHEMA = {
    "pet": [
        {"key": "frequency", "label": "Spawn frequency (min)", "kind": "range_int", "path": ("min_frequency", "max_frequency"), "unit": "minutes"},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "window_seconds", "label": "Claim window (sec)", "kind": "int", "path": "window_seconds"},
        {"key": "pet_reaction", "label": "Pet reaction emoji", "kind": "str", "path": "pet_reaction"},
        {"key": "spawn_message", "label": "Spawn message", "kind": "str", "path": "spawn_message"},
        {"key": "goodbye_message", "label": "Goodbye message", "kind": "str", "path": "goodbye_message"},
    ],
    "mathdrop": [
        {"key": "frequency", "label": "Spawn frequency (min)", "kind": "range_int", "path": ("min_frequency", "max_frequency"), "unit": "minutes"},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "response_timeout", "label": "Answer timeout (sec)", "kind": "int", "path": "response_timeout"},
        {"key": "timeout_message", "label": "Timeout message", "kind": "str", "path": "timeout_message"},
    ],
    "hunt": [
        {"key": "frequency", "label": "Spawn frequency (min)", "kind": "range_int", "path": ("min_frequency", "max_frequency"), "unit": "minutes"},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "response_timeout", "label": "Response timeout (sec)", "kind": "int", "path": "response_timeout"},
        {"key": "trigger_mode", "label": "Trigger mode", "kind": "choice", "path": "trigger_mode", "choices": ["both", "word", "reaction"]},
        {"key": "shoot_word", "label": "Shoot word", "kind": "str", "path": "shoot_word"},
        {"key": "safe_word", "label": "Safe word", "kind": "str", "path": "safe_word"},
        {"key": "shoot_reaction", "label": "Shoot reaction emoji", "kind": "str", "path": "shoot_reaction"},
        {"key": "safe_reaction", "label": "Safe reaction emoji", "kind": "str", "path": "safe_reaction"},
    ],
    "lootdrop": [
        {"key": "frequency", "label": "Spawn frequency (min)", "kind": "range_int", "path": ("min_frequency", "max_frequency"), "unit": "minutes"},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "bad_outcome_chance", "label": "Bad outcome chance (%)", "kind": "int", "path": "bad_outcome_chance"},
        {"key": "streak_bonus", "label": "Streak bonus (% per streak)", "kind": "int", "path": "streak_bonus"},
        {"key": "streak_max", "label": "Streak max", "kind": "int", "path": "streak_max"},
        {"key": "streak_timeout", "label": "Streak timeout (hours)", "kind": "int", "path": "streak_timeout"},
        {"key": "claim_timeout", "label": "Claim timeout (sec)", "kind": "int", "path": "claim_timeout"},
        {"key": "party_drop_chance", "label": "Party drop chance (%)", "kind": "int", "path": "party_drop_chance"},
        {"key": "party_drop_range", "label": "Party drop reward range", "kind": "range_int", "path": ("party_drop_min", "party_drop_max")},
        {"key": "party_drop_timeout", "label": "Party drop timeout (sec)", "kind": "int", "path": "party_drop_timeout"},
    ],
    "reacttowin": [
        {"key": "frequency", "label": "Spawn frequency (min)", "kind": "range_int", "path": ("min_frequency", "max_frequency"), "unit": "minutes"},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "response_timeout", "label": "Click timeout (sec)", "kind": "int", "path": "response_timeout"},
        {"key": "streak_bonus_pct", "label": "Streak bonus (% per streak)", "kind": "int", "path": "streak_bonus_pct"},
        {"key": "spawn_message", "label": "Spawn message", "kind": "str", "path": "spawn_message"},
    ],
    "boss": [
        {"key": "frequency", "label": "Spawn frequency (min)", "kind": "range_int", "path": ("min_frequency", "max_frequency"), "unit": "minutes"},
        {"key": "fight_duration", "label": "Fight duration (min)", "kind": "int", "path": "fight_duration", "unit": "minutes"},
        {"key": "hp_update_interval", "label": "HP bar update interval (sec)", "kind": "int", "path": "hp_update_interval"},
        {"key": "attack_cooldown", "label": "Per-user attack cooldown (sec)", "kind": "float", "path": "attack_cooldown"},
        {"key": "hit_chance", "label": "Hit chance (%)", "kind": "int", "path": "hit_chance"},
        {"key": "damage_per_hit", "label": "Damage per hit", "kind": "range_int", "path": ("damage_per_hit.0", "damage_per_hit.1")},
    ],
}


def _get_path(game_conf: dict, path):
    """path is either "key" or "key.index" (for a [min, max] list field)."""
    if "." in path:
        key, idx = path.split(".", 1)
        return game_conf[key][int(idx)]
    return game_conf[path]


def _set_path(game_conf: dict, path, value) -> None:
    if "." in path:
        key, idx = path.split(".", 1)
        game_conf[key][int(idx)] = value
    else:
        game_conf[path] = value


def _to_display(seconds) -> float:
    return round(seconds / 60, 2)


def _to_storage(minutes) -> int:
    return round(minutes * 60)


def _display_value(game_conf: dict, spec: dict) -> str:
    unit_suffix = " min" if spec.get("unit") == "minutes" else ""
    if spec["kind"] == "range_int":
        lo = _get_path(game_conf, spec["path"][0])
        hi = _get_path(game_conf, spec["path"][1])
        if spec.get("unit") == "minutes":
            return f"{_to_display(lo):g}-{_to_display(hi):g} min"
        return f"{lo}-{hi}"
    value = _get_path(game_conf, spec["path"])
    if spec.get("unit") == "minutes":
        return f"{_to_display(value):g} min"
    return f"{value}{unit_suffix}"


class _GameSelect(discord.ui.Select):
    def __init__(self, parent_view: "ConfigView"):
        options = [discord.SelectOption(label=GAME_LABELS[k], value=k, default=(k == parent_view.game_key)) for k in PARAM_SCHEMA]
        super().__init__(placeholder="Choose a game...", options=options, min_values=1, max_values=1, row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.game_key = self.values[0]
        self.parent_view.param_key = None
        await self.parent_view.refresh(interaction)


class _ParamSelect(discord.ui.Select):
    def __init__(self, parent_view: "ConfigView", options):
        super().__init__(
            placeholder="Select a parameter...",
            options=options or [discord.SelectOption(label="(choose a game first)", value="_none")],
            min_values=1, max_values=1, row=1, disabled=not options,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.param_key = self.values[0]
        await self.parent_view.refresh(interaction)


class _SetValueButton(discord.ui.Button):
    def __init__(self, parent_view: "ConfigView"):
        super().__init__(
            label="Set Value", style=discord.ButtonStyle.primary, emoji="✏️",
            row=2, disabled=parent_view.param_key is None,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        game_conf = (await self.parent_view.config.guild(self.parent_view.guild).games())[self.parent_view.game_key]
        spec = next(s for s in PARAM_SCHEMA[self.parent_view.game_key] if s["key"] == self.parent_view.param_key)
        await interaction.response.send_modal(_ParamModal(self.parent_view, spec, game_conf))


class _ParamModal(discord.ui.Modal):
    def __init__(self, parent_view: "ConfigView", spec: dict, game_conf: dict):
        super().__init__(title=spec["label"][:45])
        self.parent_view = parent_view
        self.spec = spec
        is_minutes = spec.get("unit") == "minutes"
        if spec["kind"] == "range_int":
            lo = _get_path(game_conf, spec["path"][0])
            hi = _get_path(game_conf, spec["path"][1])
            if is_minutes:
                lo, hi = _to_display(lo), _to_display(hi)
            min_label = "Minimum (min)" if is_minutes else "Minimum"
            max_label = "Maximum (min)" if is_minutes else "Maximum"
            self.min_input = discord.ui.TextInput(label=min_label, default=f"{lo:g}")
            self.max_input = discord.ui.TextInput(label=max_label, default=f"{hi:g}")
            self.add_item(self.min_input)
            self.add_item(self.max_input)
        else:
            current = _get_path(game_conf, spec["path"])
            if is_minutes:
                current = _to_display(current)
            label = spec["label"][:45]
            if spec["kind"] == "choice":
                label = f"{label} ({'/'.join(spec['choices'])})"[:45]
            long_text = spec["kind"] == "str" and len(str(current)) > 80
            default = f"{current:g}" if is_minutes else str(current)
            self.value_input = discord.ui.TextInput(
                label=label, default=default,
                style=discord.TextStyle.paragraph if long_text else discord.TextStyle.short,
                max_length=None if long_text else 200,
            )
            self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        config = self.parent_view.config
        guild = self.parent_view.guild
        spec = self.spec
        is_minutes = spec.get("unit") == "minutes"
        try:
            async with config.guild(guild).games() as games:
                game_conf = games[self.parent_view.game_key]
                if spec["kind"] == "range_int":
                    lo = float(self.min_input.value)
                    hi = float(self.max_input.value)
                    if lo < 0 or hi < lo:
                        raise ValueError("minimum must be >= 0 and maximum >= minimum")
                    if is_minutes:
                        lo, hi = _to_storage(lo), _to_storage(hi)
                    else:
                        lo, hi = int(lo), int(hi)
                    _set_path(game_conf, spec["path"][0], lo)
                    _set_path(game_conf, spec["path"][1], hi)
                elif spec["kind"] == "int":
                    val = float(self.value_input.value)
                    _set_path(game_conf, spec["path"], _to_storage(val) if is_minutes else int(val))
                elif spec["kind"] == "float":
                    _set_path(game_conf, spec["path"], float(self.value_input.value))
                elif spec["kind"] == "choice":
                    val = self.value_input.value.strip().lower()
                    if val not in spec["choices"]:
                        raise ValueError(f"must be one of {', '.join(spec['choices'])}")
                    _set_path(game_conf, spec["path"], val)
                else:
                    _set_path(game_conf, spec["path"], self.value_input.value)
        except (ValueError, TypeError) as e:
            await interaction.response.send_message(f"Invalid value: {e}", ephemeral=True)
            return

        embed = await self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class ConfigView(discord.ui.View):
    def __init__(self, config, guild: discord.Guild, game_key: Optional[str] = None):
        super().__init__(timeout=300)
        self.config = config
        self.guild = guild
        self.game_key = game_key
        self.param_key: Optional[str] = None
        self._rebuild_items()

    def _rebuild_items(self):
        self.clear_items()
        self.add_item(_GameSelect(self))
        options = []
        if self.game_key:
            options = [discord.SelectOption(label=s["label"], value=s["key"], default=(s["key"] == self.param_key)) for s in PARAM_SCHEMA[self.game_key]]
        self.add_item(_ParamSelect(self, options))
        self.add_item(_SetValueButton(self))

    async def build_embed(self) -> discord.Embed:
        if not self.game_key:
            embed = discord.Embed(
                title="⚙️ MinigameHub Settings",
                description="Choose a game above to see and edit its settings.",
                color=discord.Color.blurple(),
            )
            return embed
        game_conf = (await self.config.guild(self.guild).games())[self.game_key]
        lines = [f"**{spec['label']}**: {_display_value(game_conf, spec)}" for spec in PARAM_SCHEMA[self.game_key]]
        embed = discord.Embed(
            title=f"⚙️ {GAME_LABELS[self.game_key]} settings",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Scenario pools and boss tiers aren't editable here -- see `scenario`/`bossscenario` and `game boss tier`. Hunt's animal pool has its own GUI: `.minigamehub huntanimals`.")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        self._rebuild_items()
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)


# --------------------------------------------------------------------- #
# Hunt animal pool GUI (`.minigamehub huntanimals`)
# --------------------------------------------------------------------- #
# A select to pick which animal you're working with, plus Add/Edit/Remove
# buttons -- Add and Edit open a modal (key+emoji+text / emoji+text), Remove
# acts immediately on whatever's selected, same no-extra-confirmation
# convention as `.minigamehub huntsafe remove` and `scenario remove`.

_ANIMAL_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def _animal_label(key: str, conf: dict) -> str:
    return f"{conf.get('emoji', '')} {key}".strip()[:100]


class _AnimalSelect(discord.ui.Select):
    def __init__(self, parent_view: "HuntAnimalsView"):
        options = [
            discord.SelectOption(
                label=_animal_label(key, conf) or key,
                description=(conf.get("text") or "")[:100] or None,
                value=key,
                default=(key == parent_view.selected_key),
            )
            for key, conf in parent_view.animals.items()
        ]
        if not options:
            options = [discord.SelectOption(label="(no animals yet -- click Add)", value="_none")]
        super().__init__(placeholder="Choose an animal to edit or remove...", options=options, min_values=1, max_values=1, row=0, disabled=not parent_view.animals)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_key = self.values[0] if self.values[0] != "_none" else None
        await self.parent_view.refresh(interaction)


class _AnimalAddButton(discord.ui.Button):
    def __init__(self, parent_view: "HuntAnimalsView"):
        super().__init__(label="Add", style=discord.ButtonStyle.success, emoji="➕", row=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_AnimalAddModal(self.parent_view))


class _AnimalEditButton(discord.ui.Button):
    def __init__(self, parent_view: "HuntAnimalsView"):
        super().__init__(
            label="Edit", style=discord.ButtonStyle.primary, emoji="✏️",
            row=1, disabled=parent_view.selected_key is None,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        key = self.parent_view.selected_key
        games = await self.parent_view.config.guild(self.parent_view.guild).games()
        current = games["hunt"]["animals"].get(key)
        if current is None:
            self.parent_view.selected_key = None
            await self.parent_view.refresh(interaction)
            return
        await interaction.response.send_modal(_AnimalEditModal(self.parent_view, key, current))


class _AnimalRemoveButton(discord.ui.Button):
    def __init__(self, parent_view: "HuntAnimalsView"):
        super().__init__(
            label="Remove", style=discord.ButtonStyle.danger, emoji="\U0001F5D1",
            row=1, disabled=parent_view.selected_key is None,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        key = self.parent_view.selected_key
        async with self.parent_view.config.guild(self.parent_view.guild).games() as games:
            games["hunt"]["animals"].pop(key, None)
            # Drop any matching safe_animals entry too, so removing an animal
            # from the pool doesn't leave an orphaned penalty/reward config
            # behind that nothing can ever trigger again.
            games["hunt"]["safe_animals"].pop(key, None)
            self.parent_view.animals = games["hunt"]["animals"]
        self.parent_view.selected_key = None
        await self.parent_view.refresh(interaction)


class _AnimalAddModal(discord.ui.Modal):
    def __init__(self, parent_view: "HuntAnimalsView"):
        super().__init__(title="Add new animal")
        self.parent_view = parent_view
        self.key_input = discord.ui.TextInput(label="Key (lowercase, no spaces -- e.g. tiger)", max_length=32)
        self.emoji_input = discord.ui.TextInput(label="Emoji", max_length=100)
        self.text_input = discord.ui.TextInput(label="Spawn text (e.g. **_Roar!_**)", max_length=200)
        self.add_item(self.key_input)
        self.add_item(self.emoji_input)
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip().lower()
        if not _ANIMAL_KEY_RE.match(key):
            await interaction.response.send_message(
                "Key must be 1-32 characters: lowercase letters, numbers, and underscores only.", ephemeral=True
            )
            return
        async with self.parent_view.config.guild(self.parent_view.guild).games() as games:
            animals = games["hunt"]["animals"]
            if key in animals:
                await interaction.response.send_message(
                    f"`{key}` already exists -- pick a different key, or select it and click Edit instead.",
                    ephemeral=True,
                )
                return
            animals[key] = {"emoji": self.emoji_input.value.strip(), "text": self.text_input.value.strip()}
            self.parent_view.animals = animals
        self.parent_view.selected_key = key
        self.parent_view._rebuild_items()
        embed = self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class _AnimalEditModal(discord.ui.Modal):
    def __init__(self, parent_view: "HuntAnimalsView", key: str, current: dict):
        super().__init__(title=f"Edit: {key}"[:45])
        self.parent_view = parent_view
        self.key = key
        self.emoji_input = discord.ui.TextInput(label="Emoji", default=current.get("emoji", ""), max_length=100)
        self.text_input = discord.ui.TextInput(label="Spawn text", default=current.get("text", ""), max_length=200)
        self.add_item(self.emoji_input)
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        async with self.parent_view.config.guild(self.parent_view.guild).games() as games:
            animals = games["hunt"]["animals"]
            if self.key not in animals:
                await interaction.response.send_message("That animal was removed by someone else in the meantime.", ephemeral=True)
                return
            animals[self.key] = {"emoji": self.emoji_input.value.strip(), "text": self.text_input.value.strip()}
            self.parent_view.animals = animals
        self.parent_view._rebuild_items()
        embed = self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class HuntAnimalsView(discord.ui.View):
    """`.minigamehub huntanimals` -- add/edit/remove hunt's animal pool
    (key, emoji, spawn text) without touching JSON."""

    def __init__(self, config, guild: discord.Guild, animals: dict):
        super().__init__(timeout=300)
        self.config = config
        self.guild = guild
        self.animals = animals
        self.selected_key: Optional[str] = None
        self._rebuild_items()

    def _rebuild_items(self):
        self.clear_items()
        self.add_item(_AnimalSelect(self))
        self.add_item(_AnimalAddButton(self))
        self.add_item(_AnimalEditButton(self))
        self.add_item(_AnimalRemoveButton(self))

    def build_embed(self) -> discord.Embed:
        if not self.animals:
            desc = "No animals in the pool yet -- click **Add** to create one."
        else:
            desc = "\n".join(f"{_animal_label(key, conf)} -- {conf.get('text', '')}" for key, conf in self.animals.items())
        embed = discord.Embed(title="\U0001F985 Hunt animal pool", description=desc, color=discord.Color.blurple())
        embed.set_footer(text="Safe-animal penalty/reward (who gets fined for shooting, who pays for saluting) -- see `.minigamehub huntsafe`.")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
