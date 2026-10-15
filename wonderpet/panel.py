"""The `.wonderpet settings` control panel: the pet channel, warning pings, daily card hour and treats.

Specs only; every change goes through the cog's `_apply_*` methods. Pet management (`newegg`, `name`, `art`)
stays as commands for staff.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. Staff keep `.wonderpet newegg`, `name` and "
         "`art <slot>` as commands.")


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


async def _hour_get(cog, guild):
    return {"hour": await cog.config.guild(guild).daily_hour()}


async def _treats_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"price": await conf.treat_price(), "cap": await conf.treat_cap()}


def build_specs():
    return [
        Spec("channel", "Pet channel", "Settings", "channel", get=_conf("channel_id"), apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Where the pet lives (#cuddle)."),
        Spec("ping_role", "Warning ping role", "Settings", "role", get=_conf("ping_role_id"),
             apply=_apply("_apply_ping_role"), fmt=_role_fmt, clearable=True,
             help="Pinged on the serious warnings (sick, critical, final). Clear it for no pings."),
        Spec("hour", "Daily card hour", "Settings", "modal", get=_hour_get, apply=_apply("_apply_hour"),
             fmt=lambda v, g: f"{v['hour']}:00 Pacific" if v else "?", button="Edit…",
             fields=(Field("hour", "Pacific hour the card is re-posted (0-23)", value=lambda v: str(v["hour"]), max_length=2),)),
        Spec("treats", "Treats", "Settings", "modal", get=_treats_get, apply=_apply("_apply_treats"),
             fmt=lambda v, g: f"{v['price']:,} each (feast {v['price'] * 4:,}), {v['cap']} per member per day" if v else "?",
             button="Edit…",
             fields=(Field("price", "Price of a basic treat (feast = 4x)", value=lambda v: str(v["price"]), max_length=9),
                     Field("cap", "Treats one member can give per day", value=lambda v: str(v["cap"]), max_length=3))),
    ]
