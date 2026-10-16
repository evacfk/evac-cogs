"""Discord-facing text for contest: the announcement and rules, status, queue cards, results."""
from __future__ import annotations

import discord

from . import engine
from .constants import CONTEST_NAME, MAX_PHOTOS, MIN_VOTE_LEVEL, REJECT_REASONS


def reason_text(key: str) -> str:
    return dict(REJECT_REASONS).get(key, key)


def prize_lines(g: dict) -> str:
    shop = g.get("shop_item") or "a shop item"
    return (
        f"\N{TROPHY} **1st place** (People's Choice and Mods' Choice): {g['coins_gold']:,} wondercoins, {shop} and the "
        f"**Golden Pumpkin 2026** role\n"
        f"\N{SECOND PLACE MEDAL} **2nd place** (each award): {g['coins_second']:,} wondercoins\n"
        f"\N{THIRD PLACE MEDAL} **3rd place** (each award): {g['coins_third']:,} wondercoins\n"
        f"\N{JACK-O-LANTERN} **Everyone who enters**: {g['coins_participant']:,} wondercoins and the **Bronze Pumpkin 2026** role"
    )


def rules_text(g: dict) -> str:
    s = {k: g[k] for k in engine.SCHEDULE_KEYS}
    f = engine.fmt_pt
    days = len(engine.voting_days(s))
    return (
        f"**How to enter**\n"
        f"1. Press **Enter the contest** below. I will DM you.\n"
        f"2. Send me {MAX_PHOTOS} photos at most (1 or 2) of your costume. Your Discord **username and the word "
        f"\"Wonderland\"** must be written on paper or on yourself and clearly visible in at least one photo. "
        f"Entries without it are rejected.\n"
        f"3. Press Submit. Mods review every entry privately, then it is posted as an anonymous **Costume #N**. "
        f"Nobody but the mods sees your photos before {f(g['open_ts'])}.\n"
        f"4. Photos must be safe for work and of you in your own costume. One entry per person; "
        f"you can send a new one while yours is still waiting for review.\n\n"
        f"**Voting**\n"
        f"From {f(g['open_ts'])} to {f(g['voting_close_ts'])} every member who is level {MIN_VOTE_LEVEL} or higher "
        f"gets **1 vote a day** ({days} in all). You can't vote for yourself or for the same costume twice, "
        f"and you can change today's vote until midnight Pacific. Vote counts stay hidden until the results.\n\n"
        f"**Awards**\n"
        f"People's Choice (decided by your votes) and Mods' Choice (picked by the mods): 1st, 2nd and 3rd in each. "
        f"One prize per person, the best one you earn.\n{prize_lines(g)}\n\n"
        f"**Dates**\n"
        f"Entries close {f(g['entries_close_ts'])} \N{BULLET} Results {f(g['reveal_ts'])} \N{BULLET} "
        f"The channel and every photo are deleted {f(g['purge_ts'])}."
    )


def announcement_embed(g: dict) -> discord.Embed:
    e = discord.Embed(title=f"\N{JACK-O-LANTERN} {CONTEST_NAME}: Halloween Costume Contest",
                      description=rules_text(g), color=discord.Color.orange())
    e.set_footer(text="Entries are private until voting opens. Questions? Ask the mods.")
    return e


def status_embed(g: dict, *, phase: str, approved: int, today_new: int, now: float) -> discord.Embed:
    s = {k: g[k] for k in engine.SCHEDULE_KEYS}
    f = engine.fmt_pt
    if phase == "entries":
        head = (f"Entries are open until {f(s['entries_close_ts'])}. This channel opens to everyone on {f(s['open_ts'])}. "
                f"Until then only the mods can see it.")
    elif phase == "voting":
        today = engine.pt_date(now)
        days = engine.voting_days(s)
        n = days.index(today) + 1 if today in days else len(days)
        head = (f"**Voting is open: day {n} of {len(days)}.** Press **Vote** under the costume you like best today. "
                f"One vote a day, never your own, never the same costume twice. Counts are hidden until the results.")
        if now < s["entries_close_ts"]:
            head += f"\nNew costumes are still being added until {f(s['entries_close_ts'])}."
    elif phase == "picks":
        head = f"Voting is closed. Results are announced {f(s['reveal_ts'])}."
    else:
        head = "The contest is over. Thank you, everyone!"
    e = discord.Embed(title=f"\N{JACK-O-LANTERN} {CONTEST_NAME}", description=head, color=discord.Color.orange())
    line = f"{approved} costume{'s' if approved != 1 else ''} in the contest"
    if today_new:
        line += f" \N{BULLET} {today_new} new today"
    e.set_footer(text=line)
    return e


def queue_embed(entry: dict, *, member_name: str, status: str = "", is_mod: bool = False, resubmitted: bool = False) -> discord.Embed:
    title = f"Entry from {member_name}" + (" \N{BULLET} MOD ENTRANT" if is_mod else "")
    desc = (f"<@{entry['user_id']}>" + (f"\nTitle: **{entry['title']}**" if entry.get("title") else "")
            + ("\nThis replaces an earlier submission." if resubmitted else "")
            + "\n\nCheck: username **and** \"Wonderland\" written in at least one photo, a real costume, and safe for work."
            + (f"\n\n{status}" if status else ""))
    color = discord.Color.green() if status.startswith("\N{WHITE HEAVY CHECK MARK}") else (
        discord.Color.red() if status.startswith("\N{CROSS MARK}") else discord.Color.orange())
    return discord.Embed(title=title, description=desc, color=color)


def entry_caption(number: int, title: str) -> str:
    return f"**Costume #{number}**" + (f" \N{EM DASH} {title}" if title else "")


def results_lines(pc_places: dict[int, int], mc_places: dict[int, int], owners: dict[int, int], urls: dict[int, str],
                  *, mention: bool) -> str:
    def block(name, places):
        if not places:
            return f"**{name}**\nNo winners this time."
        rows = []
        for number, place in sorted(places.items(), key=lambda kv: (kv[1], kv[0])):
            who = f"<@{owners[number]}>" if mention else f"<@{owners[number]}>"
            link = f" ([photos]({urls[number]}))" if urls.get(number) else ""
            rows.append(f"{engine.ordinal(place)}: {who} \N{EM DASH} Costume #{number}{link}")
        return f"**{name}**\n" + "\n".join(rows)
    return block("People's Choice", pc_places) + "\n\n" + block("Mods' Choice", mc_places)


def results_embed(pc_places, mc_places, owners, urls, *, entrants: int, shop_item: str) -> discord.Embed:
    e = discord.Embed(title=f"\N{JACK-O-LANTERN} {CONTEST_NAME}: the results",
                      description=results_lines(pc_places, mc_places, owners, urls, mention=True),
                      color=discord.Color.gold())
    e.set_footer(text=f"{entrants} costumes entered. Everyone who entered got coins and the Bronze Pumpkin 2026 role; "
                      f"winners got their prizes. Thank you for playing!")
    return e
