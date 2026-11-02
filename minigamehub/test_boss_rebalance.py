"""Boss rebalance: the one-time config upgrade, the solo cap, the card-roll rules and the card settings."""
import copy
import random
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

from minigamehub import migrations
from minigamehub.config_schema import DEFAULT_GUILD
from minigamehub.games import boss as boss_game
from minigamehub.settings_panel import PanelError
from minigamehub.test_minigamehub_panel import make

NEW = DEFAULT_GUILD["games"]["boss"]


def old_boss():
    return {"tiers": {k: {"hp": list(v["hp"]), "reward": list(v["reward"]), "penalty": [1, 2], "weight": 5}
                      for k, v in migrations.OLD_BOSS_TIERS.items()}}


def test_upgrade_moves_old_defaults_and_adds_card_drops():
    conf = old_boss()
    assert migrations.upgrade_boss(conf, NEW) is True
    for k in ("weak", "medium", "strong"):
        assert conf["tiers"][k]["hp"] == NEW["tiers"][k]["hp"]
        assert conf["tiers"][k]["reward"] == NEW["tiers"][k]["reward"]
        assert conf["tiers"][k]["solo_cap_pct"] == NEW["tiers"][k]["solo_cap_pct"]
        assert conf["tiers"][k]["penalty"] == [1, 2] and conf["tiers"][k]["weight"] == 5  # untouched
    assert conf["card_drop"] == NEW["card_drop"] and conf["card_drop"] is not NEW["card_drop"]


def test_upgrade_leaves_an_edited_tier_alone_and_is_idempotent():
    conf = old_boss()
    conf["tiers"]["medium"]["reward"] = [111, 222]
    migrations.upgrade_boss(conf, NEW)
    assert conf["tiers"]["medium"]["reward"] == [111, 222]
    assert conf["tiers"]["medium"]["hp"] == NEW["tiers"]["medium"]["hp"]
    snapshot = copy.deepcopy(conf)
    conf["tiers"]["weak"]["reward"] = [1, 2]  # an admin edit after the upgrade must survive a re-run
    assert migrations.upgrade_boss(conf, NEW) is False
    assert conf["tiers"]["weak"]["reward"] == [1, 2] and conf["tiers"]["strong"] == snapshot["tiers"]["strong"]


def test_upgrade_gives_a_custom_tier_no_solo_limit():
    conf = old_boss()
    conf["tiers"]["mega"] = {"hp": [1, 2], "reward": [1, 2], "penalty": [1, 1], "weight": 1}
    migrations.upgrade_boss(conf, NEW)
    assert conf["tiers"]["mega"]["solo_cap_pct"] == 100 and conf["tiers"]["mega"]["hp"] == [1, 2]


def test_pacing_cap_rises_only_from_the_old_default():
    old = {"daily_cap_per_user": 5000}
    assert migrations.upgrade_pacing(old) and old["daily_cap_per_user"] == 15000
    assert not migrations.upgrade_pacing(old)
    custom = {"daily_cap_per_user": 9000}
    migrations.upgrade_pacing(custom)
    assert custom["daily_cap_per_user"] == 9000


def test_min_fighters():
    assert [boss_game.min_fighters(p) for p in (100, 60, 40, 50, 34, 33, 0, None, "x")] == [1, 2, 3, 2, 3, 4, 1, 1, 1]


def test_card_winners_need_the_hits_and_the_roll():
    hits = {1: 5, 2: 2, 3: 3, 4: 3}
    always, never = SimpleNamespace(random=lambda: 0.0), SimpleNamespace(random=lambda: 0.999)
    assert boss_game.roll_card_winners(hits, 3, 10, always) == [1, 3, 4]  # most hits first, then id
    assert boss_game.roll_card_winners(hits, 3, 10, never) == []
    assert boss_game.roll_card_winners(hits, 3, 0, always) == []
    assert boss_game.roll_card_winners({}, 3, 50, random) == []


