"""The `.card settings` control panel: the drop channel, drop rules, the request channel, the daily pull odds, and
the test tools.

Specs only; every change goes through the cog's `_apply_*` methods. Managing the pool (`addcard`, `importseries`)
stays commands, because they take an image or run for a while.
"""
from .settings_panel import Field, PanelError, Pick, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. Pool tools (`.card addcard`, `importseries`) stay "
         "commands; open any card with `.card find` to change its rarity, swap its art or retire it. "
         "Members who aren't admins see a read-only summary.")


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


async def _channel_get(cog, guild):
    return await cog.config.guild(guild).channel_id()


async def _request_channel_get(cog, guild):
    return await cog.config.guild(guild).request_channel_id()


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


async def _rules_get(cog, guild):
    conf = cog.config.guild(guild)
    chance = await conf.drop_chance() * 100
    return {"chance": f"{chance:g}", "cooldown": await conf.drop_cooldown_seconds(), "window": await conf.draw_window_seconds()}


def _rules_fmt(v, guild):
    if not v:
        return "?"
    return f"{v['chance']}% per message · cooldown {v['cooldown']}s · draw stays open {v['window']}s"


async def _odds_get(cog, guild):
    return dict(await cog.config.guild(guild).daily_weights())


def _odds_fmt(v, guild):
    if not v:
        return "?"
    total = sum(v.values()) or 1
    return ", ".join(f"{t} {v[t] / total * 100:g}%" for t in ("common", "rare", "epic", "legendary") if t in v)


def _odds_field(tier):
    return Field(tier, f"{tier.title()} weight", value=lambda v, t=tier: f"{v.get(t, 0):g}", max_length=8)


async def _packs_get(cog, guild):
    return await cog.config.guild(guild).packs_enabled()


async def _pack_price_get(cog, guild):
    return {"price": await cog.config.guild(guild).pack_price()}


async def _sell_prices_get(cog, guild):
    prices = await cog.config.guild(guild).sell_prices()
    return {t: int(prices.get(t, 0)) for t in ("common", "rare", "epic", "legendary")}


def _sell_prices_fmt(v, guild):
    if not v:
        return "?"
    return " · ".join(f"{t} {v[t]:,}" for t in ("common", "rare", "epic", "legendary"))


async def _upgrade_costs_get(cog, guild):
    costs = await cog.config.guild(guild).upgrade_costs()
    return {f"{finish}_{part}": (costs.get(finish) or {}).get(part, 0)
            for finish in ("holo", "prismatic") for part in ("dupes", "coins")}


def _upgrade_costs_fmt(v, guild):
    if not v:
        return "?"
    return (f"Holo: {v['holo_dupes']} dupe + {v['holo_coins']:,} · "
            f"Prismatic: {v['prismatic_dupes']} dupes + {v['prismatic_coins']:,}")


async def _featured_get(cog, guild):
    return await cog.config.guild(guild).featured_enabled()


async def _featured_fmt_get(cog, guild):
    return await cog.config.guild(guild).featured()


async def _set_roles_get(cog, guild):
    roles = await cog.config.guild(guild).set_roles()
    out = []
    for count, role_id in sorted(roles.items(), key=lambda kv: int(kv[0])):
        role = guild.get_role(int(role_id)) if hasattr(guild, "get_role") else None
        label = f"{count} set{'s' if int(count) != 1 else ''} completed"
        out.append((count, label, role.mention if role is not None else "role missing"))
    return out


async def _set_role_get(cog, guild, item_id):
    roles = await cog.config.guild(guild).set_roles()
    if item_id not in roles:
        raise PanelError("That set role no longer exists.")
    return {"sets": item_id, "role_id": roles[item_id]}


async def _set_role_save(cog, guild, member, item_id, values, picked):
    return await cog._apply_set_role_save(guild, member, item_id, values, picked)


async def _set_role_remove(cog, guild, member, item_id):
    return await cog._apply_set_role_remove(guild, member, item_id)


async def _legendary_role_get(cog, guild):
    return await cog.config.guild(guild).legendary_ping_role_id()


async def _test_mode_get(cog, guild):
    return await cog.config.guild(guild).test_mode()


