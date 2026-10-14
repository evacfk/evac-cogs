"""Package-level checks: info.json placement, setup wiring, and an import smoke test under the stubs."""
import json
import os

import pytest

HERE = os.path.dirname(__file__)


@pytest.fixture(autouse=True)
def _bank_stub(monkeypatch):
    """The shared repo stub has no `redbot.core.bank`; MyStats imports it at module level."""
    import sys
    import types

    core = sys.modules.get("redbot.core")
    if core is not None and not hasattr(core, "bank"):
        monkeypatch.setattr(core, "bank", types.SimpleNamespace(), raising=False)


def test_info_json_sits_directly_in_the_cog_folder():
    path = os.path.join(HERE, "info.json")
    assert os.path.isfile(path)
    assert not os.path.exists(os.path.join(HERE, "wonderland", "info.json"))
    info = json.load(open(path))
    assert info["name"] == "Wonderland" and info["type"] == "COG"
    assert info["end_user_data_statement"] and info["short"]


def test_old_standalone_info_files_are_gone():
    # The three merged cogs must not ship their own info.json, or Red lists them as separate installs.
    for sub in ("mystats", "introform", "introcleanup"):
        assert not os.path.exists(os.path.join(HERE, sub, "info.json")), sub


def test_guide_module_imports_and_defines_the_cog():
    from wonderland import guide

    assert hasattr(guide, "Wonderland") and hasattr(guide, "GuideView")


def test_merged_cogs_keep_their_class_names_so_saved_config_carries_over():
    from wonderland.introcleanup import introcleanup
    from wonderland.introform import introform
    from wonderland.mystats import mystats

    assert introform.IntroForm.__name__ == "IntroForm"
    assert introcleanup.IntroCleanup.__name__ == "IntroCleanup"
    assert mystats.MyStats.__name__ == "MyStats"


def test_setup_registers_all_four_cogs():
    import asyncio

    from wonderland import setup

    added = []

    class Bot:
        async def add_cog(self, cog):
            added.append(type(cog).__name__)

    asyncio.run(setup(Bot()))
    assert added == ["MyStats", "IntroForm", "IntroCleanup", "Wonderland"]
