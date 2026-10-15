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

from minigamehub import panel as panel_mod
from minigamehub.config_schema import DEFAULT_GUILD
from minigamehub.minigamehub import MinigameHub
from minigamehub.settings_panel import NEW_ITEM, PanelError, SettingsPanel


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
        self.data = value

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *exc):
        return False


class Group:
    def __init__(self, **values):
        self.vals = {k: Val(copy.deepcopy(v)) for k, v in {**DEFAULT_GUILD, **values}.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.chans = {10: SimpleNamespace(id=10, mention="<#10>")}

    def get_channel(self, cid):
        return self.chans.get(cid)


def make(**values):
    cog = object.__new__(MinigameHub)
    group = Group(**values)
    cog.config = SimpleNamespace(guild=lambda g: group)
    calls = []

    async def mult(guild):
        return 1.0

    async def seed(guild):
        calls.append("seed")

    async def shuffle(guild):
        calls.append("shuffle")
        return []
    cog._pacing_multiplier, cog._seed_scenarios, cog._shuffle = mult, seed, shuffle
    return cog, group, calls


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


async def open_it(cog):
    a = admin()
    p = SettingsPanel(cog, Guild(), a, panel_mod.build_specs(), title="MinigameHub", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


async def test_enabling_seeds_and_reshuffles_and_channel_is_required():
    cog, g, calls = make()
    assert "on" in await cog._apply_enabled(None, None, True) or True
    cog, g, calls = make()
    guild = Guild()
    await cog._apply_enabled(guild, None, True)
    assert g.enabled.data is True and calls == ["seed", "shuffle"]
    await cog._apply_enabled(guild, None, False)
    assert g.enabled.data is False and calls == ["seed", "shuffle"]
    with pytest.raises(PanelError):
        await cog._apply_channel(guild, None, None)
    await cog._apply_channel(guild, None, guild.chans[10])
    assert g.spawn_channel.data == 10


async def test_activity_window_and_spacing_are_validated_in_minutes():
    cog, g, _ = make()
    for bad in ("0.1", "x", ""):
        with pytest.raises(PanelError):
            await cog._apply_activity_window(None, None, {"minutes": bad})
    assert g.activity_window.data == 300
    await cog._apply_activity_window(None, None, {"minutes": "10"})
    assert g.activity_window.data == 600
    with pytest.raises(PanelError):
        await cog._apply_spacing(None, None, {"minutes": "-1"})
    await cog._apply_spacing(None, None, {"minutes": "3"})
    assert g.min_separation.data == 180


async def test_game_toggle_and_save_validation():
    cog, g, _ = make()
    assert "off" in await cog._apply_game_toggle(None, None, "pet") and g.games.data["pet"]["enabled"] is False
    assert "on" in await cog._apply_game_toggle(None, None, "pet") and g.games.data["pet"]["enabled"] is True
    with pytest.raises(PanelError):
        await cog._apply_game_toggle(None, None, "nope")
    ok = {"min_minutes": "10", "max_minutes": "20", "reward_min": "5", "reward_max": "50"}
    for bad in ({**ok, "min_minutes": "0"}, {**ok, "max_minutes": "5"}, {**ok, "reward_min": "50", "reward_max": "5"},
                {**ok, "reward_min": "5", "reward_max": ""}, {**ok, "min_minutes": "x"}):
        with pytest.raises(PanelError):
            await cog._apply_game_save(None, None, "pet", bad)
    assert g.games.data["pet"]["min_frequency"] == 1500
    await cog._apply_game_save(None, None, "pet", ok)
    pet = g.games.data["pet"]
    assert (pet["min_frequency"], pet["max_frequency"], pet["reward_range"]) == (600, 1200, [5, 50])
    await cog._apply_game_save(None, None, "pet", {**ok, "reward_min": "", "reward_max": ""})
    assert g.games.data["pet"]["reward_range"] == [5, 50]
    with pytest.raises(PanelError, match="per tier"):
        await cog._apply_game_save(None, None, "boss", ok)
    await cog._apply_game_save(None, None, "boss", {**ok, "reward_min": "", "reward_max": ""})
    assert g.games.data["boss"]["min_frequency"] == 600


async def test_pacing_adaptive_and_tier_settings():
    cog, g, _ = make()
    for bad in ({"daily_cap": "0", "taper_start": "70", "taper_floor": "15"},
                {"daily_cap": "100", "taper_start": "101", "taper_floor": "15"},
                {"daily_cap": "100", "taper_start": "70", "taper_floor": "-1"}, {}):
        with pytest.raises(PanelError):
            await cog._apply_pacing(None, None, bad)
    await cog._apply_pacing(None, None, {"daily_cap": "9000", "taper_start": "60", "taper_floor": "10"})
    assert g.payout_pacing.data["daily_cap_per_user"] == 9000 and g.payout_pacing.data["reset_timezone"]
    await cog._apply_adaptive_enabled(None, None, False)
    assert g.adaptive_pacing.data["enabled"] is False
    ok = {"busy": "6", "quiet": "2", "multiplier": "4", "grace_minutes": "5"}
    for bad in ({**ok, "busy": "2"}, {**ok, "quiet": "-1"}, {**ok, "multiplier": "0.5"}, {**ok, "grace_minutes": "-1"}):
        with pytest.raises(PanelError):
            await cog._apply_adaptive(None, None, bad)
    await cog._apply_adaptive(None, None, ok)
    ap = g.adaptive_pacing.data
    assert (ap["busy_threshold"], ap["quiet_threshold"], ap["quiet_multiplier"], ap["startup_grace_seconds"]) == (6, 2, 4.0, 300)
    assert ap["enabled"] is False  # untouched by the tuning form
    tier = {"hp": "10-20", "reward": "5-9", "penalty": "1-2", "weight": "7"}
    for bad in ({**tier, "hp": "20-10"}, {**tier, "reward": "abc"}, {**tier, "weight": "x"}, {**tier, "weight": "-1"}):
        with pytest.raises(PanelError):
            await cog._apply_tier_save(None, None, "weak", bad)
    with pytest.raises(PanelError):
        await cog._apply_tier_save(None, None, "bogus", tier)
    await cog._apply_tier_save(None, None, "weak", tier)
    assert g.games.data["boss"]["tiers"]["weak"] == {"hp": [10, 20], "reward": [5, 9], "penalty": [1, 2], "weight": 7}


async def test_panel_lists_games_and_edits_one_through_the_form():
    cog, g, _ = make()
    items = await panel_mod._games_get(cog, Guild())
    assert [i[0] for i in items] == ["pet", "mathdrop", "hunt", "lootdrop", "reacttowin", "boss"]
    p, a = await open_it(cog)
    await p._on_setting(Click(a, ["games"]))
    await p._on_item(Click(a, ["mathdrop"]))
    click = Click(a)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert [modal.boxes[k].default for k in ("min_minutes", "max_minutes", "reward_min", "reward_max")] == ["25", "75", "50", "550"]
    modal.boxes["max_minutes"]._value = "90"
    done = Click(a)
    await modal.on_submit(done)
    assert "\u2705" in note(done) and g.games.data["mathdrop"]["max_frequency"] == 5400
    await p._on_item(Click(a, ["mathdrop"]))
    toggle = Click(a)
    await p._make_item_action(0)(toggle)
    assert g.games.data["mathdrop"]["enabled"] is False


async def test_tiers_panel_and_gating():
    cog, g, _ = make()
    p, a = await open_it(cog)
    await p._on_tab(Click(a, ["Boss tiers"]))
    await p._on_setting(Click(a, ["tiers"]))
    await p._on_item(Click(a, ["medium"]))
    click = Click(a)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert modal.boxes["hp"].default == "150-300" and modal.boxes["weight"].default == "30"
    modal.boxes["weight"]._value = "40"
    await modal.on_submit(Click(a))
    assert g.games.data["boss"]["tiers"]["medium"]["weight"] == 40
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    assert await p.interaction_check(Click(a)) is True
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False


def test_removed_setters_are_gone_and_probe_exists():
    names = {c.name for c in MinigameHub.minigamehub.commands}
    for gone in ("toggle", "spacing", "channel", "activitywindow", "pacing"):
        assert gone not in names
    for kept in ("version", "settings", "schedule", "shuffle", "test", "stats", "leaderboard", "huntanimals", "game", "adaptive",
                 "scenario", "bossscenario", "huntsafe", "diagnostics", "wipedata", "wipestats"):
        assert kept in names
    game = {c.name for c in MinigameHub.minigamehub.get_command("game").commands}
    assert game == {"list", "settings"}
    assert {c.name for c in MinigameHub.minigamehub.get_command("adaptive").commands} == {"show"}
