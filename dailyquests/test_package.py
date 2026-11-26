"""The DailyQuests cog class: it opens Wonderland's saved-data scope (so nothing migrates) and has one admin command."""
import ast
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def test_the_cog_reuses_wonderlands_config_scope_so_saved_quests_carry_over():
    from dailyquests import constants
    assert constants.CONFIG_NAME == "Wonderland" and constants.CONFIG_ID == 0x574C44
    source = (HERE / "dailyquests.py").read_text()
    assert "cog_name=CONFIG_NAME" in source and "Config.get_conf(None" in source


def test_one_admin_command_and_a_version_probe():
    tree = ast.parse((HERE / "dailyquests.py").read_text())
    names = [kw.value.value for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "attr", "") in ("group", "command")
             for kw in node.keywords if kw.arg == "name"]
    assert sorted(names) == ["dailyquests", "version"]


def test_wonderland_no_longer_registers_quest_settings():
    guide = (HERE.parent / "wonderland" / "guide.py").read_text()
    assert "register_member(quests" not in guide and "quests=DEFAULT_GUILD_QUESTS" not in guide
