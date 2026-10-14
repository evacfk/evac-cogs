"""Pure logic for IntroForm. No discord / redbot imports."""
import re

from .constants import QUESTIONS

ZWSP = "​"

_INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\S+", re.IGNORECASE)


def clean_text(value, max_length: int) -> str:
    """Normalize one answer: tidy whitespace, defuse mass pings, truncate."""
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("@everyone", "@" + ZWSP + "everyone")
    text = text.replace("@here", "@" + ZWSP + "here")
    return text[:max_length].rstrip()


def normalize_answers(raw: dict) -> dict:
    """Clean every known question's answer; unknown keys are dropped."""
    return {q["key"]: clean_text(raw.get(q["key"]), q["max_length"]) for q in QUESTIONS}


def validate_answers(answers: dict) -> list:
    """Return a list of human-readable problems (empty list = valid)."""
    problems = []
    for q in QUESTIONS:
        if q["required"] and not answers.get(q["key"]):
            problems.append(f"**{q['label']}** is required.")
    for q in QUESTIONS:
        if _INVITE_RE.search(answers.get(q["key"], "")):
            problems.append("Server invite links aren't allowed in intros.")
            break
    return problems


def migrate_answers(answers: dict) -> dict:
    """Fold the retired "games" answer (v1.0-1.1 intros) into About Me. Safe to call repeatedly."""
    out = dict(answers or {})
    games = out.pop("games", "")
    if games:
        about = out.get("extra", "")
        out["extra"] = (about + "\n\n" if about else "") + "Games I play: " + games
    return out


def build_description(mention: str, answers: dict) -> str:
    """The intro embed body: byline, short details, then the long answers under their headings."""
    answers = migrate_answers(answers)
    parts = [f"Intro by {mention}"]
    details = [
        f"**{q['field_name']}:** {answers[q['key']]}"
        for q in QUESTIONS
        if q["field_name"] and q["inline"] and answers.get(q["key"])
    ]
    if details:
        parts.append("\n".join(details))
    for q in QUESTIONS:
        if q["field_name"] and not q["inline"] and answers.get(q["key"]):
            parts.append(f"**{q['field_name']}**\n{answers[q['key']]}")
    return "\n\n".join(parts)


def prefill_value(answers: dict, key: str, max_length: int):
    """Default text for a modal input when editing, or None when there is nothing."""
    value = (answers or {}).get(key, "").replace(ZWSP, "")
    return value[:max_length] or None


def _haystack(answers: dict) -> str:
    return " ".join(str(v) for v in answers.values()).lower().replace(ZWSP, "")


def search_intros(intros: dict, query: str, is_member=None, limit: int = 25):
    """Keyword search over stored intros.

    Every whitespace-separated word in `query` must appear somewhere in the answers.
    Ranked: exact name, name starts with first word, name contains it, anything else;
    ties alphabetical by name. `is_member(user_id)` filters out people who left.
    Returns (top `limit` matches as [(user_id, answers)], total match count).
    """
    tokens = (query or "").lower().split()
    if not tokens:
        return [], 0
    full = " ".join(tokens)
    ranked = []
    for uid_str, entry in intros.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        if is_member is not None and not is_member(uid):
            continue
        answers = entry.get("answers", {})
        if not all(t in _haystack(answers) for t in tokens):
            continue
        name = answers.get("name", "").lower()
        if name == full:
            rank = 0
        elif name.startswith(tokens[0]):
            rank = 1
        elif tokens[0] in name:
            rank = 2
        else:
            rank = 3
        ranked.append((rank, name, uid, answers))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    return [(uid, answers) for _, _, uid, answers in ranked[:limit]], len(ranked)


def option_label(answers: dict) -> str:
    """Dropdown option label (Discord max 100 chars)."""
    return (answers.get("name") or "(no name)")[:100]


def option_description(answers: dict):
    """Dropdown option description: location and the start of About Me on one line, or None."""
    answers = migrate_answers(answers)
    parts = [answers.get("location", ""), answers.get("extra", "")]
    text = " \u00b7 ".join(" ".join(p.split()) for p in parts if p).replace(ZWSP, "")
    return text[:100] or None
