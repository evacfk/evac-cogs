"""Discord-facing rendering for Acrophobia: embeds, buttons and the staff mod panel."""
from __future__ import annotations

import discord

from . import acro_engine as ae
from .constants import ACRO_MAX_ENTRIES, ACRO_MAX_ENTRY_LEN, ACRO_MIN_ENTRIES, ACRO_VOTE_SECONDS

COLOR_SUBMIT = 0xE67E22
COLOR_VOTE = 0x5865F2
COLOR_DONE = 0x95A5A6
COLOR_CANCEL = 0xBDC3C7
COLOR_MOD = 0x2C2F33
PREFIX = "verdict:acro"


def cid(rid: str, action: str) -> str:
    return f"{PREFIX}:{rid}:{action}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(ts: float) -> str:
    return f"<t:{int(ts)}:R>"


def _letters_line(cur: dict) -> str:
    return "  ".join(f"**{c}**" for c in cur["letters"])


def submit_embed(cur: dict) -> discord.Embed:
    n = len(cur["letters"])
    theme = f"\nTheme: **{cur['theme']}**" if cur.get("theme") else ""
    e = discord.Embed(title=f"\N{INPUT SYMBOL FOR LATIN CAPITAL LETTERS} Acrophobia #{cur['seq']}",
                      description=f"# {_letters_line(cur)}{theme}", color=discord.Color(COLOR_SUBMIT))
    e.add_field(name="How to play", value=(
        f"Tap **Submit** and write {n} words, one per letter, **in order**. Make it funny.\n"
        f"Edit until <t:{int(cur['submit_ends_ts'])}:t>, then vote until "
        f"<t:{int(cur['submit_ends_ts'] + ACRO_VOTE_SECONDS)}:t>. Entries stay anonymous."), inline=False)
    return e


def submit_closed_embed(cur: dict, n_entries: int) -> discord.Embed:
    theme = f" · {cur['theme']}" if cur.get("theme") else ""
    return discord.Embed(title=f"\N{INPUT SYMBOL FOR LATIN CAPITAL LETTERS} Acrophobia #{cur['seq']}: submissions closed",
                         description=f"{' '.join(cur['letters'])}{theme}\n{n_entries} entries are in. Voting is the next message.",
                         color=discord.Color(COLOR_DONE))


def cancelled_embed(cur: dict, n_entries: int) -> discord.Embed:
    return discord.Embed(
        title=f"\N{INPUT SYMBOL FOR LATIN CAPITAL LETTERS} Acrophobia #{cur['seq']}: not this time",
        description=(f"Only {n_entries} {'entry' if n_entries == 1 else 'entries'} came in and it takes {ACRO_MIN_ENTRIES} "
                     "to make a vote. Nobody loses anything. A new round opens tomorrow at 10am."),
        color=discord.Color(COLOR_CANCEL))


def _entry_lines(cur: dict) -> list[str]:
    removed = set(cur.get("removed") or [])
    lines = []
    for i, uid in enumerate(cur["order"], 1):
        if uid in removed:
            lines.append(f"**{i}.** ~~removed by a mod~~")
        else:
            lines.append(f"**{i}.** {ae.escape(cur['entries'][uid])}")
    return lines


def vote_embed(cur: dict) -> discord.Embed:
    theme = f"\nTheme: **{cur['theme']}**" if cur.get("theme") else ""
    desc = f"{' '.join(cur['letters'])}{theme}\n\n" + "\n".join(_entry_lines(cur))
    e = discord.Embed(title=f"\N{BALLOT BOX WITH BALLOT} Acrophobia #{cur['seq']}: vote!",
                      description=_clip(desc, 4000), color=discord.Color(COLOR_VOTE))
    e.add_field(name="Vote", value=(f"Tap the number of your favourite. You can change your vote until voting closes; "
                                    f"you can't vote for your own. Voting closes <t:{int(cur['vote_ends_ts'])}:t> ({_when(cur['vote_ends_ts'])})."), inline=False)
    return e


def results_embed(cur: dict, counts: dict, win: list, *, prize_line: str, removed_n: int, votes_n: int) -> discord.Embed:
    order = ae.ranked_entries(counts, cur["order"])
    number = {uid: i for i, uid in enumerate(cur["order"], 1)}
    lines = []
    for uid in order:
        c = counts[uid]
        crown = " \N{TROPHY}" if uid in win else ""
        lines.append(f"**#{number[uid]}** {ae.escape(cur['entries'][uid])}{crown}\n"
                     f"\N{BOX DRAWINGS LIGHT UP AND RIGHT} <@{uid}> · {c} vote{'s' if c != 1 else ''}")
    head = f"{' '.join(cur['letters'])}" + (f" · {cur['theme']}" if cur.get("theme") else "")
    e = discord.Embed(title=f"\N{INPUT SYMBOL FOR LATIN CAPITAL LETTERS} Acrophobia #{cur['seq']}: results",
                      description=_clip(head + "\n\n" + "\n\n".join(lines), 4000), color=discord.Color(COLOR_DONE))
    foot = [f"{len(counts)} entries", f"{votes_n} votes"]
    if removed_n:
        foot.append(f"{removed_n} removed by a mod")
    e.set_footer(text=" · ".join(foot))
    if prize_line:
        e.add_field(name="\N{TROPHY} Winner" + ("s" if len(win) > 1 else ""), value=_clip(prize_line, 1024), inline=False)
    return e


