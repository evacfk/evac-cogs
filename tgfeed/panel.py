"""The `.tgfeed` control panel: Telegram group, topic mapping, new-channel defaults and pacing.

Specs only; every change goes through the cog's `_apply_*` methods.
"""
import time

from . import constants
from .models import TopicMapping
from .settings_panel import Field, ItemAction, PanelError, Pick, Spec

INTRO = ("Set up in order: **Connection** (the Telegram group and where new channels go), then **Topics** "
         "(load the group's topics and map the ones you want), then check **Pace & posts**. "
         "`status` and `version` stay as commands. Logging in to Telegram happens once on the host (`login.py`).")

TOPIC_CACHE_SECONDS = 1800


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config, key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    found = guild.get_channel(value)
    return found.mention if found is not None else f"MISSING (id {value})"


# ------------------------------------------------------------------ connection

async def _group_get(cog, guild):
    return await cog.config.group()


def _group_fmt(value, guild):
    return value.get("title") or "?" if value else "not set"


async def _group_apply(cog, guild, member, values):
    return await cog._apply_group(guild, member, values.get("link", ""))


async def _category_apply(cog, guild, member, category):
    return await cog._apply_category(guild, member, category)


def _category_fmt(value, guild):
    if not value:
        return "not set"
    found = guild.get_channel(value)
    return found.name if found is not None else f"MISSING (id {value})"


async def _prefix_apply(cog, guild, member, values):
    return await cog._apply_prefix(guild, member, values.get("prefix", ""))


async def _view_roles_apply(cog, guild, member, roles):
    return await cog._apply_view_roles(guild, member, roles)


async def _running_get(cog, guild):
    return not await cog.config.paused()


async def _running_apply(cog, guild, member, on):
    return await cog._apply_running(guild, member, on)


def _running_fmt(value, guild):
    return "running" if value else "PAUSED (`.tgfeed status` says why)"


async def _resume_skip_apply(cog, guild, member, _value):
    return await cog._apply_resume_skip(guild, member)


# ------------------------------------------------------------------ topics

async def _load_apply(cog, guild, member, _value):
    return await cog._apply_load_topics(guild, member)


def _cached_topics(cog):
    cache = getattr(cog, "_topics_cache", None)
    if cache and time.time() - cache[0] < TOPIC_CACHE_SECONDS:
        return cache[1]
    return []


async def _topics(cog, guild):
    """Mapped topics first (from our own settings), then any unmapped ones from the last load."""
    maps = {int(k): TopicMapping.from_dict(v) for k, v in (await cog.config.mappings()).items()}
    out = []
    for m in sorted(maps.values(), key=lambda m: m.topic_id):
        state = " (paused)" if m.paused else ""
        out.append((str(m.topic_id), m.title or f"Topic {m.topic_id}", f"→ <#{m.channel_id}>{state} · {m.posted_total} files"))
    for topic in _cached_topics(cog):
        if topic.id not in maps:
            out.append((str(topic.id), topic.title, "not mapped"))
    return out


async def _topic_get(cog, guild, item_id):
    raw = (await cog.config.mappings()).get(str(item_id))
    return {"channel_id": raw["channel_id"]} if raw else {}


async def _topic_save(cog, guild, member, item_id, values, picked):
    if not picked:
        raise PanelError("Pick a channel.")
    return await cog._apply_map(guild, member, str(item_id), picked[0])


async def _topic_remove(cog, guild, member, item_id):
    return await cog._apply_unmap(guild, member, str(item_id))


def _action(name):
    async def run(cog, guild, member, item_id):
        return await getattr(cog, name)(guild, member, str(item_id))
    return run


async def _map_new(cog, guild, member, item_id):
    return await cog._apply_map(guild, member, str(item_id), None)


async def _map_by_name_apply(cog, guild, member, values):
    return await cog._apply_map(guild, member, values.get("topic", "").strip(), None)


# ------------------------------------------------------------------ pace & posts

async def _throttle_get(cog, guild):
    c = cog.config
    return {"interval": await c.poll_interval_seconds(), "gap_min": await c.file_gap_min(), "gap_max": await c.file_gap_max(),
            "per_hour": await c.max_per_hour(), "per_day": await c.max_per_day()}


def _throttle_fmt(v, guild):
    if not v:
        return "?"
    return (f"every ~{v['interval']}s, {v['gap_min']:g}-{v['gap_max']:g}s between downloads, "
            f"{v['per_hour']}/hour, {v['per_day']}/day")


async def _throttle_apply(cog, guild, member, values):
    return await cog._apply_throttle(guild, member, values)


async def _x_apply(cog, guild, member, on):
    return await cog._apply_x_button(guild, member, on)


