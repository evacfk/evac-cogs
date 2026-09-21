"""Shared formatting for "N people got paid" results.

Any game where more than one person can get rewarded from the same spawn
(pet's multi-claim, boss's multi-attacker fight, lootdrop's party drop) hits
the same readability problem: listing every person's amount on its own line
is spammy once more than a couple of people are involved, and most of the
time several of them landed on the exact same amount anyway (pet pays the
same roll to everyone who reacted; a lot of boss hits cluster around the
same damage/reward roll). Group by identical amount first, so "A, B and C
received 200 coins each" replaces three separate lines, and only genuinely
different amounts get their own line.
"""
from typing import Sequence, Tuple

import discord
from redbot.core.utils.chat_formatting import humanize_list

_MENTION_LIMIT = 12  # per amount-group, before collapsing into "+N more"


def _mentions(members: Sequence[discord.abc.User], limit: int = _MENTION_LIMIT) -> str:
    names = [m.mention for m in members[:limit]]
    extra = len(members) - limit
    text = humanize_list(names) if names else ""
    if extra > 0:
        text += f" (+{extra} more)"
    return text


def format_payout_lines(paid: Sequence[Tuple[discord.abc.User, int]], currency: str, verb: str = "received") -> list:
    """paid: [(member, amount), ...], one entry per person paid (already
    settled -- amounts are actual/post-taper). Returns display lines, one per
    distinct amount, sorted highest amount first, largest group first among
    ties. Members with the exact same amount are combined onto one line."""
    groups: dict = {}
    for member, amount in paid:
        groups.setdefault(amount, []).append(member)

    lines = []
    for amount in sorted(groups, reverse=True):
        members = groups[amount]
        who = _mentions(members)
        each = " each" if len(members) > 1 else ""
        lines.append(f"{who} {verb} {amount:,} {currency}{each}")
    return lines
