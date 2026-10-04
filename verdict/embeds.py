"""Discord-facing rendering for the Daily Verdict."""
from __future__ import annotations

import discord

from . import engine

COLOR_OPEN = 0x5865F2
COLOR_DONE = 0x95A5A6
COLOR_REVIEW = 0xF1C40F
LETTERS = "ABCD"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def question_embed(cur: dict, *, yesterday: str | None = None, award: str | None = None, post_hour: int = 10) -> discord.Embed:
    e = discord.Embed(title=f"\N{BALLOT BOX WITH BALLOT} Daily Verdict #{cur['seq']}", description=f"**{cur['question']}**",
                      color=discord.Color(COLOR_OPEN))
    e.add_field(name="How it works", value=(
        "1️⃣ Tap **your** answer.\n"
        "2️⃣ Then guess what **most people** will pick.\n"
        f"The result drops with tomorrow's question (~{post_hour % 12 or 12}{'am' if post_hour < 12 else 'pm'} Pacific)."), inline=False)
    if cur.get("submitter_id"):
        e.add_field(name="Question by", value=f"<@{cur['submitter_id']}>", inline=True)
    if yesterday:
        e.add_field(name="\N{CALENDAR} Yesterday", value=_clip(yesterday, 1024), inline=False)
    if award:
        e.add_field(name="\N{CROWN} Mind Reader", value=_clip(award, 1024), inline=False)
    return e


def results_lines(cur: dict, counts: list[int], win: set[int]) -> list[str]:
    total = sum(counts)
    lines = []
    for i, opt in enumerate(cur["options"]):
        share = counts[i] / total if total else 0
        mark = " \N{TROPHY}" if i in win else ""
        lines.append(f"**{LETTERS[i]}.** {opt}{mark}\n`{engine.bar(share)}` {engine.percent(counts[i], total)}% ({counts[i]})")
    return lines


def closed_embed(cur: dict, counts: list[int], win: set[int], extra: list[str]) -> discord.Embed:
    total = sum(counts)
    e = discord.Embed(title=f"\N{BALLOT BOX WITH BALLOT} Daily Verdict #{cur['seq']} — final",
                      description=f"**{cur['question']}**\n\n" + "\n".join(results_lines(cur, counts, win)),
                      color=discord.Color(COLOR_DONE))
    e.set_footer(text=f"{total} voted")
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
