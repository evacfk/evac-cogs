"""The `.pp set` control panel: channels, job role, rater, daily rules and strikes.

Specs only; every change goes through the cog's `_apply_*` methods. Member tools
(`drop`, `status`, `pto`, `hire`, `fire`, `ratephotos`, `rated`, ...) stay commands.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a section, then a setting. Changes save as soon as you choose. Member tools "
         "(`.pp hire`, `.pp fire`, `.pp pto`, `.pp status`, `.pp ratephotos`) stay as commands.")


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


async def _rater_get(cog, guild):
    return {"user": await cog.config.guild(guild).rater_id()}


async def _daily_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"quota": await conf.quota(), "reminder_hours": await conf.reminder_hours_before()}


async def _strikes_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"window_days": await conf.strike_window_days(), "threshold": await conf.strike_threshold()}


def _role_fmt(value, guild):
    role = guild.get_role(value) if value else None
    return role.mention if role is not None else (f"MISSING (id {value})" if value else "not set")


def build_specs():
    return [
        Spec("channel", "Shared channel", "Channels & people", "channel", get=_conf("channel_id"),
             apply=_apply("_apply_channel"), fmt=_channel_fmt, channel_types=("text",),
             help="Drops, shift reports and the end-of-day reminder go here."),
        Spec("rating_channel", "Rating channel", "Channels & people", "channel", get=_conf("rating_channel_id"),
             apply=_apply("_apply_rating_channel"),
             fmt=lambda v, g: _channel_fmt(v, g) if v else "the shared channel",
             channel_types=("text", "public_thread", "private_thread", "news_thread"), clearable=True,
             help="Nightly rating prompts and weekly polls. A thread works too."),
        Spec("job_role", "Job role", "Channels & people", "role", get=_conf("job_role_id"),
             apply=_apply("_apply_job_role"), fmt=_role_fmt,
             help="The single source of truth for who can `.pp drop`."),
        Spec("rater", "Rater", "Channels & people", "modal", get=_rater_get, apply=_apply("_apply_rater"),
             fmt=lambda v, g: f"<@{v['user']}>" if v and v["user"] else "the server owner", button="Change…",
             fields=(Field("user", "User ID or @mention (blank = server owner)",
                           value=lambda v: str(v["user"] or ""), required=False, max_length=40),),
             help="Who can click GOAT / Good / Mid / Bad on the rating prompts."),
        Spec("daily", "Daily rules", "Rules", "modal", get=_daily_get, apply=_apply("_apply_daily"),
             fmt=lambda v, g: f"{v['quota']} photo(s)/day, reminder {v['reminder_hours']}h before midnight" if v else "?",
             button="Edit…",
             fields=(Field("quota", "Photos required per day", value=lambda v: str(v["quota"]), max_length=3),
                     Field("reminder_hours", "Reminder: hours before local midnight (1-23)",
                           value=lambda v: str(v["reminder_hours"]), max_length=2)),
             help="The reminder pings the job role if anyone still hasn't submitted."),
        Spec("strikes", "Strikes", "Rules", "modal", get=_strikes_get, apply=_apply("_apply_strikes"),
             fmt=lambda v, g: f"{v['threshold']} within {v['window_days']} days" if v else "?", button="Edit…",
             fields=(Field("window_days", "Rolling window, days", value=lambda v: str(v["window_days"]), max_length=3),
                     Field("threshold", "Strikes in the window that cost the role",
                           value=lambda v: str(v["threshold"]), max_length=3))),
    ]
