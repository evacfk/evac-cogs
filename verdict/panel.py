"""The `.verdict settings` control panel: the daily post, the suggestion review and the monthly title.

Specs only; every change goes through the cog's `_apply_*` methods. `add`, `queue`, `remove` and `post`
stay commands (staff), and `.verdict` itself shows a member's record.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a section, then a setting. Changes save as soon as you choose. Staff keep `.verdict add`, `queue`, "
         "`remove` and `post` as commands.")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


def _role_fmt(none_text):
    def fmt(value, guild):
        if not value:
            return none_text
        role = guild.get_role(value)
        return role.mention if role is not None else f"MISSING (id {value})"
    return fmt


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


async def _hour_get(cog, guild):
    return {"hour": await cog.config.guild(guild).post_hour()}


async def _approvers_get(cog, guild):
    return {"ids": await cog.config.guild(guild).approver_ids()}


def _approvers_fmt(value, guild):
    ids = (value or {}).get("ids") or []
    return "owner only" if not ids else "owner + " + ", ".join(f"<@{i}>" for i in ids)


def build_specs():
    return [
        Spec("enabled", "Daily question", "Daily post", "toggle", get=_conf("enabled"), apply=_apply("_apply_enabled"),
             help="Posts one question a day at the hour below. Needs the channel first."),
        Spec("channel", "Question channel", "Daily post", "channel", get=_conf("channel_id"), apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Where the daily question is posted (#cuddle)."),
        Spec("hour", "Post hour", "Daily post", "modal", get=_hour_get, apply=_apply("_apply_hour"),
             fmt=lambda v, g: f"{v['hour']}:00 Pacific" if v else "?", button="Edit…",
             fields=(Field("hour", "Pacific hour the question goes out (0-23)", value=lambda v: str(v["hour"]), max_length=2),)),
        Spec("ping_role", "Ping role", "Daily post", "role", get=_conf("ping_role_id"), apply=_apply("_apply_ping_role"),
             fmt=_role_fmt("no ping"), clearable=True, help="Pinged on each daily post. Clear to stop pinging."),
        Spec("threads", "Discussion threads", "Daily post", "toggle", get=_conf("threads_enabled"),
             apply=_apply("_apply_threads"), help="A voters-only discussion thread per question."),
        Spec("review_channel", "Review channel", "Suggestions & title", "channel", get=_conf("review_channel_id"),
             apply=_apply("_apply_review_channel"), fmt=_channel_fmt, channel_types=("text",),
             help="Private channel where member suggestions wait for a tick or cross. Must not be visible to @everyone."),
        Spec("approvers", "Extra approvers", "Suggestions & title", "modal", get=_approvers_get,
             apply=_apply("_apply_approvers"), fmt=lambda v, g: _approvers_fmt(v, g), button="Edit…",
             fields=(Field("ids", "User IDs or @mentions (blank = owner only)", style="long", required=False,
                           value=lambda v: " ".join(str(i) for i in v["ids"]), max_length=1000),),
             help="People whose tick or cross counts, besides the bot owner. This replaces the list."),
        Spec("mind_reader_role", "Mind Reader role", "Suggestions & title", "role", get=_conf("mind_reader_role_id"),
             apply=_apply("_apply_mind_reader_role"), fmt=_role_fmt("title off"), clearable=True,
             help="Given to last month's best crowd-reader. Clear to turn the title off."),
    ]
