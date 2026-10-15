"""The `.redditfeed` control panel: feeds, the approval queue, posting and polling, and discovery defaults.

Specs only; every change goes through the cog's `_apply_*` methods.
"""
from . import constants
from .models import SubredditMapping
from .settings_panel import Field, ItemAction, PanelError, Pick, Spec

INTRO = ("**Feeds**: add subreddits to a channel, then pause / switch manual-auto / edit / remove each one. "
         "**Queue**: the mod channel where manual-approval posts wait. **Posting & polling** and **Discovery** hold the rest. "
         "`status`, `queue`, `check`, `backfill`, `discover`, `map`, `source` and `version` stay as commands.")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config, key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    found = guild.get_channel(value)
    return found.mention if found is not None else f"MISSING (id {value})"


# ------------------------------------------------------------------ feeds

async def _feeds(cog, guild):
    out = []
    for name, raw in sorted((await cog.config.mappings()).items()):
        m = SubredditMapping.from_dict(raw)
        bits = [m.approval.upper() if m.approval == constants.APPROVAL_AUTO else m.approval]
        if m.paused:
            bits.append("PAUSED")
        bits.append(" ".join(f"<#{c}>" for c in m.channel_ids) or "no channel")
        if m.require_keywords or m.block_keywords:
            bits.append("filtered")
        out.append((name, f"r/{name}", " · ".join(bits)))
    return out


async def _feed_get(cog, guild, item_id):
    raw = (await cog.config.mappings()).get(item_id)
    if not raw:
        return {}
    m = SubredditMapping.from_dict(raw)
    return {"require": ", ".join(m.require_keywords), "block": ", ".join(m.block_keywords), "channels": list(m.channel_ids)}


async def _feed_save(cog, guild, member, item_id, values, picked):
    if item_id is None:
        return await cog._apply_add(guild, member, values.get("subreddits", ""), picked)
    return await cog._apply_feed(guild, member, item_id, values.get("require", ""), values.get("block", ""), picked)


async def _feed_remove(cog, guild, member, item_id):
    return await cog._apply_remove(guild, member, item_id)


async def _pause(cog, guild, member, item_id):
    return await cog._apply_pause(guild, member, item_id, True)


async def _resume(cog, guild, member, item_id):
    return await cog._apply_pause(guild, member, item_id, False)


async def _switch_mode(cog, guild, member, item_id):
    return await cog._apply_switch_mode(guild, member, item_id)


async def _mode_all_apply(cog, guild, member, mode):
    return await cog._apply_mode_all(guild, member, mode)


async def _none(cog, guild):
    return None


async def _reset_all_apply(cog, guild, member, _value):
    return await cog._apply_reset_all(guild, member)


# ------------------------------------------------------------------ queue

async def _queue_channel_apply(cog, guild, member, channel):
    return await cog._apply_queue_channel(guild, member, channel)


async def _gates_get(cog, guild):
    c = cog.config
    return {"min_score": await c.queue_min_score(), "min_age": await c.queue_min_age_minutes(), "max_pending": await c.queue_max_pending()}


def _gates_fmt(v, guild):
    return f"score {v['min_score']}+, at least {v['min_age']} min old, cap {v['max_pending']} waiting" if v else "?"


async def _gates_apply(cog, guild, member, values):
    return await cog._apply_queue_gates(guild, member, values)


async def _queue_clear_apply(cog, guild, member, _value):
    return await cog._apply_queue_clear(guild, member)


# ------------------------------------------------------------------ posting & polling

async def _polling_get(cog, guild):
    return {"interval": await cog.config.poll_interval_seconds(), "stagger": await cog.config.stagger_seconds()}


def _polling_fmt(v, guild):
    return f"every {v['interval']}s, {v['stagger']}s between subreddits" if v else "?"


async def _polling_apply(cog, guild, member, values):
    return await cog._apply_polling(guild, member, values)


async def _x_apply(cog, guild, member, on):
    return await cog._apply_x_button(guild, member, on)


async def _redgifs_apply(cog, guild, member, mode):
    return await cog._apply_redgifs(guild, member, mode)


async def _log_apply(cog, guild, member, channel):
    return await cog._apply_log_channel(guild, member, channel)


# ------------------------------------------------------------------ discovery

def _category_fmt(value, guild):
    if not value:
        return "not set (discover never creates channels)"
    found = guild.get_channel(value)
    return found.name if found is not None else f"MISSING (id {value})"


async def _category_apply(cog, guild, member, category):
    return await cog._apply_category(guild, member, category)


async def _prefix_apply(cog, guild, member, values):
    return await cog._apply_prefix(guild, member, values.get("prefix", ""))


async def _denied(cog, guild):
    return [(name, f"r/{name}", "denied in discover") for name in sorted(await cog.config.denied_subreddits())]


async def _undeny(cog, guild, member, item_id):
    return await cog._apply_undeny(guild, member, item_id)


