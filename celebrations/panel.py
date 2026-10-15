"""The `.celebrations` control panel: channels, birthday role, gift, timing, star and anniversaries.

Specs only; every change goes through the cog's `_apply_*` methods. `setfor`, `clearfor`, `preview`,
`run` and `settings` stay commands.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a section, then a setting. Changes save as soon as you choose. `.celebrations setfor`, `clearfor`, "
         "`preview`, `run` and `settings` stay as commands; members use `.birthday`.")


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


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


async def _gift_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"gift_min": await conf.gift_min(), "gift_max": await conf.gift_max(),
            "min_days": await conf.gift_min_member_days()}


def _gift_fmt(v, guild):
    if not v:
        return "?"
    gift = ("off" if not v["gift_max"] else f"{v['gift_min']:,}" if v["gift_min"] == v["gift_max"]
            else f"{v['gift_min']:,} to {v['gift_max']:,}")
    return f"{gift} (joined less than {v['min_days']} days ago: no gift)"


def _one(key, label, tab, conf_key, field, box, apply_name, fmt, help="", max_length=8, required=True):
    async def get(cog, guild):
        return {field: await getattr(cog.config.guild(guild), conf_key)()}
    return Spec(key, label, tab, "modal", get=get, apply=_apply(apply_name), fmt=lambda v, g: fmt(v[field]) if v else "?",
                button="Edit…", fields=(Field(field, box, value=lambda v: str(v[field] if v[field] is not None else ""),
                                              max_length=max_length, required=required),), help=help)


def build_specs():
    return [
        Spec("channel", "Birthday channel", "Birthdays", "channel", get=_conf("channel_id"), apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Birthday and anniversary posts go here and stay up (usually #cuddle)."),
        Spec("announce", "Announcement channel", "Birthdays", "channel", get=_conf("announce_channel_id"),
             apply=_apply("_apply_announce_channel"), fmt=_channel_fmt, channel_types=("text",), clearable=True,
             help="A temporary birthday post, deleted when the day ends. Clear to turn it off."),
        Spec("role", "Birthday role", "Birthdays", "role", get=_conf("role_id"), apply=_apply("_apply_role"), fmt=_role_fmt,
             help="Held for the Pacific day. It is made hoisted if it isn't."),
        Spec("makerole", "Create the birthday role", "Birthdays", "action", apply=_apply("_apply_makerole"),
             button="Create role", help="Makes a hoisted 🎂 Birthday role and uses it. Needs Manage Roles."),
        Spec("gift", "Birthday gift", "Birthdays", "modal", get=_gift_get, apply=_apply("_apply_gift"), fmt=_gift_fmt,
             button="Edit…",
             fields=(Field("gift_min", "Minimum gift (0 = no gift)", value=lambda v: str(v["gift_min"]), max_length=9),
                     Field("gift_max", "Maximum gift (blank = same as minimum)",
                           value=lambda v: str(v["gift_max"]), required=False, max_length=9),
                     Field("min_days", "Days in the server before a gift", value=lambda v: str(v["min_days"]), max_length=4)),
             help="Anti-farming: members who joined more recently than the days above get no gift."),
        _one("hour", "Post hour", "Birthdays", "announce_hour", "hour", "Pacific hour the posts go out (0-23)",
             "_apply_hour", lambda h: f"{h}:00 Pacific", max_length=2,
             help="The role and gift start at midnight Pacific; the posts follow at this hour."),
        _one("star", "Star reaction", "Birthdays", "star_emoji", "emoji", "Emoji added to the post (blank = none)",
             "_apply_star", lambda e: e or "none", max_length=64, required=False,
             help="Match your starboard's emoji."),
        Spec("anniversaries", "Join anniversaries", "Anniversaries", "toggle", get=_conf("anniversaries"),
             apply=_apply("_apply_anniversaries"), help="A daily post celebrating members' join anniversaries."),
        _one("anniversary_gift", "Anniversary gift", "Anniversaries", "anniversary_gift", "amount",
             "Coins per anniversary (0 = off)", "_apply_anniversary_gift", lambda a: f"{a:,}" if a else "off", max_length=9),
    ]
