"""The `.bumpreward settings` control panel: channel, ping role, rewards, streak bonus, reminders and mode.

Specs only; every change goes through the cog's `_apply_*` methods. Leaderboards and the troubleshooting
tools (`top`, `status`, `debug`, `replay`, `seed`, `nexttimer`) stay commands.
"""
from . import engine
from .settings_panel import Field, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. `.bumpreward status`, `debug`, `replay`, `seed` "
         "and `nexttimer` stay as commands for troubleshooting.")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


def _role_fmt(value, guild):
    if not value:
        return "none (no pings)"
    role = guild.get_role(value)
    return role.mention if role is not None else f"MISSING (id {value})"


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


async def _reward_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"min": await conf.min_reward(), "max": await conf.max_reward()}


async def _streak_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"pct": await conf.streak_bonus_pct(), "steps": await conf.streak_max()}


async def _lead_get(cog, guild):
    return {"seconds": await cog.config.guild(guild).reminder_lead_seconds()}


def build_specs():
    return [
        Spec("enabled", "Bump rewards", "Basics", "toggle", get=_conf("enabled"), apply=_apply("_apply_enabled"),
             help="Rewards, reminders and the daily board. Needs the bump channel first."),
        Spec("channel", "Bump channel", "Basics", "channel", get=_conf("channel_id"), apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Bumps anywhere else are ignored."),
        Spec("role", "Reminder role", "Basics", "role", get=_conf("role_id"), apply=_apply("_apply_role"),
             fmt=_role_fmt, clearable=True, help="Pinged on reminders. Clear it to stop pinging."),
        Spec("mode", "Server mode", "Basics", "choice", get=_conf("mode"), apply=_apply("_apply_mode"),
             choices=(("Free: one bump every 2 hours", "free"),
                      ("Pro: faster while under 12 bumps in 24h", "pro")),
             help="Match your Disboard plan. Applies from the next bump."),
        Spec("reward", "Base reward", "Rewards", "modal", get=_reward_get, apply=_apply("_apply_reward"),
             fmt=lambda v, g: f"{v['min']:,} to {v['max']:,}" if v else "?", button="Edit…",
             fields=(Field("min", "Minimum reward", value=lambda v: str(v["min"]), max_length=9),
                     Field("max", "Maximum reward", value=lambda v: str(v["max"]), max_length=9)),
             help="Before the in-a-row bonus."),
        Spec("streak", "Streak bonus", "Rewards", "modal", get=_streak_get, apply=_apply("_apply_streak"),
             fmt=lambda v, g: f"+{v['pct']}% per bump in a row, up to {v['steps']} steps (max +{v['pct'] * v['steps']}%)" if v else "?",
             button="Edit…",
             fields=(Field("pct", "Extra % per bump in a row", value=lambda v: str(v["pct"]), max_length=4),
                     Field("steps", "How many bumps it keeps growing", value=lambda v: str(v["steps"]), max_length=3))),
        Spec("reminders", "Reminders", "Reminders", "toggle", get=_conf("reminders_enabled"), apply=_apply("_apply_reminders"),
             help="Off = rewards only; use Disboard's own /reminder instead."),
        Spec("lead", "Heads-up", "Reminders", "modal", get=_lead_get, apply=_apply("_apply_lead"),
             fmt=lambda v, g: (f"{v['seconds']}s before ready" if v["seconds"] else "exactly when ready") if v else "?",
             button="Edit…",
             fields=(Field("seconds", f"Seconds before ready (0 to {engine.MAX_LEAD_SECONDS})",
                           value=lambda v: str(v["seconds"]), max_length=5),),
             help="The single reminder goes out this long before the server can be bumped."),
    ]
