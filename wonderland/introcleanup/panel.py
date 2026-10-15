"""The `.introcleanup` control panel: the intros channel to watch and the log channel."""
from ..settings_panel import Spec

INTRO = "Pick a setting. Changes save as soon as you choose. `.introcleanup sweep` and `.introcleanup settings` stay as commands."


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


async def _intros_apply(cog, guild, member, channel):
    return await cog._apply_intros_channel(guild, member, channel)


async def _log_apply(cog, guild, member, channel):
    return await cog._apply_log_channel(guild, member, channel)


def build_specs():
    return [
        Spec("intros_channel", "Intros channel", "Settings", "channel", get=_conf("intros_channel_id"),
             apply=_intros_apply, fmt=_channel_fmt, channel_types=("text",),
             help="Messages from members who leave are deleted from this channel."),
        Spec("log_channel", "Log channel", "Settings", "channel", get=_conf("log_channel_id"), apply=_log_apply,
             fmt=_channel_fmt, channel_types=("text",), clearable=True, help="Cleanup events are posted here."),
    ]
