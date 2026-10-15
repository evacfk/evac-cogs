"""The `.event settings` control panel: the events channel, the reminder and the event kinds.

Specs only; every change goes through the cog's `_apply_*` methods. Creating and managing events
(`create`, `edit`, `cancel`, `poll`, ...) stays as commands.
"""
from .constants import DEFAULT_DURATION_MIN
from .settings_panel import Field, PanelError, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. Hosts keep `.event create`, `edit`, `cancel`, "
         "`poll`, `attendance` and `regulars` as commands.")


async def _channel_get(cog, guild):
    return await cog.config.guild(guild).channel_id()


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


async def _channel_apply(cog, guild, member, channel):
    return await cog._apply_channel(guild, member, channel)


async def _remind_get(cog, guild):
    return {"minutes": await cog.config.guild(guild).remind_minutes()}


async def _remind_apply(cog, guild, member, values):
    return await cog._apply_remind(guild, member, values)


async def _kinds_get(cog, guild):
    out = []
    for key, kind in (await cog._kinds(guild)).items():
        hours = int(kind.get("duration") or DEFAULT_DURATION_MIN) / 60
        out.append((key, f"{kind.get('emoji', '')} {kind.get('label', key.title())} ({key})".strip(),
                    f"pings <@&{kind.get('ping_role_id')}> · {hours:g}h"))
    return out


async def _kind_get(cog, guild, item_id):
    kind = (await cog._kinds(guild)).get(item_id)
    if kind is None:
        raise PanelError("That kind no longer exists.")
    return {"key": item_id, "emoji": kind.get("emoji", ""),
            "hours": f"{int(kind.get('duration') or DEFAULT_DURATION_MIN) / 60:g}"}


async def _kind_save(cog, guild, member, item_id, values, picked):
    return await cog._apply_kind_save(guild, member, item_id, values, picked)


async def _kind_remove(cog, guild, member, item_id):
    return await cog._apply_kind_remove(guild, member, item_id)


def build_specs():
    return [
        Spec("channel", "Events channel", "Settings", "channel", get=_channel_get, apply=_channel_apply,
             fmt=_channel_fmt, channel_types=("text",), help="Where event announcements are posted."),
        Spec("remind", "Reminder", "Settings", "modal", get=_remind_get, apply=_remind_apply,
             fmt=lambda v, g: (f"{v['minutes']} min before start" if v and v["minutes"] else "off") if v else "?",
             button="Edit…",
             fields=(Field("minutes", "Minutes before start (0 = off)", value=lambda v: str(v["minutes"]), max_length=5),),
             help="Pings everyone who said Going or Maybe."),
        Spec("kinds", "Event kinds", "Settings", "collection", get=_kinds_get, item_get=_kind_get,
             save=_kind_save, remove=_kind_remove,
             fields=(Field("key", "Key (what hosts type: .event create <key>)", new_only=True, max_length=24,
                           placeholder="trivia"),
                     Field("emoji", "Emoji (blank = default)", value=lambda r: r.get("emoji", ""), required=False,
                           max_length=64),
                     Field("hours", "Length in hours (0.5 to 72)", value=lambda r: r.get("hours", "3"),
                           new_default="3", max_length=5)),
             help="movie, game and event are built in with their own ping roles: you can change their emoji "
                  "and length, and Remove puts them back to default. New kinds ping the generic event role."),
    ]
