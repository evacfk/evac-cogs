"""Copy _panel/settings_panel.py (and its tests) into every cog that uses it. Run after editing the master."""
import pathlib, shutil, sys

root = pathlib.Path(__file__).resolve().parent.parent
master = root / "_panel" / "settings_panel.py"
tests = root / "_panel" / "test_settings_panel.py"
cogs = [p.parent.name for p in root.glob("*/settings_panel.py") if p.parent.name != "_panel"]
targets = sys.argv[1:] or cogs
for name in targets:
    shutil.copyfile(master, root / name / "settings_panel.py")
    print("synced", name)
