"""Shared emoji parsing/comparison for game types that use a configurable
reaction emoji (pet, hunt).

Config stores these as plain strings (a unicode emoji, or a custom emoji's
`<:name:id>` / `<a:name:id>` code, however the admin typed/pasted it into
`.mgh settings` or the raw JSON commands). Comparing those strings directly
against a live `Reaction.emoji`'s `str()` is fragile -- a stray leading/
trailing space from copy-paste, or an Emoji-vs-PartialEmoji object identity
difference, silently breaks the match even though it's clearly "the same
emoji" to a human. Parsing both sides and comparing by ID (for custom
emoji) or normalized string (for unicode) is robust to all of that.
"""
import discord


def parse_emoji(raw: str) -> discord.PartialEmoji:
    """Parse a stored emoji string into a PartialEmoji, tolerant of stray
    surrounding whitespace. Use the result for `add_reaction()` (passing the
    object directly is more robust than round-tripping through a raw string)
    and for comparisons via `emoji_matches`."""
    return discord.PartialEmoji.from_str(raw.strip())


def emoji_matches(stored_raw: str, other) -> bool:
    """Compare a stored config emoji string against a live reaction's emoji
    (`Emoji`, `PartialEmoji`, or `str`). Custom emoji match by ID (the only
    thing that actually identifies "the same emoji" -- name/animated flag can
    both be stale if the emoji was renamed, and exact-string equality is
    brittle to whitespace); unicode emoji match by normalized string.
    """
    parsed = parse_emoji(stored_raw)
    if parsed.id is not None:
        other_id = getattr(other, "id", None)
        return other_id is not None and int(other_id) == int(parsed.id)
    return str(other) == str(parsed)
