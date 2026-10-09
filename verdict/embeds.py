"""Discord-facing rendering for the Daily Verdict."""
from __future__ import annotations

import discord

from . import engine

COLOR_OPEN = 0x5865F2
COLOR_DONE = 0x95A5A6
COLOR_REVIEW = 0xF1C40F
LETTERS = "ABCD"
DESCRIPTION_BUDGET = 3300  # characters of voter mentions shared across the options (embed description caps at 4096)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def question_embed(cur: dict, *, yesterday: str | None = None, award: str | None = None, post_hour: int = 10) -> discord.Embed:
    when = f"~{post_hour % 12 or 12}{'am' if post_hour < 12 else 'pm'} Pacific"
    e = discord.Embed(title=f"\N{BALLOT BOX WITH BALLOT} Daily Verdict #{cur['seq']}", description=f"**{cur['question']}**",
                      color=discord.Color(COLOR_OPEN))
    e.add_field(name="How to play (2 taps)", value=(
        "**1.** Tap **your own answer** below. Pick what *you* would choose.\n"
        "**2.** A private pop-up then asks what you think **most people** will pick. Tap that.\n"
        "Both taps are needed to count. Closed the pop-up early? Tap any answer again to finish."), inline=False)
    e.add_field(name="Scoring", value=(
        "\N{BRAIN} Your guess matches the winning answer = a **Mind Reader** point. "
        "The best score each month earns the title.\n"
        "\N{FIRE} Play every day to build a streak."), inline=False)
    e.add_field(name="Results", value=f"This post turns into the results at {when} tomorrow and shows **who picked what**.",
                inline=False)
    if cur.get("submitter_id"):
        e.add_field(name="Question by", value=f"<@{cur['submitter_id']}>", inline=True)
    if yesterday:
        e.add_field(name="\N{CALENDAR} Yesterday", value=_clip(yesterday, 1024), inline=False)
    if award:
        e.add_field(name="\N{CROWN} Mind Reader", value=_clip(award, 1024), inline=False)
    return e


def _mentions(uids: list[str], readers: frozenset | set, budget: int) -> str:
    """Mentions that fit in `budget` characters; a read-the-crowd voter gets a brain. The rest become '+N more'."""
    shown, used = [], 0
    for n, uid in enumerate(uids):
        piece = f"<@{uid}>" + ("\N{BRAIN}" if uid in readers else "")
        if used + len(piece) + 1 > budget:
            shown.append(f"+{len(uids) - n} more")
            break
        shown.append(piece)
        used += len(piece) + 1
    return " ".join(shown)


def voters_by_option(votes: dict, n_options: int) -> list[list[str]]:
    by_opt: list[list[str]] = [[] for _ in range(n_options)]
    for uid in sorted(votes, key=int):
        idx = int(votes[uid])
        if 0 <= idx < n_options:
            by_opt[idx].append(uid)
    return by_opt


def results_lines(cur: dict, counts: list[int], win: set[int], voters: list[list[str]] | None = None,
                  readers: frozenset | set = frozenset()) -> list[str]:
    total = sum(counts)
    budget = DESCRIPTION_BUDGET // max(1, len(cur["options"]))
    lines = []
    for i, opt in enumerate(cur["options"]):
        share = counts[i] / total if total else 0
        mark = " \N{TROPHY}" if i in win else ""
        line = f"**{LETTERS[i]}.** {opt}{mark}\n`{engine.bar(share)}` {engine.percent(counts[i], total)}% ({counts[i]})"
        if voters and voters[i]:
            line += "\n" + _mentions(voters[i], readers, budget)
        lines.append(line)
    return lines


def closed_embed(cur: dict, counts: list[int], win: set[int], extra: list[str], *,
                 votes: dict | None = None, readers: frozenset | set = frozenset()) -> discord.Embed:
    total = sum(counts)
    voters = voters_by_option(votes, len(cur["options"])) if votes else None
    e = discord.Embed(title=f"\N{BALLOT BOX WITH BALLOT} Daily Verdict #{cur['seq']} — final",
                      description=f"**{cur['question']}**\n\n" + "\n\n".join(results_lines(cur, counts, win, voters, readers)),
                      color=discord.Color(COLOR_DONE))
    e.set_footer(text=f"{total} voted · \N{BRAIN} guessed the winning answer · \N{WOLF FACE} lone wolf: picked a rare answer")
    if extra:
        e.add_field(name="Highlights", value=_clip("\n".join(extra), 1024), inline=False)
    return e


def review_embed(data: dict, *, status: str | None = None) -> discord.Embed:
    opts = "\n".join(f"**{LETTERS[i]}.** {o}" for i, o in enumerate(data["options"]))
    e = discord.Embed(title="New Daily Verdict question", description=f"**{data['question']}**\n\n{opts}",
                      color=discord.Color(COLOR_REVIEW))
    e.add_field(name="Submitted by", value=f"<@{data['submitter_id']}>" if data.get("submitter_id") else "staff", inline=True)
    e.set_footer(text=status or "React ✅ to queue it or ❌ to bin it.")
    return e
