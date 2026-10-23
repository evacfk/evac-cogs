"""The `.secrethunt` control panel: pool, Hall, announcements, puzzles and reward roles.

Specs only; every change goes through the cog's `_apply_*` methods.
"""
from . import engine
from .constants import NO_ROLE_TIERS, POOL_MAX, POOL_MIN, TIER_ROLE_NAMES, TRAIL_MAX_ROOMS
from .puzzles import PUZZLES
from .settings_panel import Field, ItemAction, PanelError, Pick, Spec

INTRO = ("Set up in order: **Hunt** (pool of 2-5 hiding places, Hall, announcements), then **Puzzles** "
         "(set up objects, go live), then turn the hunt on. Players, `reset`, `who`, `preview`, `status` and "
         "`roleicon` stay as commands.")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    found = getattr(guild, "get_channel_or_thread", guild.get_channel)(value)
    return found.mention if found is not None else f"MISSING (id {value})"


def _pool_fmt(value, guild):
    if not value:
        return "not set"
    lookup = getattr(guild, "get_channel_or_thread", guild.get_channel)
    return ", ".join(f"<#{c}>" if lookup(c) is not None else f"MISSING ({c})" for c in value)


def _role_fmt(value, guild):
    if not value:
        return "not set"
    role = guild.get_role(value)
    return role.mention if role is not None else f"MISSING (id {value})"


async def _tier_get(cog, guild, tier):
    return (await cog.config.guild(guild).roles()).get(tier)


def _tier_spec(tier):
    async def get(cog, guild):
        return await _tier_get(cog, guild, tier)

    async def apply(cog, guild, member, role):
        return await cog._apply_tier_role(guild, member, tier, role)
    return Spec(f"role_{tier}", f"{tier.title()} reward role", "Roles", "role", get=get, apply=apply, fmt=_role_fmt,
                clearable=True, help=f"Given to everyone who solves a {tier} puzzle. Easy and Medium never have one.")


async def _pool_apply(cog, guild, member, channels):
    return await cog._apply_pool(guild, member, channels)


async def _pool_text_apply(cog, guild, member, values):
    return await cog._apply_pool_text(guild, member, values)


async def _trail_text_apply(cog, guild, member, values):
    return await cog._apply_trail_text(guild, member, values)


async def _test_channel_apply(cog, guild, member, channel):
    return await cog._apply_test_channel(guild, member, channel)


async def _blocked_apply(cog, guild, member, role):
    return await cog._apply_blocked_role(guild, member, role)


async def _enabled_apply(cog, guild, member, on):
    return await cog._apply_enabled(guild, member, on)


async def _test_apply(cog, guild, member, on):
    return await cog._apply_test_mode(guild, member, on)


async def _hall_apply(cog, guild, member, channel):
    return await cog._apply_hall(guild, member, channel)


async def _announce_apply(cog, guild, member, channel):
    return await cog._apply_announce(guild, member, channel)


async def _refresh_apply(cog, guild, member, _value):
    return await cog._apply_hallrefresh(guild, member)


async def _poem_apply(cog, guild, member, channel):
    if channel is None:
        raise PanelError("Pick a channel.")
    target = getattr(guild, "get_channel_or_thread", guild.get_channel)(channel.id) or channel
    return await cog._apply_poem(guild, member, target)


async def _create_roles_apply(cog, guild, member, _value):
    return await cog._apply_create_tier_roles(guild, member)


async def _none(cog, guild):
    return None


async def _launch_get(cog, guild):
    """Everything the launch form needs: the saved schedules, plus the puzzle and channel to prefill."""
    conf = cog.config.guild(guild)
    launches = await conf.launches()
    pending = [p for p, l in launches.items() if l.get("state") == "pending" and p in PUZZLES]
    pid = pending[0] if pending else (await conf.active())
    item = launches.get(pid) or {}
    channel = item.get("channel") or (await conf.poem_targets()).get(pid)
    return {"launches": launches, "pid": pid, "channel": channel, "at": item.get("at") if item.get("state") == "pending" else None}


def _launch_fmt(value, guild):
    items = [(p, l) for p, l in (value or {}).get("launches", {}).items() if p in PUZZLES]
    if not items:
        return "none scheduled"
    return "; ".join(f"{PUZZLES[p]['title']} <t:{int(l['at'])}:R> ({l['state']})" for p, l in items)


