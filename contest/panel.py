"""The `.contest` control panel: set up, launch, schedule and run Wonderween 2026.

Specs only; every change goes through the cog's `_apply_*` methods.
"""
from . import engine
from .settings_panel import Field, Spec

INTRO = ("Work through **Setup**, **Prizes** and **Schedule**, press **Check setup** in **Run**, then **Launch**. "
         "Photos are deleted at the purge.")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


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


def _roles_fmt(value, guild):
    ids = [i for i in (value or []) if i]
    return ", ".join(f"<@&{i}>" for i in ids) if ids else "none yet"


async def _coins_get(cog, guild):
    g = await cog.config.guild(guild).all()
    return {k: g[f"coins_{k}"] for k in ("gold", "second", "third", "participant")}


def _coins_fmt(v, guild):
    if not v or not any(v.values()):
        return "not set"
    return f"gold {v['gold']:,} · 2nd {v['second']:,} · 3rd {v['third']:,} · everyone {v['participant']:,}"


async def _shop_get(cog, guild):
    return {"item": await cog.config.guild(guild).shop_item()}


async def _schedule_get(cog, guild):
    g = await cog.config.guild(guild).all()
    return {k: g[k] for k in engine.SCHEDULE_KEYS}


def _schedule_fmt(v, guild):
    if not v:
        return "?"
    return " · ".join(f"{engine.SCHEDULE_LABELS[k].split(',')[0]} {engine.fmt_pt(v[k])}" for k in engine.SCHEDULE_KEYS)


def _stamp(key):
    return lambda v: engine.fmt_pt(v[key]).replace(" PT", "") if v else ""


def _stamp_input(key):
    from datetime import datetime
    from .constants import HOME_TZ

    def value(v):
        return datetime.fromtimestamp(v[key], HOME_TZ).strftime("%Y-%m-%d %H:%M") if v else ""
    return value


async def _picks_get(cog, guild):
    g = await cog.config.guild(guild).all()
    p = list(g["mods_picks"]) + [None] * 3
    return {"first": p[0], "second": p[1], "third": p[2], "eligible": cog._podium(g)["eligible"]}


def _picks_fmt(v, guild):
    if not v:
        return "?"
    show = lambda n: f"#{n}" if n else "blank"  # noqa: E731
    pick = f"1st {show(v['first'])} · 2nd {show(v['second'])} · 3rd {show(v['third'])}"
    return pick + " (pickable: " + (", ".join(f"#{n}" for n in v["eligible"]) or "none") + ")"


