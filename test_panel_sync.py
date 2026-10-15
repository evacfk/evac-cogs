"""Every cog's settings_panel.py must be byte-identical to the master in _panel/.
Edit the master, then run `python _panel/sync.py` to copy it out."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent


def test_cog_copies_match_the_master():
    master = (ROOT / "_panel" / "settings_panel.py").read_bytes()
    copies = [p for p in ROOT.glob("*/settings_panel.py") if p.parent.name != "_panel"]
    drifted = [p.parent.name for p in copies if p.read_bytes() != master]
    assert not drifted, f"run `python _panel/sync.py`: out of date copies in {drifted}"