def closed_vote_embed(cur: dict) -> discord.Embed:
    return discord.Embed(title=f"\N{BALLOT BOX WITH BALLOT} Acrophobia #{cur['seq']}: voting closed",
                         description=_clip("\n".join(_entry_lines(cur)), 4000), color=discord.Color(COLOR_DONE))


# ---------------------------------------------------------------- buttons


def _mod_button(rid: str, row: int) -> discord.ui.Button:
    return discord.ui.Button(label="Mod", emoji="\N{SHIELD}", style=discord.ButtonStyle.secondary,
                             custom_id=cid(rid, "mod"), row=row)


def submit_view(rid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Submit your acronym", style=discord.ButtonStyle.success,
                                    custom_id=cid(rid, "submit"), row=0))
    view.add_item(_mod_button(rid, 0))
    return view


def vote_view(cur: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    removed = set(cur.get("removed") or [])
    for i, uid in enumerate(cur["order"][:ACRO_MAX_ENTRIES]):
        view.add_item(discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary,
                                        custom_id=cid(cur["rid"], f"v{i}"), row=i // 5, disabled=uid in removed))
    view.add_item(_mod_button(cur["rid"], 4))
    return view


# ---------------------------------------------------------------- mod panel


def mod_embed(*, status: str, stats: str, note: str = "") -> discord.Embed:
    e = discord.Embed(title="\N{SHIELD} Acrophobia: mod panel", description=status, color=discord.Color(COLOR_MOD))
    e.add_field(name="Stats", value=_clip(stats, 1024), inline=False)
    if note:
        e.add_field(name="Last action", value=_clip(note, 1024), inline=False)
    return e


def mod_view(rid: str, *, enabled: bool, active: bool, removable: list) -> discord.ui.View:
    """`removable`: [(entry number, text)] for the live entries of a round that is being voted on."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Start a round now", style=discord.ButtonStyle.success,
                                    custom_id=cid(rid, "m_start"), disabled=(active or not enabled), row=0))
    view.add_item(discord.ui.Button(label="Cancel this round", style=discord.ButtonStyle.danger,
                                    custom_id=cid(rid, "m_cancel"), disabled=not active, row=0))
    view.add_item(discord.ui.Button(label="Edit themes", style=discord.ButtonStyle.secondary,
                                    custom_id=cid(rid, "m_themes"), row=0))
    view.add_item(discord.ui.Button(label="Refresh", style=discord.ButtonStyle.secondary,
                                    custom_id=cid(rid, "m_refresh"), row=0))
    if removable:
        options = [discord.SelectOption(label=_clip(f"#{n} {text}", 100), value=str(n)) for n, text in removable[:25]]
        view.add_item(discord.ui.Select(custom_id=cid(rid, "m_rm"), placeholder="Remove an entry from the vote",
                                        options=options, row=1))
    return view


def entry_modal(rid: str, cur: dict, existing: str = "") -> discord.ui.Modal:
    """Handled in Verdict.on_interaction (custom id verdict:acro:<rid>:entry), so it keeps working after a restart."""
    n = len(cur["letters"])
    modal = discord.ui.Modal(title=f"Acrophobia: {' '.join(cur['letters'])}"[:45], timeout=180,
                             custom_id=cid(rid, "entry"))
    modal.add_item(discord.ui.TextInput(
        label=f"{n} words, one per letter, in order"[:45], custom_id="text", default=existing or None,
        placeholder=f"e.g. {' '.join(c.lower() + '…' for c in cur['letters'])}"[:100],
        max_length=ACRO_MAX_ENTRY_LEN, required=True))
    return modal


def themes_modal(rid: str, current: list) -> discord.ui.Modal:
    modal = discord.ui.Modal(title="Acrophobia themes", timeout=300, custom_id=cid(rid, "themes"))
    modal.add_item(discord.ui.TextInput(
        label="One theme per line (blank = no themes)", custom_id="themes", style=discord.TextStyle.paragraph,
        default="\n".join(current)[:3900] or None, required=False, max_length=4000))
    return modal
