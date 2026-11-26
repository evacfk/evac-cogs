"""The `.verdict settings` control panel: the daily post, the suggestion review and the monthly title.

Specs only; every change goes through the cog's `_apply_*` methods. `add`, `queue`, `remove` and `post`
stay commands (staff), and `.verdict` itself shows a member's record.
"""
from .constants import ACRO_MAX_ENTRIES
from .settings_panel import Field, Spec

INTRO = ("Pick a section, then a setting. Changes save as soon as you choose. Staff keep `.verdict add`, `queue`, "
         "`remove` and `post` as commands, and get an Acrophobia mod panel from `.verdict settings`.")


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


async def _acro_prize_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"prize": await conf.acro_prize(), "cap": await conf.acro_daily_cap()}


async def _acro_themes_get(cog, guild):
    return {"themes": list(await cog.config.guild(guild).acro_themes())}


def _acro_themes_fmt(v, g):
    n = len((v or {}).get("themes") or [])
    return f"{n} themes" if n else "none (no theme on any round)"


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
        Spec("acro_enabled", "Acrophobia", "Acrophobia", "toggle", get=_conf("acro_enabled"),
             apply=_apply("_apply_acro_enabled"),
             confirm="Acrophobia pays wondercoin: the prize on every win, up to the daily cap below. Switch it on?",
             help="A daily acronym game in the daily question channel: submit until ~2pm, vote until ~6pm Pacific, opens with the question."),
        Spec("acro_prize", "Prize & daily cap", "Acrophobia", "modal", get=_acro_prize_get,
             apply=_apply("_apply_acro_prize"),
             fmt=lambda v, g: f"{v['prize']:,} per win · {v['cap']:,} a day at most" if v else "?", button="Edit…",
             fields=(Field("prize", "Wondercoin for the winner (split on a tie)", value=lambda v: str(v["prize"]), max_length=7),
                     Field("cap", "Most wondercoin paid per day, all rounds", value=lambda v: str(v["cap"]), max_length=8)),
             help="The daily cap is the faucet: once it's used up, wins still count but pay nothing until tomorrow."),
        Spec("acro_themes", "Themes", "Acrophobia", "modal", get=_acro_themes_get, apply=_apply("_apply_acro_themes"),
             fmt=_acro_themes_fmt, button="Edit…",
             fields=(Field("themes", "One theme per line", style="long", required=False,
                           value=lambda v: "\n".join(v["themes"]), max_length=4000),),
             help=f"Most rounds pick one at random as a prompt. Max {ACRO_MAX_ENTRIES} entries a round."),
        Spec("acro_start", "Start a round now", "Acrophobia", "action", apply=_apply("_apply_acro_start"),
             button="Start now", help="Needs Acrophobia switched on and no round already open."),
        Spec("acro_cancel", "Cancel the open round", "Acrophobia", "action", apply=_apply("_apply_acro_cancel"),
             button="Cancel round", confirm="Cancel the round that's open right now? Nothing is paid.",
             help="Mods can also do this, and remove single entries, from the mod panel (Mod button on the round)."),
    ]
