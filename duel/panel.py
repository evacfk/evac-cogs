"""The `.duelset` control panel: wager limits and the cooldown. Specs only; changes go through `Duel._apply_limits`."""
from .settings_panel import Field, Spec

INTRO = "Set the wager limits and the cooldown between a member's duels. `.duelset version` confirms the build."


async def _get(cog, guild):
    return await cog.config.guild(guild).all()


def _fmt(value, guild):
    if not value:
        return "?"
    high = value["max_bet"]
    return (f"wager {value['min_bet']:,} to {'no cap' if not high else format(high, ',')}, "
            f"cooldown {value['cooldown']}s" + ("" if value["cooldown"] else " (off)"))


async def _apply(cog, guild, member, values):
    return await cog._apply_limits(guild, member, values)


def build_specs():
    return [
        Spec("limits", "Wager limits and cooldown", "Rules", "modal", get=_get, apply=_apply, fmt=_fmt, button="Edit…",
             fields=(
                 Field("min_bet", "Minimum wager (0 = none)", value=lambda v: str(v["min_bet"]), max_length=12),
                 Field("max_bet", "Maximum wager (0 = no cap)", value=lambda v: str(v["max_bet"]), max_length=12),
                 Field("cooldown", "Cooldown between duels, seconds (0 = off)", value=lambda v: str(v["cooldown"]), max_length=8),
             ),
             help="The cooldown starts for both players when a duel finishes."),
    ]
