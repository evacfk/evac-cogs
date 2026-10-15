"""Import-time smoke + structural checks for the cog module (see photodrop's equivalent for why).

These can't prove live Discord behaviour; they catch the classes of bug that only
show up at import/registration time: a missing stub attribute, a group without
invoke_without_command (the callback would run *and* the subcommand), a command
renamed away from what the docs say.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
SRC = (ROOT / "serverpulse.py").read_text(encoding="utf-8")

EXPECTED_COMMANDS = {
    "pulse", "version", "day", "week", "month", "hours", "hour", "heatmap", "best", "channels", "channel", "top",
    "anomalies", "members", "export", "board", "digest", "now", "backfill", "status", "resume", "cancel",
    "settings", "recap", "retention", "sources", "joinlog", "test", "guilds", "add", "remove",
}
# setters that moved into the `.pulse settings` panel
REMOVED_COMMANDS = {
    "start", "stop", "refresh", "weekly", "monthly", "ignore", "unignore", "ignored", "modchannel", "modrole", "commands",
    "auto", "voice", "note", "exclude", "include", "cap", "reactions", "model", "source",
}


def test_module_imports_and_defines_the_cog():
    from serverpulse import serverpulse

    assert hasattr(serverpulse, "ServerPulse")


def test_every_command_group_uses_invoke_without_command():
    groups = re.findall(r"\.group\(([^)]*)\)", SRC)
    assert len(groups) >= 5  # pulse, board, digest, recap, backfill, joinlog, guilds
    for args in groups:
        assert "invoke_without_command=True" in args, args


def test_all_documented_commands_exist():
    names = set(re.findall(r'(?:commands|pulse|pulse_\w+)\.(?:group|command)\(name="(\w+)"', SRC))
    assert EXPECTED_COMMANDS <= names, EXPECTED_COMMANDS - names


def test_setter_commands_moved_into_the_panel():
    names = set(re.findall(r'(?:commands|pulse|pulse_\w+)\.(?:group|command)\(name="(\w+)"', SRC))
    assert not (REMOVED_COMMANDS & names), REMOVED_COMMANDS & names
    assert 'name="settings", aliases=["set"]' in SRC


def test_command_names_do_not_collide_with_other_repo_cogs():
    # `.pulse` / `.activity` must stay free: other cogs in this repo may not define them
    for other in ROOT.parent.glob("*/*.py"):
        if other.parent.name == "serverpulse":
            continue
        text = other.read_text(encoding="utf-8", errors="ignore")
        assert 'name="pulse"' not in text and 'name="activity"' not in text, other


def test_info_json_is_flat_and_valid():
    info = json.loads((ROOT / "info.json").read_text(encoding="utf-8"))
    assert info["type"] == "COG" and info["name"] == "serverpulse"
    assert "Pillow" in info["requirements"] and info["end_user_data_statement"]
    assert not (ROOT / "serverpulse" / "info.json").exists()  # never nested


def test_alias_and_mod_gate_are_wired():
    assert 'aliases=["activity"]' in SRC
    assert "async def cog_check" in SRC


def test_loops_are_wrapped_per_guild():
    for loop in ("_flush_loop", "_board_loop", "_digest_loop"):
        body = SRC.split(f"async def {loop}(self):")[1].split("@tasks.loop")[0].split("@_flush_loop.before_loop")[0]
        assert "try:" in body and "except Exception:" in body and "log.exception" in body, loop
