"""Embed builders for hungergames. Rendering only: no Config, no game rules."""
from __future__ import annotations

import time

import discord

from . import engine
from .constants import CHALLENGE_LABEL, CHALLENGES, ITEMS, MAX_TRIBUTES, PHASE_LABEL, STRATEGIES

GOLD = 0xD4A017
RED = 0xB22222
GREEN = 0x2E8B57
GREY = 0x5865F2


def _embed(title, description="", color=GOLD):
    return discord.Embed(title=title, description=description, color=discord.Color(color))


def ts(t: float, style: str = "R") -> str:
    return f"<t:{int(t)}:{style}>"


def roster_lines(ed: dict, *, scores: bool = False, mentions: bool = True) -> list:
    out = []
    hidden = engine.pending_deaths(ed)
    order = engine.ranking(ed["roster"]) if scores else list(ed["roster"])
    for uid in order:
        t = ed["roster"][uid]
        who = t["name"] if t.get("fake") or not mentions else f"<@{uid}>"
        bits = [who]
        if scores:
            bits.append(f"**{t['score']}**")
        if t["origin"] == "volunteer":
            bits.append("🙋 volunteer")
        elif t.get("tag"):
            bits.append("🎲 *Drafted — volunteer to take their place*")
        else:
            bits.append("🎲 drafted")
        if not t["alive"] and uid not in hidden:
            bits.append("💀")
        out.append(" · ".join(bits))
    return out


def reaping_embed(ed: dict, *, closed: bool = False, now: float | None = None) -> "discord.Embed":
    """The Reaping post. A step that has already happened reads "done" (a relative timestamp would keep
    counting up, "31 seconds ago"), and once the roster locks the post says so."""
    now = time.time() if now is None else now
    sched = ed["schedule"]
    seats = MAX_TRIBUTES - len(ed["roster"])
    second = engine.step_ts(sched, "second_reaping")
    lock = engine.step_ts(sched, "lock")
    if closed:
        desc = "The Reaping is closed and the roster is locked. See you in training!\n\n"
    else:
        desc = (
            "The Reaping is open! Press **I Volunteer** to take a seat. When every seat is full you can send "
            "**one** swap request to a drafted tribute instead.\n\n"
            f"🎲 Second Reaping (top-up draw): {'✅ done' if second <= now else ts(second)}\n"
            f"🔒 Roster locks: {ts(lock)}\n"
        )
    desc += f"🏋️ Training: {ts(engine.step_ts(sched, 'training'), 'F')}\n"
    e = _embed(f"🎯 {engine.edition_title(ed)} — The Reaping", desc)
    lines = roster_lines(ed)
    e.add_field(name=f"Tributes ({len(ed['roster'])}/{MAX_TRIBUTES}" + ("" if closed else f", {seats} open") + ")",
                value="\n".join(lines) or "Nobody yet.", inline=False)
    if not closed:
        e.set_footer(text="Drafted and don't want to play? Press Withdraw. Never want to be drafted? Never draft me.")
    return e


def signup_embed(reaping_ts: float) -> "discord.Embed":
    """The pinned sign-up post: what the game is, when it starts, and the two opt buttons under it."""
    desc = (
        "A weekly arena game. Volunteer as a tribute, train, sponsor your favorites with Wondercoin and watch the "
        "live broadcasts. One Victor is crowned every Saturday.\n\n"
        f"🗓️ **Next Reaping:** {ts(reaping_ts, 'F')} ({ts(reaping_ts)})\n"
        "🙋 Sun: the Reaping (volunteer or get drafted)\n"
        "🏋️ Mon: training challenges\n"
        "💰 Tue: scores and sponsorship open\n"
        "📺 Tue-Fri from 7 PM PT: live arena broadcasts (about an hour each)\n"
        "👑 Sat, 7 PM PT: the Finale and the Crowning\n\n"
        "🔔 **Hunger Games pings** gets you a ping for the Reaping, the broadcasts and the Crowning. Press it again to turn them off.\n"
        "🚫 Hana drafts a few active members each week. Press **Never draft me** to opt out of the draft (you can still "
        "volunteer). Press it again to opt back in."
    )
    e = _embed("🎯 The Hunger Games", desc)
    e.set_footer(text="May the odds be ever in your favor! Full guide: .wld")
    return e


