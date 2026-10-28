"""Discord embed builders for cardcollect. This module (unlike engine.py /
models.py / storage.py) is allowed to import discord -- it's the
presentation layer, not game logic, mirroring how minigamehub and
blackjacktable split their embeds.py out from the main cog file.
"""

import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import discord

from . import import_characters
from .constants import ACTIVITY_TIMEZONE, SET_MIN_CARDS, TIERS, WISHLIST_MAX
from .imagegen import RARITY_COLORS
from .models import Card, SellToken, SeriesPreview

RARITY_EMOJI = {
    "common": "⚪",
    "rare": "\U0001f535",
    "epic": "\U0001f7e3",
    "legendary": "\U0001f7e1",
}

EMBED_COLOR = discord.Color.from_rgb(155, 89, 182)
TEST_COLOR = discord.Color.from_rgb(231, 76, 60)


def _rarity_line(rarity: str) -> str:
    return f"{RARITY_EMOJI.get(rarity, '')} {rarity.title()}"


def _rarity_color(rarity: str) -> discord.Color:
    """Same rarity->color mapping imagegen.py uses for a card's border, so
    the claim embed's accent bar matches the card art itself instead of
    always being flat purple -- live feedback: "can the embed border be the
    color of the rarity?"."""
    r, g, b = RARITY_COLORS.get(rarity, RARITY_COLORS["common"])
    return discord.Color.from_rgb(r, g, b)


def sell_result_embed(token: SellToken, price: int, member: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="Card sold", color=EMBED_COLOR)
    embed.add_field(name="Rarity", value=_rarity_line(token.rarity), inline=True)
    embed.add_field(name="Sold by", value=member.mention, inline=True)
    embed.add_field(name="Payout", value=f"{price} wondercoin", inline=True)
    return embed


def give_result_embed(card: Card, giver: discord.abc.User, receiver: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="Card gifted", color=EMBED_COLOR)
    embed.add_field(name="Card", value=f"**{card.name}** ({card.series})", inline=True)
    embed.add_field(name="Rarity", value=_rarity_line(card.rarity), inline=True)
    embed.add_field(name="From → To", value=f"{giver.mention} → {receiver.mention}", inline=False)
    return embed


def leaderboard_embed(entries: Sequence[Tuple[discord.abc.User, int]], guild_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"Card Collection Leaderboard — {guild_name}", color=EMBED_COLOR)
    if not entries:
        embed.description = "Nobody has collected a card yet."
        return embed
    lines = []
    medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
    for i, (member, count) in enumerate(entries):
        prefix = medals[i] if i < len(medals) else f"`#{i + 1}`"
        lines.append(f"{prefix} {member.mention} — **{count}** cards")
    embed.description = "\n".join(lines)
    return embed


def settings_embed(guild_config: dict, guild_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"cardcollect settings — {guild_name}", color=EMBED_COLOR)
    channel_id = guild_config.get("channel_id")
    embed.add_field(name="Drop channel", value=f"<#{channel_id}>" if channel_id else "Not set", inline=True)
    embed.add_field(name="Drop chance", value=f"{guild_config.get('drop_chance', 0) * 100:.2f}% / message", inline=True)
    embed.add_field(name="Drop cooldown", value=f"{guild_config.get('drop_cooldown_seconds', 0)}s", inline=True)
    embed.add_field(name="Draw window", value=f"{guild_config.get('draw_window_seconds', 0)}s", inline=True)

    weights = guild_config.get("drop_weights", {})
    weight_lines = [f"{_rarity_line(t)}: {weights.get(t, 0)}" for t in TIERS]
    embed.add_field(name="Drop weights", value="\n".join(weight_lines), inline=True)

    prices = guild_config.get("sell_prices", {})
    price_lines = [f"{_rarity_line(t)}: {prices.get(t, 0)}" for t in TIERS]
    embed.add_field(name="Sell prices", value="\n".join(price_lines), inline=True)

    pool_size = len(guild_config.get("pool", {}))
    embed.add_field(name="Pool size", value=f"{pool_size} characters", inline=True)

    test_mode = guild_config.get("test_mode", False)
    embed.add_field(name="Test mode", value="ON — drops award nothing" if test_mode else "off", inline=True)
    return embed


