"""Dropdown-driven settings UI for `.minigamehub settings`, styled after the
heist cog's "Select a parameter... -> Set Value" flow (the screenshot that
prompted this). Covers the common scalar/range fields per game type;
anything structurally bigger -- hunt's animal pool, lootdrop/boss's
scenario pools, boss's difficulty tiers -- stays on its existing dedicated
command (`game <key> settings` for raw JSON, `scenario`/`bossscenario`
CRUD, `game boss tier`) since a text-input modal isn't a good fit for
nested/list data.

PARAM_SCHEMA maps game_key -> list of param specs:
  key:     unique id within the game, used as the Select option value
  label:   shown in the dropdown and the embed
  kind:    "range_int" | "int" | "float" | "str" | "choice"
  path:    single Config key (str) for scalar kinds, or a (min_key, max_key)
           tuple for "range_int"
  choices: allowed values, only for kind == "choice"
"""
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
        {"key": "frequency", "label": "Spawn frequency (sec)", "kind": "range_int", "path": ("min_frequency", "max_frequency")},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "window_seconds", "label": "Claim window (sec)", "kind": "int", "path": "window_seconds"},
        {"key": "pet_reaction", "label": "Pet reaction emoji", "kind": "str", "path": "pet_reaction"},
        {"key": "spawn_message", "label": "Spawn message", "kind": "str", "path": "spawn_message"},
        {"key": "goodbye_message", "label": "Goodbye message", "kind": "str", "path": "goodbye_message"},
    ],
    "mathdrop": [
        {"key": "frequency", "label": "Spawn frequency (sec)", "kind": "range_int", "path": ("min_frequency", "max_frequency")},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "response_timeout", "label": "Answer timeout (sec)", "kind": "int", "path": "response_timeout"},
        {"key": "timeout_message", "label": "Timeout message", "kind": "str", "path": "timeout_message"},
    ],
    "hunt": [
        {"key": "frequency", "label": "Spawn frequency (sec)", "kind": "range_int", "path": ("min_frequency", "max_frequency")},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "response_timeout", "label": "Response timeout (sec)", "kind": "int", "path": "response_timeout"},
        {"key": "trigger_mode", "label": "Trigger mode", "kind": "choice", "path": "trigger_mode", "choices": ["both", "word", "reaction"]},
        {"key": "shoot_word", "label": "Shoot word", "kind": "str", "path": "shoot_word"},
        {"key": "safe_word", "label": "Safe word", "kind": "str", "path": "safe_word"},
        {"key": "shoot_reaction", "label": "Shoot reaction emoji", "kind": "str", "path": "shoot_reaction"},
        {"key": "safe_reaction", "label": "Safe reaction emoji", "kind": "str", "path": "safe_reaction"},
    ],
    "lootdrop": [
        {"key": "frequency", "label": "Spawn frequency (sec)", "kind": "range_int", "path": ("min_frequency", "max_frequency")},
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
        {"key": "frequency", "label": "Spawn frequency (sec)", "kind": "range_int", "path": ("min_frequency", "max_frequency")},
        {"key": "reward_range", "label": "Reward range", "kind": "range_int", "path": ("reward_range.0", "reward_range.1")},
        {"key": "response_timeout", "label": "Click timeout (sec)", "kind": "int", "path": "response_timeout"},
        {"key": "streak_bonus_pct", "label": "Streak bonus (% per streak)", "kind": "int", "path": "streak_bonus_pct"},
        {"key": "spawn_message", "label": "Spawn message", "kind": "str", "path": "spawn_message"},
    ],
    "boss": [
        {"key": "frequency", "label": "Spawn frequency (sec)", "kind": "range_int", "path": ("min_frequency", "max_frequency")},
        {"key": "fight_duration", "label": "Fight duration (sec)", "kind": "int", "path": "fight_duration"},
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


def _display_value(game_conf: dict, spec: dict) -> str:
    if spec["kind"] == "range_int":
        lo = _get_path(game_conf, spec["path"][0])
        hi = _get_path(game_conf, spec["path"][1])
        return f"{lo}-{hi}"
    return str(_get_path(game_conf, spec["path"]))


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
        if spec["kind"] == "range_int":
            lo = _get_path(game_conf, spec["path"][0])
            hi = _get_path(game_conf, spec["path"][1])
            self.min_input = discord.ui.TextInput(label="Minimum", default=str(lo))
            self.max_input = discord.ui.TextInput(label="Maximum", default=str(hi))
            self.add_item(self.min_input)
            self.add_item(self.max_input)
        else:
            current = _get_path(game_conf, spec["path"])
            label = spec["label"][:45]
            if spec["kind"] == "choice":
                label = f"{label} ({'/'.join(spec['choices'])})"[:45]
            long_text = spec["kind"] == "str" and len(str(current)) > 80
            self.value_input = discord.ui.TextInput(
                label=label, default=str(current),
                style=discord.TextStyle.paragraph if long_text else discord.TextStyle.short,
                max_length=None if long_text else 200,
            )
            self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        config = self.parent_view.config
        guild = self.parent_view.guild
        spec = self.spec
        try:
            async with config.guild(guild).games() as games:
                game_conf = games[self.parent_view.game_key]
                if spec["kind"] == "range_int":
                    lo = int(self.min_input.value)
                    hi = int(self.max_input.value)
                    if lo < 0 or hi < lo:
                        raise ValueError("minimum must be >= 0 and maximum >= minimum")
                    _set_path(game_conf, spec["path"][0], lo)
                    _set_path(game_conf, spec["path"][1], hi)
                elif spec["kind"] == "int":
                    _set_path(game_conf, spec["path"], int(self.value_input.value))
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
        embed.set_footer(text="Animal pools, scenario pools, and boss tiers aren't editable here -- see `.minigamehub game <key> settings`, `scenario`/`bossscenario`, and `game boss tier`.")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        self._rebuild_items()
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