def roster_embed(ed: dict, title: str, desc: str = "") -> "discord.Embed":
    e = _embed(title, desc)
    e.add_field(name=f"Tributes ({len(ed['roster'])})", value="\n".join(roster_lines(ed)) or "—", inline=False)
    return e


def training_intro(ed: dict, uid: str) -> str:
    close = engine.step_ts(ed["schedule"], "training_close")
    return (
        f"<@{uid}>, welcome to the Training Center! You have until {ts(close, 'F')} ({ts(close)}).\n"
        "Each challenge is **one attempt**, timed from the moment it starts, so start when you're ready. "
        "Everyone can watch your results here. The Interview is judged by the crowd: answer it early to collect cheers."
    )


def challenge_menu_line() -> str:
    return " · ".join(f"{CHALLENGE_LABEL[k][1]} {CHALLENGE_LABEL[k][0]}" for k in CHALLENGES) + " · 🎤 Interview"


def scores_embed(ed: dict) -> "discord.Embed":
    e = _embed(f"📊 {engine.edition_title(ed)} — Training Scores",
               "The Gamemakers have judged. Sponsorship is now **open**: tap **🪂 Sponsor** to send a parachute.\n"
               "Tributes: tap **📋 My plan** to choose how you play each night.")
    rows = []
    for uid in engine.ranking(ed["roster"]):
        t = ed["roster"][uid]
        st = t["stats"]
        rows.append(f"**{t['score']:>2}** · {t['name']} — "
                    f"🏃{st.get('agility', 0)} 🥊{st.get('strength', 0)} 🧠{st.get('endurance', 0)} "
                    f"📡{st.get('intellect', 0)} 🎤{st.get('charisma', 0)}")
    e.add_field(name="Scores (1-12)", value="\n".join(rows) or "—", inline=False)
    e.set_footer(text="🏃 agility · 🥊 strength · 🧠 endurance · 📡 intellect · 🎤 charisma (crowd cheers)")
    return e


SCENE_COLOR = {
    "bloodbath": 0xB22222, "day": 0xE6B422, "night": 0x2C3E91, "feast": 0xA0522D,
    "arena": 0x8E44AD, "finale": 0xD4A017,
}
LINE_ICON = {"death": "💀", "save": "🪂", "boost": "💪", "gift": "🎁", "find": "🎒", "victor": "👑"}
SPOTLIGHT = ("death", "save", "gift", "victor")   # the kinds whose tribute gets the avatar
CARD_LIMIT = 3600                                  # an embed description holds 4096; start a new card before it


def header_embed(item: dict) -> "discord.Embed":
    return _embed(item["title"], item.get("desc", ""), SCENE_COLOR.get(item.get("scene"), GREY))


def scene_line(item: dict) -> str:
    return f"{LINE_ICON.get(item.get('kind'), '▫️')} {item['text']}"


def scene_embed(head: dict, lines: list, avatar_for=None, next_ts: float | None = None, flavor: str = "") -> "discord.Embed":
    """One scene card: a phase header and the lines posted under it so far. The thumbnail is the avatar of the
    tribute in the most recent big moment (a kill, a rescue, a gift, the Victor). Items from an older queue,
    without a kind or uids, still render, just without icons or avatars."""
    desc = head.get("desc", "")
    body = "\n".join(scene_line(i) for i in lines)
    e = _embed(head["title"], f"*{desc}*\n\n{body}" if desc and body else (f"*{desc}*" if desc else body),
               SCENE_COLOR.get(head.get("scene"), GREY))
    if next_ts is not None:
        e.description = (e.description or "") + f"\n\n⏳ *{flavor or 'The broadcast continues.'}* Next update {ts(next_ts)}"
    if avatar_for is not None:
        for item in reversed(lines):
            if item.get("kind") in SPOTLIGHT and item.get("uids"):
                url = avatar_for(item["uids"][0])
                if url:
                    e.set_thumbnail(url=url)
                break
    return e


