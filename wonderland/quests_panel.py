"""`.wld quests`: the mods' panel for daily quests. Switch, rewards, quest pool, channels and usage counts."""
from __future__ import annotations

from . import quests as Q
from .settings_panel import Field, ItemAction, PanelError, Spec

INTRO = ("Daily quests: three a day per active member, opened from the Daily Quests button on the #wondercasino hub. "
         "Each quest pays a third of the member's own payday x the multiplier; a full set pays one payday.")
ON_WARNING = ("Switch daily quests on? This shows the Daily Quests button on the #wondercasino hub and starts "
              "paying coins. Only do this once the coin ledger is live in `.wld economy`.")


async def _conf(cog, guild):
    return await cog._q_conf(guild)


def _get(name):
    async def get(cog, guild):
        return (await cog._q_conf(guild))[name]
    return get


async def _apply_enabled(cog, guild, member, on):
    await cog._q_set(guild, enabled=bool(on))
    state = "on: the Daily Quests button is live" if on else "off: the button is hidden and nothing is paid"
    return f"Daily quests are {state}." + await cog._q_refresh_hub(guild)


async def _apply_multiplier(cog, guild, member, values):
    try:
        value = round(float(str(values.get("multiplier", "")).strip()), 2)
    except ValueError:
        raise PanelError("Type a number, like 1 or 0.75.")
    if not 0 <= value <= Q.MAX_MULTIPLIER:
        raise PanelError(f"Pick a multiplier from 0 to {Q.MAX_MULTIPLIER:g}.")
    await cog._q_set(guild, multiplier=value)
    return f"Multiplier set to x{value:g}. A full set now pays {value:g} payday{'s' if value != 1 else ''}."


def _fmt_multiplier(value, guild):
    return f"x{float(value):g} payday per full set (each quest pays a third)"


async def _estimate(cog, guild, member, _value):
    return await cog._q_estimate(guild)


async def _apply_chat(cog, guild, member, channel):
    channel_id = getattr(channel, "id", channel) or None
    if channel_id:
        picked = guild.get_channel(channel_id)
        if picked is not None and not picked.permissions_for(guild.default_role).view_channel:
            raise PanelError("Quests only point at channels everyone can see. Pick a public channel.")
    await cog._q_set(guild, chat_channel_id=channel_id)
    cog._q_chat_cache.pop(guild.id, None)
    return "Chat quest channel saved." if channel_id else "Chat quest channel cleared: it uses #cuddle by name."


async def _apply_commands(cog, guild, member, channel):
    await cog._q_set(guild, commands_channel_id=getattr(channel, "id", channel) or None)
    return "Commands channel saved."


def _fmt_chat(value, guild):
    if not value:
        return "#cuddle (found by name)"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


