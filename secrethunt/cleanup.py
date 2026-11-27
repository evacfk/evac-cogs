"""One-time cleanup for the retired Secret Hunt: finds every message it pinned and unpins it.

The old cog recorded most pinned messages in its Config, but a fresh set of objects left the previous set pinned and
forgotten. So this looks in two places: the recorded message ids, and the pins of every room the hunt ever used.
In those rooms it touches only pins that the bot itself posted and that carry one of the hunt's own embed titles, so
other bot pins (the pixel canvas, Hunger Games posts, ...) are never unpinned.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import discord

DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)

# Embed titles of the hunt's poems and objects. A witness object carries a " — <label>" suffix.
POEM_TITLES = (
    "Pinned to the Mad Hatter's table",
    "Hung where the Queen forgot it",
    "Nailed to the courtroom door",
    "Written on the Dormouse's teacup",
    "Written in smoke, above the pins",
)
OBJECT_TITLES = (
    "A pocket watch",
    "A looking glass",
    "A witness waits here",
    "A tea table",
    "A large blue Caterpillar",
)
TITLES = POEM_TITLES + OBJECT_TITLES


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def title_matches(title) -> bool:
    if not title:
        return False
    return any(title == t or title.startswith(t + " \N{EM DASH}") for t in TITLES)


def config_targets(data: dict):
    """(room ids, {room id: recorded pinned message ids}) from the old guild config."""
    rooms: set[int] = set()
    recorded: dict[int, set[int]] = {}

    def add(room, message=None):
        rid = _int(room)
        if rid is None:
            return
        rooms.add(rid)
        mid = _int(message)
        if mid is not None:
            recorded.setdefault(rid, set()).add(mid)

    for cid in data.get("pool") or []:
        add(cid)
    for cid in data.get("trail") or []:
        add(cid)
    for by_room in (data.get("objects") or {}).values():
        for cid, mid in (by_room or {}).items():
            add(cid, mid)
    for cid, mid in (data.get("poems") or {}).items():
        add(cid, mid)
    for cid in (data.get("poem_targets") or {}).values():
        add(cid)
    test_room = data.get("test_channel_id")
    if test_room:
        add(test_room)
        for mid in (data.get("test_poems") or {}).values():
            add(test_room, mid)
    return rooms, recorded


async def pins_of(channel) -> list:
    """The pinned messages of a channel or forum post, whichever way this discord.py version returns them."""
    result = channel.pins()
    items = [m async for m in result] if hasattr(result, "__aiter__") else await result
    return [getattr(m, "message", m) for m in items]  # newer discord.py wraps each pin


def is_hunt_pin(message, bot_id: int, recorded: set[int]) -> bool:
    author = getattr(message, "author", None)
    if author is None or author.id != bot_id:
        return False  # never touch a pin the bot did not post
    if message.id in recorded:
        return True
    return any(title_matches(getattr(e, "title", None)) for e in getattr(message, "embeds", None) or [])


@dataclass
class Report:
    lines: list = field(default_factory=list)
    found: int = 0
    unpinned: int = 0
    failed: int = 0
    rooms: int = 0  # rooms that held at least one hunt pin

    def summary(self, confirm: bool) -> str:
        if confirm:
            return (f"Unpinned {self.unpinned} of {self.found} hunt message(s) in {self.rooms} room(s)"
                    + (f"; {self.failed} failed." if self.failed else "."))
        return f"Dry run: {self.found} hunt message(s) still pinned in {self.rooms} room(s). Nothing was changed."


async def _room(guild, cid: int):
    room = guild.get_channel_or_thread(cid) if hasattr(guild, "get_channel_or_thread") else guild.get_channel(cid)
    if room is None:  # archived forum posts are not always cached
        try:
            room = await guild.fetch_channel(cid)
        except (*DISCORD_ERRORS, AttributeError):
            return None
    return room


async def run(guild, bot_id: int, data: dict, confirm: bool) -> Report:
    report = Report()
    rooms, recorded = config_targets(data)
    for cid in sorted(rooms):
        room = await _room(guild, cid)
        if room is None:
            report.lines.append(f"- <#{cid}>: not found (deleted, or I cannot see it)")
            continue
        try:
            mine = [m for m in await pins_of(room) if is_hunt_pin(m, bot_id, recorded.get(cid, set()))]
        except DISCORD_ERRORS as err:
            report.lines.append(f"- {room.mention}: could not read pins ({type(err).__name__}); needs View Channel and Pin Messages")
            continue
        if not mine:
            continue
        report.rooms += 1
        report.found += len(mine)
        if not confirm:
            report.lines.append(f"- {room.mention}: {len(mine)} pinned" + (" (archived post)" if getattr(room, "archived", False) else ""))
            continue
        reopened = False
        if getattr(room, "archived", False):  # an archived forum post will not take changes until it is reopened
            try:
                await room.edit(archived=False)
                reopened = True
            except DISCORD_ERRORS:
                pass
        done = bad = 0
        for m in mine:
            try:
                await m.unpin(reason="Secret Hunt retired")
                done += 1
            except DISCORD_ERRORS:
                bad += 1
        if reopened:
            try:
                await room.edit(archived=True)
            except DISCORD_ERRORS:
                pass
        report.unpinned += done
        report.failed += bad
        report.lines.append(f"- {room.mention}: unpinned {done}" + (f", {bad} failed (needs Pin Messages there)" if bad else ""))
    return report
