"""The `.rejoinwatch` control panel: on/off, channels, mod role, retention and the warning text.

Specs only; every change goes through the cog's `_apply_*` methods. `settings`, `check` and `clear` stay commands.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. `.rejoinwatch settings` shows the health check, "
         "`.rejoinwatch check @user` and `.rejoinwatch clear @user` look up or forgive a member.")


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
        return "none"
    role = guild.get_role(value)
    return role.mention if role is not None else f"MISSING (id {value})"


async def _enabled_apply(cog, guild, member, on):
    return await cog._apply_enabled(guild, member, on)


async def _mod_channel_apply(cog, guild, member, channel):
    return await cog._apply_mod_channel(guild, member, channel)


async def _fallback_apply(cog, guild, member, channel):
    return await cog._apply_fallback(guild, member, channel)


async def _mod_role_apply(cog, guild, member, role):
    return await cog._apply_mod_role(guild, member, role)


async def _retention_get(cog, guild):
    return {"days": await cog.config.guild(guild).retention_days()}


async def _retention_apply(cog, guild, member, values):
    return await cog._apply_retention(guild, member, values)


async def _warning_get(cog, guild):
    return {"text": await cog.config.guild(guild).warning_text()}


async def _warning_apply(cog, guild, member, values):
    return await cog._apply_warning(guild, member, values)


def build_specs():
    return [
        Spec("enabled", "Tracking", "Settings", "toggle", get=_conf("enabled"), apply=_enabled_apply,
             help="Off stops recording and warning. Existing records are kept either way."),
        Spec("mod_channel", "Mod channel", "Settings", "channel", get=_conf("mod_channel_id"), apply=_mod_channel_apply,
             fmt=_channel_fmt, channel_types=("text",), help="Where repeat-leave alerts and the ban buttons go."),
        Spec("fallback", "Fallback channel", "Settings", "channel", get=_conf("cuddle_channel_id"), apply=_fallback_apply,
             fmt=_channel_fmt, channel_types=("text",), clearable=True,
             help="Where a rejoin warning is posted when the member's DMs are closed (#cuddle)."),
        Spec("mod_role", "Mod role", "Settings", "role", get=_conf("mod_role_id"), apply=_mod_role_apply,
             fmt=_role_fmt, clearable=True,
             help="Can press the ban buttons. Anyone with Ban Members or Administrator can regardless."),
        Spec("retention", "Retention", "Settings", "modal", get=_retention_get, apply=_retention_apply,
             fmt=lambda v, g: f"{v['days']} days" if v else "?", button="Edit…",
             fields=(Field("days", "Days a leave is remembered (7 to 730)", value=lambda v: str(v["days"]), max_length=4),),
             help="Older leaves are deleted right away when you shorten this."),
        Spec("warning", "Rejoin warning", "Settings", "modal", get=_warning_get, apply=_warning_apply,
             fmt=lambda v, g: (v["text"][:90] + "…") if v and len(v["text"]) > 90 else (v["text"] if v else "?"),
             button="Edit…",
             fields=(Field("text", "Warning text ({server} = server name; blank = default)", style="long",
                           value=lambda v: v["text"], required=False, max_length=1500),),
             help="Sent on a member's first rejoin after leaving."),
    ]
