"""The `.contest` panel builds with the real discord.py widgets: every tab, every editor, every form (Discord's limits)."""
import pytest

discord = pytest.importorskip("discord")
if not hasattr(discord, "Client"):
    pytest.skip("needs the real discord.py", allow_module_level=True)

from contest import panel as P
from contest.settings_panel import SettingsPanel, make_modal
from contest.test_contest_cog import mod, w  # noqa: F401  (fixtures)


async def test_every_tab_and_setting_renders_within_discords_limits(w):
    specs = P.build_specs()
    panel = SettingsPanel(w.cog, w.g, w.owner, specs, title="Wonderween 2026", intro=P.INTRO)
    assert panel.tabs == ["Setup", "Prizes", "Schedule", "Run"]
    await w.launch()
    for tab in panel.tabs:
        panel.tab = tab
        assert len(panel.tab_specs()) <= 25
        for spec in panel.tab_specs():
            panel.spec_key = spec.key
            await panel.refresh_data()
            panel.rebuild()
            embed = panel.build_embed()
            assert len(embed.description) <= 4096
            for child in panel.children:
                for option in getattr(child, "options", []):
                    assert len(option.label) <= 100 and len(option.description or "") <= 100
            if spec.kind == "modal":
                modal = make_modal(spec.label, spec.fields, panel.values.get(spec.key), lambda *a: None)
                assert 1 <= len(modal.boxes) <= 5
                for box in modal.boxes.values():
                    assert len(box.label) <= 45
                    assert box.default is None or len(box.default) <= (box.max_length or 4000)


async def test_modal_prefill_shows_current_values(w):
    await w.configure()
    schedule = next(s for s in P.build_specs() if s.key == "schedule")
    modal = make_modal(schedule.label, schedule.fields, await P._schedule_get(w.cog, w.g), lambda *a: None)
    assert modal.boxes["entries_close_ts"].default == "2026-10-31 23:00"
    assert modal.boxes["reveal_ts"].default == "2026-11-01 12:00"
    coins = next(s for s in P.build_specs() if s.key == "coins")
    cm = make_modal(coins.label, coins.fields, await P._coins_get(w.cog, w.g), lambda *a: None)
    assert cm.boxes["gold"].default == "1000"