def build_specs():
    sched_fields = tuple(
        Field(k, f"{engine.SCHEDULE_LABELS[k]} (PT)", value=_stamp_input(k), max_length=16, placeholder="2026-10-31 23:00")
        for k in engine.SCHEDULE_KEYS)
    return [
        # ---- Setup
        Spec("approval", "Review channel", "Setup", "channel", get=_conf("approval_channel_id"),
             apply=_apply("_apply_approval_channel"), fmt=_channel_fmt, channel_types=("text",),
             help="Private channel where entries wait for Approve / Reject (#mod-chat works). Photos sit there until the purge."),
        Spec("announce", "Announcement channel", "Setup", "channel", get=_conf("announce_channel_id"),
             apply=_apply("_apply_announce_channel"), fmt=_channel_fmt, channel_types=("text",),
             help="Where the rules and the Enter button are posted at launch."),
        Spec("results", "Results channel", "Setup", "channel", get=_conf("results_channel_id"),
             apply=_apply("_apply_results_channel"), fmt=_channel_fmt, channel_types=("text",),
             help="A permanent channel for the text-only winners post (the contest channel is deleted)."),
        Spec("category", "Category for the contest channel", "Setup", "channel", get=_conf("category_id"),
             apply=_apply("_apply_category"), fmt=_channel_fmt, channel_types=("category",), clearable=True,
             help="Optional. Blank = next to the announcement channel."),
        Spec("excluded", "Excluded roles", "Setup", "role", get=_conf("excluded_role_ids"), apply=_apply("_apply_excluded"),
             fmt=_roles_fmt, multi=10, clearable=True,
             help="Cannot see the contest channel, enter or vote: pick 13-17 and Muted. The Lurker role is added automatically."),
        # ---- Prizes
        Spec("coins", "Wondercoin amounts", "Prizes", "modal", get=_coins_get, apply=_apply("_apply_coins"), fmt=_coins_fmt,
             button="Edit…",
             fields=(Field("gold", "Gold (1st in either award)", value=lambda v: str(v["gold"]), max_length=9),
                     Field("second", "2nd place", value=lambda v: str(v["second"]), max_length=9),
                     Field("third", "3rd place", value=lambda v: str(v["third"]), max_length=9),
                     Field("participant", "Everyone who enters", value=lambda v: str(v["participant"]), max_length=9)),
             help="Paid at the reveal. One tier per person: their best."),
        Spec("shop", "Gold shop item", "Prizes", "modal", get=_shop_get, apply=_apply("_apply_shop"),
             fmt=lambda v, g: (v or {}).get("item") or "not set", button="Edit…",
             fields=(Field("item", "Shop item for each gold winner", value=lambda v: v["item"], max_length=80),),
             help="At the reveal I list who to deliver it to in the review channel."),
        Spec("bronze", "Bronze Pumpkin role", "Prizes", "role", get=_conf("bronze_role_id"),
             apply=lambda cog, guild, member, role: cog._apply_role(guild, member, role, "bronze_role_id", "Bronze Pumpkin role"),
             fmt=_role_fmt("created at launch"), clearable=True, help="Everyone who enters. Leave blank to create it."),
        Spec("gold", "Golden Pumpkin role", "Prizes", "role", get=_conf("gold_role_id"),
             apply=lambda cog, guild, member, role: cog._apply_role(guild, member, role, "gold_role_id", "Golden Pumpkin role"),
             fmt=_role_fmt("created at launch"), clearable=True, help="Gold winners. Leave blank to create it."),
        # ---- Schedule
        Spec("schedule", "Dates (Pacific time)", "Schedule", "modal", get=_schedule_get, apply=_apply("_apply_schedule"),
             fmt=_schedule_fmt, button="Edit…", fields=sched_fields,
             help="YYYY-MM-DD HH:MM. Blank boxes keep their value. The announcement keeps the dates it was posted with."),
        # ---- Run
        Spec("check", "Check setup", "Run", "action", apply=_apply("_apply_check"), button="Check setup",
             help="Permissions, roles, LevelUp, economy and anything missing."),
        Spec("launch", "Launch", "Run", "action", apply=_apply("_apply_launch"), button="Launch the contest",
             confirm="Launch the contest now? This posts the announcement with the Enter button and creates the hidden contest channel.",
             help="Posts the rules and opens entries."),
        Spec("summary", "Status", "Run", "action", apply=_apply("_apply_summary"), button="Show status",
             help="Entries, votes and dates."),
        Spec("picks", "Mods' Choice picks", "Run", "modal", get=_picks_get, apply=_apply("_apply_picks"), fmt=_picks_fmt,
             button="Edit…",
             fields=(Field("first", "1st place costume number", value=lambda v: str(v["first"] or ""), required=False, max_length=4),
                     Field("second", "2nd place costume number", value=lambda v: str(v["second"] or ""), required=False, max_length=4),
                     Field("third", "3rd place costume number", value=lambda v: str(v["third"] or ""), required=False, max_length=4)),
             help="Mod entrants and the People's Choice podium cannot be picked. The reveal waits until these are filled."),
        Spec("remove", "Remove an entry", "Run", "modal", get=None, apply=_apply("_apply_remove"), button="Remove…",
             fields=(Field("number", "Costume number to remove", max_length=4),),
             help="Deletes the post and its votes. Also how you clear a test entry."),
        Spec("open", "Open the channel now", "Run", "action", apply=_apply("_apply_open_now"), button="Open now",
             confirm="Make the contest channel visible to everyone except the excluded roles right now?",
             help="Normally automatic at the open time."),
        Spec("hide", "Hide the channel again", "Run", "action", apply=_apply("_apply_hide"), button="Hide",
             confirm="Hide the contest channel from everyone but the mods again?",
             help="Reopens automatically at the open time if that has passed."),
        Spec("reveal", "Reveal results now", "Run", "action", apply=_apply("_apply_reveal"), button="Reveal now",
             confirm="Post the results and pay everyone now? Blank Mods' Choice slots stay blank. This cannot be undone.",
             help="Normally automatic at the reveal time once the picks are filled."),
        Spec("hold", "Hold the automatic purge", "Run", "toggle", get=_conf("hold_purge"), apply=_apply("_apply_hold"),
             help="On = nothing is deleted at the purge time until you turn it off."),
        Spec("preview", "Preview the purge", "Run", "action", apply=_apply("_apply_purge_preview"), button="Preview purge",
             help="Lists exactly what the purge will delete."),
        Spec("purge", "Purge now", "Run", "action", apply=_apply("_apply_purge"), button="Purge now",
             confirm="Delete the contest channel, every stored photo, the review cards and the votes right now? This cannot be undone.",
             help="Normally automatic at the purge time after the reveal."),
    ]