def scene_text_len(head: dict, lines: list) -> int:
    return len(head.get("desc", "")) + sum(len(scene_line(i)) + 1 for i in lines)


def fallen_embed(ed: dict, item: dict, avatar_for=None) -> "discord.Embed":
    names = [ed["roster"][u]["name"] for u in item["uids"] if u in ed["roster"]]
    if item.get("night") is None:
        title = "💥 The final cannons"
    else:
        title = f"💥 The fallen — night {item['night']}"
    if not names:
        return _embed(title, "No cannons tonight. The Gamemakers are *not* pleased.", RED)
    alive = len(engine.alive_ids(ed))
    desc = "\n".join(f"💥 {n}" for n in names) + f"\n\n{len(names)} cannon{'s' if len(names) != 1 else ''}. {alive} remain."
    e = _embed(title, desc, RED)
    if avatar_for is not None:
        for u in item["uids"]:
            url = avatar_for(u)
            if url:
                e.set_thumbnail(url=url)
                break
    return e


def crowning_embed(ed: dict, guild, prize: int, currency: str, awards: dict, wins: int | None = None) -> "discord.Embed":
    uid = ed["victor"]
    t = ed["roster"][uid]
    e = _embed(f"🏆 Victor of {engine.edition_title(ed)}", f"## {t['name']}", GOLD)
    member = guild.get_member(int(uid)) if not t.get("fake") and uid.isdigit() else None
    if member is not None and getattr(member, "display_avatar", None) is not None:
        e.set_thumbnail(url=member.display_avatar.url)
    sponsors = engine.sponsors_of(ed, uid)
    e.add_field(name="Training Score", value=str(t["score"]), inline=True)
    e.add_field(name="Kills", value=str(t["kills"]), inline=True)
    e.add_field(name="Prize", value=f"{prize:,} {currency}" if prize else "—", inline=True)
    if wins:
        e.add_field(name="Total wins", value=f"{wins:,}", inline=True)
    e.add_field(name="Sponsors", value=", ".join(f"<@{s}>" for s in sponsors) or "Nobody. A self-made Victor.", inline=False)
    names = {
        "most_kills": "🗡️ Most Kills", "darling": "💝 Capitol's Darling", "top_patron": "💰 Top Patron",
        "fan_favorite": "👏 Fan Favorite",
    }
    rows = []
    for key, label in names.items():
        if key in awards:
            who, n = awards[key]
            name = ed["roster"][who]["name"] if who in ed["roster"] else f"<@{who}>"
            suffix = {"most_kills": "kills", "darling": "parachutes", "top_patron": currency, "fan_favorite": "cheers"}[key]
            rows.append(f"{label}: {name} ({n:,} {suffix})")
    if rows:
        e.add_field(name="Awards", value="\n".join(rows), inline=False)
    return e


def status_embed(ed: dict | None, hall: list, now: float, next_reaping: float | None, shop: bool) -> "discord.Embed":
    if not ed:
        desc = f"Next Reaping: {ts(next_reaping, 'F')} ({ts(next_reaping)})" if next_reaping else "The Games are switched off."
        e = _embed("🏹 The Hunger Games", desc)
        if hall:
            last = hall[-1]
            e.add_field(name="Reigning Victor", value=f"{last['name']} — {last['title']}", inline=False)
        return e
    phase = engine.current_phase(ed)
    e = _embed(f"🏹 {engine.edition_title(ed)}", PHASE_LABEL.get(phase, phase))
    if ed["step"] < len(ed["schedule"]):
        nxt, at = ed["schedule"][ed["step"]]
        e.description += f"\nNext: **{PHASE_LABEL.get(nxt, nxt)}** {ts(at)}"
    scored = any(t["score"] for t in ed["roster"].values())
    lines = []
    hidden = engine.pending_deaths(ed)   # deaths in a broadcast that hasn't finished posting stay secret
    for uid in (engine.ranking(ed["roster"]) if scored else list(ed["roster"])):
        t = ed["roster"][uid]
        mark = "🟢" if t["alive"] or uid in hidden else "💀"
        score = f" · {t['score']}" if scored else ""
        shown = engine.shown_kills(ed, uid, hidden)
        kills = f" · 🗡️{shown}" if shown else ""
        lines.append(f"{mark} {t['name']}{score}{kills}")
    e.add_field(name="Tributes", value="\n".join(lines) or "—", inline=False)
    if shop:
        e.add_field(name="🪂 Sponsorship", value="Open now. Tap Sponsor below.", inline=False)
    return e