class FakeResp:
    def __init__(self):
        self.sent, self.deferred = [], False

    async def send_message(self, text, ephemeral=False):
        self.sent.append(text)

    async def defer(self):
        self.deferred = True


def click(uid):
    return SimpleNamespace(user=SimpleNamespace(id=uid), response=FakeResp())


@pytest.mark.asyncio
async def test_one_member_cannot_pass_the_solo_cap(monkeypatch):
    from minigamehub import pacing, stats

    async def paid(config, member, base, dry_run=False):
        return base

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(pacing, "settle_reward", paid)
    monkeypatch.setattr(pacing, "settle_penalty", nothing)
    monkeypatch.setattr(stats, "add_boss_damage", nothing)
    monkeypatch.setattr(stats, "record_result", nothing)
    conf = {"fight_duration": 300, "attack_cooldown": 0.0, "hit_chance": 100, "damage_per_hit": [10, 10]}
    tier = {"reward": [5, 5], "penalty": [1, 1], "solo_cap_pct": 40}
    guild = SimpleNamespace(get_member=lambda i: SimpleNamespace(id=i))
    view = boss_game._BossView(SimpleNamespace(config=None), {}, "strong", tier, 100, conf, guild)
    assert view.solo_cap == 40
    for _ in range(4):
        await view.attack.callback(click(1))
    assert view.damage_dealt[1] == 40 and view.hit_counts[1] == 4
    blocked = click(1)
    await view.attack.callback(blocked)
    assert "other fighters" in blocked.response.sent[0] and not blocked.response.deferred
    assert view.damage_dealt[1] == 40 and view.reward_dealt[1] == 20  # no extra hit, no extra pay
    for uid in (2, 3):
        for _ in range(4):
            await view.attack.callback(click(uid))
    assert view.hp == 0 and view.ended


@pytest.mark.asyncio
async def test_no_solo_cap_on_a_weak_boss():
    conf = {"fight_duration": 300}
    view = boss_game._BossView(SimpleNamespace(config=None), {}, "weak", {"solo_cap_pct": 100}, 80, conf, None)
    assert view.solo_cap is None


@pytest.mark.asyncio
async def test_card_settings_validate_and_save():
    cog, g, _ = make(games=copy.deepcopy(DEFAULT_GUILD["games"]))
    await cog._apply_card_enabled(None, None, False)
    assert g.games.data["boss"]["card_drop"]["enabled"] is False
    for bad in ({"min_hits": "0"}, {"min_hits": "x"}, {"min_hits": "1001"}, {}):
        with pytest.raises(PanelError):
            await cog._apply_card_min_hits(None, None, bad)
    await cog._apply_card_min_hits(None, None, {"min_hits": "5"})
    assert g.games.data["boss"]["card_drop"]["min_hits"] == 5
    for bad in ({"chance": "101", "max_rarity": "epic"}, {"chance": "-1", "max_rarity": "epic"},
                {"chance": "x", "max_rarity": "epic"}, {"chance": "5", "max_rarity": "legendary"}):
        with pytest.raises(PanelError):
            await cog._apply_card_tier_save(None, None, "weak", bad)
    with pytest.raises(PanelError):
        await cog._apply_card_tier_save(None, None, "bogus", {"chance": "5", "max_rarity": "epic"})
    await cog._apply_card_tier_save(None, None, "weak", {"chance": "7.5", "max_rarity": "Rare"})
    assert g.games.data["boss"]["card_drop"]["tiers"]["weak"] == {"chance": 7.5, "max_rarity": "rare"}


@pytest.mark.asyncio
async def test_saving_a_tier_keeps_other_keys():
    cog, g, _ = make(games=copy.deepcopy(DEFAULT_GUILD["games"]))
    ok = {"hp": "10-20", "reward": "5-9", "penalty": "1-2", "weight": "7", "solo_cap": "100"}
    await cog._apply_tier_save(None, None, "strong", ok)
    assert g.games.data["boss"]["tiers"]["strong"]["solo_cap_pct"] == 100.0
