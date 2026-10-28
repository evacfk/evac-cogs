"""The `.card settings` control panel: the drop channel, drop rules, the request channel, the daily pull odds, and
the test tools.

Specs only; every change goes through the cog's `_apply_*` methods. Managing the pool (`addcard`, `importseries`,
`importpool`, `removecard`...) stays commands, because they take an image or run for a while.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. Pool tools (`.card addcard`, `importseries`, `importpool`, "
         "`removecard`) stay as commands; open any card with `.card find` to change its rarity, swap its art or retire it. "
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
        Spec("odds", "Daily pull odds", "Drops", "modal", get=_odds_get, apply=_apply("_apply_daily_odds"),
             fmt=_odds_fmt, button="Edit…",
             fields=tuple(_odds_field(t) for t in ("common", "rare", "epic", "legendary")),
             help="Relative weights for `.card daily`; they don't need to add up to 100."),
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