def diagnostics_embed(
    hourly_buckets: Dict[str, int],
    suggested_drop_chance: float,
    suggested_drop_cooldown_seconds: int,
    sampling_since: float,
    guild_name: str,
    stats: Optional[dict] = None,
) -> discord.Embed:
    embed = discord.Embed(title=f"cardcollect diagnostics — {guild_name}", color=EMBED_COLOR)
    since = datetime.datetime.fromtimestamp(sampling_since, tz=datetime.timezone.utc)
    embed.description = (
        f"Sampling since {since.strftime('%Y-%m-%d %H:%M UTC')}. "
        f"Hour-of-day buckets are in {ACTIVITY_TIMEZONE}."
    )

    total = sum(hourly_buckets.values())
    if total == 0:
        embed.add_field(name="Activity", value="No messages tracked yet — run this again after some traffic.", inline=False)
    else:
        lines = []
        for hour in range(24):
            count = hourly_buckets.get(str(hour), 0)
            bar_len = int((count / max(hourly_buckets.values())) * 20) if hourly_buckets.values() else 0
            lines.append(f"`{hour:02d}:00` {'█' * bar_len} {count}")
        # keep the embed within field-length limits
        chunk = "\n".join(lines)
        if len(chunk) > 1024:
            chunk = chunk[:1000] + "\n… (see attached JSON for full data)"
        embed.add_field(name="Messages by hour", value=chunk, inline=False)

    if stats and stats.get("drops"):
        drops, offered = stats["drops"], max(1, stats.get("cards_offered", 0))
        since = datetime.datetime.fromtimestamp(stats.get("since") or sampling_since, tz=datetime.timezone.utc)
        picks = stats.get("picks", 0)
        unpicked = stats.get("cards_unpicked", 0)
        daily = stats.get("daily_pulls", {})
        embed.add_field(
            name=f"Draws since {since.strftime('%Y-%m-%d')}",
            value=(
                f"Drops posted: **{drops}** ({stats.get('cards_offered', 0)} cards)\n"
                f"Picks: **{picks}** (about {picks / drops:.1f} per drop)\n"
                f"Cards nobody picked: **{unpicked}** ({unpicked / offered * 100:.0f}%)\n"
                f"Draws with a real contest: **{stats.get('contested', 0)}**"
            ),
            inline=False,
        )
    if stats and stats.get("daily_pulls"):
        days = sorted(stats["daily_pulls"])[-7:]
        embed.add_field(
            name="Daily pulls (last 7 days with pulls)",
            value="\n".join(f"`{d}` {stats['daily_pulls'][d]}" for d in days),
            inline=False,
        )

    embed.add_field(name="Suggested drop_chance", value=f"{suggested_drop_chance * 100:.3f}%", inline=True)
    embed.add_field(name="Suggested drop_cooldown_seconds", value=str(suggested_drop_cooldown_seconds), inline=True)
    embed.set_footer(text="Full data attached as JSON. Re-run periodically as activity patterns shift.")
    return embed


def card_added_embed(card: Card) -> discord.Embed:
    embed = discord.Embed(title="Character added to pool", color=EMBED_COLOR)
    embed.add_field(name="Name", value=card.name, inline=True)
    embed.add_field(name="Series", value=card.series or "—", inline=True)
    embed.add_field(name="Rarity", value=_rarity_line(card.rarity), inline=True)
    embed.add_field(name="Card ID", value=str(card.card_id), inline=True)
    return embed


def card_image_updated_embed(card: Card) -> discord.Embed:
    embed = discord.Embed(title="Card art updated", color=EMBED_COLOR)
    embed.description = (
        f"**{card.name}** (ID {card.card_id}) now uses the new art. "
        "Anyone who already owns a copy sees the new image too -- the ID is what's stored, not the picture."
    )
    embed.add_field(name="Rarity", value=_rarity_line(card.rarity), inline=True)
    return embed


def card_removed_embed(card_id: int, name: str) -> discord.Embed:
    embed = discord.Embed(title="Character removed from pool", color=EMBED_COLOR)
    embed.description = f"**{name}** (ID {card_id}) will no longer drop. Members who already own it keep their copy."
    return embed


