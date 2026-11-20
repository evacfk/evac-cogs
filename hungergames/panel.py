"""The `.hg admin` control panel. Specs only; every change goes through the cog's `_apply_*` / `_act_*` methods."""
from .constants import ITEMS
from .settings_panel import Field, Spec

INTRO = ("Hunger Games controls. Setup and Economy save as soon as you choose. In **Run**, use *What happens next* "
         "first (it changes nothing), then *Advance* or *Cancel*, which ask before acting.")

CHEAP = ("water", "bread", "stew", "serum", "burn_cream")
BIG = ("med_kit", "survival_kit", "weapon", "armor")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


def _fmt_channel(value, guild):
    if not value:
        return "not set"
    ch = guild.get_channel(value)
    return ch.mention if ch is not None else f"MISSING (id {value})"


def _fmt_role(value, guild):
    if not value:
        return "not set"
    role = guild.get_role(value)
    return role.mention if role is not None else f"MISSING (id {value})"


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


async def _prize_get(cog, guild):
    conf = cog.config.guild(guild)
    return {"share": await conf.prize_share(), "floor": await conf.prize_floor()}


def _prices_get(keys):
    async def get(cog, guild):
        prices = await cog.config.guild(guild).prices()
        return {k: prices.get(k, ITEMS[k][2]) for k in keys}
    return get


def _prices_fmt(value, guild):
    return ", ".join(f"{ITEMS[k][1]} {v:,}" for k, v in (value or {}).items())


def _price_fields(keys):
    return tuple(Field(k, ITEMS[k][0][:45], value=(lambda v, k=k: str(v[k])), max_length=9) for k in keys)


async def _status(cog, guild):
    return await cog._status_get(guild)


def build_specs():
    return [
        Spec("enabled", "Games on", "Setup", "toggle", get=_conf("enabled"), apply=_apply("_apply_enabled"),
             confirm="Turn the Games on? The next Reaping starts on schedule (Sunday 10 AM Pacific) with real pings.",
             help="On: a new edition starts every Sunday at 10 AM Pacific."),
        Spec("channel", "Hunger Games channel", "Setup", "channel", get=_conf("channel_id"), apply=_apply("_apply_channel"),
             fmt=_fmt_channel, channel_types=("text",), help="Where everything posts. Members need Send Messages in Threads denied here."),
        Spec("ping_role", "Ping role", "Setup", "role", get=_conf("ping_role_id"), apply=_apply("_apply_ping_role"),
             fmt=_fmt_role, clearable=True, help="Opt-in role pinged for the Reaping, broadcasts and the Crowning."),
        Spec("victor_role", "Victor role", "Setup", "role", get=_conf("victor_role_id"), apply=_apply("_apply_victor_role"),
             fmt=_fmt_role, clearable=True, help="Given to every Victor, permanently."),
        Spec("teaser", "#cuddle teasers", "Setup", "channel", get=_conf("teaser_channel_id"), apply=_apply("_apply_teaser"),
             fmt=_fmt_channel, channel_types=("text",), clearable=True,
             help="Optional one-liners when the Reaping opens and when a Victor is crowned. Clear to turn off."),
        Spec("test_channel", "Test channel", "Setup", "channel", get=_conf("test_channel_id"),
             apply=_apply("_apply_test_channel"), fmt=_fmt_channel, channel_types=("text",), clearable=True,
             help="An owner-only channel for test runs."),
        Spec("prize", "Victor's prize", "Economy", "modal", get=_prize_get, apply=_apply("_apply_prize"),
             fmt=lambda v, g: f"{v['share']:.0%} of sponsorship spend, at least {v['floor']:,}" if v else "?",
             button="Edit…", help="Everything else that sponsors spend is burned.",
             fields=(Field("share", "Percent of spend paid to the Victor", value=lambda v: f"{v['share'] * 100:g}", max_length=5),
                     Field("floor", "Minimum prize (minted if spend is low)", value=lambda v: str(v["floor"]), max_length=9))),
        Spec("prices_small", "Supply prices", "Economy", "modal", get=_prices_get(CHEAP), apply=_apply("_apply_prices"),
             fmt=_prices_fmt, button="Edit…", fields=_price_fields(CHEAP),
             help="Tuesday prices. They rise each day: Wed x1.15, Thu x1.3, Fri x1.4, Sat x1.5."),
        Spec("prices_big", "Med Kit and gear prices", "Economy", "modal", get=_prices_get(BIG), apply=_apply("_apply_prices"),
             fmt=_prices_fmt, button="Edit…", fields=_price_fields(BIG)),
        Spec("preview", "What happens next", "Run", "action", get=_status, apply=_apply("_act_preview"),
             fmt=lambda v, g: v or "", button="Show", help="Changes nothing."),
        Spec("advance", "Advance to the next step", "Run", "action", apply=_apply("_act_advance"), button="Advance…",
             confirm="Run the next step of the Games right now instead of at its scheduled time?"),
        Spec("cancel", "Cancel this edition", "Run", "action", apply=_apply("_act_cancel"), button="Cancel edition…",
             confirm="Cancel the running edition? Sponsors are refunded. This can't be undone."),
        Spec("test", "Start a test run", "Run", "action", apply=_apply("_act_test"), button="Start test…",
             confirm="Start a fast test run in the test channel with fake tributes and you? No coins move, nobody is pinged."),
        Spec("report", "Edition report", "Run", "action", apply=_apply("_act_report"), button="Show",
             help="Usage counts: volunteers, training, sponsors, coins spent and burned."),
    ]
