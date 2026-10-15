"""Pure logic for wonderkb: turn server text, shop data, aliases and the guide into knowledge
entries, and work out what changed. No discord/redbot imports, so it is fully testable."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .constants import AUTO_PREFIX, BOT_CHANNEL, CURRENCY, MAX_CHUNK_CHARS, QUESTION_MAX_CHARS


@dataclass(frozen=True)
class Entry:
    name: str
    text: str


# ---------------------------------------------------------------- channels


def match_channel(slug: str, names: Dict[int, str]) -> Optional[int]:
    """Find the live channel for a slug. Names carry decoration ("🧸・cuddle"), so match the tail,
    and require a non-word character before it so "shop" never matches "wondershop"."""
    target = slug.lower().strip()
    if not target:
        return None
    for channel_id, name in names.items():
        lowered = name.lower().strip()
        if not lowered.endswith(target):
            continue
        head = lowered[: len(lowered) - len(target)]
        if head == "" or not (head[-1].isalnum() or head[-1] == "-"):
            return channel_id
    return None


def display_name(channel_name: str) -> str:
    """"📖・server-guide" -> "server-guide"."""
    return re.sub(r"^[^a-z0-9]+", "", channel_name.lower().strip()) or channel_name.strip()


_USER_MENTION = re.compile(r"<@[!&]?\d+>")
_EVERYONE = re.compile(r"@(everyone|here)\b")
_BLANKS = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    text = _USER_MENTION.sub("", text or "")
    text = _EVERYONE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def message_text(content: str, embeds: Sequence[Dict[str, Any]] = ()) -> str:
    """A message's own text plus the text inside its embeds (guides are posted as embeds)."""
    parts = [content or ""]
    for e in embeds:
        for key in ("title", "description"):
            if e.get(key):
                parts.append(str(e[key]))
        for f in e.get("fields", []) or []:
            if f.get("name") or f.get("value"):
                parts.append(f"{f.get('name', '')}: {f.get('value', '')}".strip(": "))
    return "\n".join(p for p in parts if p.strip())


