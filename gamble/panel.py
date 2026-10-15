"""The `.gambleset` control panel: the hub channel, the thread timeouts and the hub menu.

Specs only; every change goes through the cog's `_apply_*` methods. The owner-only game registry
(`.gamblehub addgame`, `addmodalgame`, `removegame`, `notifyrecipient`), `lockdown` and `listgames` stay commands.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. After changing the hub channel, press "
         "Post / refresh hub menu. The game registry (`.gamblehub addgame`, `removegame`, `listgames`) stays as commands.")


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


async def _channel_get(cog, guild):
    return await cog.config.guild(guild).bot_channel_id()


def _channel_fmt(value, guild):
    if not value:
        return "not set (`.gamble` falls back to the channel it is used in)"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


async def _timeouts_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"personal": await conf.timeout_minutes(), "shared": await conf.shared_timeout_minutes()}


def build_specs():
    return [
        Spec("channel", "Hub channel", "Hub", "channel", get=_channel_get, apply=_apply("_apply_channel"), fmt=_channel_fmt,
             channel_types=("text",), help="Where personal and shared threads spawn from."),
        Spec("hub_menu", "Post / refresh hub menu", "Hub", "action", apply=_apply("_apply_post_hub"),
             button="Post / refresh hub menu", help="Posts the button menu in the hub channel, or updates the existing one."),
        Spec("timeouts", "Thread timeouts", "Hub", "modal", get=_timeouts_get, apply=_apply("_apply_timeouts"),
             fmt=lambda v, g: f"personal {v['personal']} min · shared {v['shared']} min of inactivity" if v else "?",
             button="Edit…",
             fields=(Field("personal", "Personal threads, minutes of inactivity", value=lambda v: str(v["personal"]), max_length=5),
                     Field("shared", "Shared tables, minutes of inactivity", value=lambda v: str(v["shared"]), max_length=5))),
    ]
