"""Pure formatting for mystats: raw numbers in, display sections out. No discord/redbot imports.

Every function takes the raw data a source returned (or None when the game's cog is not loaded)
and returns a list of text lines. An empty list means "nothing to show" and the section is hidden.
"""
from __future__ import annotations

from typing import Any, Optional

from .constants import CASINO_TOP_GAMES, FIELD_LIMIT, GAME_LABELS, WINS_ONLY


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def pct(part: int, whole: int) -> str:
    return f"{part * 100 // whole}%" if whole > 0 else "-"


def minigame_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    games = data.get("games") or {}
    lines = []
    for key, label in GAME_LABELS.items():
        entry = games.get(key) or {}
        good, bad, best = _int(entry.get("good")), _int(entry.get("bad")), _int(entry.get("highest_streak"))
        if not good and not bad:
            continue
        if key in WINS_ONLY or not bad:
            text = f"**{label}:** {good:,} win{'s' if good != 1 else ''}"
        else:
            text = f"**{label}:** {good:,} won, {bad:,} lost"
        if best > 1:
            text += f" · best streak {best}"
        lines.append(text)
    damage = _int(data.get("boss_damage"))
    if damage:
        lines.append(f"**Boss damage dealt:** {damage:,}")
    return lines


def duel_lines(data: Optional[dict], currency: str = "coins") -> list[str]:
    if not data:
        return []
    wins, losses = _int(data.get("wins")), _int(data.get("losses"))
    if not wins and not losses:
        return []
    net = _int(data.get("coins_won")) - _int(data.get("coins_lost"))
    sign = "+" if net >= 0 else "-"
    return [f"{wins:,} won, {losses:,} lost ({pct(wins, wins + losses)}) · net {sign}{abs(net):,} {currency}"]


def heist_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    stats = data.get("stats") or {}
    ok, fail, caught = _int(stats.get("success")), _int(stats.get("fail")), _int(stats.get("caught"))
    if not ok and not fail and not caught:
        return []
    line = f"Level {_int(data.get('level')) or 1} · {ok:,} pulled off, {fail:,} failed, {caught:,} times caught"
    return [line]


def casino_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    played, won = data.get("Played") or {}, data.get("Won") or {}
    total_played = sum(_int(v) for v in played.values())
    if not total_played:
        return []
    total_won = sum(_int(v) for v in won.values())
    lines = [f"{total_played:,} games played, {total_won:,} won ({pct(total_won, total_played)})"]
    top = sorted(((name, _int(n)) for name, n in played.items() if _int(n)), key=lambda kv: (-kv[1], kv[0]))
    for name, n in top[:CASINO_TOP_GAMES]:
        lines.append(f"{name}: {n:,} played, {_int(won.get(name)):,} won")
    return lines


def card_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    owned, streak = _int(data.get("owned")), _int(data.get("daily_streak"))
    if not owned and not streak:
        return []
    line = f"{owned:,} card{'s' if owned != 1 else ''} collected"
    if streak:
        line += f" · daily pull streak {streak}"
    return [line]


def puzzle_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    won, pieces = _int(data.get("puzzles_won")), _int(data.get("pieces_collected"))
    if not won and not pieces:
        return []
    return [f"{won:,} puzzle{'s' if won != 1 else ''} won · {pieces:,} pieces collected"]


def verdict_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    played, correct = _int(data.get("played")), _int(data.get("correct"))
    if not played:
        return []
    streak, best = _int(data.get("streak")), _int(data.get("best_streak"))
    return [f"{played:,} played · {correct:,} right guesses ({pct(correct, played)}) · streak {streak} (best {best})"]


def pet_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    cared = _int(data.get("care_recent"))
    best = _int(data.get("best_streak"))
    if not cared and not best:
        return []
    lines = []
    if cared:
        lines.append(f"Looked after the pet {cared:,} time{'s' if cared != 1 else ''} in the last few weeks")
    if best >= 2:
        lines.append(f"Best pet care streak: {best} days")
    return lines


def bump_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    total = _int(data.get("total_bumps"))
    return [f"{total:,} server bump{'s' if total != 1 else ''}"] if total else []


def lottery_lines(data: Optional[dict]) -> list[str]:
    if not data:
        return []
    tickets = sum(_int(v) for v in (data.get("tickets") or {}).values() if not isinstance(v, (dict, list)))
    return [f"Holding {tickets:,} lottery ticket{'s' if tickets != 1 else ''}"] if tickets else []


def clip(text: str, limit: int = FIELD_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_sections(raw: dict, currency: str = "coins") -> list[tuple[str, list[str]]]:
    """[(title, lines)] for every game that has something to show, in display order."""
    order = [
        ("\N{GAME DIE} Chat minigames", minigame_lines(raw.get("minigames"))),
        ("\N{CROSSED SWORDS} Duels", duel_lines(raw.get("duel"), currency)),
        ("\N{MONEY BAG} Heists", heist_lines(raw.get("heist"))),
        ("\N{SLOT MACHINE} Casino", casino_lines(raw.get("casino"))),
        ("\N{PLAYING CARD BLACK JOKER} Cards", card_lines(raw.get("cards"))),
        ("\N{JIGSAW PUZZLE PIECE} Server puzzle", puzzle_lines(raw.get("puzzle"))),
        ("\N{BLACK QUESTION MARK ORNAMENT} Daily Verdict", verdict_lines(raw.get("verdict"))),
        ("\N{EGG} Server pet", pet_lines(raw.get("pet"))),
        ("\N{PUBLIC ADDRESS LOUDSPEAKER} Bumps", bump_lines(raw.get("bumps"))),
        ("\N{ADMISSION TICKETS} Lottery", lottery_lines(raw.get("lottery"))),
    ]
    return [(title, lines) for title, lines in order if lines]
