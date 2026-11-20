"""The four training challenges, driven click by click with fake interactions (real discord.py views)."""
import importlib
import random
import sys
import types

import pytest

from hungergames import _testkit as tk


@pytest.fixture
def ui(monkeypatch):
    tk.install(monkeypatch)
    tk.install_ui(monkeypatch)
    monkeypatch.delitem(sys.modules, "hungergames.views", raising=False)
    m = importlib.import_module("hungergames.views")
    monkeypatch.setattr(m, "ENDURANCE_SHOW_SECONDS", 0)
    yield m
    sys.modules.pop("hungergames.views", None)


class Recorder:
    def __init__(self):
        self.results = []

    async def finish_challenge(self, guild, uid, kind, result):
        self.results.append((kind, result))


USER = types.SimpleNamespace(id=42)


def inter():
    return tk.FakeInteraction(None, USER)


def button(view, label):
    return next(b for b in view.children if b.label == label)


async def test_sprint_perfect_run(ui):
    rec = Recorder()
    s = ui.SprintChallenge(rec, None, 42, None, random.Random(1))
    for i in range(len(s.steps)):
        await button(s, s.steps[i][1]).callback(inter())
    kind, r = rec.results[0]
    assert kind == "agility" and r["acc"] == 70 and r["finished"]
    assert len(s.live.parts) == 6


async def test_sprint_misses_cost_ten_each_and_buttons_include_the_answer(ui):
    rec = Recorder()
    s = ui.SprintChallenge(rec, None, 42, None, random.Random(2))
    for i in range(len(s.steps)):
        labels = [b.label for b in s.children]
        assert s.steps[i][1] in labels and len(labels) == 6 == len(set(labels))
        wrong = next(lab for lab in labels if lab != s.steps[i][1])
        await button(s, wrong if i < 2 else s.steps[i][1]).callback(inter())
    assert rec.results[0][1]["acc"] == 50


async def test_sprint_timeout_counts_remaining_steps_as_missed(ui):
    rec = Recorder()
    s = ui.SprintChallenge(rec, None, 42, None, random.Random(3))
    await button(s, s.steps[0][1]).callback(inter())
    await s.on_timeout()
    r = rec.results[0][1]
    assert r["acc"] == 20 and r["finished"] is False


async def test_only_the_tribute_can_click(ui):
    rec = Recorder()
    s = ui.SprintChallenge(rec, None, 42, None, random.Random(4))
    other = tk.FakeInteraction(None, types.SimpleNamespace(id=7))
    assert await s.interaction_check(other) is False
    assert await s.interaction_check(inter()) is True


async def test_combat_counts_correct_counters(ui):
    rec = Recorder()
    s = ui.CombatChallenge(rec, None, 42, None, random.Random(5))
    for i in range(len(s.rounds)):
        right = s.rounds[i][1]
        pick = right if i % 2 == 0 else next(b.label for b in s.children if b.label != right)
        await button(s, pick).callback(inter())
    assert rec.results[0][1]["acc"] == 35


async def test_memory_march_clears_then_fails(ui):
    rec = Recorder()
    s = ui.MarchChallenge(rec, None, 42, None, random.Random(6))
    first = inter()
    await s.start(first)
    assert first.original_edits                       # the buttons appear after the show phase
    for emoji in list(s.seq):                         # clear length 3
        await button(s, emoji).callback(inter())
    assert s.cleared == 3 and s.length == 4
    wrong = next(e for e in ui.MARCH if e != s.seq[0])
    await button(s, wrong).callback(inter())
    r = rec.results[0][1]
    assert r["finished"] and r["acc"] == pytest.approx(7.0)   # 70 * (3 - 2) / 10


async def test_cipher_first_second_and_failed(ui):
    from hungergames import content, engine
    rec = Recorder()
    puzzles = engine.pick_puzzles(content.load_ciphers(), random.Random(7))
    s = ui.CipherChallenge(rec, None, 42, None, random.Random(7), puzzles)
    await s._submit(inter(), puzzles[0]["answers"][0].lower())      # first try
    await s._submit(inter(), "nope")
    await s._submit(inter(), puzzles[1]["answers"][0])              # second try: half
    await s._submit(inter(), "nope")
    await s._submit(inter(), "still nope")                          # out of attempts
    r = rec.results[0][1]
    assert r["finished"] and r["acc"] == pytest.approx(70 * 1.5 / 3)


async def test_challenge_only_reports_once(ui):
    rec = Recorder()
    s = ui.SprintChallenge(rec, None, 42, None, random.Random(8))
    await s.end(None, finished=False)
    await s.on_timeout()
    assert len(rec.results) == 1