def cards_removed_embed(retired: Sequence[Tuple[int, str]], not_found: Sequence[int]) -> discord.Embed:
    """Bulk version of card_removed_embed, for `.card removecard <id> <id> ...`."""
    embed = discord.Embed(title="Characters removed from pool", color=EMBED_COLOR)
    if retired:
        lines = [f"**{name}** (ID {cid})" for cid, name in retired]
        text = "\n".join(lines)
        if len(text) > 1024:
            text = text[:1000] + f"\n… and {len(lines) - 1} more"
        embed.add_field(
            name=f"Retired ({len(retired)})",
            value=text + "\n\nMembers who already own any of these keep their copy.",
            inline=False,
        )
    if not_found:
        embed.add_field(
            name=f"Not found ({len(not_found)})",
            value=", ".join(str(cid) for cid in not_found),
            inline=False,
        )
    if not retired and not not_found:
        embed.description = "Nothing to remove."
    return embed


def card_info_embed(card: Card, owner_count: int, owner_mentions: Sequence[str]) -> discord.Embed:
    embed = discord.Embed(title=card.name, color=EMBED_COLOR)
    series = card.series or "—"
    if card.series_alt:
        series += f"\n{card.series_alt}"
    embed.add_field(name="Series", value=series, inline=True)
    embed.add_field(name="Rarity", value=_rarity_line(card.rarity), inline=True)
    embed.add_field(name="Card ID", value=str(card.card_id), inline=True)
    embed.add_field(name="AniList favourites", value=str(card.favourites), inline=True)
    embed.add_field(
        name="Currently owned by",
        value=f"{owner_count} member{'s' if owner_count != 1 else ''}",
        inline=True,
    )
    if card.retired:
        embed.add_field(name="Status", value="Retired — no longer drops", inline=True)
    if owner_mentions:
        text = ", ".join(owner_mentions)
        if owner_count > len(owner_mentions):
            text += f", +{owner_count - len(owner_mentions)} more"
        embed.add_field(name="Owners", value=text, inline=False)
    return embed


def pool_reset_embed(count: int) -> discord.Embed:
    embed = discord.Embed(title="Pool reset", color=EMBED_COLOR)
    embed.description = (
        f"Retired all {count} active characters. Nothing will drop until you re-import "
        f"(`.card importpool`) or re-add (`.card addcard`). Members who already own a "
        f"retired card keep their copy."
    )
    return embed


def _media_line(media: dict) -> str:
    bits = [b for b in (str(media.get("format") or "").replace("_", " ").title(), str(media.get("year") or "")) if b]
    english = f" / {media['title_english']}" if media.get("title_english") else ""
    return f"**{media['title']}**{english}" + (f" · {' · '.join(bits)}" if bits else "")


def series_search_embed(query: str, results: Sequence[dict]) -> discord.Embed:
    embed = discord.Embed(title="Import series: which one?", color=EMBED_COLOR)
    shown = discord.utils.escape_mentions(discord.utils.escape_markdown(query.strip()))[:60]
    embed.description = f"AniList matches for **{shown}**. Pick one below to preview it; nothing is imported yet.\n\n" + "\n".join(
        f"{i}. {_media_line(m)}" for i, m in enumerate(results, 1)
    )
    return embed


def _tier_counts(entries: Sequence[dict]) -> Dict[str, int]:
    counts = {t: 0 for t in TIERS}
    for e in entries:
        counts[e["rarity"]] = counts.get(e["rarity"], 0) + 1
    return counts


