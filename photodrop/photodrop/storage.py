"""On-disk photo storage, keyed by member + date.

Discord attachment CDN URLs expire after roughly 24h (they're signed), so the
raw bytes must be saved to Red's data folder at drop time -- storing only the
URL would mean `.pp view`/calendar/collage-building break on anything older
than a day. This module owns that: saving new drops and listing/loading what
was saved for a given member+day.
"""
from __future__ import annotations

import re
from pathlib import Path

_SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")
DEFAULT_EXTENSION = "png"


def _safe_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lstrip(".")
    if suffix and _SAFE_EXT_RE.match(suffix):
        return suffix.lower()
    return DEFAULT_EXTENSION


def member_day_dir(base_dir: Path, member_id: int, date_key: str) -> Path:
    return Path(base_dir) / "photos" / str(member_id) / date_key


def save_photo_bytes(base_dir: Path, member_id: int, date_key: str, index: int, filename: str, data: bytes) -> Path:
    """Write one photo's bytes to disk, returning the path it was saved at.

    `index` is the attachment's 0-based position within that day's drop, used
    to keep filenames stable and sorted (e.g. 0.png, 1.jpg, 2.png).
    """
    day_dir = member_day_dir(base_dir, member_id, date_key)
    day_dir.mkdir(parents=True, exist_ok=True)
    ext = _safe_extension(filename)
    path = day_dir / f"{index}.{ext}"
    path.write_bytes(data)
    return path


def _sort_key(path: Path) -> tuple[int, object]:
    """Numeric sort on the `{index}.ext` stem so 10+ photos in one drop order
    correctly (lexical sort would put "10.png" before "2.png"). Anything that
    doesn't parse as our own naming scheme sorts after, by name, rather than
    raising.
    """
    try:
        return (0, int(path.stem))
    except ValueError:
        return (1, path.name)


def list_photos(base_dir: Path, member_id: int, date_key: str) -> list[Path]:
    day_dir = member_day_dir(base_dir, member_id, date_key)
    if not day_dir.exists():
        return []
    return sorted(day_dir.iterdir(), key=_sort_key)
