"""The `.lurkerset` control panel: the Lurker role and sweep, mod reports, and the year club roles.

Specs only; every change goes through the cog's `_apply_*` methods. Flagging by hand, previews, backfills,
`.lurkerset settings`/`postinfo` and `.yearclub check`/`sync` stay commands.
"""
from . import engine
from .settings_panel import Field, Pick, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. Turn the daily sweep on last, after the role, "
         "channel and exempt roles are set. `.lurkersweeppreview`, `.lurkerbackfill` and `.yearclub sync` stay commands.")


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
        return "not set"
    role = guild.get_role(value)
    return role.mention if role is not None else f"MISSING (id {value})"


def _apply(name, *prefix):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, *prefix, value)
    return apply


def _exempt_fmt(value, guild):
    if not value:
        return "none (anyone can be flagged)"
    return ", ".join(r.mention if (r := guild.get_role(i)) is not None else f"MISSING ({i})" for i in value)


async def _rules_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"days": await conf.threshold_days(), "limit": await conf.sweep_max()}


async def _days_get(cog, guild):
    return {"days": await cog.config.guild(guild).report_interval_days()}


async def _clubs_get(cog, guild):
    mapping = engine.club_mapping(await cog.config.guild(guild).yearclub_roles())
    out = []
    for years in sorted(mapping):
        role = guild.get_role(mapping[years])
        out.append((str(years), f"{years} year club", role.mention if role is not None else "role deleted"))
    return out


async def _club_get(cog, guild, item_id):
    mapping = engine.club_mapping(await cog.config.guild(guild).yearclub_roles())
    return {"years": item_id, "role_id": mapping.get(int(item_id))}


async def _club_save(cog, guild, member, item_id, values, picked):
    return await cog._apply_yearclub_save(guild, member, item_id, values, picked)


async def _club_remove(cog, guild, member, item_id):
    return await cog._apply_yearclub_remove(guild, member, item_id)


def build_specs():
    return [
        # -- Lurkers
        Spec("role", "Lurker role", "Lurkers", "role", get=_conf("lurker_role_id"), apply=_apply("_apply_lurker_role"),
             fmt=_role_fmt, help="Given to inactive members. It should hide every channel except the one below."),
        Spec("channel", "Reactivation channel", "Lurkers", "channel", get=_conf("lurker_channel_id"),
             apply=_apply("_apply_lurker_channel"), fmt=_channel_fmt, channel_types=("text",),
             help="The only place a lurker can post to come back. Posting there restores their roles."),
        Spec("exempt", "Never flag these roles", "Lurkers", "role", get=_conf("exempt_role_ids"), apply=_apply("_apply_exempt"),
             fmt=_exempt_fmt, multi=25, clearable=True, help="Like Mod and Booster."),
        Spec("rules", "Sweep rules", "Lurkers", "modal", get=_rules_get, apply=_apply("_apply_sweep_rules"),
             fmt=lambda v, g: f"{v['days']} inactive days · abort above {v['limit']:,} flags" if v else "?", button="Edit…",
             fields=(Field("days", "Inactive days before a member is flagged", value=lambda v: str(v["days"]), max_length=4),
                     Field("limit", "Safety limit: flags per sweep (1-5000)", value=lambda v: str(v["limit"]), max_length=4)),
             help="A sweep that would flag more than the limit aborts and alerts mods."),
        Spec("enabled", "Automatic daily sweep", "Lurkers", "toggle", get=_conf("enabled"), apply=_apply("_apply_sweep_enabled"),
             help="First run is about 24 hours after turning it on. Needs the role and channel."),
        # -- Reports
        Spec("report_channel", "Mod report channel", "Reports", "channel", get=_conf("report_channel_id"),
             apply=_apply("_apply_report_channel"), fmt=_channel_fmt, channel_types=("text",), clearable=True,
             help="A digest of flags and restores. Clear it to turn reports off."),
        Spec("report_days", "Days between reports", "Reports", "modal", get=_days_get, apply=_apply("_apply_report_days"),
             fmt=lambda v, g: f"every {v['days']} days" if v else "?", button="Edit…",
             fields=(Field("days", "Days (1-30)", value=lambda v: str(v["days"]), max_length=2),)),
        Spec("welcome_channel", "Welcome-back channel", "Reports", "channel", get=_conf("welcome_channel_id"),
             apply=_apply("_apply_welcome_channel"), fmt=lambda v, g: _channel_fmt(v, g) if v else "off",
             channel_types=("text",), clearable=True, help="Mods get a post when a lurker reactivates themselves."),
        # -- Year club
        Spec("yearclub_enabled", "Year club upkeep", "Year club", "toggle", get=_conf("yearclub_enabled"),
             apply=_apply("_apply_yearclub_enabled"),
             help="Swaps each active member's \"N year club!\" role on their join anniversary (checked hourly)."),
        Spec("yearclub_detect", "Find club roles", "Year club", "action", apply=_apply("_apply_yearclub_autodetect"),
             button="Find club roles", confirm="This replaces the whole year → role list with every role named like `5 year club!`.",
             help="Maps every role named like `5 year club!`. Replaces the list below."),
        Spec("yearclub_roles", "Club roles", "Year club", "collection", get=_clubs_get, item_get=_club_get,
             save=_club_save, remove=_club_remove,
             fields=(Field("years", "Years in the server (1-50)", new_only=True, max_length=2),),
             pick=Pick("role", "Choose the role for this year", defaults=lambda r: [r["role_id"]] if r.get("role_id") else []),
             help="A member holds the role for their completed years (the highest year at or below theirs)."),
    ]
