"""On-disk card art storage, keyed by card_id, under Red's cog data folder.

Mirrors photodrop's storage.py rationale: source images (from AniList, or an
admin's addcard upload) are downloaded/saved permanently at import time,
never referenced by their original hosted URL at drop-time.
"""

from pathlib import Path
from typing import Optional


def card_image_dir(base_data_path: Path, guild_id: int) -> Path:
    path = base_data_path / str(guild_id) / "cards"
    path.mkdir(parents=True, exist_ok=True)
    return path


def card_image_path(base_data_path: Path, guild_id: int, card_id: int) -> Path:
    return card_image_dir(base_data_path, guild_id) / f"{card_id}.png"


def save_card_image(base_data_path: Path, guild_id: int, card_id: int, image_bytes: bytes) -> Path:
    path = card_image_path(base_data_path, guild_id, card_id)
    path.write_bytes(image_bytes)
    return path


def delete_card_image(base_data_path: Path, guild_id: int, card_id: int) -> None:
    path = card_image_path(base_data_path, guild_id, card_id)
    path.unlink(missing_ok=True)


def list_card_ids(base_data_path: Path, guild_id: int) -> list:
    """Every card_id with an image on disk, sorted numerically (not
    lexically -- storage.list_photos() in photodrop shipped a bug once where
    '10.png' sorted before '2.png'; sorting on the parsed int from the start
    avoids repeating that)."""
    directory = card_image_dir(base_data_path, guild_id)
    ids = []
    for p in directory.glob("*.png"):
        try:
            ids.append(int(p.stem))
        except ValueError:
            continue
    return sorted(ids)


def card_image_exists(base_data_path: Path, guild_id: int, card_id: int) -> bool:
    return card_image_path(base_data_path, guild_id, card_id).exists()


def read_card_image(base_data_path: Path, guild_id: int, card_id: int) -> Optional[bytes]:
    path = card_image_path(base_data_path, guild_id, card_id)
    if not path.exists():
        return None
    return path.read_bytes()


# --- rendered animations --------------------------------------------------------
#
# An animated (prismatic) card takes a couple of seconds to render, so each
# finished GIF is kept on disk and reused. File names start with the card id and
# a fingerprint of its art; when the art changes, the old renders for that card
# are cleared out the next time one is saved.


def render_cache_dir(base_data_path: Path, guild_id: int) -> Path:
    path = base_data_path / str(guild_id) / "render_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def art_fingerprint(image_bytes: bytes) -> str:
    import hashlib

    return hashlib.sha1(image_bytes).hexdigest()[:12]


def read_render_cache(base_data_path: Path, guild_id: int, card_id: int, art_key: str, variant: str) -> Optional[bytes]:
    path = render_cache_dir(base_data_path, guild_id) / f"{card_id}-{art_key}-{variant}.gif"
    return path.read_bytes() if path.exists() else None


def write_render_cache(base_data_path: Path, guild_id: int, card_id: int, art_key: str, variant: str, data: bytes) -> None:
    directory = render_cache_dir(base_data_path, guild_id)
    keep = f"{card_id}-{art_key}-"
    for old in directory.glob(f"{card_id}-*.gif"):
        if not old.name.startswith(keep):
            old.unlink(missing_ok=True)
    (directory / f"{keep}{variant}.gif").write_bytes(data)
