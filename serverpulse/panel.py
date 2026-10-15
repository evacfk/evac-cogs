"""The `.pulse settings` control panel: who can use `.pulse`, the live board, digests, join log and the weekly recap.

Specs only; every change goes through the cog's `_apply_*` methods. Reports, the export, backfills and the
join-log tools (`test`, `backfill`) stay commands.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. Reports (`.pulse day`, `week`, `heatmap`...), "
         "`.pulse export`, `.pulse backfill` and `.pulse joinlog backfill` stay as commands.")

VOICE_FIELD_MAX = 4000  # a Discord text box holds at most 4000 characters


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel_or_thread(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


def _role_fmt(value, guild):
    if not value:
        return "none (admins and Red mods only)"
    role = guild.get_role(value)
    return role.mention if role is not None else f"MISSING (id {value})"


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _sub(key, sub):
    async def get(cog, guild):
        return (await getattr(cog.config.guild(guild), key)()).get(sub)
    return get


def _apply(name, *prefix):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, *prefix, value)
    return apply


async def _ignored_get(cog, guild):
    return {"channels": " ".join(f"<#{c}>" for c in await cog.config.guild(guild).ignored_channels())}


async def _options_get(cog, guild):
    r = await cog.config.guild(guild).recap()
    return {"model": r["model"], "drafts": r["max_drafts_week"], "reactions": r["min_reactions"]}


async def _voice_get(cog, guild):
    return {"voice": (await cog.config.guild(guild).recap())["voice"]}


async def _note_get(cog, guild):
    return {"note": (await cog.config.guild(guild).recap())["notes"]}


async def _excluded_get(cog, guild):
    return {"users": " ".join(f"<@{u}>" for u in (await cog.config.guild(guild).recap())["excluded_users"])}


async def _sources_get(cog, guild):
    labels = await cog.config.guild(guild).invite_sources()
    return [(code, name, f"discord.gg/{code}") for code, name in sorted(labels.items(), key=lambda kv: kv[1].lower())]


async def _source_get(cog, guild, item_id):
    labels = await cog.config.guild(guild).invite_sources()
    return {"code": item_id, "name": labels.get(item_id, "")}


async def _source_save(cog, guild, member, item_id, values, picked):
    return await cog._apply_source_save(guild, member, item_id, values)


async def _source_remove(cog, guild, member, item_id):
    return await cog._apply_source_remove(guild, member, item_id)


def build_specs():
    return [
        # -- Access
        Spec("mod_channel", "Mod channel", "Access", "channel", get=_conf("mod_channel_id"), apply=_apply("_apply_mod_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="`.pulse` only works here, and digests are posted here."),
        Spec("mod_role", "Mod role", "Access", "role", get=_conf("mod_role_id"), apply=_apply("_apply_mod_role"),
             fmt=_role_fmt, clearable=True, help="Allowed to use `.pulse`. Admins and Red mods always can."),
        Spec("exclude_commands", "Ignore bot commands", "Access", "toggle", get=_conf("exclude_commands"),
             apply=_apply("_apply_exclude_commands"),
             help="Messages like `.gamble` don't count, so game nights don't read as chatty."),
        Spec("ignored", "Ignored channels", "Access", "modal", get=_ignored_get, apply=_apply("_apply_ignored"),
             fmt=lambda v, g: (v["channels"] or "none") if v else "?", button="Edit…",
             fields=(Field("channels", "Channels (mentions or IDs; blank = none)", value=lambda v: v["channels"], style="long",
                           required=False, max_length=2000),),
             help="Not counted. Threads follow their parent channel."),
        # -- Board & digests
        Spec("board", "Live board", "Board & digests", "channel", get=_sub("board", "channel_id"), apply=_apply("_apply_board"),
             fmt=_channel_fmt, channel_types=("text",), clearable=True,
             help="Pick a channel to post the self-updating board there (it moves if one exists). Clear stops and removes it."),
        Spec("board_refresh", "Update the board now", "Board & digests", "action", apply=_apply("_apply_board_refresh"),
             button="Update now"),
        Spec("weekly", "Weekly digest", "Board & digests", "toggle", get=_sub("digest_weekly", "enabled"),
             apply=_apply("_apply_digest", "digest_weekly"), help="Mondays 9am Pacific, in the mod channel."),
        Spec("monthly", "Monthly digest", "Board & digests", "toggle", get=_sub("digest_monthly", "enabled"),
             apply=_apply("_apply_digest", "digest_monthly"), help="The 1st, 9am Pacific, in the mod channel."),
        # -- Join log
        Spec("joinlog_join", "Join log channel", "Join log", "channel", get=_sub("joinlog", "join_channel"),
             apply=_apply("_apply_joinlog", "join"), fmt=_channel_fmt, channel_types=("text",),
             help="Where your bot logs joins. Then run `.pulse joinlog test` and `.pulse joinlog backfill`."),
        Spec("joinlog_leave", "Leave log channel", "Join log", "channel", get=_sub("joinlog", "leave_channel"),
             apply=_apply("_apply_joinlog", "leave"), fmt=lambda v, g: _channel_fmt(v, g) if v else "same as joins",
             channel_types=("text",), clearable=True, help="Only if leaves are logged in a different channel."),
        Spec("sources", "Invite names", "Join log", "collection", get=_sources_get, item_get=_source_get,
             save=_source_save, remove=_source_remove,
             fields=(Field("code", "Invite code or discord.gg link", new_only=True, max_length=80),
                     Field("name", "Name to show (like Disboard)", value=lambda r: r.get("name", ""), max_length=40)),
             help="Names for `.pulse sources`. Joins through other invites are labelled by who made them."),
        # -- Recap
        Spec("recap_source", "Read the week from", "Recap", "channel", get=_sub("recap", "source_channel"),
             apply=_apply("_apply_recap_channel", "source"), fmt=lambda v, g: _channel_fmt(v, g) if v else "the channel named cuddle",
             channel_types=("text",), clearable=True, help="Where highlights come from (usually #cuddle)."),
        Spec("recap_post", "Post approved recaps in", "Recap", "channel", get=_sub("recap", "post_channel"),
             apply=_apply("_apply_recap_channel", "post"), fmt=_channel_fmt, channel_types=("text",),
             help="Approve does nothing until this is set."),
        Spec("recap_auto", "Draft every Monday", "Recap", "toggle", get=_sub("recap", "auto"), apply=_apply("_apply_recap_auto"),
             help="After the weekly digest. Nothing goes public until you press Approve."),
        Spec("recap_options", "Model and limits", "Recap", "modal", get=_options_get, apply=_apply("_apply_recap_options"),
             fmt=lambda v, g: f"{v['model']} · {v['drafts']} drafts/week · {v['reactions']}+ reactions" if v else "?",
             button="Edit…",
             fields=(Field("model", "Claude model", value=lambda v: v["model"], max_length=64),
                     Field("drafts", "Most drafts in any 7 days (1-30)", value=lambda v: str(v["drafts"]), max_length=2),
                     Field("reactions", "Reactions a highlight needs (1-100)", value=lambda v: str(v["reactions"]), max_length=3)),
             help="The drafts limit is the cost cap, and it counts regenerates."),
        Spec("recap_voice", "Your voice", "Recap", "modal", get=_voice_get, apply=_apply("_apply_recap_voice"),
             fmt=lambda v, g: (f"{len(v['voice']):,} characters saved" if v["voice"] else "not set (warm default tone)") if v else "?",
             button="Edit…",
             fields=(Field("voice", "Past announcements in your style", value=lambda v: v["voice"][:VOICE_FIELD_MAX], style="long",
                           required=False, max_length=VOICE_FIELD_MAX),),
             help="Claude copies the tone only, never the content, so old announcements are fine."),
        Spec("recap_note", "Note for the next draft", "Recap", "modal", get=_note_get, apply=_apply("_apply_recap_note"),
             fmt=lambda v, g: ("saved" if v["note"] else "none") if v else "?", button="Edit…",
             fields=(Field("note", "What happened this week (blank = none)", value=lambda v: v["note"], style="long",
                           required=False, max_length=600),),
             help="Events or news. Used once by the next draft, then cleared."),
        Spec("recap_excluded", "Left out of recaps", "Recap", "modal", get=_excluded_get, apply=_apply("_apply_recap_excluded"),
             fmt=lambda v, g: (v["users"] or "nobody") if v else "?", button="Edit…",
             fields=(Field("users", "Members (mentions or IDs; blank = nobody)", value=lambda v: v["users"], style="long",
                           required=False, max_length=1500),),
             help="Their messages are never read or highlighted."),
    ]