def build_specs():
    return [
        Spec("channel", "Drop channel", "Drops", "channel", get=_channel_get, apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Where cards drop."),
        Spec("rules", "Drop rules", "Drops", "modal", get=_rules_get, apply=_apply("_apply_drop_rules"), fmt=_rules_fmt,
             button="Edit…",
             fields=(Field("chance", "Chance per qualifying message, % (0-100)", value=lambda v: v["chance"], max_length=8),
                     Field("cooldown", "Seconds between drops (0 = none)", value=lambda v: str(v["cooldown"]), max_length=7),
                     Field("window", "Seconds the draw stays open (30-600)", value=lambda v: str(v["window"]), max_length=4)),
             help="Takes effect on the next drop."),
        Spec("legendrole", "Legendary drop ping", "Drops", "role", get=_legendary_role_get,
             apply=_apply("_apply_legendary_role"), fmt=lambda v, g: f"<@&{v}>" if v else "no ping", clearable=True,
             help="A drop with a legendary in it always gets a big announcement line. Pick a role to also ping it "
                  "(an opt-in role works best); clear it for no ping."),
        Spec("odds", "Daily pull odds", "Drops", "modal", get=_odds_get, apply=_apply("_apply_daily_odds"),
             fmt=_odds_fmt, button="Edit…",
             fields=tuple(_odds_field(t) for t in ("common", "rare", "epic", "legendary")),
             help="Relative weights for `.card daily`; they don't need to add up to 100."),
        Spec("packs", "Daily pack", "Economy", "toggle", get=_packs_get, apply=_apply("_apply_packs_enabled"),
             help="A button on `.card daily` that sells each member one pack a day: 3 cards, one guaranteed rare "
                  "or better, using the daily pull odds. Five packs in a row without an epic make the next one "
                  "guarantee an epic."),
        Spec("packprice", "Pack price", "Economy", "modal", get=_pack_price_get, apply=_apply("_apply_pack_price"),
             fmt=lambda v, g: f"{v['price']:,} wondercoin" if v else "?", button="Edit…",
             fields=(Field("price", "Wondercoin per pack (0 = free)", value=lambda v: str(v["price"]), max_length=9),),
             help="Keep it above what a pack's cards sell back for (about 11,000 with the default prices), "
                  "or members can open packs just to sell the duplicates."),
        Spec("sellprices", "Sell prices", "Economy", "modal", get=_sell_prices_get,
             apply=_apply("_apply_sell_prices"), fmt=_sell_prices_fmt, button="Edit…",
             fields=tuple(Field(t, f"{t.title()} token sells for", value=lambda v, t=t: str(v[t]), max_length=9)
                          for t in ("common", "rare", "epic", "legendary")),
             help="What `.card sell` pays per sell token (a copy past the limit of 2). Applies to tokens members "
                  "already hold too. Keep the pack price well above 3 cards' worth, or opening packs to sell becomes "
                  "free money."),
        Spec("upgrades", "Upgrade costs", "Economy", "modal", get=_upgrade_costs_get,
             apply=_apply("_apply_upgrade_costs"), fmt=_upgrade_costs_fmt, button="Edit…",
             fields=(Field("holo_dupes", "Holo: duplicates needed", value=lambda v: str(v["holo_dupes"]), max_length=2),
                     Field("holo_coins", "Holo: wondercoin", value=lambda v: str(v["holo_coins"]), max_length=9),
                     Field("prismatic_dupes", "Prismatic: duplicates needed",
                           value=lambda v: str(v["prismatic_dupes"]), max_length=2),
                     Field("prismatic_coins", "Prismatic: wondercoin", value=lambda v: str(v["prismatic_coins"]),
                           max_length=9)),
             help="What it takes to upgrade a card from `.card find`: duplicates of that card (its sell tokens, "
                  "then the spare copy) plus wondercoin. Prismatic is the second upgrade, on top of Holo."),
        Spec("featured", "Weekly featured series", "Featured", "toggle", get=_featured_get,
             apply=_apply("_apply_featured_enabled"),
             help="Every Monday (Pacific) a random series is featured and announced in the drop channel; its cards "
                  "are 3x as likely in drops, daily pulls and packs that week. Fully automatic."),
        Spec("featurednow", "Pick a new featured series now", "Featured", "action", get=_featured_fmt_get,
             apply=_apply("_apply_featured_reroll"), button="Re-roll",
             fmt=lambda v, g: f"this week: {v.get('series') or 'none yet'}" if v else "",
             confirm="Replace this week's featured series with a new random one (and announce it)?",
             help="Only if you want a different one this week; otherwise it changes by itself on Monday."),
        Spec("setroles", "Set roles", "Rewards", "collection", get=_set_roles_get, item_get=_set_role_get,
             save=_set_role_save, remove=_set_role_remove,
             fields=(Field("sets", "Sets completed to earn it", value=lambda r: str(r.get("sets", "")),
                           new_default="1", max_length=3),),
             pick=Pick("role", "Choose the role to give", defaults=lambda r: [r["role_id"]] if r.get("role_id") else []),
             help="Cosmetic roles for finishing card sets, e.g. 1 set = Collector, 5 sets = Completionist. Given "
                  "the moment someone reaches the count. The bot needs Manage Roles, and each role must sit below "
                  "the bot's own role."),
        Spec("setrolesync", "Give set roles now", "Rewards", "action", apply=_apply("_apply_set_roles_sync"),
             button="Give roles",
             help="Hands out set roles to everyone who already qualifies (for sets finished before a role existed)."),
        Spec("requests", "Request channel", "Requests", "channel", get=_request_channel_get,
             apply=_apply("_apply_request_channel"), fmt=lambda v, g: _channel_fmt(v, g) if v else "off", channel_types=("text",),
             clearable=True,
             help="Where members' \"Request this\" cards arrive for you to approve. Use a private, admin-only channel. "
                  "Clear it to turn requests off."),
        Spec("testmode", "Test mode", "Testing", "toggle", get=_test_mode_get, apply=_apply("_apply_test_mode"),
             help="While on, ambient drops run the full draw but award nothing."),
        Spec("testdrop", "Post a test drop", "Testing", "action", apply=_apply("_apply_test_drop"), button="Post it",
             help="Posts one test drop in the drop channel now. Press a card, wait for the draw, and check the results."),
    ]
