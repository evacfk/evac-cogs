"""Discord embeds for wonderevents."""
from __future__ import annotations

import discord

from . import engine
from .constants import COLOR_DONE, COLOR_EVENT, COLOR_LIVE, LIST_MENTIONS_MAX


def _people(ids: list[int]) -> str:
    if not ids:
        return "—"
    shown = " ".join(f"<@{i}>" for i in ids[:LIST_MENTIONS_MAX])
    return shown + (f" +{len(ids) - LIST_MENTIONS_MAX} more" if len(ids) > LIST_MENTIONS_MAX else "")


def event_embed(ev: dict, *, guild_id: int):
    emoji = ev.get("emoji") or "\N{CALENDAR}"
    ts = int(ev["start_ts"])
    if ev.get("cancelled"):
        title, color = f"\N{CROSS MARK} Cancelled: {ev['title']}", COLOR_DONE
    elif ev.get("ended"):
        came = len(engine.attendees(ev.get("attend") or {}))
        title = f"\N{WHITE HEAVY CHECK MARK} {ev['title']}" + (f" — {came} came" if came else "")
        color = COLOR_DONE
    elif ev.get("started"):
        title, color = f"\N{LARGE RED CIRCLE} LIVE NOW: {ev['title']}", COLOR_LIVE
    else:
        title, color = f"{emoji} {ev['title']}", COLOR_EVENT
    e = discord.Embed(title=title[:256], description=(ev.get("desc") or None), color=discord.Color(color))
    e.add_field(name="\N{SPIRAL CALENDAR PAD} When", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=True)
    if ev.get("host_id"):
        e.add_field(name="\N{MICROPHONE} Host", value=f"<@{ev['host_id']}>", inline=True)
    lists = engine.rsvp_lists(ev.get("rsvp") or {})
    if not ev.get("ended") and not ev.get("cancelled"):
        e.add_field(name=f"\N{WHITE HEAVY CHECK MARK} Going ({len(lists['going'])})", value=_people(lists["going"]), inline=False)
        e.add_field(name=f"\N{THINKING FACE} Maybe ({len(lists['maybe'])})", value=_people(lists["maybe"]), inline=False)
    elif ev.get("ended"):
        e.add_field(name="\N{BUSTS IN SILHOUETTE} Came", value=_people(engine.attendees(ev.get("attend") or {})), inline=False)
    if ev.get("scheduled_event_id") and not ev.get("ended") and not ev.get("cancelled"):
        e.add_field(name="\N{BELL} Reminder from Discord",
                    value=f"[Click *Interested* here](https://discord.com/events/{guild_id}/{ev['scheduled_event_id']})",
                    inline=False)
    if ev.get("image"):
        e.set_image(url=ev["image"])
    e.set_footer(text=f"Event #{ev['id']} · times show in your own timezone")
    return e


def list_text(events: list[dict]) -> str:
    if not events:
        return "No upcoming events."
    lines = []
    for ev in events:
        going = len(engine.rsvp_lists(ev.get("rsvp") or {})["going"])
        state = " (LIVE)" if ev.get("started") else ""
        lines.append(f"`#{ev['id']}` {ev.get('emoji') or ''} **{ev['title']}**{state} — <t:{int(ev['start_ts'])}:f> · {going} going")
    return "\n".join(lines)