def _launch_when(value):
    from datetime import datetime, timedelta
    from .constants import HOME_TZ
    at = (value or {}).get("at")
    if at:
        return datetime.fromtimestamp(at, HOME_TZ).strftime("%Y-%m-%d %H:%M")
    nxt = datetime.now(HOME_TZ).replace(hour=9, minute=0, second=0, microsecond=0)
    if nxt <= datetime.now(HOME_TZ):
        nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d %H:%M")


async def _launch_apply(cog, guild, member, values):
    return await cog._apply_launch(guild, member, values)


async def _puzzles(cog, guild):
    conf = cog.config.guild(guild)
    published, finishers, objects = await conf.published(), await conf.finishers(), await conf.objects()
    members = await cog.config.all_members(guild)
    pool, trail_rooms = await conf.pool(), await conf.trail()
    out = []
    for pid, puzzle in PUZZLES.items():
        started = sum(1 for d in members.values() if pid in d["puzzles"])
        bits = ["LIVE" if pid in published else "not live"]
        placed = len(objects.get(pid, {}))
        if puzzle.get("kind") == "trail":
            bits.append(f"tea tables {placed}/{len(trail_rooms)}" if trail_rooms else "no trail rooms yet")

            if puzzle.get("wip"):
                bits.append("admins only")
        else:
            bits.append(f"objects {placed}/{len(pool)}" if pool else "no pool yet")
        launch = (await conf.launches()).get(pid) or {}
        if launch.get("state") == "pending":
            bits.append(f"LAUNCHES <t:{int(launch['at'])}:R>")
        if (await conf.test_objects()).get(pid):
            bits.append("REHEARSING in the test channel")
        if pid in published:
            bits.append(f"{started} started, {len(finishers.get(pid, []))} solved")
        out.append((pid, f"{puzzle['title']} ({puzzle['tier']})", " · ".join(bits)))
    return out


def _action(fn_name, *args):
    async def run(cog, guild, member, item_id):
        return await getattr(cog, fn_name)(guild, member, item_id, *args)
    return run


async def _badges(cog, guild):
    roles = await cog.config.guild(guild).puzzle_roles()
    out = []
    for pid, puzzle in PUZZLES.items():
        if puzzle["tier"] in NO_ROLE_TIERS:
            continue
        role = guild.get_role(roles.get(pid) or 0)
        out.append((pid, puzzle["title"], role.mention if role else "no badge role"))
    return out


async def _badge_get(cog, guild, item_id):
    return {"role_id": (await cog.config.guild(guild).puzzle_roles()).get(item_id)}


async def _badge_save(cog, guild, member, item_id, values, picked):
    if not picked:
        raise PanelError("Pick a role.")
    return await cog._apply_puzzle_role(guild, member, item_id, picked[0])


async def _badge_remove(cog, guild, member, item_id):
    return await cog._apply_puzzle_role(guild, member, item_id, None)


