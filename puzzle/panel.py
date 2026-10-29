"""The `.puzzle` control panel: every setting that used to be a `set*` / `livestatus` command.

Specs only; every change goes through the cog's `_apply_*` methods. Pool management (`addimage`,
`images`, `next`, `retire`, ...) and the round controls (`start`, `stop`, `skip`) stay commands.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a section, then a setting. Changes save as soon as you choose. Pool and round controls "
         "(`addimage`, `images`, `next`, `retire`, `start`, `stop`) stay as commands; `.puzzle settings` prints a summary.")


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


def _value_get(key, field):
    async def get(cog, guild):
        return {field: await getattr(cog.config.guild(guild), key)()}
    return get


def _single(key, label, tab, conf_key, field, box, apply_name, fmt, help="", max_length=8, default=None):
    """A one-box modal over one config key."""
    def value(v):
        current = v[field]
        return str(current if current is not None else (default if default is not None else ""))
    return Spec(key, label, tab, "modal", get=_value_get(conf_key, field), apply=_apply(apply_name),
                fmt=lambda v, g: fmt(v[field]) if v else "?", button="Edit…",
                fields=(Field(field, box, value=value, max_length=max_length),), help=help)


async def _interval_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"min_hours": await conf.interval_min_hours(), "max_hours": await conf.interval_max_hours()}


async def _pieces_get(cog, guild):
    from .puzzle import DEFAULT_PIECE_COUNT
    return {"count": await cog.config.guild(guild).next_piece_count() or DEFAULT_PIECE_COUNT}


def build_specs():
    return [
        # -- Round -------------------------------------------------------------------------------
        Spec("channel", "Puzzle channel", "Round", "channel", get=_conf("channel_id"), apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Where pieces are posted and claimed."),
        Spec("interval", "Posting interval", "Round", "modal", get=_interval_get, apply=_apply("_apply_interval"),
             fmt=lambda v, g: f"random {v['min_hours']:g} to {v['max_hours']:g} hours" if v else "?", button="Edit…",
             fields=(Field("min_hours", "Shortest wait, hours", value=lambda v: f"{v['min_hours']:g}", max_length=8),
                     Field("max_hours", "Longest wait, hours", value=lambda v: f"{v['max_hours']:g}", max_length=8)),
             help="Each piece picks a fresh random wait in this range, so the schedule can't be camped."),
        _single("winners", "Winners to end a puzzle", "Round", "winners_count", "count",
                "People who must collect every piece", "_apply_winners", lambda c: str(c), max_length=4,
                help="The round ends once this many people have every distinct piece."),
        Spec("reward_role", "Winner role", "Round", "role", get=_conf("reward_role_id"), apply=_apply("_apply_reward_role"),
             fmt=_role_fmt, clearable=True, help="Given to the latest winners and taken from the previous ones."),
        _single("emoji", "Claim emoji", "Round", "claim_emoji", "emoji", "The emoji members react with", "_apply_emoji",
                lambda c: c, max_length=64),
        Spec("pieces", "Default piece count", "Round", "modal", get=_pieces_get, apply=_apply("_apply_default_pieces"),
             fmt=lambda v, g: f"{v['count']} pieces" if v else "?", button="Edit…",
             fields=(Field("count", "Pieces for images added without a size (2 to 25)", value=lambda v: str(v["count"]), max_length=2),),
             help="Only affects images added afterwards. `.puzzle addimage 4x4` overrides it for one image."),
        # -- Sharing & status ----------------------------------------------------------------------
        Spec("shared_mode", "Shared-credit mode", "Sharing & status", "toggle", get=_conf("shared_mode"),
             apply=_apply("_apply_shared_mode"),
             help="On: everyone who reacts within the window gets credit. Off: first reaction wins. Affects new pieces only."),
        _single("shared_window", "Shared window", "Sharing & status", "shared_window_minutes", "minutes",
                "Minutes a piece stays open (up to 60)", "_apply_shared_window", lambda m: f"{m:g} minute(s)", max_length=6,
                help="Only matters while shared mode is on."),
        Spec("announce", "Announcement channel", "Sharing & status", "channel", get=_conf("announce_channel_id"),
             apply=_apply("_apply_announce_channel"), fmt=_channel_fmt, channel_types=("text",), clearable=True,
             help="Completions are also posted here, for when the puzzle channel is busy."),
        Spec("live_status", "Live status message", "Sharing & status", "channel", get=_conf("live_status_channel_id"),
             apply=_apply("_apply_live_status"), fmt=lambda v, g: f"posted in {_channel_fmt(v, g)}" if v else "off",
             channel_types=("text",), clearable=True,
             help="Pick a channel to post a standings message that updates itself. Clear to stop refreshing it."),
        # -- Pool & rewards --------------------------------------------------------------------------
        _single("low_pool", "Low-pool warning", "Pool & rewards", "low_pool_threshold", "count",
                "Warn at this many unplayed images (0 = off)", "_apply_low_pool",
                lambda c: f"at {c} or fewer" if c else "off", max_length=4,
                help="Won images are retired for good, so this is the heads-up to add more."),
        Spec("alert_channel", "Warning channel", "Pool & rewards", "channel", get=_conf("alert_channel_id"),
             apply=_apply("_apply_alert_channel"), fmt=lambda v, g: _channel_fmt(v, g) if v else "the puzzle channel",
             channel_types=("text",), clearable=True, help="Where low-pool warnings go."),
        _single("payout", "Win payout", "Pool & rewards", "win_payout", "amount", "Credits for each winner (0 = off)",
                "_apply_payout", lambda a: f"{a:,}" if a else "off", max_length=12,
                help="Every winner gets the full amount, including in shared mode."),
    ]