def shop_embed(ed: dict, tribute: str | None, prices: dict, blocked: dict) -> "discord.Embed":
    e = _embed("🪂 Sponsor a tribute", "One parachute per tribute from you for the whole Games. "
               "Each tribute can get 3 a day. Prices rise each day of the Games.")
    if tribute:
        t = ed["roster"][tribute]
        inv = t["inventory"]
        held = [ITEMS[i][1] + " " + ITEMS[i][0] for i in inv["gear"] + inv["consumables"]]
        e.add_field(name=f"{t['name']} · score {t['score']}", value="Holding: " + (", ".join(held) or "nothing"), inline=False)
    rows = []
    for key, (name, emoji, _base, kind, effect) in ITEMS.items():
        line = f"{emoji} **{name}** — {prices[key]:,} · {effect}"
        if key in blocked:
            line = f"~~{emoji} {name}~~ — {blocked[key]}"
        rows.append(line)
    e.add_field(name="Items", value="\n".join(rows), inline=False)
    return e


def plan_text(t: dict) -> str:
    label, emoji, effect = STRATEGIES[t["strategy"]]
    return f"Your plan: {emoji} **{label}** ({effect})."


def stats_embed(name: str, m: dict) -> "discord.Embed":
    e = _embed(f"🏹 {name} in the Hunger Games")
    e.add_field(name="As a tribute", value=(
        f"Games played: {m['editions']}\nVictories: {m['wins']}\nKills: {m['kills']}\nFallen: {m['deaths']}\n"
        f"Best Training Score: {m['best_score'] or '—'}"), inline=True)
    e.add_field(name="As a sponsor", value=f"Parachutes sent: {m['items_sent']}\nCoins spent: {m['coins_spent']:,}", inline=True)
    e.set_footer(text="Draft opt-out: on" if m["opt_out"] else "Draft opt-out: off")
    return e


def hall_embed(hall: list) -> "discord.Embed":
    e = _embed("🏛️ Hall of Victors")
    rows = [f"**{h['title']}** — {h['name']} (score {h['score']}, {h['kills']} kills)" for h in reversed(hall[-20:])]
    e.description = "\n".join(rows) or "No Victors yet. Will it be you?"
    return e


def leaderboard_embed(rows: list) -> "discord.Embed":
    e = _embed("🏹 Hunger Games leaderboard")
    e.description = "\n".join(
        f"{i}. {name} — {w} win{'s' if w != 1 else ''}, {k} kills" for i, (name, w, k) in enumerate(rows, 1)
    ) or "Nobody has played yet."
    return e


def report_text(r: dict, currency: str) -> str:
    ch = r["challenges_done"]
    return (
        f"**{'Test run' if r['test'] else 'Edition ' + str(r['edition'])}**: {r['tributes']} tributes, "
        f"{r['volunteers']} volunteers, {r['swap_requests']} swap requests ({r['swaps']} accepted), "
        f"{r['withdrawals']} withdrawals\n"
        f"Training done: " + ", ".join(f"{k} {v}" for k, v in ch.items()) + f"; {r['cheer_voters']} cheer voters\n"
        f"Sponsors: {r['sponsors']} people, {r['items']} parachutes, {r['spent']:,} {currency} spent, "
        f"{r['burned']:,} burned, prize {r['prize']:,} ({r['minted']:,} newly minted)\n"
        f"Members who pressed any button: {r['unique_members']}"
    )