def build_specs():
    return [
        # ------------------------------------------------------------------ feeds
        Spec("feeds", "Subreddit feeds", "Feeds", "collection", get=_feeds, item_get=_feed_get, save=_feed_save, remove=_feed_remove,
             fields=(
                 Field("subreddits", "Subreddits (spaces or commas)", style="long", max_length=1000, new_only=True,
                       placeholder="feet footfetish r/soles"),
                 Field("require", "Must contain one of (comma separated)", style="long", required=False, max_length=500, edit_only=True,
                       value=lambda r: r.get("require", "")),
                 Field("block", "Never contain (comma separated)", style="long", required=False, max_length=500, edit_only=True,
                       value=lambda r: r.get("block", "")),
             ),
             pick=Pick("channel", "Choose the channel(s) the posts go to", min_values=1, max_values=10,
                       defaults=lambda r: list(r.get("channels", []))),
             item_actions=(
                 ItemAction("Pause", _pause), ItemAction("Resume", _resume),
                 ItemAction("Switch mode", _switch_mode),
             ),
             help="Add maps the subreddits to the channel(s) you pick (age-restricted only). Edit sets a feed's channels and keyword filters. "
                  "New feeds start on manual approval; Switch mode toggles manual/auto."),
        Spec("mode_all", "Set every feed to…", "Feeds", "choice", get=_none, apply=_mode_all_apply,
             choices=(("Manual: posts wait in the mod queue", constants.APPROVAL_MANUAL),
                      ("Auto: posts go straight to the channel", constants.APPROVAL_AUTO)),
             confirm="Change the approval mode of EVERY subreddit feed at once?", fmt=lambda v, g: "pick a mode to apply to all feeds"),
        Spec("reset_all", "Remove every feed", "Feeds", "action", apply=_reset_all_apply, button="Remove all feeds",
             confirm="Remove EVERY subreddit mapping, forget what was already posted and discard the waiting queue? Settings, the denied list and learned topics are kept.",
             help="Start over. Cards already in the queue channel stay in Discord but stop working."),
        # ------------------------------------------------------------------ queue
        Spec("queue_channel", "Queue channel", "Queue", "channel", get=_conf("queue_channel_id"), apply=_queue_channel_apply, fmt=_channel_fmt,
             channel_types=("text",), help="Mods-only and age-restricted: manual-approval posts wait here with Approve / Reject buttons."),
        Spec("queue_gates", "Queue gates", "Queue", "modal", get=_gates_get, apply=_gates_apply, fmt=_gates_fmt,
             fields=(
                 Field("min_score", "Minimum score (0 = none)", value=lambda v: str(v["min_score"]), max_length=6),
                 Field("min_age", "Minimum age in minutes", value=lambda v: str(v["min_age"]), max_length=6),
                 Field("max_pending", "Stop queueing at this many waiting", value=lambda v: str(v["max_pending"]), max_length=6),
             ), button="Edit gates…", help="Lets a post's score settle before a mod sees it."),
        Spec("queue_clear", "Clear the queue", "Queue", "action", apply=_queue_clear_apply, button="Clear the queue",
             confirm="Reject EVERYTHING waiting in the queue? Nothing is posted and those posts never come back.",
             help="Rejects every waiting item. Settings, subreddits and decided items are untouched. `.redditfeed queue` shows what is waiting."),
        # ------------------------------------------------------------------ posting & polling
        Spec("polling", "Polling", "Posting & polling", "modal", get=_polling_get, apply=_polling_apply, fmt=_polling_fmt,
             fields=(
                 Field("interval", "Seconds between poll cycles (min %d)" % constants.MIN_POLL_INTERVAL_SECONDS, value=lambda v: str(v["interval"]), max_length=6),
                 Field("stagger", "Seconds between subreddits in a cycle", value=lambda v: f"{v['stagger']:g}", max_length=6),
             ), button="Edit polling…"),
        Spec("x_button", "Mod-only X under posts", "Posting & polling", "toggle", get=_conf("x_button"), apply=_x_apply,
             help="Lets moderators delete a feed post. Existing posts keep theirs."),
        Spec("redgifs", "RedGifs posts", "Posting & polling", "choice", get=_conf("redgifs_mode"), apply=_redgifs_apply,
             choices=(("Upload: post the clip as a playable video", constants.REDGIFS_UPLOAD),
                      ("Link: post the bare link (no preview)", constants.REDGIFS_LINK))),
        Spec("log_channel", "Log channel", "Posting & polling", "channel", get=_conf("log_channel_id"), apply=_log_apply, fmt=_channel_fmt,
             channel_types=("text",), help="Where X removals and queue pauses are logged."),
        # ------------------------------------------------------------------ discovery
        Spec("category", "Category for new channels", "Discovery", "channel", get=_conf("new_channel_category_id"), apply=_category_apply,
             fmt=_category_fmt, clearable=True, channel_types=("category",),
             help="`discover` may create new age-restricted channels here. Unset = it never creates one."),
        Spec("prefix", "New channel name prefix", "Discovery", "modal", get=_conf("new_channel_prefix"), apply=_prefix_apply,
             fields=(Field("prefix", "Prefix (blank clears it)", value=lambda v: v or "", required=False, max_length=20, placeholder="\U0001F51E・"),),
             fmt=lambda v, g: f"`{v}`" if v else "none"),
        Spec("denied", "Denied in discover", "Discovery", "collection", get=_denied, remove=_undeny, can_add=False,
             help="Remove = let that subreddit be suggested again."),
    ]