async def _log_apply(cog, guild, member, channel):
    return await cog._apply_log_channel(guild, member, channel)


def build_specs():
    return [
        # ------------------------------------------------------------------ connection
        Spec("running", "Feed", "Connection", "toggle", get=_running_get, apply=_running_apply, fmt=_running_fmt,
             help="Off pauses everything. It also pauses itself after repeated Telegram rate limits or a login problem."),
        Spec("resume_skip", "Resume and skip what was missed", "Connection", "action", apply=_resume_skip_apply,
             button="Resume and skip", confirm="Turn the feed on and jump every topic to 'now'? Anything posted in Telegram while it was off is never mirrored.",
             help="Use after a long pause when you don't want a flood of old posts."),
        Spec("group", "Telegram group", "Connection", "modal", get=_group_get, apply=_group_apply, fmt=_group_fmt,
             fields=(Field("link", "Invite or t.me link", max_length=200, placeholder="https://t.me/+AbCdEf... (a group you're already in)"),),
             button="Set group…", help="Must be a forum-style group this Telegram account has already joined."),
        Spec("category", "Category for new channels", "Connection", "channel", get=_conf("category_id"), apply=_category_apply,
             fmt=_category_fmt, clearable=True, channel_types=("category",),
             help="Where mapping a topic creates its channel. Without one, map topics to existing channels."),
        Spec("prefix", "Channel name prefix", "Connection", "modal", get=_conf("name_prefix"), apply=_prefix_apply,
             fields=(Field("prefix", "Prefix (blank clears it)", value=lambda v: v or "", required=False, max_length=40, placeholder="tg-"),),
             fmt=lambda v, g: f"`{v}`" if v else "none"),
        Spec("view_roles", "Roles that can see new channels", "Connection", "role", get=_conf("view_role_ids"), apply=_view_roles_apply,
             multi=25, clearable=True,
             help="New channels are hidden from everyone but you and the bot, plus these roles. Existing channels keep their permissions."),
        # ------------------------------------------------------------------ topics
        Spec("load_topics", "Load Telegram topics", "Topics", "action", apply=_load_apply, button="Load topics",
             help="Fetches the group's topic list so unmapped topics show up below."),
        Spec("topics", "Topics", "Topics", "collection", get=_topics, item_get=_topic_get, save=_topic_save, remove=_topic_remove,
             can_add=False, pick=Pick("channel", "Choose an existing channel for this topic", defaults=lambda r: [r["channel_id"]] if r.get("channel_id") else []),
             item_actions=(
                 ItemAction("Map (new channel)", _map_new, style="success"),
                 ItemAction("Pause", _action("_apply_pause_topic")),
                 ItemAction("Resume", _action("_apply_resume_topic")),
             ),
             help="Edit = send this topic to an existing channel; Remove = unmap (the channel is left alone). Only new posts are mirrored."),
        Spec("map_by_name", "Map a topic by id or title", "Topics", "modal", apply=_map_by_name_apply,
             fmt=lambda v, g: "creates a new channel", button="Map…",
             fields=(Field("topic", "Topic id or exact title", max_length=100),),
             help="For topics missing from the list above (it shows 25 at most)."),
        # ------------------------------------------------------------------ pace & posts
        Spec("throttle", "Pacing and caps", "Pace & posts", "modal", get=_throttle_get, apply=_throttle_apply, fmt=_throttle_fmt,
             fields=(
                 Field("interval", "Seconds between polls (min %d)" % constants.MIN_POLL_INTERVAL_SECONDS, value=lambda v: str(v["interval"]), max_length=6),
                 Field("gap_min", "Shortest pause between downloads (s)", value=lambda v: f"{v['gap_min']:g}", max_length=6),
                 Field("gap_max", "Longest pause between downloads (s)", value=lambda v: f"{v['gap_max']:g}", max_length=6),
                 Field("per_hour", "Files per hour (min %d)" % constants.MIN_FILES_PER_HOUR, value=lambda v: str(v["per_hour"]), max_length=6),
                 Field("per_day", "Files per day", value=lambda v: str(v["per_day"]), max_length=6),
             ), button="Edit pacing…", help="Slow on purpose to protect the Telegram account. Raise caps carefully."),
        Spec("x_button", "Mod-only X under posts", "Pace & posts", "toggle", get=_conf("x_button"), apply=_x_apply,
             help="Lets moderators delete a mirrored post. Applies to new posts."),
        Spec("log_channel", "Log channel", "Pace & posts", "channel", get=_conf("log_channel_id"), apply=_log_apply, fmt=_channel_fmt,
             channel_types=("text",), help="Where removals and Telegram warnings are logged."),
    ]
