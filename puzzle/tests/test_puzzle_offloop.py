"""Source-level guards for the puzzle cog (it has no redbot-free import path, so
these parse puzzle.py instead of importing it)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "puzzle.py"
HEAVY = {"_slice_image", "_ensure_full_image", "_build_pool_preview_image", "_build_progress_image"}


def _tree():
    return ast.parse(SRC.read_text())


def test_heavy_image_work_is_never_called_directly_from_async_code():
    """REGRESSION: Pillow slicing/stitching/rendering ran inline in async commands and
    the round-finish path, stalling the event loop (and every button on the bot).
    Inside any `async def`, these must be passed to asyncio.to_thread, not called."""
    bad = []
    for fn in ast.walk(_tree()):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in HEAVY:
                    bad.append((fn.name, node.lineno, name))
    assert bad == []


def test_heavy_image_work_is_offloaded_at_each_site():
    offloaded = set()
    for node in ast.walk(_tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_thread" and node.args):
            a = node.args[0]
            name = a.attr if isinstance(a, ast.Attribute) else getattr(a, "id", None)
            offloaded.add(name)
    assert HEAVY <= offloaded


def test_version_probe_matches_class_version():
    src = SRC.read_text()
    assert '__version__ = "1.3.0"' in src
    assert 'name="version"' in src and 'f"puzzle v{self.__version__}"' in src