def _fmt_channel(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


# ---- XP, XP weekend, streak prizes, weekly prize, reroll costs, set pack, heist, post channels

def _int(values, name, low, high, what):
    try:
        value = int(str(values.get(name, "")).replace(",", "").strip())
    except ValueError:
        raise PanelError(f"{what}: type a whole number.")
    if not low <= value <= high:
        raise PanelError(f"{what}: pick a number from {low:,} to {high:,}.")
    return value


def _num(values, name, low, high, what):
    try:
        value = round(float(str(values.get(name, "")).strip()), 2)
    except ValueError:
        raise PanelError(f"{what}: type a number.")
    if not low <= value <= high:
        raise PanelError(f"{what}: pick a number from {low:g} to {high:g}.")
    return value


async def _apply_xp(cog, guild, member, values):
    xp = _int(values, "xp", 0, Q.MAX_XP, "XP per quest")
    await cog._q_set(guild, xp_per_quest=xp)
    return f"Each quest now gives {xp:,} XP (LevelUp; it never counts toward the weekly leaderboard)."


async def _apply_boost(cog, guild, member, values):
    start, end = str(values.get("start", "")).strip(), str(values.get("end", "")).strip()
    mult = _num(values, "multiplier", 1, 10, "Multiplier")
    if not start and not end:
        await cog._q_set(guild, xp_boost={"start": "", "end": "", "multiplier": mult})
        return "XP weekend cleared."
    for text in (start, end):
        if not Q.valid_day(text):
            raise PanelError("Dates look like 2026-12-25 (year-month-day, Pacific).")
    if end < start:
        raise PanelError("The end date is before the start date.")
    await cog._q_set(guild, xp_boost={"start": start, "end": end, "multiplier": mult})
    return f"Quest XP x{mult:g} from {start} to {end} (inclusive). The panel shows a banner while it runs."


async def _boost_end(cog, guild, member, _value):
    conf = await cog._q_conf(guild)
    await cog._q_set(guild, xp_boost={"start": "", "end": "", "multiplier": conf["xp_boost"]["multiplier"]})
    return "XP weekend ended."


def _fmt_boost(value, guild):
    if not value.get("start"):
        return "none scheduled"
    return f"x{float(value['multiplier']):g} from {value['start']} to {value['end']}"


async def _apply_weekly(cog, guild, member, values):
    days = _int(values, "days", 1, 7, "Days")
    coins = _num(values, "coins", 0, 5, "Paydays")
    xp = _int(values, "xp", 0, Q.MAX_XP, "XP")
    pack = str(values.get("pack", "")).strip().lower() in ("y", "yes", "true", "1")
    await cog._q_set(guild, weekly={"days": days, "coins": coins, "xp": xp, "pack": pack})
    return f"Weekly prize: quests on {days} of 7 days earns {coins:g} payday, {xp:,} XP" + (" and a free pack." if pack else ".")


def _fmt_weekly(value, guild):
    return (f"{value['days']}/7 days: {float(value['coins']):g} payday, {int(value['xp']):,} XP" +
            (", free pack" if value["pack"] else ""))


async def _milestones(cog, guild):
    conf = await cog._q_conf(guild)
    rows = []
    for days in Q.MILESTONE_DAYS:
        p = Q.prize_for(conf["milestones"], days)
        rows.append((str(days), f"{Q.badge_text(days)} · {days} days",
                     f"{float(p['coins']):g} payday · {int(p['xp']):,} XP" + (" · free pack" if p["pack"] else "")))
    return rows


async def _milestone_item(cog, guild, key):
    p = Q.prize_for((await cog._q_conf(guild))["milestones"], int(key))
    return {"coins": f"{float(p['coins']):g}", "xp": str(p["xp"]), "pack": "yes" if p["pack"] else "no"}


async def _milestone_save(cog, guild, member, key, values, _picked):
    if not key or int(key) not in Q.MILESTONE_DAYS:
        raise PanelError("Pick a milestone first.")
    prize = {"coins": _num(values, "coins", 0, 5, "Paydays"), "xp": _int(values, "xp", 0, Q.MAX_XP, "XP"),
             "pack": str(values.get("pack", "")).strip().lower() in ("y", "yes", "true", "1")}
    saved = dict((await cog._q_conf(guild))["milestones"])
    saved[str(key)] = prize
    await cog._q_set(guild, milestones=saved)
    return f"{key}-day prize saved."


async def _apply_rerolls(cog, guild, member, values):
    raw = [p for p in str(values.get("costs", "")).replace(",", " ").split() if p]
    try:
        costs = [int(p) for p in raw]
    except ValueError:
        raise PanelError("Type whole numbers separated by spaces, like 1000 5000 10000.")
    if not 1 <= len(costs) <= 8 or any(c < 0 or c > 10_000_000 for c in costs):
        raise PanelError("Give 1 to 8 prices, each from 0 to 10,000,000.")
    await cog._q_set(guild, reroll_costs=costs)
    return "Reroll prices: " + ", ".join(f"{Q.reroll_cost(costs, n):,}" for n in range(6)) + "... (doubles after the last)."


def _fmt_rerolls(value, guild):
    return ", ".join(f"{Q.reroll_cost(value, n):,}" for n in range(5)) + "..."


async def _apply_set_pack(cog, guild, member, value):
    await cog._q_set(guild, set_pack=value)
    return {"off": "A full set no longer earns a pack.", "half": "A full set earns a pack at half price.",
            "free": "A full set earns a free pack."}[value]


async def _apply_heist(cog, guild, member, values):
    command = str(values.get("command", "")).strip().lstrip(".").lower()
    if command and not cog._exists(command):
        raise PanelError(f"`.{command}` isn't a command on this bot. Use the full name, like `heist play`.")
    await cog._q_set(guild, heist_command=command)
    return f"Heist quest runs on `.{command}`." if command else "Heist quest off (no command set)."


def _fmt_heist(value, guild):
    return f"`.{value}`" if value else "not set (quest off)"


def _post_channel_spec(key):
    async def get(cog, guild):
        return ((await cog._q_conf(guild))["post_channels"] or {}).get(key)

    async def apply(cog, guild, member, channel):
        channel_id = getattr(channel, "id", channel) or None
        if channel_id:
            picked = guild.get_channel(channel_id)
            if picked is not None and not picked.permissions_for(guild.default_role).view_channel:
                raise PanelError("Quests only point at channels everyone can see.")
        saved = dict((await cog._q_conf(guild))["post_channels"] or {})
        if channel_id:
            saved[key] = channel_id
        else:
            saved.pop(key, None)
        await cog._q_set(guild, post_channels=saved)
        return "Saved." if channel_id else f"Cleared: it uses #{Q.BY_KEY[key].channel} by name."

    def fmt(value, guild):
        if not value:
            return f"#{Q.BY_KEY[key].channel} (found by name)"
        channel = guild.get_channel(value)
        return channel.mention if channel is not None else f"MISSING (id {value})"
    return get, apply, fmt


# ---- quest pool (one record per quest key)

async def _pool(cog, guild):
    conf = await cog._q_conf(guild)
    rates = Q.completion(conf["stats"], Q.today())
    rows = []
    for quest in Q.QUESTS:
        on = "off" if quest.key in conf["disabled"] else "on"
        target = Q.target_of(quest.key, conf["targets"])
        offered, done = rates.get(quest.key, (0, 0))
        detail = f"{on}" + (f" · target {target}" if quest.target > 1 else "") + f" · 21d: {Q.rate_text(offered, done)}"
        rows.append((quest.key, Q.title_of(quest.key, conf["targets"]).replace("{#", "#").replace("}", ""), detail))
    return rows


async def _pool_item(cog, guild, key):
    conf = await cog._q_conf(guild)
    return {"target": str(Q.target_of(key, conf["targets"]))}


async def _pool_save(cog, guild, member, key, values, _picked):
    if key not in Q.BY_KEY:
        raise PanelError("Pick a quest first.")
    if Q.BY_KEY[key].target <= 1:
        raise PanelError("This quest is done in one go, so it has no target to change.")
    try:
        target = int(str(values.get("target", "")).strip())
    except ValueError:
        raise PanelError("Type a whole number.")
    if not 1 <= target <= 100:
        raise PanelError("Pick a target from 1 to 100.")
    conf = await cog._q_conf(guild)
    targets = dict(conf["targets"])
    targets[key] = target
    await cog._q_set(guild, targets=targets)
    return f"{Q.title_of(key, targets)}: target saved. Quests already dealt today use it straight away."


async def _pool_toggle(cog, guild, member, key):
    conf = await cog._q_conf(guild)
    disabled = set(conf["disabled"])
    if key in disabled:
        disabled.discard(key)
        word = "back in the pool"
    else:
        disabled.add(key)
        word = "out of the pool from tomorrow's deal (already dealt quests still count today)"
    await cog._q_set(guild, disabled=sorted(disabled))
    return f"{Q.BY_KEY[key].title.replace('{n}', 'N')}: {word}."


async def _why(cog, guild, member, key):
    status, channel, reason = await cog._q_status(key, member, await cog._q_conf(guild))
    where = f" in {channel.mention}" if channel is not None else ""
    return f"For you right now: {status}{where}" + (f" ({reason})" if reason else "") + "."


# ---- usage (read-only rows, newest day first)

async def _usage(cog, guild):
    conf = await cog._q_conf(guild)
    stats, day = conf["stats"], Q.today()
    days = Q.window(stats, day, Q.REVIEW_DAYS)
    rows = []
    week = Q.window(stats, day, 7)
    if days:
        paid7 = sum(int(stats[d].get("paid", 0)) for d in week)
        sets7 = sum(int(stats[d].get("sets", 0)) for d in week)
        rows.append(("summary", "Last 7 days", f"{sets7:,} full sets · {paid7:,} coins paid (faucet)"))
    for d in reversed(days):
        rows.append((d, d, Q.day_line(stats[d])))
    return rows


async def _usage_item(cog, guild, item_id):
    return {}


def build_specs():
    return [
        Spec("enabled", "Daily quests", "Switch", "toggle", get=_get("enabled"), apply=_apply_enabled,
             confirm=ON_WARNING, confirm_if=lambda value: bool(value),
             help="Off hides the hub button and stops all tracking and payouts."),
        Spec("multiplier", "Reward multiplier", "Rewards", "modal", get=_get("multiplier"), apply=_apply_multiplier,
             fmt=_fmt_multiplier, button="Change…",
             fields=(Field("multiplier", "Paydays per full set (0 to 3)", value=lambda v: f"{float(v):g}",
                           max_length=5, placeholder="1"),),
             help="1 = a full set pays one payday. Payday itself is never changed here."),
        Spec("xp", "XP per quest", "Rewards", "modal", get=_get("xp_per_quest"), apply=_apply_xp,
             fmt=lambda v, g: f"{int(v):,} XP each (LevelUp, not the weekly leaderboard)", button="Change…",
             fields=(Field("xp", "XP per quest", value=lambda v: str(v), max_length=6, placeholder="50"),)),
        Spec("boost", "Holiday XP weekend", "Rewards", "modal", get=_get("xp_boost"), apply=_apply_boost,
             fmt=_fmt_boost, button="Schedule…",
             fields=(Field("start", "Start (YYYY-MM-DD, Pacific)", value=lambda v: v.get("start", ""), max_length=10),
                     Field("end", "End, inclusive (YYYY-MM-DD)", value=lambda v: v.get("end", ""), max_length=10),
                     Field("multiplier", "Multiplier (1 to 10)", value=lambda v: f"{float(v['multiplier']):g}", max_length=5)),
             help="Multiplies quest XP only. Leave both dates empty to clear."),
        Spec("boost_end", "End XP weekend now", "Rewards", "action", apply=_boost_end, button="End now"),
        Spec("weekly", "Weekly prize", "Rewards", "modal", get=_get("weekly"), apply=_apply_weekly, fmt=_fmt_weekly,
             button="Change…",
             fields=(Field("days", "Days with a quest (1 to 7)", value=lambda v: str(v["days"]), max_length=1),
                     Field("coins", "Paydays (0 to 5)", value=lambda v: f"{float(v['coins']):g}", max_length=4),
                     Field("xp", "XP", value=lambda v: str(v["xp"]), max_length=6),
                     Field("pack", "Free pack? (yes/no)", value=lambda v: "yes" if v["pack"] else "no", max_length=3)),
             help="Paid once per week (Mon-Sun Pacific)."),
        Spec("milestones", "Streak prizes", "Rewards", "collection", get=_milestones, item_get=_milestone_item,
             save=_milestone_save, can_add=False,
             fields=(Field("coins", "Paydays (0 to 5)", value=lambda r: r.get("coins", ""), max_length=4),
                     Field("xp", "XP", value=lambda r: r.get("xp", ""), max_length=6),
                     Field("pack", "Free pack? (yes/no)", value=lambda r: r.get("pack", ""), max_length=3)),
             help="A streak day = one quest finished. Prizes pay once per streak."),
        Spec("reroll", "Reroll prices", "Rewards", "modal", get=_get("reroll_costs"), apply=_apply_rerolls,
             fmt=_fmt_rerolls, button="Change…",
             fields=(Field("costs", "Prices in order (space-separated)",
                           value=lambda v: " ".join(str(c) for c in v), max_length=80, placeholder="1000 5000 10000"),),
             help="Unlimited rerolls a day; after the last price it doubles each time. A coin sink."),
        Spec("set_pack", "Full-set pack", "Rewards", "choice", get=_get("set_pack"), apply=_apply_set_pack,
             choices=[("Off", "off"), ("Half price", "half"), ("Free", "free")],
             help="Pack offered once a day to members who finish all quests. Free packs mint coins via card sell-back."),
        Spec("estimate", "Faucet estimate", "Rewards", "action", apply=_estimate, button="Estimate",
             help="100 full sets a day, priced from every active member's real payday."),
        Spec("pool", "Quest pool", "Quest pool", "collection", get=_pool, item_get=_pool_item, save=_pool_save,
             fields=(Field("target", "Target (count quests only)", value=lambda r: r.get("target", ""), max_length=3),),
             item_actions=(ItemAction("Turn on / off", _pool_toggle), ItemAction("Can I do it now?", _why)),
             can_add=False, help="21-day completion per quest. Under 20% after the review: take it out."),
        Spec("chat_channel", "Chat quest channel", "Channels", "channel", get=_get("chat_channel_id"),
             apply=_apply_chat, fmt=_fmt_chat, channel_types=("text",), clearable=True,
             help="Where the send-messages quest counts. Clear = #cuddle by name."),
        Spec("commands_channel", "Commands channel", "Channels", "channel", get=_get("commands_channel_id"),
             apply=_apply_commands, fmt=_fmt_channel, channel_types=("text",),
             help="Where `.fish` and `.stars` quests send members. They must be able to post there."),
        Spec("selfie_channel", "Selfie quest channel", "Channels", "channel", get=_post_channel_spec("selfie")[0],
             apply=_post_channel_spec("selfie")[1], fmt=_post_channel_spec("selfie")[2], channel_types=("text",),
             clearable=True, help="A photo posted here counts. Clear = #selfie by name."),
        Spec("counting_channel", "Counting quest channel", "Channels", "channel", get=_post_channel_spec("counting")[0],
             apply=_post_channel_spec("counting")[1], fmt=_post_channel_spec("counting")[2], channel_types=("text",),
             clearable=True, help="A number posted here counts (deleted wrong counts don't). Clear = #counting by name."),
        Spec("heist", "Heist command", "Channels", "modal", get=_get("heist_command"), apply=_apply_heist,
             fmt=_fmt_heist, button="Set…",
             fields=(Field("command", "Command to count (no dot)", value=lambda v: v or "", max_length=40),),
             help="The quest stays off until a real command is set. Find it with `.findcog heist`."),
        Spec("usage", "Usage by day", "Usage", "collection", get=_usage, item_get=_usage_item, can_add=False,
             help="Opened = unique members. Payday users = members who claimed payday; the share is how many opened quests."),
    ]
