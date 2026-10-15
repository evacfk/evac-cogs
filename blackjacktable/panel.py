"""The `.wonderjackset` control panel: on/off, bet limits, shoe size and count checking.

Specs only; every change goes through the cog's `_apply_*` methods.
"""
from .constants import DEFAULT_CHANNEL_ID, MAX_SEATS, MIN_SEATS
from .settings_panel import Field, Spec

INTRO = (f"The table runs in <#{DEFAULT_CHANNEL_ID}> and its threads, {MIN_SEATS}-{MAX_SEATS} seats. "
         "`.wonderjackset version` confirms the build.")


def _conf(key):
    async def get(cog, guild):
        return await getattr(cog.config.guild(guild), key)()
    return get


async def _bets_get(cog, guild):
    data = await cog.config.guild(guild).all()
    return {"min_bet": data["min_bet"], "max_bet": data["max_bet"]}


async def _shoe_get(cog, guild):
    data = await cog.config.guild(guild).all()
    return {"deck_count": data["deck_count"], "penetration_pct": data["penetration_pct"]}


async def _enabled_apply(cog, guild, member, on):
    return await cog._apply_enabled(guild, member, on)


async def _count_apply(cog, guild, member, on):
    return await cog._apply_show_count(guild, member, on)


async def _bets_apply(cog, guild, member, values):
    return await cog._apply_bets(guild, member, values)


async def _shoe_apply(cog, guild, member, values):
    return await cog._apply_shoe(guild, member, values)


def build_specs():
    return [
        Spec("enabled", "Blackjack table", "Table", "toggle", get=_conf("enabled"), apply=_enabled_apply,
             help="Off only blocks new lobbies; a table already in progress finishes normally."),
        Spec("bets", "Bet limits", "Table", "modal", get=_bets_get, apply=_bets_apply,
             fmt=lambda v, g: f"{v['min_bet']:,} to {v['max_bet']:,}" if v else "?", button="Edit bets…",
             fields=(Field("min_bet", "Minimum bet (at least 1)", value=lambda v: str(v["min_bet"]), max_length=10),
                     Field("max_bet", "Maximum bet", value=lambda v: str(v["max_bet"]), max_length=10))),
        Spec("shoe", "Shoe", "Table", "modal", get=_shoe_get, apply=_shoe_apply,
             fmt=lambda v, g: f"{v['deck_count']} deck(s), reshuffle at {v['penetration_pct']}% dealt" if v else "?",
             button="Edit shoe…",
             fields=(Field("deck_count", "Decks in the shoe (1-8)", value=lambda v: str(v["deck_count"]), max_length=2),
                     Field("penetration_pct", "Reshuffle after this % is dealt (10-95)", value=lambda v: str(v["penetration_pct"]), max_length=3)),
             help="Fewer decks = an easier count to learn. Takes effect the next time the shoe reshuffles."),
        Spec("show_count", "Count checking", "Table", "toggle", get=_conf("show_count"), apply=_count_apply,
             help="Lets anyone run `.wonderjack count` during a table to check their own manual count."),
    ]
