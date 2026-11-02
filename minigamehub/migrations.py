"""One-time upgrades for settings a server already has saved.

Red only applies a registered default to a key that was never set, so changing the defaults in
config_schema.py never changes a live server. These helpers move the old *default* boss numbers to the
new ones, and leave anything an admin edited alone.
"""
import copy

# the boss tier numbers shipped before the 2026-11 rebalance
OLD_BOSS_TIERS = {
    "weak":   {"hp": [50, 100],  "reward": [50, 200]},
    "medium": {"hp": [150, 300], "reward": [150, 500]},
    "strong": {"hp": [400, 700], "reward": [400, 1200]},
}
OLD_DAILY_CAP = 5000
NEW_DAILY_CAP = 15000


def upgrade_boss(boss_conf: dict, new_defaults: dict) -> bool:
    """Rebalance a saved boss config once. Returns True if it changed anything.

    A tier's hp/reward move to the new defaults only if they still equal the old defaults; a tier an admin
    edited keeps its numbers. `solo_cap_pct` (the most of a boss one member can deal) is added to every tier.
    """
    if boss_conf.get("rebalance_v2"):
        return False
    tiers = boss_conf.setdefault("tiers", {})
    for name, new in new_defaults["tiers"].items():
        live = tiers.get(name)
        if live is None:
            continue
        old = OLD_BOSS_TIERS.get(name, {})
        for field in ("hp", "reward"):
            if old and list(live.get(field, [])) == old[field]:
                live[field] = list(new[field])
        live.setdefault("solo_cap_pct", new["solo_cap_pct"])
    for live in tiers.values():
        live.setdefault("solo_cap_pct", 100)
    boss_conf.setdefault("card_drop", copy.deepcopy(new_defaults["card_drop"]))
    boss_conf["rebalance_v2"] = True
    return True


def upgrade_pacing(pacing_conf: dict) -> bool:
    """Raise the shared daily cap from the old default once (a cap an admin changed is left alone)."""
    if pacing_conf.get("cap_v2"):
        return False
    if pacing_conf.get("daily_cap_per_user") == OLD_DAILY_CAP:
        pacing_conf["daily_cap_per_user"] = NEW_DAILY_CAP
    pacing_conf["cap_v2"] = True
    return True
