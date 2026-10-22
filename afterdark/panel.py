"""The `.afterdark` control panel: every setting that used to be a `set`/`drop`/`interest` command.

Each spec reads Config through the cog and writes only through the cog's `_apply_*` methods, so the
validation is the same as ever. No discord objects are created here, only plain specs.
"""
from . import constants as C
from . import engine
from .models import Interest
from .settings_panel import Field, ItemAction, PanelError, Pick, Spec

INTRO = ("Pick a section, then a setting. Changes save as soon as you choose. "
         "Member lookups (`status`, `check`, `grant`, `revoke`, `exclude`, `clear`, `sweep`) stay as commands.")


def _cfg(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _role_fmt(value, guild):
    if not value:
        return "not set"
    role = guild.get_role(value)
    return role.mention if role is not None else f"MISSING (id {value})"


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


def _role(key, label, clearable=False, help=""):
    async def apply(cog, guild, member, role):
        return await cog._apply_role(guild, member, key, role)
    return Spec(key, label, "Roles & channels", "role", get=_cfg(key), apply=apply, fmt=_role_fmt, clearable=clearable, help=help)


def _channel(key, label, clearable=False, help="", fmt=None, tab="Roles & channels"):
    async def apply(cog, guild, member, channel):
        return await cog._apply_channel(guild, member, key, channel)
    return Spec(key, label, tab, "channel", get=_cfg(key), apply=apply,
                fmt=fmt or _channel_fmt, clearable=clearable, help=help)


def _modal(key, label, tab, getter, apply_name, fields, fmt, help=""):
    async def apply(cog, guild, member, values):
        return await getattr(cog, apply_name)(guild, member, values)
    return Spec(key, label, tab, "modal", get=getter, apply=apply, fields=fields, fmt=fmt, help=help, button="Change…")


async def _get_access(cog, guild):
    return "dry" if await cog.config.guild(guild).dry_run() else "live"


async def _get_inactivity(cog, guild):
    conf = cog.config.guild(guild)
    return {"warn": await conf.warn_days(), "remove": await conf.remove_days()}


async def _get_per_day(cog, guild):
    conf = cog.config.guild(guild)
    return {"low": await conf.invite_min(), "high": await conf.invite_max()}


async def _get_timing(cog, guild):
    conf = cog.config.guild(guild)
    return {"min": await conf.drop_min_minutes(), "max": await conf.drop_max_minutes(),
            "life": await conf.drop_life_minutes(), "slots": await conf.drop_max_claims()}


async def _get_next(cog, guild):
    cfg = await cog.config.guild(guild).all()
    state = cfg["drop_state"]
    nxt = state.get("next_ts")
    mins = max(0, int(round((nxt - cog._clock()) / 60))) if cfg["drop_enabled"] and nxt else None
    return {"minutes": mins, "live": bool(state.get("message_id"))}


def _fmt_next(value, guild):
    if value is None:
        return "off"
    live = " (a drop is live now)" if value.get("live") else ""
    if value.get("minutes") is None:
        return "nothing scheduled" + live
    return f"in about {value['minutes']} min" + live


def _fmt_timing(v, guild):
    slots = v["slots"] if v["slots"] else "unlimited"
    return f"every {v['min']}-{v['max']} min, stays {v['life']} min, {slots} slot(s)"


async def _get_interests(cog, guild):
    cfg = await cog.config.guild(guild).all()
    out = []
    for key, interest in cog._interests(cfg).items():
        emoji = interest.emoji + " " if engine.valid_emoji(interest.emoji) else ""
        channels = ", ".join(f"<#{c}>" for c in interest.channel_ids)
        role = f" · role <@&{interest.role_id}>" if interest.role_id else ""
        hidden = " · HIDDEN" if cog._interest_hidden(interest, cfg) else ""
        out.append((key, f"{emoji}{interest.name}", f"{channels}{role}{hidden}"))
    return out


async def _interest_get(cog, guild, item_id):
    cfg = await cog.config.guild(guild).all()
    interests = cog._interests(cfg)
    interest = interests.get(item_id)
    if interest is None:
        raise PanelError("That interest no longer exists.")
    return interest.to_dict()


async def _interest_save(cog, guild, member, item_id, values, picked):
    return await cog._apply_interest_save(guild, member, item_id, values, picked)


async def _interest_remove(cog, guild, member, item_id):
    return await cog._apply_interest_remove(guild, member, item_id)


async def _hide_preview_apply(cog, guild, member, _value):
    return await cog._apply_hide_preview(guild, member)


async def _hide_apply(cog, guild, member, action):
    return await cog._apply_hide(guild, member, action)


async def _hidden_get(cog, guild):
    return len(await cog.config.guild(guild).hidden_channels())


async def _toggle_invites(cog, guild, member, value):
    return await cog._apply_invites_enabled(guild, member, value)


async def _apply_mode(cog, guild, member, value):
    return await cog._apply_access_mode(guild, member, value)


async def _apply_sweep_mode(cog, guild, member, value):
    return await cog._apply_dry_run(guild, member, value)


async def _drops_enabled(cog, guild, member, value):
    return await cog._apply_drops_enabled(guild, member, value)


async def _drop_now(cog, guild, member, _value):
    return await cog._apply_drop_now(guild, member)


async def _invite_now(cog, guild, member, _value):
    return await cog._apply_invite_now(guild, member)


async def _invite_stop(cog, guild, member, _value):
    return await cog._apply_invite_stop(guild, member)


TAB_INTERESTS = "Interests & feeds"


async def _get_feeds(cog, guild):
    return await cog._feed_items(guild)


async def _feed_get(cog, guild, item_id):
    return await cog._feed_record(guild, item_id)


async def _feed_save(cog, guild, member, item_id, values, picked):
    return await cog._apply_feed_save(guild, member, item_id, values, picked)


async def _feed_remove(cog, guild, member, item_id):
    return await cog._apply_feed_remove(guild, member, item_id)


async def _feed_pause(cog, guild, member, item_id):
    return await cog._apply_feed_pause(guild, member, item_id)


async def _feed_approval(cog, guild, member, item_id):
    return await cog._apply_feed_approval(guild, member, item_id)


async def _category_apply(cog, guild, member, value):
    return await cog._apply_interest_category(guild, member, value)


def _category_fmt(value, guild):
    if not value:
        return "not set"
    category = guild.get_channel(value)
    return category.name if category is not None else f"MISSING (id {value})"


def build_specs():
    interest_fields = (
        Field("name", "Name (also the button text)", value=lambda r: r.get("name", ""), max_length=80,
              placeholder="Feet"),
        Field("emoji", "Button emoji (optional)", value=lambda r: r.get("emoji", ""), required=False, max_length=64,
              placeholder="\U0001F9B6"),
        Field("new_channel", "Create a new channel named (optional)", required=False, max_length=90,
              placeholder="feet-pics (age-restricted, hidden until members join)"),
        Field("join_message", "Message shown to them when they join", value=lambda r: r.get("join_message", ""),
              style="long", required=False, max_length=2000,
              placeholder="Leave empty for the default: You're in <name>."),
    )
    feed_fields = (
        Field("reddit", "Subreddit (leave blank for a Telegram topic)", new_only=True, required=False, max_length=60,
              placeholder="feet"),
        Field("telegram", "Telegram topic id or title (blank for a subreddit)", new_only=True, required=False, max_length=100),
        Field("new_channel", "Create a new channel named (optional)", new_only=True, required=False, max_length=90),
        Field("interest", "Add the channel to this interest (optional)", new_only=True, required=False, max_length=80,
              placeholder="Feet"),
    )
    return [
        # ---------------------------------------------------------------- roles & channels
        _role("rabbit_role_id", "Rabbit Hole role", help="The single role that means 'in the Rabbit Hole'."),
        _role("adult_role_id", "Adult Chat role", help="Prerequisite: members need this to be admitted or to stay."),
        _role("verified_role_id", "Age Verified role", clearable=True,
              help="Members with it are invited first. Clear it to go by level only."),
        _channel("log_channel_id", "Log channel", help="Where grants, revokes, sweeps and review prompts are posted."),
        _channel("clue_channel_id", "Clue channel", help="Where `.afterdark post` puts the rabbit button by default."),
        # ---------------------------------------------------------------- access & sweep
        Spec("sweep_mode", "Hourly sweep", "Access & sweep", "choice", get=_get_access, apply=_apply_sweep_mode,
             choices=(("Dry run: only report", "dry"), ("Live: remove access from members who fail", "live")),
             confirm="Go live? The hourly sweep will start removing access from members who fail the checks. It aborts at the circuit-breaker limit.",
             confirm_if=lambda v: v == "live",
             help="Starts as a dry run. Check `.afterdark sweep` output before going live."),
        _modal("min_level", "Minimum level to be admitted", "Access & sweep", _cfg("min_level"), "_apply_min_level",
               (Field("level", "LevelUp level", value=lambda v: str(v), max_length=4),), lambda v, g: f"level {v}",
               help="Only needed to get in, never to stay."),
        _modal("sweep_max", "Sweep circuit breaker", "Access & sweep", _cfg("sweep_max"), "_apply_sweep_max",
               (Field("count", "Most members a live sweep may act on", value=lambda v: str(v), max_length=5),),
               lambda v, g: f"{v} members",
               help="A live sweep that would act on more members than this aborts."),
        _modal("inactivity", "Inactivity (interest channels)", "Access & sweep", _get_inactivity, "_apply_inactivity",
               (Field("warn", "Warn after (days)", value=lambda v: f"{v['warn']:g}", max_length=6),
                Field("remove", "Remove after (days)", value=lambda v: f"{v['remove']:g}", max_length=6)),
               lambda v, g: f"warn {v['warn']:g}d, remove {v['remove']:g}d"),
        # ---------------------------------------------------------------- invitations
        Spec("invites", "Daily invitations", "Invitations", "toggle", get=_cfg("invites_enabled"), apply=_toggle_invites,
             help="Once a day a review prompt appears in the log channel. Nothing is sent until you approve."),
        _modal("per_day", "Invitations per day", "Invitations", _get_per_day, "_apply_invites_per_day",
               (Field("low", "At least", value=lambda v: str(v["low"]), max_length=3),
                Field("high", "At most", value=lambda v: str(v["high"]), max_length=3)),
               lambda v, g: f"{v['low']}-{v['high']}"),
        _modal("snooze", "'Not now' snooze", "Invitations", _cfg("invite_snooze_days"), "_apply_invite_snooze",
               (Field("days", "Days to skip a member after 'Not now'", value=lambda v: str(v), max_length=3),),
               lambda v, g: f"{v} day(s)"),
        Spec("invite_now", "Start a review round now", "Invitations", "action", apply=_invite_now, button="Start round",
             help="Ignores the once-a-day limit. The first candidate appears in the log channel."),
        Spec("invite_stop", "Stop today's invitations", "Invitations", "action", apply=_invite_stop, button="Stop for today",
             confirm="Stop today's invitations and close any waiting prompt?"),
        # ---------------------------------------------------------------- drops
        Spec("drops", "Random rabbit drops", "Drops", "toggle", get=_cfg("drop_enabled"), apply=_drops_enabled,
             confirm="Turning drops on deletes the permanent rabbit message and starts the schedule. Continue?",
             help="Replaces the permanent rabbit message with one that appears now and then and vanishes."),
        _channel("drop_channel_id", "Drop channel", clearable=True, tab="Drops",
                 fmt=lambda v, g: _channel_fmt(v, g) if v else "the clue channel", help="Empty means the clue channel."),
        _modal("drop_timing", "Timing", "Drops", _get_timing, "_apply_drop_timing",
               (Field("min", "Shortest gap (minutes)", value=lambda v: str(v["min"]), max_length=5),
                Field("max", "Longest gap (minutes)", value=lambda v: str(v["max"]), max_length=5),
                Field("life", "How long a drop stays (minutes)", value=lambda v: str(v["life"]), max_length=5),
                Field("slots", "People let in per drop (0 = unlimited)", value=lambda v: str(v["slots"]), max_length=3)),
               _fmt_timing),
        _modal("drop_next", "Next drop", "Drops", _get_next, "_apply_drop_next",
               (Field("minutes", "Next drop in (minutes)", value=lambda v: str(v.get("minutes") or ""), max_length=5),),
               _fmt_next, help="Set exactly when the next drop happens. Later gaps stay random."),
        Spec("drop_now", "Drop one right now", "Drops", "action", apply=_drop_now, button="Drop now",
             help="A test drop. It still disappears after its lifetime."),
        # ---------------------------------------------------------------- interests and feeds
        Spec("interests", "Interests", TAB_INTERESTS, "collection", get=_get_interests, item_get=_interest_get,
             save=_interest_save, remove=_interest_remove, fields=interest_fields,
             pick=Pick("channel", "Also open these existing channels (skip if you named a new one)", 0, 25,
                       defaults=lambda r: [r["channel_id"]] + list(r.get("extra_channel_ids") or []) if r else []),
             help="Members join with a button and get access to the interest's channels one by one (no role). "
                  "After saving, re-post the member panel with `.afterdark panel #channel` so the buttons match. "
                  "Remove also hides its channels from everyone except you and the bot."),
        Spec("feeds", "Feeds", TAB_INTERESTS, "collection", get=_get_feeds, item_get=_feed_get,
             save=_feed_save, remove=_feed_remove, fields=feed_fields,
             pick=Pick("channel", "Also post into these existing channels (age-restricted)", 0, 25,
                       defaults=lambda r: list(r.get("channel_ids") or []) if r else []),
             item_actions=(ItemAction("Pause / resume", _feed_pause), ItemAction("Approval: manual / auto", _feed_approval)),
             help="Reddit subreddits and Telegram topics that post into a channel. Add one with a subreddit or a topic, "
                  "and either a new channel or existing ones; name an interest to add the channel to it. "
                  "Edit moves a feed to other channels; Remove stops it and leaves the channel."),
        Spec("hide_preview", "Preview: hide the \U0001F916 channels", TAB_INTERESTS, "action", apply=_hide_preview_apply,
             button="Preview (changes nothing)",
             help="Step 1. Looks only inside the NSFW category and lists the text channels whose name starts with \U0001F916, "
                  "flags the ones that are NOT a feed or interest channel, and shows which buttons and feeds would go. "
                  "Nothing changes."),
        Spec("hide_channels", "Hide or unhide the \U0001F916 channels\u2026", TAB_INTERESTS, "choice", get=_hidden_get,
             apply=_hide_apply, confirm="Hide the channels from the last preview? Everyone except you and the bots loses "
                                        "access until you unhide.",
             confirm_if=lambda v: v == "hide",
             choices=(("Hide the channels from the last preview", "hide"), ("Unhide everything I hid", "unhide")),
             fmt=lambda v, g: f"{v} hidden" if v else "nothing hidden",
             help="Step 2. Hide pauses their feeds and removes their buttons; members keep their access underneath, "
                  "so Unhide restores everything. Other bots keep access so they can still post."),
        Spec("interest_category", "Category for new channels", TAB_INTERESTS, "channel", get=_cfg("interest_category_id"),
             apply=_category_apply, fmt=_category_fmt, channel_types=("category",), clearable=True,
             help="Where channels created from the forms above go. Needed only if you create channels here."),
    ]
