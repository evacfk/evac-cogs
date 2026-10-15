"""The `.card settings` control panel: the drop channel, drop rules, the daily claim quota and the daily pull odds.

Specs only; every change goes through the cog's `_apply_*` methods. Managing the pool (`addcard`, `importpool`,
`removecard`...) and testing (`testmode`, `testdrop`) stay commands.
"""
from .settings_panel import Field, Spec

INTRO = ("Pick a setting. Changes save as soon as you choose. Pool tools (`.card addcard`, `importpool`, `removecard`) "
         "and testing (`.card testmode`, `testdrop`) stay as commands. Members who aren't admins see a read-only summary.")


def _apply(name):
    async def apply(cog, guild, member, value):
        return await getattr(cog, name)(guild, member, value)
    return apply


async def _channel_get(cog, guild):
    return await cog.config.guild(guild).channel_id()


def _channel_fmt(value, guild):
    if not value:
        return "not set"
    channel = guild.get_channel(value)
    return channel.mention if channel is not None else f"MISSING (id {value})"


async def _rules_get(cog, guild):
    conf = cog.config.guild(guild)
    chance = await conf.drop_chance() * 100
    return {"chance": f"{chance:g}", "cooldown": await conf.drop_cooldown_seconds(), "expiry": await conf.drop_expiry_seconds(),
            "wrong": await conf.max_wrong_guesses(), "penalty": await conf.wrong_guess_penalty_seconds()}


def _rules_fmt(v, guild):
    if not v:
        return "?"
    return (f"{v['chance']}% per message · cooldown {v['cooldown']}s · expire {v['expiry']}s · "
            f"{v['wrong'] or 'unlimited'} wrong guesses · lockout {v['penalty']}s")


async def _quota_get(cog, guild):
    return {"quota": await cog.config.guild(guild).claim_quota()}


async def _odds_get(cog, guild):
    return dict(await cog.config.guild(guild).daily_weights())


def _odds_fmt(v, guild):
    if not v:
        return "?"
    total = sum(v.values()) or 1
    return ", ".join(f"{t} {v[t] / total * 100:g}%" for t in ("common", "rare", "epic", "legendary") if t in v)


def _odds_field(tier):
    return Field(tier, f"{tier.title()} weight", value=lambda v, t=tier: f"{v.get(t, 0):g}", max_length=8)


def build_specs():
    return [
        Spec("channel", "Drop channel", "Drops", "channel", get=_channel_get, apply=_apply("_apply_channel"),
             fmt=_channel_fmt, channel_types=("text",), help="Where cards drop. Needed before `.card testdrop`."),
        Spec("rules", "Drop rules", "Drops", "modal", get=_rules_get, apply=_apply("_apply_drop_rules"), fmt=_rules_fmt,
             button="Edit…",
             fields=(Field("chance", "Chance per qualifying message, % (0-100)", value=lambda v: v["chance"], max_length=8),
                     Field("cooldown", "Seconds between drops (0 = none)", value=lambda v: str(v["cooldown"]), max_length=7),
                     Field("expiry", "Seconds a drop stays claimable (30+)", value=lambda v: str(v["expiry"]), max_length=7),
                     Field("wrong", "Wrong guesses per drop (0 = unlimited)", value=lambda v: str(v["wrong"]), max_length=4),
                     Field("penalty", "Lockout after using them up, seconds", value=lambda v: str(v["penalty"]), max_length=7)),
             help="Takes effect on the next drop."),
        Spec("quota", "Daily claim quota", "Claims & pulls", "modal", get=_quota_get, apply=_apply("_apply_claim_quota"),
             fmt=lambda v, g: (f"{v['quota']} a day" if v["quota"] else "unlimited") if v else "?", button="Edit…",
             fields=(Field("quota", "Claims per member per day (0 = unlimited)", value=lambda v: str(v["quota"]), max_length=4),),
             help="Resets at midnight Pacific."),
        Spec("odds", "Daily pull odds", "Claims & pulls", "modal", get=_odds_get, apply=_apply("_apply_daily_odds"),
             fmt=_odds_fmt, button="Edit…",
             fields=tuple(_odds_field(t) for t in ("common", "rare", "epic", "legendary")),
             help="Relative weights for `.card daily`; they don't need to add up to 100."),
    ]
