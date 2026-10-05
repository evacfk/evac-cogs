"""Pure logic for the Daily Verdict: no discord/redbot imports."""
from __future__ import annotations

import random
from datetime import datetime
from typing import Iterable, Optional, Sequence

from .constants import (
    BAR_WIDTH, HOME_TZ, LONE_WOLF_MAX_SHARE, LONE_WOLF_MIN_VOTES, MAX_OPTION_LEN, MAX_OPTIONS,
    MAX_QUESTION_LEN, MIN_OPTIONS, MIND_READER_MIN_PLAYED, STREAK_MILESTONES,
)


class QuestionError(ValueError):
    pass


def parse_question(text: str) -> tuple[str, list[str]]:
    """'Pineapple on pizza? | Yes | No | Only if hot' -> (question, options). Raises QuestionError."""
    parts = [" ".join(p.split()) for p in text.split("|")]
    parts = [p for p in parts if p]
    if len(parts) < 1 + MIN_OPTIONS:
        raise QuestionError(f"Use `question | option 1 | option 2` (2 to {MAX_OPTIONS} options).")
    question, options = parts[0], parts[1:]
    if len(options) > MAX_OPTIONS:
        raise QuestionError(f"At most {MAX_OPTIONS} options.")
    if len(question) > MAX_QUESTION_LEN:
        raise QuestionError(f"The question is too long (max {MAX_QUESTION_LEN} characters).")
    if any(len(o) > MAX_OPTION_LEN for o in options):
        raise QuestionError(f"Each option must be {MAX_OPTION_LEN} characters or fewer.")
    if len({o.casefold() for o in options}) != len(options):
        raise QuestionError("Options must be different from each other.")
    return question, options


def tally(answers: dict, n_options: int) -> list[int]:
    counts = [0] * n_options
    for idx in answers.values():
        if 0 <= int(idx) < n_options:
            counts[int(idx)] += 1
    return counts


def winners(counts: Sequence[int]) -> set[int]:
    """Option(s) with the most votes (all of them on a tie); empty when nobody voted."""
    top = max(counts, default=0)
    return {i for i, c in enumerate(counts) if c == top} if top > 0 else set()


def lone_wolves(answers: dict, counts: Sequence[int]) -> list[str]:
    """Voters whose own pick was chosen by only a small minority (needs a decent turnout)."""
    total = sum(counts)
    if total < LONE_WOLF_MIN_VOTES:
        return []
    small = {i for i, c in enumerate(counts) if c / total <= LONE_WOLF_MAX_SHARE}
    if len(small) == len(counts):  # nobody is in a "majority"; not a meaningful label
        return []
    return sorted(uid for uid, idx in answers.items() if int(idx) in small)


def mind_readers(predictions: dict, win: set[int]) -> list[str]:
    return sorted(uid for uid, idx in predictions.items() if int(idx) in win)


def participants(answers: dict, predictions: dict) -> list[str]:
    """Only people who locked in BOTH their answer and their guess count."""
    return sorted(set(answers) & set(predictions))


def update_streak(last_seq: int, streak: int, seq: int) -> int:
    """Consecutive questions played. Same question twice does nothing."""
    if last_seq == seq:
        return streak
    return streak + 1 if last_seq == seq - 1 else 1


def streak_milestone(streak: int) -> Optional[int]:
    return streak if streak in STREAK_MILESTONES else None


def bar(share: float, width: int = BAR_WIDTH) -> str:
    filled = round(max(0.0, min(1.0, share)) * width)
    return "█" * filled + "░" * (width - filled)


def percent(count: int, total: int) -> int:
    return round(100 * count / total) if total else 0


def month_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, HOME_TZ).strftime("%Y-%m")


def previous_month_key(key: str) -> str:
    y, m = int(key[:4]), int(key[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def pt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, HOME_TZ).date().isoformat()


def pt_hour(ts: float) -> int:
    return datetime.fromtimestamp(ts, HOME_TZ).hour


def uses_up_day(ts: float, post_hour: int, early_hours: int = 6) -> bool:
    """False for a manual post made well before the scheduled hour (e.g. 12:36am), so the daily post still goes out."""
    return pt_hour(ts) >= post_hour - early_hours


def pick_monthly_winner(scores: dict, min_played: int = MIND_READER_MIN_PLAYED) -> Optional[str]:
    """Most correct guesses, then best accuracy, then lowest user id (stable). Needs enough games played."""
    eligible = [(uid, c, p) for uid, (c, p) in scores.items() if p >= min_played and c > 0]
    if not eligible:
        return None
    eligible.sort(key=lambda t: (-t[1], -(t[1] / t[2]), int(t[0])))
    return eligible[0][0]


def ranking(scores: dict, limit: int = 10) -> list[tuple[str, int, int]]:
    rows = [(uid, c, p) for uid, (c, p) in scores.items() if p > 0]
    rows.sort(key=lambda t: (-t[1], -(t[1] / t[2]), int(t[0])))
    return rows[:limit]


def pick_seed(used: Iterable[int], total: int, rng: Optional[random.Random] = None) -> tuple[int, list[int]]:
    """Choose an unused seed question; when all have been used, start over. Returns (index, new_used)."""
    rng = rng or random
    used = [u for u in used if 0 <= u < total]
    fresh = [i for i in range(total) if i not in used]
    if not fresh:
        used, fresh = [], list(range(total))
    choice = rng.choice(fresh)
    return choice, used + [choice]


def history_entry(cur: dict, counts: list, win, players, readers, wolves) -> dict:
    """What we keep about a closed question. User ids only; who picked which option is NOT kept."""
    return {"seq": cur["seq"], "question": cur["question"], "options": list(cur["options"]),
            "posted_ts": cur.get("posted_ts"), "counts": list(counts), "winners": sorted(win),
            "players": sorted(players, key=int), "readers": sorted(readers, key=int),
            "wolves": sorted(wolves, key=int)}


def results_pages(entry: dict, limit: int = 1900) -> list:
    """Plain-text pages (each under `limit` chars) listing everyone who played one closed question."""
    total = sum(entry["counts"])
    head = f"**Daily Verdict #{entry['seq']}**: {entry['question']}\n"
    head += " · ".join(f"{o}: {n}" for o, n in zip(entry["options"], entry["counts"]))
    head += f"\n{len(entry['players'])} played · {len(entry['readers'])} read the crowd · {len(entry['wolves'])} lone wolves"
    if not total:
        return [head]
    sections = []
    for title, key in (("Played", "players"), ("Read the crowd", "readers"), ("Lone wolves", "wolves")):
        if entry[key]:
            sections.append((title, [f"<@{u}>" for u in entry[key]]))
    pages, cur = [], head
    for title, mentions in sections:
        line = f"\n**{title}:** "
        for m in mentions:
            piece = (m if line.endswith(": ") else ", " + m)
            if len(cur) + len(line) + len(piece) > limit:
                pages.append(cur + line)
                cur, line = "", f"**{title} (cont.):** " + m
            else:
                line += piece
        cur += line
    pages.append(cur)
    return [p for p in pages if p.strip()]