def series_preview_embed(preview: SeriesPreview, limit: int) -> discord.Embed:
    """The dry-run: exactly what importing the top `limit` characters would do."""
    sel = import_characters.select_top(preview.built, limit, preview.overrides, preview.skips)
    shown = import_characters.select_top(preview.built, limit, preview.overrides)  # skipped ones too, struck through
    everything = import_characters.select_top(preview.built, 10**9, preview.overrides, preview.skips)
    numbers = import_characters.entry_numbers(preview.built)
    info = preview.info
    embed = discord.Embed(title=f"Import series: {info['title']}", color=EMBED_COLOR)
    head = []
    if info.get("title_english"):
        head.append(info["title_english"])
    head.append(
        f"AniList lists **{info['total']}** characters; the top **{len(info['characters'])}** by favourites were checked."
    )
    embed.description = "\n".join(head)

    in_pool = sum(1 for x in sel["skipped"] if x["reason"] == "already in the pool")
    no_art = len(sel["skipped"]) - in_pool
    bits = [f"**{len(sel['entries'])}** new"]
    if in_pool:
        bits.append(f"{in_pool} already in the pool")
    if sel["links"]:
        bits.append(f"{len(sel['links'])} hand-added card{'s' if len(sel['links']) != 1 else ''} will be linked, not duplicated")
    if no_art:
        bits.append(f"{no_art} left out (no image on AniList)")
    embed.add_field(name=f"Top {min(limit, len(info['characters']))}", value=" · ".join(bits), inline=False)

    if shown["entries"]:
        lines = []
        for e in shown["entries"]:
            rank = e["rank"]
            number = f"`{numbers[rank]}`"
            if rank in preview.skips:
                lines.append(f"{number} ~~{e['name']}~~ skipped")
                continue
            line = f"{number} {RARITY_EMOJI.get(e['rarity'], '')} {e['name']} — {e['favourites']:,} ♥"
            if rank in preview.art:
                line += " 🖼 your art"
            elif rank in preview.flags:
                line += f" ⚠ {preview.flags[rank]}"
            lines.append(line)
        text = ""
        for i, line in enumerate(lines):
            if len(text) + len(line) + 40 > 1000:
                text += f"\n…and {len(lines) - i} more"
                break
            text += ("\n" if text else "") + line
        embed.add_field(name="New characters", value=text, inline=False)
    else:
        embed.add_field(name="New characters", value="Nothing new at this size.", inline=False)

    if sel["entries"]:
        after = _tier_counts(sel["entries"])
        rows = [
            f"{_rarity_line(t)}: {preview.pool_tiers.get(t, 0)} → {preview.pool_tiers.get(t, 0) + after[t]} (+{after[t]})"
            for t in TIERS
            if after[t]
        ]
        embed.add_field(
            name="Pool after importing",
            value="\n".join(rows) + "\nA tier with more cards makes each of them drop less often.",
            inline=False,
        )

    if len(everything["entries"]) > len(sel["entries"]):
        counts = _tier_counts(everything["entries"])
        mix = ", ".join(f"{counts[t]} {t}" for t in reversed(TIERS) if counts[t])
        embed.add_field(name="Import everything checked", value=f"{len(everything['entries'])} new: {mix}", inline=False)

    edits = []
    if preview.overrides:
        edits.append(f"{len(preview.overrides)} rarity")
    if preview.skips:
        edits.append(f"{len(preview.skips)} skipped")
    if preview.art:
        edits.append(f"{len(preview.art)} new art")
    if edits:
        embed.add_field(name="Your edits", value=", ".join(edits), inline=True)
    embed.set_footer(
        text="Nothing is imported until you press an Import button. Red border = poor art, green = your upload. "
        "Replace art swaps a picture; in Adjust, delete a character's line to leave it out."
    )
    return embed


def series_result_embed(
    title: str,
    added: Sequence[dict],
    failed: Sequence[str],
    linked: int,
    already_in_pool: int,
) -> discord.Embed:
    embed = discord.Embed(title=f"Imported: {title}", color=EMBED_COLOR)
    counts = _tier_counts(added)
    mix = ", ".join(f"{counts[t]} {t}" for t in reversed(TIERS) if counts[t])
    embed.description = f"Added **{len(added)}** character{'s' if len(added) != 1 else ''}" + (f" ({mix})" if mix else "") + "."
    notes = []
    if linked:
        notes.append(f"{linked} hand-added card{'s' if linked != 1 else ''} matched and linked to AniList (no duplicates made)")
    if already_in_pool:
        notes.append(f"{already_in_pool} already in the pool, skipped")
    if failed:
        shown = ", ".join(failed[:8]) + (f", +{len(failed) - 8} more" if len(failed) > 8 else "")
        notes.append(f"{len(failed)} image download(s) failed, not added: {shown}. Run the import again to retry them")
    if notes:
        embed.add_field(name="Notes", value="\n".join(f"• {n}" for n in notes), inline=False)
    embed.set_footer(text="Wrong tier? Run .card info <card> and use the Change rarity dropdown.")
    return embed


_OUTCOME_TAG = {"new": "NEW", "duplicate": "spare copy", "sell_token": "sell token"}


