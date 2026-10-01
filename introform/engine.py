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


def embed_fields(answers: dict) -> list:
    """(name, value, inline) tuples for the non-empty, non-title answers, in form order."""
    fields = []
    for q in QUESTIONS:
        if q["field_name"] and answers.get(q["key"]):
            fields.append((q["field_name"], answers[q["key"]], q["inline"]))
    return fields


def prefill_value(answers: dict, key: str, max_length: int):
    """Default text for a modal input when editing, or None when there is nothing."""
    value = (answers or {}).get(key, "").replace(ZWSP, "")
    return value[:max_length] or None
