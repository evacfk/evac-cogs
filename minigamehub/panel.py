"""The `.minigamehub` control panel: spawning, per-game timing, payout pacing, adaptive pacing and boss tiers.

Specs only; every change goes through the cog's `_apply_*` methods. Per-game details (messages, timeouts,
rewards) stay in `.minigamehub settings`, hunt's animals in `.minigamehub huntanimals`.
"""
from .constants import GAME_KEYS
from .settings_panel import Field, ItemAction, PanelError, Spec

INTRO = ("Pick a section, then a setting. Changes save as soon as you choose. More: `.minigamehub settings` (per-game "
         "messages, timeouts and rewards), `huntanimals`, `schedule`, `shuffle`, `test <game>`, `diagnostics`, `stats`.")


def _fmt_secs(seconds) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    minutes = seconds / 60
    if minutes < 120:
        return f"{minutes:g}m"
    return f"{minutes / 60:.1f}h"


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


def _minutes(key):
    async def get(cog, guild):
        return {"minutes": await getattr(cog.config.guild(guild), key)()}
    return get


def _minutes_value(v):
    return f"{v['minutes'] / 60:g}"


async def _games_get(cog, guild):
    games = await cog.config.guild(guild).games()
    out = []
    for key in GAME_KEYS:
        g = games[key]
        reward = "" if key == "boss" or "reward_range" not in g else f", reward {g['reward_range'][0]:,}-{g['reward_range'][1]:,}"
        out.append((key, f"{key} ({'on' if g['enabled'] else 'off'})",
                    f"every {_fmt_secs(g['min_frequency'])} to {_fmt_secs(g['max_frequency'])}{reward}"))
    return out


async def _game_get(cog, guild, key):
    games = await cog.config.guild(guild).games()
    if key not in games:
        raise PanelError("That game no longer exists.")
    g = games[key]
    reward = g.get("reward_range") or ["", ""]
    return {"min_minutes": f"{g['min_frequency'] / 60:g}", "max_minutes": f"{g['max_frequency'] / 60:g}",
            "reward_min": "" if key == "boss" else str(reward[0]), "reward_max": "" if key == "boss" else str(reward[1])}


async def _game_save(cog, guild, member, key, values, picked):
    return await cog._apply_game_save(guild, member, key, values, picked)


async def _game_toggle(cog, guild, member, key):
    return await cog._apply_game_toggle(guild, member, key)


async def _pacing_get(cog, guild):
    pc = await cog.config.guild(guild).payout_pacing()
    return {"daily_cap": pc["daily_cap_per_user"], "taper_start": pc["taper_start_pct"], "taper_floor": pc["taper_floor_pct"]}


async def _adaptive_enabled_get(cog, guild):
    return (await cog.config.guild(guild).adaptive_pacing()).get("enabled", True)


async def _adaptive_get(cog, guild):
    ap = await cog.config.guild(guild).adaptive_pacing()
    return {"busy": ap["busy_threshold"], "quiet": ap["quiet_threshold"], "multiplier": ap["quiet_multiplier"],
            "grace_minutes": ap["startup_grace_seconds"] / 60}


async def _tiers_get(cog, guild):
    tiers = (await cog.config.guild(guild).games())["boss"]["tiers"]
    return [(name, name.title(),
             f"HP {t['hp'][0]}-{t['hp'][1]} · reward {t['reward'][0]}-{t['reward'][1]} · "
             f"penalty {t['penalty'][0]}-{t['penalty'][1]} · weight {t['weight']}") for name, t in tiers.items()]


async def _tier_get(cog, guild, name):
    tiers = (await cog.config.guild(guild).games())["boss"]["tiers"]
    if name not in tiers:
        raise PanelError("That tier no longer exists.")
    t = tiers[name]
    return {"hp": f"{t['hp'][0]}-{t['hp'][1]}", "reward": f"{t['reward'][0]}-{t['reward'][1]}",
            "penalty": f"{t['penalty'][0]}-{t['penalty'][1]}", "weight": str(t["weight"])}


async def _tier_save(cog, guild, member, name, values, picked):
    return await cog._apply_tier_save(guild, member, name, values, picked)