def draw_results_embed(
    results: Sequence[dict],
    cards: Dict[int, Optional[Card]],
    picker_count: int,
    set_lines: Sequence[str] = (),
    is_test: bool = False,
) -> discord.Embed:
    """The drop message after its draw: one line per card with the winner, how
    many picked it, and what it was to them. Pick counts are shown only now
    that the draw is over."""
    embed = discord.Embed(
        title="Test draw (nothing awarded)" if is_test else "Draw results",
        color=TEST_COLOR if is_test else EMBED_COLOR,
    )
    lines = []
    for result in results:
        card = cards.get(result["card_id"])
        name = f"**{discord.utils.escape_markdown(card.name)}**" if card is not None else "a removed card"
        emoji = RARITY_EMOJI.get(card.rarity, "") if card is not None else ""
        head = f"{emoji} {name}".strip()
        if result["outcome"] == "error":
            lines.append(f"{head} — couldn't be awarded, please tell an admin")
        elif result["winner"] is None:
            lines.append(f"{head} — nobody picked it, wasted")
        else:
            tag = "" if is_test else _OUTCOME_TAG.get(result["outcome"], "")
            extra = f" · {tag}" if tag else ""
            lines.append(f"{head} — <@{result['winner']}> ({result['pickers']} picked){extra}")
    embed.description = "\n".join(lines)
    if set_lines:
        embed.add_field(name="Sets", value="\n".join(set_lines), inline=False)
    embed.set_footer(text=f"{picker_count} member{'s' if picker_count != 1 else ''} picked")
    return embed


def sets_embed(rows: Sequence[Tuple[str, int, int]], claimed: set, whose: str, total_sets: int) -> discord.Embed:
    """The Sets tab: series the member has started, with progress."""
    embed = discord.Embed(title=f"{whose} sets", color=EMBED_COLOR)
    if not rows:
        embed.description = (
            f"No sets started yet. A set is every character from one series (at least {SET_MIN_CARDS}); "
            "finish one for a wondercoin bonus."
        )
        return embed
    complete = sum(1 for _, have, total in rows if have == total)
    lines = []
    for series, have, total in rows[:15]:
        done = have == total
        bar = "█" * round(8 * have / total) + "░" * (8 - round(8 * have / total))
        mark = "✅" if done else "▫️"
        paid = " 🏆" if done and series in claimed else ""
        lines.append(f"{mark} **{discord.utils.escape_markdown(series)[:60]}** `{bar}` {have}/{total}{paid}")
    if len(rows) > 15:
        lines.append(f"…and {len(rows) - 15} more started")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{complete} complete of {total_sets} sets · finishing one pays a wondercoin bonus once")
    return embed


def wishlist_embed(cards: Sequence[Card], pings_off: bool) -> discord.Embed:
    embed = discord.Embed(title="Your wishlist", color=EMBED_COLOR)
    if cards:
        embed.description = "\n".join(
            f"{RARITY_EMOJI.get(c.rarity, '')} **{discord.utils.escape_markdown(c.name)}**"
            + (f" — {discord.utils.escape_markdown(c.series)[:50]}" if c.series else "")
            for c in cards
        )
    else:
        embed.description = "Nothing on your wishlist. Open a card with `.card find` and press ★ Wishlist."
    embed.set_footer(
        text=f"{len(cards)}/{WISHLIST_MAX} · "
        + ("Pings are OFF: you won't be pinged when these drop." if pings_off else "You're pinged when one of these drops. A card leaves the list when you own it.")
    )
    return embed


def request_embed(request_id: int, requester, text: str, status: str = "pending", by=None) -> discord.Embed:
    who = requester.mention if hasattr(requester, "mention") else f"<@{requester}>"
    color = {"pending": EMBED_COLOR, "approved": discord.Color.green(), "declined": TEST_COLOR}.get(status, EMBED_COLOR)
    embed = discord.Embed(title=f"Card request #{request_id}", color=color)
    embed.add_field(name="Character", value=discord.utils.escape_markdown(text), inline=False)
    embed.add_field(name="Requested by", value=who, inline=True)
    embed.add_field(name="Status", value=status.title() + (f" by {by.mention}" if by is not None else ""), inline=True)
    if status == "pending":
        embed.set_footer(text="Approve it to put the card on their wishlist once you've added it to the pool.")
    return embed


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="cardcollect", description=message, color=discord.Color.red())