def build_specs():
    return [
        # ------------------------------------------------------------------ hunt
        Spec("enabled", "The hunt is on", "Hunt", "toggle", get=_conf("enabled"), apply=_enabled_apply,
             help="Needs a pool and a live puzzle. Turning it off keeps everyone's progress."),
        Spec("test_mode", "Test mode", "Hunt", "toggle", get=_conf("test_mode"), apply=_test_apply,
             confirm="Turning Test mode off posts everything you rehearsed for real: the real objects, and each poem in its "
                     "regular channel. Continue?", confirm_if=lambda v: not v,
             help="Waits become 30 seconds so you can run a whole chain yourself. With a Test channel set, setup and poems "
                  "go there instead of the real rooms until you turn it off."),
        Spec("pool", "Hiding places (pool)", "Hunt", "channel", get=_conf("pool"), apply=_pool_apply, fmt=_pool_fmt,
             multi=POOL_MAX, min_multi=POOL_MIN,
             confirm="Changing the pool moves everyone's hiding place and forgets the posted objects. Do it between puzzles. Continue?",
             help=f"{POOL_MIN} to {POOL_MAX} channels or forum posts. Variant 1 is the first. Can't find a forum post? Use 'Paste hiding places'."),
        Spec("pool_text", "Paste hiding places", "Hunt", "modal", get=_conf("pool"), apply=_pool_text_apply, fmt=lambda v, g: "mentions or IDs",
             fields=(Field("channels", "Channels or forum posts (mentions or IDs)", style="long", max_length=1000,
                           value=lambda v: " ".join(f"<#{c}>" for c in (v or [])), placeholder="#post1 #post2 #post3"),),
             help="Forum posts are threads and may not appear in the dropdown. Paste their links as mentions or IDs."),
        Spec("trail_text", "Trail rooms (Treacle Well)", "Hunt", "modal", get=_conf("trail"), apply=_trail_text_apply,
             fmt=_pool_fmt,
             fields=(Field("rooms", "Forum posts or channels (mentions or IDs)", style="long", max_length=1500,
                           value=lambda v: " ".join(f"<#{c}>" for c in (v or [])), placeholder="#post1 #post2 ... (12 planned)"),),
             help=f"The public rooms that each hold a tea table: 12 planned, at most {TRAIL_MAX_ROOMS}. Paste links as mentions or IDs. "
                  "Changing them moves everyone's sisters, so do it between puzzles."),
        Spec("test_channel", "Test channel (rehearse any puzzle)", "Hunt", "channel", get=_conf("test_channel_id"),
             apply=_test_channel_apply, fmt=_channel_fmt, clearable=True,
             help="A private channel. While Test mode is on, Set up objects and the poem post here instead of the real "
                  "rooms (each object labelled with its room). Turn Test mode off to post them for real."),
        Spec("blocked_role", "Roles that cannot play (Lurker)", "Hunt", "role", get=_conf("blocked_role_id"),
             apply=_blocked_apply, fmt=_role_fmt, clearable=True,
             help="Members with this role are ignored by every secret: no clues, no clicks, no answers. Their progress just waits."),
        Spec("hall", "Hall of Secrets channel", "Hunt", "channel", get=_conf("hall_channel_id"), apply=_hall_apply,
             fmt=_channel_fmt, clearable=True, help="One post per puzzle listing its solvers."),
        Spec("announce", "Solve announcements", "Hunt", "channel", get=_conf("announce_channel_id"), apply=_announce_apply,
             fmt=_channel_fmt, clearable=True, help="A one-line 'X has solved <puzzle>' that never pings. Clear = off."),
        Spec("hall_refresh", "Refresh Hall posts", "Hunt", "action", apply=_refresh_apply, button="Refresh now",
             help="Rewrite every puzzle's Hall post from the saved solver lists."),
        # ------------------------------------------------------------------ puzzles
        Spec("puzzles", "Puzzles", "Puzzles", "collection", get=_puzzles, can_add=False,
             item_actions=(
                 ItemAction("Set up objects", _action("_apply_setup", False), style="primary"),
                 ItemAction("Redo objects", _action("_apply_setup", True),
                            confirm="Post and pin fresh objects in every hiding place? The old ones stay pinned until you unpin them."),
                 ItemAction("Go live", _action("_apply_start"), style="success"),
                 ItemAction("DM me answers", _action("_apply_preview")),
             ), help="Set up the objects, go live, then turn the hunt on under Hunt."),
        Spec("launch", "Schedule a launch", "Puzzles", "modal", get=_launch_get, apply=_launch_apply, fmt=_launch_fmt,
             button="Schedule…",
             fields=(Field("puzzle", "Puzzle id", value=lambda v: (v or {}).get("pid") or "", max_length=40),
                     Field("when", "When (Pacific, YYYY-MM-DD HH:MM). Empty = cancel", value=_launch_when, required=False,
                           max_length=25),
                     Field("channel", "Channel for the poem (mention or ID)", required=False, max_length=60,
                           value=lambda v: f"<#{v['channel']}>" if (v or {}).get("channel") else "")),
             help="At that time the puzzle goes live and its poem is posted and pinned. The hunt must be on, Test mode off and "
                  "the objects set up; I DM you an hour before if something is missing, and again when it has run."),
        Spec("poem", "Post the newest poem in…", "Puzzles", "channel", get=_none, apply=_poem_apply,
             fmt=lambda v, g: "pick a channel to post and pin it there",
             help="Posts the newest live puzzle's public poem and pins it, replacing the previous poem there."),
        # ------------------------------------------------------------------ roles
        *[_tier_spec(tier) for tier in TIER_ROLE_NAMES],
        Spec("create_roles", "Create the missing tier roles", "Roles", "action", apply=_create_roles_apply,
             button="Create roles", help="Makes the default hoisted role for any tier that has none."),
        Spec("badges", "Puzzle badge roles (Hard and up)", "Roles", "collection", get=_badges, item_get=_badge_get,
             save=_badge_save, remove=_badge_remove, can_add=False, pick=Pick("role", "Choose the badge role"),
             item_actions=(ItemAction("Create a role", _action("_apply_create_puzzle_role")),),
             help="A per-puzzle badge for future solvers. Easy and Medium puzzles never have one."),
    ]
