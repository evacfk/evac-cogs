"""Discord side of `.wld economy`: reads every wondercoin balance and reports how it is spread out.

Mixed into the Wonderland cog (no separate cog, no extra install). Read-only on the bank: it never
changes a balance. The only thing it stores is one aggregate snapshot per day, so the next run can
show the change.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from . import economy, embeds
from .constants import ECONOMY_TZ

log = logging.getLogger("red.evac-cogs.wonderland.economy")


class EconomyMixin:
    """Needs `self.config` with a guild value `economy` ({"snapshots": [...]}) and `self.bot` from the cog it is mixed into."""

    async def _lurker_role_id(self, guild):
        """The Lurker cog's role id (the same lookup afterdark uses), or None when it can't be read."""
        cog = self.bot.get_cog("Lurker")
        if cog is None:
            return None
        try:
            return await cog.config.guild(guild).lurker_role_id()
        except Exception:
            return None

    async def _eco_report(self, guild: discord.Guild) -> discord.Embed:
        from redbot.core import bank  # imported here: the dev test stub has no bank

        currency = await bank.get_currency_name(guild)
        # Red's own member filter for a global bank is not reliable, so ask for everyone and filter here.
        accounts = await bank.get_leaderboard(positions=None, guild=None if await bank.is_global() else guild)

        lurker_role = await self._lurker_role_id(guild)
        balances: dict = {}
        names: dict = {}
        lurkers: set = set()
        gone_n = gone_total = 0
        for user_id, data in accounts:
            balance = max(0, int(data.get("balance", 0)))
            member = guild.get_member(int(user_id))
            if member is None:
                gone_n += 1
                gone_total += balance
                continue
            if member.bot:
                continue
            balances[member.id] = balance
            names[member.id] = member.display_name
            if lurker_role and any(r.id == lurker_role for r in getattr(member, "roles", ())):
                lurkers.add(member.id)

        stats = economy.summarize(list(balances.values()))
        active = None
        if lurker_role:
            active = {
                "stats": economy.summarize([b for uid, b in balances.items() if uid not in lurkers]),
                "lurker_holders": sum(1 for uid in lurkers if balances[uid] > 0),
                "lurker_total": sum(balances[uid] for uid in lurkers),
            }
        today = datetime.now(ZoneInfo(ECONOMY_TZ)).date().isoformat()
        entry = economy.snapshot(stats, today, active["stats"] if active else None)
        async with self.config.guild(guild).economy() as saved:
            history = saved.get("snapshots", [])
            previous = economy.previous_snapshot(history, today)
            saved["snapshots"] = economy.add_snapshot(history, entry)
        delta = economy.change(previous, entry) if previous else None
        top = [(names[uid], balance) for uid, balance in economy.top_holders(balances)]
        return embeds.economy_embed(currency, stats, top, gone_n, gone_total, delta, active)