def build_specs():
    return [
        # -- Spawning --------------------------------------------------------------------------
        Spec("enabled", "Spawning", "Spawning", "toggle", get=_conf("enabled"), apply=_apply("_apply_enabled"),
             help="Turning it on seeds the scenario pools and re-rolls every game's timer."),
        Spec("channel", "Spawn channel", "Spawning", "channel", get=_conf("spawn_channel"), apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Games only spawn here, and only while it is active."),
        Spec("activity_window", "Activity window", "Spawning", "modal", get=_minutes("activity_window"),
             apply=_apply("_apply_activity_window"), fmt=lambda v, g: f"{v['minutes'] / 60:g} min" if v else "?",
             button="Edit…",
             fields=(Field("minutes", "Minutes a message must be recent (at least 0.17)", value=_minutes_value, max_length=8),),
             help="Spawns only fire when someone has talked in the spawn channel this recently."),
        Spec("spacing", "Spacing between games", "Spawning", "modal", get=_minutes("min_separation"),
             apply=_apply("_apply_spacing"), fmt=lambda v, g: f"{v['minutes'] / 60:g} min" if v else "?", button="Edit…",
             fields=(Field("minutes", "Minimum minutes between any two games", value=_minutes_value, max_length=8),)),
        Spec("games", "Games", "Spawning", "collection", get=_games_get, item_get=_game_get, save=_game_save,
             can_add=False,
             fields=(Field("min_minutes", "Shortest wait, minutes", value=lambda r: r.get("min_minutes", ""), max_length=8),
                     Field("max_minutes", "Longest wait, minutes", value=lambda r: r.get("max_minutes", ""), max_length=8),
                     Field("reward_min", "Reward minimum (blank = unchanged)", value=lambda r: r.get("reward_min", ""),
                           required=False, max_length=9),
                     Field("reward_max", "Reward maximum (blank = unchanged)", value=lambda r: r.get("reward_max", ""),
                           required=False, max_length=9)),
             item_actions=(ItemAction("Turn on / off", _game_toggle),),
             help="Pick a game to edit how often it spawns and its base reward, or turn it on or off. Boss rewards are per tier."),
        # -- Pacing ----------------------------------------------------------------------------------
        Spec("payout", "Daily payout cap", "Pacing", "modal", get=_pacing_get, apply=_apply("_apply_pacing"),
             fmt=lambda v, g: (f"{v['daily_cap']:,} per user, taper {v['taper_start']:g}% down to {v['taper_floor']:g}%" if v else "?"),
             button="Edit…",
             fields=(Field("daily_cap", "Daily cap per user, all games combined", value=lambda v: str(v["daily_cap"]), max_length=9),
                     Field("taper_start", "Taper starts at this % of the cap", value=lambda v: f"{v['taper_start']:g}", max_length=5),
                     Field("taper_floor", "Lowest reward % once at the cap", value=lambda v: f"{v['taper_floor']:g}", max_length=5)),
             help="Rewards shrink as a member nears their daily cap, down to the floor."),
        Spec("adaptive_enabled", "Adaptive pacing", "Pacing", "toggle", get=_adaptive_enabled_get,
             apply=_apply("_apply_adaptive_enabled"),
             help="On: games stretch out as chat gets quiet. Off: every game uses its own timing as-is."),
        Spec("adaptive", "Adaptive tuning", "Pacing", "modal", get=_adaptive_get, apply=_apply("_apply_adaptive"),
             fmt=lambda v, g: (f"busy {v['busy']}+, quiet {v['quiet']} or fewer, {v['multiplier']:g}x slower, "
                               f"{v['grace_minutes']:g} min grace" if v else "?"),
             button="Edit…",
             fields=(Field("busy", "Busy: talkers in the last 5 min", value=lambda v: str(v["busy"]), max_length=4),
                     Field("quiet", "Quiet: talkers in the last 5 min", value=lambda v: str(v["quiet"]), max_length=4),
                     Field("multiplier", "How many times slower when quiet", value=lambda v: f"{v['multiplier']:g}", max_length=5),
                     Field("grace_minutes", "Startup grace, minutes", value=lambda v: f"{v['grace_minutes']:g}", max_length=5)),
             help="After a reload the live reading is blank, so a fixed blend is used for the grace period."),
        # -- Boss tiers ----------------------------------------------------------------------------------
        Spec("tiers", "Boss tiers", "Boss tiers", "collection", get=_tiers_get, item_get=_tier_get, save=_tier_save,
             can_add=False,
             fields=(Field("hp", "HP range, like 50-100", value=lambda r: r.get("hp", ""), max_length=15),
                     Field("reward", "Reward range", value=lambda r: r.get("reward", ""), max_length=15),
                     Field("penalty", "Penalty range for a loss", value=lambda r: r.get("penalty", ""), max_length=15),
                     Field("weight", "Spawn weight (higher = more often)", value=lambda r: r.get("weight", ""), max_length=4)),
             help="Each boss fight picks a tier by weight."),
    ]