def chunk_text(text: str, limit: int = MAX_CHUNK_CHARS) -> List[str]:
    """Pack paragraphs into chunks of at most `limit` characters; split an over-long paragraph on
    the nearest space so no chunk is cut mid-word."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: List[str] = []
    for p in paragraphs:
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            pieces.append(p)
    chunks: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def channel_prefix(channel_id: int) -> str:
    return f"{AUTO_PREFIX}ch:{channel_id}:"


def channel_entries(channel_id: int, channel_label: str, items: Iterable[Tuple[int, str]]) -> List[Entry]:
    """One entry per chunk of each message: (message_id, text) pairs, in any order."""
    out: List[Entry] = []
    for message_id, raw in items:
        for i, chunk in enumerate(chunk_text(clean_text(raw))):
            out.append(Entry(f"{channel_prefix(channel_id)}{message_id}:{i}", f"From #{channel_label}: {chunk}"))
    return out


# ---------------------------------------------------------------- shop

_COST_KEYS = ("price", "Cost", "cost", "Price")
_INFO_KEYS = ("description", "Info", "info", "Description")
_ITEM_KEYS = ("stock", "Items", "items")


def _first(d: Dict[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def shop_prefix() -> str:
    return f"{AUTO_PREFIX}shop:"


def _money(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def shop_entries(shops: Any) -> List[Entry]:
    """Read the shop cog's config tolerantly: {shop: {"stock": {item: {"price", "description", ...}}}}
    (older layouts with "Items"/"Cost" also work). Anything that does not look like that is skipped.

    Per shop this writes how to buy from it, the prices grouped (so 35 colors at one price is one line,
    not 35), and any item that has its own description. It deliberately does not write one entry per item."""
    out: List[Entry] = []
    if not isinstance(shops, dict):
        return out
    for shop_name, shop in shops.items():
        if not isinstance(shop, dict):
            continue
        items = _first(shop, _ITEM_KEYS)
        if not isinstance(items, dict) or not items:
            continue
        prefix = f"{shop_prefix()}{shop_name}:"
        howto = (
            f"The shop '{shop_name}' sells {len(items)} items. To buy: run .shop buy in #{BOT_CHANNEL}, "
            f"pick '{shop_name}', then choose the item and how many. Everything is paid for in {CURRENCY}. "
            "See what you own with .shop inventory."
        )
        if shop.get("giftable"):
            howto += " You can buy one for a friend with .shop gift."
        if isinstance(shop.get("description"), str) and shop["description"].strip():
            howto += f" About this shop: {shop['description'].strip()}"
        out.append(Entry(f"{prefix}__howto__", howto))

        by_price: Dict[Any, List[str]] = {}
        for item_name, item in items.items():
            if not isinstance(item, dict):
                continue
            by_price.setdefault(_first(item, _COST_KEYS), []).append(str(item_name))
            info = _first(item, _INFO_KEYS)
            if info:
                out.append(Entry(f"{prefix}item:{item_name}", f"'{item_name}' in the shop '{shop_name}': {str(info).strip()}"))
        for price, names in by_price.items():
            label = f"cost {_money(price)} {CURRENCY} each" if price is not None else "have no listed price"
            header = f"In the shop '{shop_name}', these items {label}: "
            # Every chunk repeats the shop and the price, so a continuation is never a bare list of names.
            for i, chunk in enumerate(chunk_text(", ".join(sorted(names)), MAX_CHUNK_CHARS - len(header) - 12)):
                out.append(Entry(f"{prefix}price:{price}:{i}", f"{header}{chunk}."))
    return out


# ---------------------------------------------------------------- aliases, guide, seed


def alias_prefix() -> str:
    return f"{AUTO_PREFIX}alias:"


def alias_entries(aliases: Iterable[Tuple[str, str]]) -> List[Entry]:
    """(alias name, command it runs) pairs from the Alias cog."""
    out: List[Entry] = []
    for name, command in aliases:
        name, command = str(name).strip(), str(command).strip()
        if not name or not command:
            continue
        out.append(Entry(
            f"{alias_prefix()}{name}",
            f"Alias: .{name} is a shortcut for .{command}. Members can type .{name} (and any arguments) "
            f"instead of .{command}.",
        ))
    return out


def guide_prefix() -> str:
    return f"{AUTO_PREFIX}guide:"


def guide_entries(pages: Iterable[Any]) -> List[Entry]:
    """Member-facing guide pages (wonderland content.py shape). Mod pages are skipped on purpose:
    these entries are shared with everyone, so a mod command must never be in them."""
    out: List[Entry] = []
    for page in pages:
        if getattr(page, "mod_only", False):
            continue
        for i, e in enumerate(page.entries):
            if e.usage == "{cmds}":
                cmds = ", ".join("." + x for x in e.expand)
                text = f"{e.desc.capitalize()}. Commands: {cmds}."
            else:
                text = f"{e.desc.capitalize()}. Use {e.usage}"
            out.append(Entry(f"{guide_prefix()}{page.key}:{i}", f"{page.title}: {text}"))
    return out


def seed_prefix() -> str:
    return f"{AUTO_PREFIX}seed:"


def seed_entries(rows: Iterable[Tuple[str, str, str, Sequence[str]]]) -> List[Entry]:
    """(section, title, text, commands) rows from seed.py."""
    out: List[Entry] = []
    for section, title, text, cmds in rows:
        slug = re.sub(r"[^a-z0-9]+", "-", f"{section}-{title}".lower()).strip("-")
        body = f"{section}: {title}. {text}"
        if cmds:
            body += " Commands: " + ", ".join(cmds) + "."
        out.append(Entry(f"{seed_prefix()}{slug}", body))
    return out


# ---------------------------------------------------------------- cog overviews


@dataclass(frozen=True)
class CogInfo:
    name: str
    doc: str
    commands: Tuple[str, ...]


def is_public_command(command: Any) -> bool:
    """True only if everyone can run it: no mod/admin/owner level, no required permissions, not hidden,
    and the same for every parent group (a subcommand of a mod group must not look public)."""
    for c in [command, *list(getattr(command, "parents", []) or [])]:
        if getattr(c, "hidden", False):
            return False
        requires = getattr(c, "requires", None)
        level = getattr(getattr(requires, "privilege_level", None), "name", None)
        if level not in (None, "NONE"):
            return False
        perms = getattr(requires, "user_perms", None)
        if perms is not None and getattr(perms, "value", 0):
            return False
    return True


def cog_prefix() -> str:
    return f"{AUTO_PREFIX}cog:"


MAX_COG_COMMANDS = 15
SKIP_COGS = frozenset({"Assistant", "WonderKB", "Core", "Downloader", "CogManagerUI", "Dev", "Permissions"})


def cog_entries(cogs: Iterable[CogInfo]) -> List[Entry]:
    """One short entry per cog that has commands anyone can run: what it is, and those commands."""
    out: List[Entry] = []
    for cog in cogs:
        if cog.name in SKIP_COGS or not cog.commands:
            continue
        doc = " ".join((cog.doc or "").split())
        doc = doc if len(doc) <= 220 else doc[:219].rstrip() + "…"
        shown = ", ".join("." + c for c in cog.commands[:MAX_COG_COMMANDS])
        more = f" (and {len(cog.commands) - MAX_COG_COMMANDS} more)" if len(cog.commands) > MAX_COG_COMMANDS else ""
        text = f"The {cog.name} cog." + (f" {doc}" if doc else "") + f" Commands anyone can run: {shown}{more}."
        out.append(Entry(f"{cog_prefix()}{cog.name}", text))
    return out


# ---------------------------------------------------------------- diffing


def plan_sync(
    existing: Dict[str, str], wanted: Sequence[Entry], fetched_prefixes: Set[str]
) -> Tuple[List[Entry], List[str]]:
    """What to (re)embed and what to delete.

    `existing` is name -> stored text for entries this cog owns. Only entries whose text changed are
    re-embedded. A stale entry is deleted only if its source was read successfully this run
    (`fetched_prefixes`), so a failed read can never wipe knowledge."""
    changed = [e for e in wanted if existing.get(e.name) != e.text]
    keep = {e.name for e in wanted}
    stale = [
        n for n in existing
        if n not in keep and any(n.startswith(p) for p in fetched_prefixes)
    ]
    return changed, stale


# ---------------------------------------------------------------- unanswered questions


def normalize_marker(text: str) -> str:
    return (text or "").replace("’", "'").strip().lower()


def is_unsure_reply(content: str, marker: str) -> bool:
    return bool(marker) and normalize_marker(content).startswith(normalize_marker(marker))


def question_key(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", question.lower())
    return hashlib.sha1(" ".join(words).encode()).hexdigest()[:12]


def trim(text: str, limit: int = QUESTION_MAX_CHARS) -> str:
    text = clean_text(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_qa(raw: str) -> Optional[Tuple[str, str]]:
    """`question | answer` -> (question, answer); None if either side is empty."""
    if "|" not in raw:
        return None
    q, a = raw.split("|", 1)
    q, a = q.strip(), a.strip()
    return (q, a) if q and a else None


def manual_name(question: str) -> str:
    from .constants import MANUAL_PREFIX

    return f"{MANUAL_PREFIX}{question_key(question)}"


def manual_text(question: str, answer: str) -> str:
    return f"Q: {question}\nA: {answer}"


def prune_seen(seen: Dict[str, float], now: float, ttl: float, cap: int) -> Dict[str, float]:
    fresh = {k: t for k, t in seen.items() if now - t < ttl}
    if len(fresh) > cap:
        fresh = dict(sorted(fresh.items(), key=lambda kv: kv[1], reverse=True)[:cap])
    return fresh
