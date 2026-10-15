"""The `.introform` control panel: intros channel and free-form message removal."""
from ..settings_panel import Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. `.introform panel` re-posts the form button, "
         "`.introform prune` clears intros of members who left, `.introform settings` prints a summary.")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


async def _channel_apply(cog, guild, member, channel):
    return await cog._apply_channel(guild, member, channel)


async def _enforce_apply(cog, guild, member, on):
    return await cog._apply_enforce(guild, member, on)


def build_specs():
    return [
        Spec("channel", "Intros channel", "Settings", "channel", get=_conf("channel_id"), apply=_channel_apply,
             fmt=_channel_fmt, channel_types=("text",),
             help="Picking a channel also posts the form button panel there."),
        Spec("enforce", "Remove free-form messages", "Settings", "toggle", get=_conf("enforce"), apply=_enforce_apply,
             help="On: messages from non-mods in the intros channel are deleted, so only form intros stay."),
    ]
