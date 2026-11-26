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
        Spec("usage", "Usage by day", "Usage", "collection", get=_usage, item_get=_usage_item, can_add=False,
             help="Opened = unique members. Payday users = members who claimed payday; the share is how many opened quests."),
    ]
